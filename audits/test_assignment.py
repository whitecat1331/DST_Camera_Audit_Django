import json
from collections import Counter
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audits.assignment import assign_new_cameras, assign_unassigned, reviewer_for
from audits.models import AuditJob, AuditRound, InstallationAuditReview
from audits.review import Filters, build_round_rows, filter_rows, summarize, tech_progress
from cameras.models import UserProfile

TEAM = ["carthur", "jgreene", "ployd"]
SITE = AuditJob.DeviceType.DST_SITE


def cams(kind, n, start=0):
    return [(f"{kind}{i:03d}", kind) for i in range(start, start + n)]


class AssignmentMathTests(TestCase):
    def setUp(self):
        self.round = AuditRound.objects.create(name="r")

    def counts(self, kind=None):
        qs = InstallationAuditReview.objects.filter(round=self.round)
        if kind:
            qs = qs.filter(kind=kind)
        return Counter(qs.values_list("assigned_to", flat=True))

    def test_reviewer_for_wraps(self):
        self.assertEqual([reviewer_for(TEAM, 1, k) for k in range(4)], ["jgreene", "ployd", "carthur", "jgreene"])
        self.assertEqual(reviewer_for([], 0, 5), "")

    def test_x_mod_n_in_arrival_order(self):
        rows = assign_new_cameras(self.round, cams("lti", 7), reviewers=TEAM)
        self.assertEqual([r.assigned_to for r in rows], (TEAM * 3)[:7])
        self.assertEqual([r.assignment_seq for r in rows], list(range(7)))

    def test_balanced_per_type_and_overall(self):
        # LTI run first (7 cameras), then the DE run (5), then a late LTI batch (4).
        assign_new_cameras(self.round, cams("lti", 7), reviewers=TEAM)
        assign_new_cameras(self.round, cams("de", 5), reviewers=TEAM)
        assign_new_cameras(self.round, cams("lti", 4, start=7), reviewers=TEAM)
        for kind in ("lti", "de", None):
            with self.subTest(kind=kind):
                c = self.counts(kind)
                self.assertLessEqual(max(c.values()) - min(c.values()), 1, c)
        # DE starts where the total left off (7 % 3 = 1), not at tech 0.
        self.round.refresh_from_db()
        self.assertEqual(self.round.assignment_offsets, {"lti": 0, "de": 1})
        first_de = InstallationAuditReview.objects.get(round=self.round, camera_key="de000")
        self.assertEqual(first_de.assigned_to, "jgreene")

    def test_existing_rows_never_move_and_repeat_is_noop(self):
        assign_new_cameras(self.round, cams("lti", 4), reviewers=TEAM)
        InstallationAuditReview.objects.filter(camera_key="lti000").update(assigned_to="ployd", completed=True, notes="x")
        before = dict(InstallationAuditReview.objects.values_list("camera_key", "assigned_to"))
        again = assign_new_cameras(self.round, cams("lti", 4) + [("lti000", "lti")], reviewers=TEAM)
        self.assertEqual(again, [])
        self.assertEqual(dict(InstallationAuditReview.objects.values_list("camera_key", "assigned_to")), before)
        kept = InstallationAuditReview.objects.get(camera_key="lti000")
        self.assertEqual((kept.completed, kept.notes), (True, "x"))

    def test_reviewers_snapshotted_on_round(self):
        assign_new_cameras(self.round, cams("lti", 2), reviewers=TEAM)
        rows = assign_new_cameras(self.round, cams("lti", 2, start=2), reviewers=["someone_else"])
        self.round.refresh_from_db()
        self.assertEqual(self.round.reviewers, TEAM)
        self.assertEqual([r.assigned_to for r in rows], ["ployd", "carthur"])

    def test_duplicate_keys_in_one_batch_and_blank_keys(self):
        rows = assign_new_cameras(self.round, [("a", "lti"), ("a", "lti"), ("", "lti"), (" b ", "LTI")], reviewers=TEAM)
        self.assertEqual([(r.camera_key, r.kind) for r in rows], [("a", "lti"), ("b", "lti")])

    def test_no_team_leaves_unassigned_then_backfill(self):
        assign_new_cameras(self.round, cams("lti", 2) + cams("de", 2), reviewers=[])
        self.assertEqual(self.counts(), Counter({"": 4}))
        self.assertEqual(assign_unassigned(self.round, reviewers=TEAM), 4)
        self.assertEqual(
            dict(InstallationAuditReview.objects.values_list("camera_key", "assigned_to")),
            {"lti000": "carthur", "lti001": "jgreene", "de000": "ployd", "de001": "carthur"},
        )
        self.assertEqual(assign_unassigned(self.round, reviewers=TEAM), 0)


