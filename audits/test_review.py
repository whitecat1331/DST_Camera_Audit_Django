from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audits.models import AuditJob, AuditRound, AuditScreenshot
from audits.review import Filters, build_round_rows, filter_rows, summarize
from cameras.models import Installation, UserProfile

SITE = AuditJob.DeviceType.DST_SITE
OK = AuditJob.Status.SUCCEEDED


class ReviewRoundTests(TestCase):
    def setUp(self):
        self.round = AuditRound.objects.create(name="2026-11-01 fall back")
        now = timezone.now()
        # OvrC read just after the 2026-11-01 fall back (07:11 UTC = 02:11 EST).
        read_at = datetime(2026, 11, 1, 7, 11, tzinfo=dt_timezone.utc)
        Installation.objects.create(ims_id=1, identifier="I-DE-266134", pole_number="266134", agency="Knoxville", state="Tennessee")
        Installation.objects.create(ims_id=2, identifier="I-LTI-233038", pole_number="233038", agency="Bainbridge", state="Georgia")
        # DE camera: an older failed attempt and a newer success with a wrong-zone clock.
        self.de_old = self.site("266134", "de", status=AuditJob.Status.FAILED, finished=now - timedelta(hours=2), error_message="TV timeout")
        self.de_new = self.site(
            "266134", "de", finished=now, ovrc_fx="FX1261", ovrc_zone="ADT",
            ovrc_local_time_raw="Sun, Nov 01, 2026 02:10 AM ADT", ovrc_captured_at=read_at, drift_seconds=3540,
        )
        for label in ("ovrc_fx1261", "de_l2", "de_l1"):
            AuditScreenshot.objects.create(job=self.de_new, label=label, image=f"audits/{self.de_new.pk}/{label}.png")
        # LTI camera: success, no OvrC.
        self.lti = self.site("233038", "lti", finished=now)
        for label in ("vnc_l1", "cbw"):
            AuditScreenshot.objects.create(job=self.lti, label=label, image=f"audits/{self.lti.pk}/{label}.png")
        # Same pole in another round must not leak in.
        other = AuditRound.objects.create(name="other")
        self.site("233038", "lti", round_=other, finished=now + timedelta(hours=1), status=AuditJob.Status.FAILED)

    def site(self, pole, kind, *, status=OK, finished=None, round_=None, **fields):
        return AuditJob.objects.create(
            pole_number=pole, target_host=kind, device_type=SITE, status=status,
            finished_at=finished, round=round_ or self.round, **fields,
        )

    def test_one_row_per_camera_newest_wins(self):
        rows = {r.key: r for r in build_round_rows(self.round)}
        self.assertEqual(set(rows), {"266134", "233038"})
        de = rows["266134"]
        self.assertEqual(de.job, self.de_new)
        self.assertEqual(de.attempts, 2)
        self.assertEqual(de.installation.identifier, "I-DE-266134")
        self.assertEqual([c.key for c in de.captures], ["de_l1", "de_l2", "ovrc_fx1261"])
        self.assertEqual([c.key for c in rows["233038"].captures], ["cbw", "vnc_l1"])

    def test_drift_and_failure_flags_and_filters(self):
        rows = build_round_rows(self.round)
        de = next(r for r in rows if r.key == "266134")
        self.assertTrue(de.drift_flagged)
        self.assertEqual(de.drift_minutes, 59.0)
        self.assertFalse(de.failed)  # the newest run succeeded
        self.assertEqual(de.eastern_at_read, "Nov 01, 2026 02:11 AM EST")
        self.assertEqual(
            summarize(rows),
            {"cameras": 2, "lti": 1, "de": 1, "failed": 0, "skipped": 0, "drift": 1, "complete": 0, "unassigned": 2},
        )
        self.assertEqual([r.key for r in filter_rows(rows, Filters(drift=True))], ["266134"])
        self.assertEqual([r.key for r in filter_rows(rows, Filters(kind="lti"))], ["233038"])
        self.assertEqual(filter_rows(rows, Filters(failed=True)), [])

    def test_a_skipped_offline_site_counts_as_skipped_not_failed(self):
        self.site("600004", "lti", status=AuditJob.Status.SKIPPED, finished=timezone.now(),
                  error_message="Not attempted: offline: CBW power-on failed")
        rows = build_round_rows(self.round)
        skipped = next(r for r in rows if r.key == "600004")
        self.assertTrue(skipped.skipped)
        self.assertFalse(skipped.failed)
        summary = summarize(rows)
        self.assertEqual((summary["skipped"], summary["failed"]), (1, 0))

    def test_small_drift_is_not_flagged(self):
        AuditJob.objects.filter(pk=self.de_new.pk).update(drift_seconds=-48)
        de = next(r for r in build_round_rows(self.round) if r.key == "266134")
        self.assertFalse(de.drift_flagged)

    def test_query_count_is_constant_in_cameras(self):
        with CaptureQueriesContext(connection) as small:
            build_round_rows(self.round)
        for n in range(10):
            Installation.objects.create(ims_id=100 + n, pole_number=f"9{n:05d}")
            job = self.site(f"9{n:05d}", "lti", finished=timezone.now())
            AuditScreenshot.objects.create(job=job, label="cbw", image=f"audits/{job.pk}/cbw.png")
        with CaptureQueriesContext(connection) as large:
            rows = build_round_rows(self.round)
        self.assertEqual(len(rows), 12)
        self.assertEqual(len(large), len(small))

    def test_page_renders_for_any_signed_in_user(self):
        user = get_user_model().objects.create_user("viewer")
        UserProfile.objects.create(user=user, ims_role="readonly")
        self.client.force_login(user)
        latest = self.client.get("/audits/review/").content.decode()
        self.assertIn('<option value="/audits/review/%d/" selected>other</option>' % (self.round.pk + 1), latest)
        res = self.client.get(f"/audits/review/{self.round.pk}/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("I-DE-266134", body)
        self.assertIn("Sun, Nov 01, 2026 02:10 AM ADT", body)
        self.assertIn("+59.0 min", body)
        self.assertIn("newest of 2 runs", body)
        self.assertEqual(self.client.get(f"/audits/review/{self.round.pk}/?drift=1").status_code, 200)
        self.assertEqual(self.client.get(f"/audits/review/{self.round.pk + 999}/").status_code, 404)

    def test_requires_sign_in(self):
        self.assertEqual(self.client.get("/audits/review/").status_code, 302)
