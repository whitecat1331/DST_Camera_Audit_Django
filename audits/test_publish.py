import io
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import OperationalError, connections
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from audits.models import AuditJob, AuditRound, AuditScreenshot, StoredFile
from audits.publish import SHARED, PublishError, publish_runs

MEDIA = tempfile.mkdtemp(prefix="dst-publish-test-")


def png_bytes(color="red", size=(800, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


# Uploads inline: worker threads can't share the test databases' transaction.
@override_settings(MEDIA_ROOT=MEDIA, DST_PUBLISH_UPLOAD_CONCURRENT=1)
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

    # ── Live publish: sites go out as they finish; the final publish sends only changes ──

    def live(self, *sites):
        from audits.publish import LivePublisher

        live = LivePublisher(self.run.pk, "2026-11-01 fall back", self.user, reachable=lambda: True)
        live._publish([s.pk for s in sites])  # one batch, on this thread
        return live

    def test_site_published_live_shows_before_the_run_ends_and_is_not_resent(self):
        AuditJob.objects.filter(pk=self.run.pk).update(status=AuditJob.Status.RUNNING)
        live = self.live(self.site)
        self.assertEqual(live.published, 1)
        self.assertTrue(AuditJob.objects.using(SHARED).filter(public_id=self.site.public_id).exists())
        self.assertTrue(StoredFile.objects.using(SHARED).filter(name=self.shot.image.name).exists())

        AuditJob.objects.filter(pk=self.run.pk).update(status=AuditJob.Status.SUCCEEDED)
        report = publish_runs([self.run.pk], "2026-11-01 fall back", username="tech", caches={self.run.pk: live.cache})
        self.assertEqual((report.jobs, report.sent_live, report.images_uploaded), (1, 1, 0))  # parent only
        self.assertEqual(
            AuditJob.objects.using(SHARED).get(public_id=self.run.public_id).status, AuditJob.Status.SUCCEEDED
        )

    def test_site_changed_after_its_live_publish_is_resent(self):
        live = self.live(self.site)
        # The fleet OvrC pass adds a time screenshot to a DE site after it was sent.
        ovrc = AuditScreenshot(job=self.site, label="ovrc_fx1261")
        ovrc.image.save("ovrc.png", ContentFile(png_bytes("green")), save=True)
        AuditJob.objects.filter(pk=self.site.pk).update(drift_seconds=0)
        report = publish_runs([self.run.pk], "2026-11-01 fall back", username="tech", caches={self.run.pk: live.cache})
        self.assertEqual((report.jobs, report.sent_live, report.images_uploaded), (2, 0, 1))
        self.assertEqual(AuditScreenshot.objects.using(SHARED).count(), 2)
        self.assertEqual(AuditJob.objects.using(SHARED).get(public_id=self.site.public_id).drift_seconds, 0)

    def test_failed_live_batch_is_left_for_the_final_publish(self):
        from audits import publish

        with mock.patch.object(publish, "_publish_run", side_effect=OperationalError("connection reset")):
            live = self.live(self.site)  # logged, never raised into the capture
        self.assertEqual(live.published, 0)
        report = publish_runs([self.run.pk], "2026-11-01 fall back", username="tech", caches={self.run.pk: live.cache})
        self.assertEqual((report.jobs, report.sent_live, report.images_uploaded), (2, 0, 1))


class LivePublisherThreadTests(SimpleTestCase):
    """The thread holds sites while the shared DB is unreachable (VPN on), then drains."""

    def make(self, reachable):
        from audits.publish import LivePublisher

        live = LivePublisher(1, "R1", None, reachable=reachable, poll_seconds=0.01)
        live._publish = mock.Mock(side_effect=lambda batch: setattr(live, "published", live.published + len(batch)))
        return live

    def test_holds_while_unreachable_then_drains(self):
        answers = iter([False, False] + [True] * 50)
        live = self.make(lambda: next(answers))
        live.site_done(11)
        live.site_done(12)
        live.start()
        deadline = time.time() + 5
        while live.published < 2 and time.time() < deadline:
            time.sleep(0.01)
        live.close()
        self.assertEqual(live._publish.call_args_list, [mock.call([11, 12])])

    def test_close_while_held_returns_without_publishing(self):
        live = self.make(lambda: False).start()
        live.site_done(11)
        t0 = time.time()
        live.close()
        self.assertLess(time.time() - t0, 2)
        live._publish.assert_not_called()


class ParallelUploadTests(SimpleTestCase):
    @override_settings(DST_PUBLISH_UPLOAD_CONCURRENT=4)
    def test_uploads_run_at_once_and_are_counted(self):
        from audits import publish

        threads = set()

        def upload(shot, cache):
            threads.add(threading.current_thread().name)
            time.sleep(0.02)
            return {"a": "uploaded", "b": "unchanged", "c": "missing"}[shot.image.name[0]]

        shots = []
        for i, kind in enumerate("aaaaabbbbc"):  # 5 uploaded, 4 unchanged, 1 missing
            shot = mock.Mock()
            shot.image.name = f"{kind}{i}"
            shots.append(shot)
        report = publish.PublishReport()
        with mock.patch.object(publish, "_upload_image", side_effect=upload):
            publish._upload_images(shots, report, lambda _m: None, "Run #1", None)
        self.assertEqual((report.images_uploaded, report.images_unchanged, len(report.missing_files)), (5, 4, 1))
        self.assertGreater(len(threads), 1)

    @override_settings(DST_PUBLISH_UPLOAD_CONCURRENT=4)
    def test_a_worker_error_is_raised_to_the_publish(self):
        from audits import publish

        shots = [mock.Mock() for _ in range(6)]
        with mock.patch.object(publish, "_upload_image", side_effect=OperationalError("reset")):
            with self.assertRaises(OperationalError):
                publish._upload_images(shots, publish.PublishReport(), lambda _m: None, "Run #1", None)