class AssignReviewersCommandTests(TestCase):
    def test_backfills_round_in_arrival_order(self):
        rnd = AuditRound.objects.create(name="Acceptance")
        for pole, kind in (("266059", "lti"), ("266006", "de"), ("266007", "de"), ("266006", "de")):
            AuditJob.objects.create(pole_number=pole, target_host=kind, device_type=SITE, round=rnd)
        out = StringIO()
        call_command("assign_reviewers", "--round", "Acceptance", "--reviewers", ",".join(TEAM), "--database", "default", stdout=out)
        self.assertEqual(
            dict(rnd.reviews.values_list("camera_key", "assigned_to")),
            {"266059": "carthur", "266006": "jgreene", "266007": "ployd"},
        )
        call_command("assign_reviewers", "--round", "Acceptance", "--reviewers", "x", "--database", "default", stdout=StringIO())
        self.assertEqual(rnd.reviews.count(), 3)
        self.assertEqual(set(rnd.reviews.values_list("assigned_to", flat=True)), set(TEAM))


class ReviewPageAssignmentTests(TestCase):
    def setUp(self):
        self.round = AuditRound.objects.create(name="r")
        for pole, kind in cams("lti", 4) + cams("de", 2):
            AuditJob.objects.create(
                pole_number=pole, target_host=kind, device_type=SITE, round=self.round,
                status=AuditJob.Status.SUCCEEDED, finished_at=timezone.now(),
            )
        assign_new_cameras(self.round, cams("lti", 4) + cams("de", 2), reviewers=TEAM)
        self.user = get_user_model().objects.create_user("jgreene")
        UserProfile.objects.create(user=self.user, ims_role="readonly")
        self.client.force_login(self.user)
        self.url = f"/audits/review/{self.round.pk}/item/"

    def post(self, **body):
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def review(self, key):
        return InstallationAuditReview.objects.get(round=self.round, camera_key=key)

    def test_filters_and_progress(self):
        rows = build_round_rows(self.round)
        mine = filter_rows(rows, Filters(tech="mine"), "jgreene")
        self.assertEqual({r.key for r in mine}, {"lti001", "de000"})
        self.assertEqual({r.key for r in filter_rows(rows, Filters(tech="ployd"))}, {"lti002", "de001"})
        self.assertEqual({r.key for r in filter_rows(rows, Filters(tech="ployd", kind="de"))}, {"de001"})
        InstallationAuditReview.objects.filter(camera_key="lti001").update(completed=True)
        rows = build_round_rows(self.round)
        self.assertEqual({r.key for r in filter_rows(rows, Filters(tech="mine", status="incomplete"), "jgreene")}, {"de000"})
        self.assertEqual(
            tech_progress(rows, TEAM),
            [
                {"name": "carthur", "total": 2, "complete": 0},
                {"name": "jgreene", "total": 2, "complete": 1},
                {"name": "ployd", "total": 2, "complete": 0},
            ],
        )
        self.assertEqual(summarize(rows)["complete"], 1)
        self.assertEqual({r.key for r in filter_rows(rows, Filters(status="complete"))}, {"lti001"})
        self.assertEqual(len(filter_rows(rows, Filters())), 6)
        self.assertFalse(Filters().active)
        self.assertTrue(Filters(drift=True).active)

    def test_filter_panel_reflects_and_toggles(self):
        url = f"/audits/review/{self.round.pk}/"
        body = self.client.get(url + "?tech=carthur&kind=lti&status=incomplete").content.decode()
        self.assertIn('<option value="carthur" selected>carthur</option>', body)
        self.assertIn('<option value="lti" selected>LTI</option>', body)
        self.assertIn("Showing 2 of 6 cameras", body)
        # The active tech chip links back to everyone, keeping the other filters.
        self.assertIn('href="?kind=lti&amp;status=incomplete"', body)
        self.assertIn('href="?tech=jgreene&amp;kind=lti&amp;status=incomplete"', body)
        # Clear goes back to the unfiltered round; unfiltered it's disabled.
        self.assertIn(f'href="{url}" class="btn-secondary "', body)
        self.assertIn("btn-disabled", self.client.get(url).content.decode())
        # Garbage values fall back to All.
        body = self.client.get(url + "?tech=x&kind=y&status=z&drift=yes").content.decode()
        self.assertIn("Showing 6 of 6 cameras", body)

    def test_page_filters_by_tech(self):
        body = self.client.get(f"/audits/review/{self.round.pk}/?tech=mine").content.decode()
        self.assertIn("lti001", body)
        self.assertNotIn("lti000", body)
        body = self.client.get(f"/audits/review/{self.round.pk}/?tech=carthur&kind=lti").content.decode()
        self.assertIn('data-key="lti000"', body)
        self.assertIn('data-key="lti003"', body)
        self.assertNotIn('data-key="lti001"', body)
        self.assertIn('<strong>carthur</strong> <span class="review-progress-n">0/2</span>', body)
        # Unknown tech falls back to everyone.
        body = self.client.get(f"/audits/review/{self.round.pk}/?tech=nobody").content.decode()
        self.assertEqual(body.count("<tr class="), 6)

    def test_page_query_count_constant(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get(f"/audits/review/{self.round.pk}/")
        for pole, kind in cams("lti", 10, start=50):
            AuditJob.objects.create(pole_number=pole, target_host=kind, device_type=SITE, round=self.round)
        assign_new_cameras(self.round, cams("lti", 10, start=50), reviewers=TEAM)
        with CaptureQueriesContext(connection) as large:
            self.client.get(f"/audits/review/{self.round.pk}/")
        self.assertEqual(len(large), len(small))

    def test_readonly_user_can_complete_and_uncomplete(self):
        res = self.post(camera_key="lti000", field="completed", value=True)
        self.assertEqual(res.status_code, 200)
        r = self.review("lti000")
        self.assertEqual((r.completed, r.completed_by, r.version), (True, "jgreene", 1))
        self.assertIsNotNone(r.completed_at)
        self.post(camera_key="lti000", field="completed", value=False)
        r = self.review("lti000")
        self.assertEqual((r.completed, r.completed_by, r.completed_at), (False, "", None))
        self.assertEqual(self.post(camera_key="lti000", field="completed", value="yes").status_code, 400)

    def test_notes_save_and_conflict(self):
        res = self.post(camera_key="de000", field="notes", value="clock fixed\r\nby ployd", prev="")
        self.assertEqual(res.json()["state"]["notes"], "clock fixed\nby ployd")
        self.assertEqual(self.review("de000").notes_updated_by, "jgreene")
        # A second editor who opened the modal before that save gets a 409, not an overwrite.
        res = self.post(camera_key="de000", field="notes", value="mine", prev="")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["state"]["notes"], "clock fixed\nby ployd")
        self.assertEqual(self.review("de000").notes, "clock fixed\nby ployd")
        # Saving again with the current text as the base replaces it.
        self.assertEqual(self.post(camera_key="de000", field="notes", value="mine", prev="clock fixed\nby ployd").status_code, 200)
        self.assertEqual(self.review("de000").notes, "mine")
        # A completed toggle in between does not conflict with notes.
        self.post(camera_key="de000", field="completed", value=True)
        self.assertEqual(self.post(camera_key="de000", field="notes", value="m2", prev="mine").status_code, 200)
        self.assertEqual(self.post(camera_key="de000", field="notes", value="x" * 4001, prev="m2").status_code, 400)

    def test_manual_reassign(self):
        res = self.post(camera_key="lti000", field="assigned_to", value="ployd")
        self.assertEqual(res.status_code, 200)
        r = self.review("lti000")
        self.assertEqual((r.assigned_to, r.assigned_by, r.assignment_seq), ("ployd", "jgreene", 0))
        self.assertEqual(self.post(camera_key="lti000", field="assigned_to", value="mallory").status_code, 400)
        self.assertEqual(self.post(camera_key="lti000", field="assigned_to", value="").status_code, 200)
        self.assertEqual(self.review("lti000").assigned_to, "")

    def test_unknown_camera_field_and_sign_in(self):
        self.assertEqual(self.post(camera_key="nope", field="completed", value=True).status_code, 404)
        self.assertEqual(self.post(camera_key="lti000", field="version", value=9).status_code, 400)
        self.assertEqual(self.client.post(self.url, "{", content_type="application/json").status_code, 400)
        self.client.logout()
        self.assertEqual(self.post(camera_key="lti000", field="completed", value=True).status_code, 302)

    def test_edit_creates_row_for_camera_published_before_assignment(self):
        AuditJob.objects.create(pole_number="old1", target_host="de", device_type=SITE, round=self.round)
        self.assertEqual(self.post(camera_key="old1", field="completed", value=True).status_code, 200)
        r = self.review("old1")
        self.assertEqual((r.kind, r.assigned_to, r.completed), ("de", "", True))

    @override_settings(DST_ROLE="review")
    def test_review_role_allows_review_edits(self):
        self.assertEqual(self.post(camera_key="lti000", field="completed", value=True).status_code, 200)
        self.assertEqual(self.client.post("/audits/dst/start/").status_code, 403)
