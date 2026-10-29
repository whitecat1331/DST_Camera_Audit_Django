"""One-time scheduled DST audits: task XML, schedule lifecycle, single fire."""

import json
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from audits import scheduling
from audits.models import AuditJob, ScheduledAudit
from services import task_scheduler

MEDIA = tempfile.mkdtemp(prefix="dst-sched-test-")
NY = ZoneInfo("America/New_York")


class TaskXmlTests(SimpleTestCase):
    def xml(self, fire_at):
        return task_scheduler.task_xml(
            5, fire_at, user="LAPTOP\\ployd", python="C:\\py\\pythonw.exe", workdir="C:\\dst"
        )

    def test_one_time_interactive_wake_and_command(self):
        xml = self.xml(datetime(2026, 10, 28, 23, 0, tzinfo=NY))
        self.assertIn("<StartBoundary>2026-10-29T03:00:00Z</StartBoundary>", xml)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<WakeToRun>true</WakeToRun>", xml)
        self.assertIn("<StartWhenAvailable>false</StartWhenAvailable>", xml)
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", xml)
        self.assertIn("<Arguments>manage.py run_scheduled_audit 5</Arguments>", xml)
        self.assertIn("<UserId>LAPTOP\\ployd</UserId>", xml)
        self.assertNotIn("<Repetition>", xml)

    def test_clock_change_night_is_unambiguous(self):
        # 2026-11-01 01:30 happens twice in New York; each is its own UTC instant.
        first = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=0)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=1)
        self.assertEqual(task_scheduler.start_boundary(first), "2026-11-01T05:30:00Z")
        self.assertEqual(task_scheduler.start_boundary(second), "2026-11-01T06:30:00Z")

    def test_naive_time_is_refused(self):
        with self.assertRaises(ValueError):
            task_scheduler.start_boundary(datetime(2026, 10, 28, 23, 0))

    def test_parse_local_reads_the_device_clock(self):
        parsed = scheduling.parse_local("2026-10-28T23:30")
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.astimezone().replace(tzinfo=None), datetime(2026, 10, 28, 23, 30))
        with self.assertRaises(scheduling.ScheduleError):
            scheduling.parse_local("")


def opts(ids=(1,), round_name=""):
    return {
        "scope": "all",
        "state": "",
        "agency": "",
        "installation_ids": list(ids),
        "publish_round": round_name,
    }


