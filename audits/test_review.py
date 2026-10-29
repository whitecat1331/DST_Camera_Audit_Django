from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audits.models import AuditJob, AuditRound, AuditScreenshot
from audits.review import Filters, build_round_rows, filter_rows, format_drift, summarize
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
            ovrc_local_time_raw="Sun, Nov 01, 2026 03:10 AM ADT", ovrc_captured_at=read_at, drift_seconds=3540,
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
        ovrc = next(c for c in de.captures if c.key == "ovrc_fx1261")
        # Older run: the reading comes from the job's OvrC fields.
        self.assertEqual((ovrc.drift, ovrc.verdict), (3540, "fail"))
        self.assertEqual(ovrc.captured_eastern, "Nov 01, 02:11:00 AM EST")
        self.assertEqual(ovrc.device_note, "OvrC, says ADT")
        self.assertFalse(de.failed)  # the newest run succeeded
        self.assertEqual(
            summarize(rows),
            {
                "cameras": 2, "lti": 1, "de": 1, "failed": 0, "skipped": 0, "drift": 1,
                "devices_failing": 1, "devices_unread": 4, "complete": 0, "unassigned": 2,
            },
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
        AuditJob.objects.filter(pk=self.de_new.pk).update(ovrc_local_time_raw="Sun, Nov 01, 2026 02:10 AM EST")
        de = next(r for r in build_round_rows(self.round) if r.key == "266134")
        self.assertEqual(next(c for c in de.captures if c.key == "ovrc_fx1261").drift, -60)
        self.assertFalse(de.drift_flagged)

    def test_each_device_checked_against_the_site_zone(self):
        """A Louisiana CBW on Central time passes; its VNC an hour off fails; an unread DE is neither."""
        grabbed = datetime(2026, 10, 29, 15, 12, 19, tzinfo=dt_timezone.utc)  # 11:12:19 EDT, 10:12:19 CDT
        job = self.site("233121", "lti", finished=timezone.now(), site_tz="America/Chicago")
        for label, device_time, source in (
            ("cbw", "2026-10-29T10:12:19", "page"),
            ("vnc_l1", "2026-10-29T11:12:15", "ocr"),
            ("vnc_l2", "", ""),
        ):
            AuditScreenshot.objects.create(
                job=job, label=label, image=f"audits/{job.pk}/{label}.png", captured_at=grabbed,
                device_time=device_time, device_time_source=source,
            )
        row = next(r for r in build_round_rows(self.round) if r.key == "233121")
        checks = {c.key: (c.drift, c.verdict) for c in row.captures}
        self.assertEqual(checks, {"cbw": (0, "pass"), "vnc_l1": (3596, "fail"), "vnc_l2": (None, "unread")})
        cbw = row.captures[0]
        self.assertEqual(cbw.captured_eastern, "Oct 29, 11:12:19 AM EDT")
        self.assertEqual(cbw.expected_local, "Oct 29, 10:12:19 AM CDT")
        self.assertFalse(cbw.captured_estimated)
        self.assertEqual((row.site_tz_name, row.devices_failing, row.devices_unread), ("Central", 1, 1))
        self.assertTrue(row.drift_flagged)

    def test_older_run_without_a_stored_zone_uses_the_agency_list(self):
        Installation.objects.create(ims_id=3, identifier="I-LTI-500001", pole_number="500001", agency="Woodbury", state="Tennessee")
        self.site("500001", "lti", finished=timezone.now())
        row = next(r for r in build_round_rows(self.round) if r.key == "500001")
        self.assertEqual(row.site_tz, "America/Chicago")
        self.assertEqual(row.lines, [None])  # no captures still renders one line

    def test_drift_format(self):
        self.assertEqual([format_drift(s) for s in (0, -5, 123, -3600, 3602)], ["+0s", "-5s", "+2m 03s", "-1h 00m 00s", "+1h 00m 02s"])

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
        self.assertIn('title="Sun, Nov 01, 2026 03:10 AM ADT"', body)
        self.assertIn("+59m 00s", body)
        self.assertIn('<span class="check-badge check-fail">Fail</span>', body)
        self.assertIn("Unread — check the image", body)
        self.assertIn("newest of 2 runs", body)
        # One block per camera; its capture lines share the camera's cells.
        self.assertEqual(body.count('<tbody class="review-site'), 2)
        self.assertIn('rowspan="3"', body)
        self.assertEqual(self.client.get(f"/audits/review/{self.round.pk}/?drift=1").status_code, 200)
        self.assertEqual(self.client.get(f"/audits/review/{self.round.pk + 999}/").status_code, 404)

    def test_requires_sign_in(self):
        self.assertEqual(self.client.get("/audits/review/").status_code, 302)
