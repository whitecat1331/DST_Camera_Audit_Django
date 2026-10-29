import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from audits import live
from audits.assignment import assign_new_cameras
from audits.models import AuditJob, AuditRound
from cameras.models import UserProfile

TEAM = ["carthur", "jgreene", "ployd"]


class BrokerTests(TestCase):
    def test_broadcast_reaches_only_that_rounds_subscribers(self):
        b = live.Broker(max_clients=5)
        a, other = b.try_subscribe(1), b.try_subscribe(2)
        b.broadcast(1, "review", {"x": 1})
        self.assertEqual(a.get_nowait(), ("review", {"x": 1}))
        self.assertTrue(other.empty())
        b.unsubscribe(a)
        b.unsubscribe(a)  # twice is fine
        self.assertEqual(b.count(), 1)

    def test_cap_and_full_queue_drops(self):
        b = live.Broker(max_clients=1)
        q = b.try_subscribe(1)
        self.assertIsNone(b.try_subscribe(1))
        for i in range(70):  # queue holds 64; the rest are dropped, not blocking
            b.broadcast(1, "review", {"i": i})
        self.assertEqual(q.qsize(), 64)

    def test_stream_frames_heartbeat_and_unsubscribes(self):
        q = live.broker.try_subscribe(7)
        before = live.broker.count()
        live.broker.broadcast(7, "review", {"a": "b"})
        out = list(live.stream(q, max_seconds=0.3, heartbeat=0.1))
        self.assertTrue(out[0].startswith("retry: 3000\nevent: connected\n"))
        self.assertEqual(out[1], 'event: review\ndata: {"a":"b"}\n\n')
        self.assertIn(": ping\n\n", out[2:])
        self.assertEqual(live.broker.count(), before - 1)


class LiveReviewViewTests(TestCase):
    def setUp(self):
        self.round = AuditRound.objects.create(name="r")
        for pole, kind in (("a", "lti"), ("b", "lti"), ("c", "de")):
            AuditJob.objects.create(
                pole_number=pole, target_host=kind, device_type=AuditJob.DeviceType.DST_SITE,
                round=self.round, status=AuditJob.Status.SUCCEEDED, finished_at=timezone.now(),
            )
        assign_new_cameras(self.round, [("a", "lti"), ("b", "lti"), ("c", "de")], reviewers=TEAM)
        user = get_user_model().objects.create_user("jgreene")
        UserProfile.objects.create(user=user, ims_role="readonly")
        self.client.force_login(user)

    def test_save_broadcasts_state_and_progress(self):
        q = live.broker.try_subscribe(self.round.pk)
        try:
            self.client.post(
                f"/audits/review/{self.round.pk}/item/",
                json.dumps({"camera_key": "b", "field": "completed", "value": True}),
                content_type="application/json",
                HTTP_X_REVIEW_CLIENT="tab-1",
            )
            event, data = q.get_nowait()
        finally:
            live.broker.unsubscribe(q)
        self.assertEqual(event, "review")
        self.assertEqual(data["state"]["camera_key"], "b")
        self.assertTrue(data["state"]["completed"])
        self.assertEqual((data["by"], data["client"], data["field"]), ("jgreene", "tab-1", "completed"))
        self.assertEqual(data["progress"]["complete"], 1)
        self.assertEqual(data["progress"]["techs"]["jgreene"], {"total": 1, "complete": 1})
        self.assertEqual(data["progress"]["unassigned"], 0)

    def test_failed_save_broadcasts_nothing(self):
        q = live.broker.try_subscribe(self.round.pk)
        try:
            self.client.post(
                f"/audits/review/{self.round.pk}/item/",
                json.dumps({"camera_key": "b", "field": "completed", "value": "nope"}),
                content_type="application/json",
            )
            self.assertTrue(q.empty())
        finally:
            live.broker.unsubscribe(q)

    def test_events_stream_and_cap(self):
        res = self.client.get(f"/audits/review/{self.round.pk}/events/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "text/event-stream")
        self.assertEqual(res["Cache-Control"], "no-cache")
        first = next(iter(res.streaming_content)).decode()
        self.assertIn("event: connected", first)
        res.close()
        old = live.broker.max_clients
        live.broker.max_clients = live.broker.count()
        try:
            self.assertEqual(self.client.get(f"/audits/review/{self.round.pk}/events/").status_code, 503)
        finally:
            live.broker.max_clients = old

    def test_events_need_sign_in_and_page_has_live_hooks(self):
        body = self.client.get(f"/audits/review/{self.round.pk}/").content.decode()
        self.assertIn(f'data-events-url="/audits/review/{self.round.pk}/events/"', body)
        self.assertIn('id="review-live"', body)
        self.assertIn('data-tech="jgreene"', body)
        self.client.logout()
        self.assertEqual(self.client.get(f"/audits/review/{self.round.pk}/events/").status_code, 302)