@override_settings(MEDIA_ROOT=MEDIA)
class ScheduleLifecycleTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = get_user_model().objects.create_user("tech")
        self.later = timezone.now() + timedelta(hours=3)
        patcher = mock.patch(
            "audits.runner._active_dst_installations",
            return_value=[mock.Mock(pk=1), mock.Mock(pk=2)],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch.object(task_scheduler, "register")
    def test_create_registers_the_task(self, register):
        sched = scheduling.create_schedule(self.user, self.later, opts(round_name="R1"))
        register.assert_called_once_with(f"DST scheduled audit {sched.pk}", sched.pk, self.later)
        self.assertEqual(sched.installation_ids, [1, 2])
        self.assertEqual(sched.status, ScheduledAudit.Status.SCHEDULED)

    @mock.patch.object(
        task_scheduler, "register", side_effect=task_scheduler.TaskSchedulerError("denied")
    )
    def test_refused_task_leaves_no_schedule(self, _register):
        with self.assertRaisesRegex(scheduling.ScheduleError, "denied"):
            scheduling.create_schedule(self.user, self.later, opts())
        self.assertFalse(ScheduledAudit.objects.exists())

    @mock.patch.object(task_scheduler, "register")
    def test_past_time_refused(self, register):
        with self.assertRaises(scheduling.ScheduleError):
            scheduling.create_schedule(self.user, timezone.now(), opts())
        register.assert_not_called()

    @mock.patch.object(task_scheduler, "delete")
    @mock.patch.object(task_scheduler, "register")
    def test_cancel_removes_the_task(self, _register, delete):
        sched = scheduling.create_schedule(self.user, self.later, opts())
        scheduling.cancel_schedule(sched)
        delete.assert_called_once_with(sched.task_name)
        self.assertEqual(ScheduledAudit.objects.get().status, ScheduledAudit.Status.CANCELLED)
        with self.assertRaises(scheduling.ScheduleError):
            scheduling.cancel_schedule(sched)

    @mock.patch.object(task_scheduler, "delete")
    @mock.patch.object(task_scheduler, "register")
    def test_fires_once_and_deletes_its_task(self, _register, delete):
        sched = scheduling.create_schedule(self.user, self.later, opts())

        def fake_run(job_id):
            AuditJob.objects.filter(pk=job_id).update(
                status=AuditJob.Status.SUCCEEDED, progress_message="Done"
            )

        with mock.patch("audits.runner._run_job", side_effect=fake_run) as run_job:
            first = scheduling.run_schedule(sched.pk)
            second = scheduling.run_schedule(sched.pk)  # a double fire
        self.assertEqual(run_job.call_count, 1)
        self.assertEqual(first.status, ScheduledAudit.Status.DONE)
        self.assertEqual(second.status, ScheduledAudit.Status.DONE)
        self.assertIn(f"DST audit #{first.job_id}: succeeded", first.message)
        delete.assert_called_with(sched.task_name)

    @mock.patch.object(task_scheduler, "delete")
    @mock.patch.object(task_scheduler, "register")
    def test_refuses_while_another_audit_runs(self, _register, _delete):
        busy = AuditJob.objects.create(
            device_type=AuditJob.DeviceType.DST_AUDIT, status=AuditJob.Status.RUNNING
        )
        sched = scheduling.create_schedule(self.user, self.later, opts())
        with mock.patch("audits.runner._run_job") as run_job:
            result = scheduling.run_schedule(sched.pk)
        run_job.assert_not_called()
        self.assertEqual(result.status, ScheduledAudit.Status.FAILED)
        self.assertIn(f"#{busy.pk} was already running", result.message)

    @mock.patch.object(task_scheduler, "register")
    def test_reap_spares_a_live_scheduled_run(self, _register):
        from audits.runner import reap_orphaned_audit_jobs

        sched = scheduling.create_schedule(self.user, self.later, opts())
        job = AuditJob.objects.create(
            device_type=AuditJob.DeviceType.DST_AUDIT, status=AuditJob.Status.RUNNING
        )
        child = AuditJob.objects.create(
            device_type=AuditJob.DeviceType.DST_SITE, status=AuditJob.Status.RUNNING, parent_job=job
        )
        stray = AuditJob.objects.create(
            device_type=AuditJob.DeviceType.CBW, status=AuditJob.Status.RUNNING
        )
        ScheduledAudit.objects.filter(pk=sched.pk).update(
            status=ScheduledAudit.Status.RUNNING, job=job, pid=123
        )
        with mock.patch("services.task_scheduler.pid_alive", return_value=True):
            self.assertEqual(reap_orphaned_audit_jobs(), 1)
        self.assertEqual(AuditJob.objects.get(pk=child.pk).status, AuditJob.Status.RUNNING)
        self.assertEqual(AuditJob.objects.get(pk=stray.pk).status, AuditJob.Status.FAILED)
        with mock.patch("services.task_scheduler.pid_alive", return_value=False):
            self.assertEqual(reap_orphaned_audit_jobs(), 2)
        self.assertEqual(ScheduledAudit.objects.get().status, ScheduledAudit.Status.FAILED)


class ScheduleViewTests(TestCase):
    def setUp(self):
        from cameras.models import UserProfile

        user = get_user_model().objects.create_user("tech", password="pw")
        UserProfile.objects.create(user=user, ims_role="technician")
        self.client.force_login(user)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    @mock.patch.object(task_scheduler, "register")
    def test_schedule_then_cancel(self, _register):
        at = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        with mock.patch("audits.runner._active_dst_installations", return_value=[mock.Mock(pk=4)]):
            res = self.post("/audits/dst/schedule/", {"installation_ids": [4], "fire_at": at})
        self.assertEqual(res.status_code, 200, res.content)
        sched = ScheduledAudit.objects.get(pk=res.json()["id"])
        self.assertEqual(sched.fire_at.astimezone().strftime("%Y-%m-%dT%H:%M"), at)
        page = self.client.get("/audits/dst/")
        self.assertContains(page, 'id="dst-schedule-btn"')
        self.assertContains(page, "Scheduled audits")
        with mock.patch.object(task_scheduler, "delete"):
            res = self.post(f"/audits/dst/schedule/{sched.pk}/cancel/", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.post(f"/audits/dst/schedule/{sched.pk}/explode/", {}).status_code, 404)

    def test_missing_time_is_a_clear_error(self):
        with mock.patch("audits.runner._active_dst_installations", return_value=[mock.Mock(pk=4)]):
            res = self.post("/audits/dst/schedule/", {"installation_ids": [4], "fire_at": ""})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "Pick a date and time.")

    def test_status_endpoint_reports_a_fired_schedule_and_its_job(self):
        job = AuditJob.objects.create(
            pole_number="DST-ALL", target_host="scope=all", device_type=AuditJob.DeviceType.DST_AUDIT,
            status=AuditJob.Status.RUNNING,
        )
        sched = ScheduledAudit.objects.create(
            fire_at=datetime.now().astimezone(), installation_ids=[4], status=ScheduledAudit.Status.RUNNING,
            job=job, publish_round="DST audit 2026-10-28", message=f"Running DST audit #{job.pk} (1 site(s))",
        )
        res = self.client.get("/audits/dst/schedule/status/")
        self.assertEqual(res.status_code, 200)
        row = res.json()["schedules"][0]
        self.assertEqual(
            (row["id"], row["status"], row["job_id"], row["publish_round"]),
            (sched.pk, "running", job.pk, "DST audit 2026-10-28"),
        )
        page = self.client.get("/audits/dst/")
        self.assertContains(page, "/audits/dst/schedule/status/")
