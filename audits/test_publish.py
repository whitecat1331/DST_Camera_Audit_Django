import io
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import OperationalError, connections
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from audits.models import AuditJob, AuditRound, AuditScreenshot, StoredFile
from audits.publish import SHARED, PublishError, publish_runs

MEDIA = tempfile.mkdtemp(prefix="dst-publish-test-")


def png_bytes(color="red", size=(800, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


@override_settings(MEDIA_ROOT=MEDIA)
class PublishRunsTests(TestCase):
    databases = {"default", SHARED}

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = get_user_model().objects.create_user("tech")
        done = AuditJob.Status.SUCCEEDED
        self.run = AuditJob.objects.create(
            pole_number="fleet",
            device_type=AuditJob.DeviceType.DST_AUDIT,
            status=done,
            created_by=self.user,
            finished_at=timezone.now(),
        )
        self.site = AuditJob.objects.create(
            pole_number="233038",
            target_host="de",
            device_type=AuditJob.DeviceType.DST_SITE,
            status=done,
            parent_job=self.run,
            ovrc_fx="FX1261",
            ovrc_zone="ADT",
            drift_seconds=3540,
        )
        self.old = timezone.now() - timedelta(days=3)
        AuditJob.objects.filter(pk__in=[self.run.pk, self.site.pk]).update(created_at=self.old)
        self.shot = AuditScreenshot(job=self.site, label="de_l1")
        self.shot.image.save("de_l1.png", ContentFile(png_bytes()), save=True)

    def publish(self, *ids, round_name="2026-11-01 fall back"):
        return publish_runs(list(ids or [self.run.pk]), round_name, username="tech")

    def test_copies_run_children_screenshots_and_image(self):
        report = self.publish()
        self.assertEqual((report.runs, report.jobs, report.screenshots), (1, 2, 1))
        self.assertEqual(report.images_uploaded, 1)

        shared_site = AuditJob.objects.using(SHARED).get(public_id=self.site.public_id)
        shared_run = AuditJob.objects.using(SHARED).get(public_id=self.run.public_id)
        self.assertEqual(shared_site.parent_job_id, shared_run.pk)
        self.assertEqual(shared_site.ovrc_zone, "ADT")
        self.assertEqual(shared_site.drift_seconds, 3540)
        self.assertEqual(shared_site.created_at, self.old)  # not overwritten by auto_now_add
        self.assertEqual(shared_run.round.name, "2026-11-01 fall back")

        shared_shot = AuditScreenshot.objects.using(SHARED).get(public_id=self.shot.public_id)
        self.assertEqual(shared_shot.job_id, shared_site.pk)
        stored = StoredFile.objects.using(SHARED).get(name=self.shot.image.name)
        self.assertEqual(bytes(stored.data), png_bytes())
        self.assertEqual(stored.content_type, "image/png")
        with Image.open(io.BytesIO(bytes(stored.thumb))) as thumb:
            self.assertLessEqual(max(thumb.size), 480)

        self.site.refresh_from_db()
        self.assertEqual(self.site.round.name, "2026-11-01 fall back")  # local round tag

    def test_republish_is_idempotent(self):
        self.publish()
        report = self.publish()
        self.assertEqual(report.images_uploaded, 0)
        self.assertEqual(report.images_unchanged, 1)
        self.assertEqual(AuditJob.objects.using(SHARED).count(), 2)
        self.assertEqual(AuditScreenshot.objects.using(SHARED).count(), 1)
        self.assertEqual(StoredFile.objects.using(SHARED).count(), 1)
        self.assertEqual(AuditRound.objects.using(SHARED).count(), 1)

    def test_changed_image_is_reuploaded_and_row_updates(self):
        self.publish()
        with open(self.shot.image.path, "wb") as fh:
            fh.write(png_bytes("blue"))
        AuditJob.objects.filter(pk=self.site.pk).update(drift_seconds=0)
        report = self.publish()
        self.assertEqual(report.images_uploaded, 1)
        stored = StoredFile.objects.using(SHARED).get(name=self.shot.image.name)
        self.assertEqual(bytes(stored.data), png_bytes("blue"))
        self.assertEqual(
            AuditJob.objects.using(SHARED).get(public_id=self.site.public_id).drift_seconds, 0
        )

    def test_unfinished_run_is_refused(self):
        AuditJob.objects.filter(pk=self.site.pk).update(status=AuditJob.Status.RUNNING)
        with self.assertRaisesMessage(PublishError, "still in progress"):
            self.publish()
        self.assertFalse(AuditJob.objects.using(SHARED).exists())

    def test_skipped_offline_site_is_finished_and_published(self):
        skipped = AuditJob.objects.create(
            pole_number="600004", target_host="lti", device_type=AuditJob.DeviceType.DST_SITE,
            status=AuditJob.Status.SKIPPED, parent_job=self.run,
        )
        report = self.publish()
        self.assertEqual(report.jobs, 3)
        self.assertEqual(
            AuditJob.objects.using(SHARED).get(public_id=skipped.public_id).status, AuditJob.Status.SKIPPED
        )

    def test_non_dst_run_is_refused(self):
        with self.assertRaisesMessage(PublishError, "Not DST audit runs"):
            self.publish(self.site.pk)

    def test_round_name_required(self):
        with self.assertRaisesMessage(PublishError, "Round name is required"):
            self.publish(round_name="   ")

    def test_missing_file_is_reported_and_row_still_published(self):
        import os

        os.remove(self.shot.image.path)
        report = self.publish()
        self.assertEqual(report.missing_files, [self.shot.image.name])
        self.assertTrue(
            AuditScreenshot.objects.using(SHARED).filter(public_id=self.shot.public_id).exists()
        )
        self.assertFalse(StoredFile.objects.using(SHARED).exists())

    def test_unreachable_shared_db_says_turn_off_vpn(self):
        with mock.patch.object(
            connections[SHARED],
            "ensure_connection",
            side_effect=OperationalError("connection timeout expired"),
        ):
            with self.assertRaisesMessage(PublishError, "turn it off and publish again"):
                self.publish()

    def test_users_matched_by_username_or_left_null(self):
        self.publish()
        self.assertIsNone(
            AuditJob.objects.using(SHARED).get(public_id=self.run.public_id).created_by_id
        )
        shared_tech = get_user_model().objects.using(SHARED).create(username="tech")
        self.publish()
        self.assertEqual(
            AuditJob.objects.using(SHARED).get(public_id=self.run.public_id).created_by_id,
            shared_tech.pk,
        )

    @override_settings(DST_REVIEWERS=["carthur", "jgreene", "ployd"])
    def test_publish_assigns_new_cameras_once(self):
        from audits.models import InstallationAuditReview

        report = self.publish()
        self.assertEqual(report.assigned, 1)
        review = InstallationAuditReview.objects.using(SHARED).get()
        self.assertEqual((review.camera_key, review.kind, review.assigned_to), ("233038", "de", "carthur"))
        InstallationAuditReview.objects.using(SHARED).filter(pk=review.pk).update(
            assigned_to="ployd", completed=True, notes="checked"
        )
        # A second run with the same camera plus a new one: only the new one is assigned.
        run2 = AuditJob.objects.create(
            pole_number="fleet", device_type=AuditJob.DeviceType.DST_AUDIT,
            status=AuditJob.Status.SUCCEEDED, finished_at=timezone.now(),
        )
        for pole in ("233038", "266006"):
            AuditJob.objects.create(
                pole_number=pole, target_host="de", device_type=AuditJob.DeviceType.DST_SITE,
                status=AuditJob.Status.SUCCEEDED, parent_job=run2,
            )
        self.assertEqual(self.publish(run2.pk).assigned, 1)
        self.assertEqual(self.publish().assigned, 0)
        got = dict(InstallationAuditReview.objects.using(SHARED).values_list("camera_key", "assigned_to"))
        self.assertEqual(got, {"233038": "ployd", "266006": "jgreene"})
        kept = InstallationAuditReview.objects.using(SHARED).get(camera_key="233038")
        self.assertEqual((kept.completed, kept.notes), (True, "checked"))
        self.assertEqual(AuditRound.objects.using(SHARED).get().reviewers, ["carthur", "jgreene", "ployd"])
