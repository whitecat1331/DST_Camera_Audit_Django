"""The DST status poll scales to a fleet run: totals for every site, a bounded row list."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from audits import views
from audits.models import AuditJob

SITE = AuditJob.DeviceType.DST_SITE
S = AuditJob.Status


class DstStatusPollTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tech"))
        self.run = AuditJob.objects.create(
            pole_number="DST-ALL", device_type=AuditJob.DeviceType.DST_AUDIT, status=S.RUNNING,
            target_host="capture 5/814",
        )
        for i, status in enumerate([S.SUCCEEDED, S.SUCCEEDED, S.SKIPPED, S.FAILED, S.RUNNING]):
            AuditJob.objects.create(pole_number=f"p{i}", target_host="lti", device_type=SITE,
                                    status=status, parent_job=self.run)
        AuditJob.objects.get(pole_number="p0").screenshots.create(label="cbw", image="x/cbw.png")

    def status(self):
        return self.client.get(f"/audits/{self.run.pk}/status/").json()["dst"]

    def test_totals_cover_every_site_and_rows_are_bounded(self):
        original = views.DST_STATUS_SITE_ROWS
        views.DST_STATUS_SITE_ROWS = 3
        try:
            dst = self.status()
        finally:
            views.DST_STATUS_SITE_ROWS = original
        self.assertEqual(
            {k: dst[k] for k in ("sites_total", "sites_shown", "sites_done", "sites_ok", "sites_failed", "sites_skipped")},
            {"sites_total": 5, "sites_shown": 3, "sites_done": 4, "sites_ok": 2, "sites_failed": 1, "sites_skipped": 1},
        )
        self.assertEqual(dst["sites"][0]["status"], "running")  # the live one leads

    def test_rows_carry_a_shot_count_not_screenshot_lists(self):
        rows = {r["pole"]: r for r in self.status()["sites"]}
        self.assertEqual(rows["p0"]["shot_count"], 1)
        self.assertNotIn("screenshots", rows["p0"])
