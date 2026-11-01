"""DST page search: one term or a pasted list, like IMS's Installation Multi-Search."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from audits.views import parse_search_terms, site_term_hits
from cameras.models import Installation, UserProfile


class ParseTermsTests(SimpleTestCase):
    def test_lines_commas_and_semicolons_split_and_repeats_drop(self):
        raw = "233201\r\n255037, FX1168;  \n\nfx1168\nBainbridge"
        self.assertEqual(parse_search_terms(raw), ["233201", "255037", "FX1168", "Bainbridge"])

    def test_empty(self):
        self.assertEqual(parse_search_terms("  \n , "), [])

    def test_pole_matches_exactly_other_terms_by_substring(self):
        hay = "233201 i-lti-233201 bainbridge georgia"
        self.assertEqual(site_term_hits(["233201", "bainbridge", "999999"], "233201", hay), ["233201", "bainbridge"])
        self.assertEqual(site_term_hits(["2332"], "233201", hay), ["2332"])  # a partial still finds it, as before


class DstPageMultiSearchTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("tech")
        UserProfile.objects.create(user=user, ims_role="technician")
        self.client.force_login(user)
        sites = [
            Installation(pk=1, pole_number="233201", identifier="I-LTI-233201", agency="Shreveport", state="Louisiana"),
            Installation(pk=2, pole_number="255037", identifier="I-DE-255037", agency="Blountstown", state="Florida",
                         serial_number="FX1168"),
            Installation(pk=3, pole_number="233127", identifier="I-LTI-233127", agency="Bainbridge", state="Georgia"),
        ]
        for patch in (
            mock.patch("audits.runner._active_dst_installations", return_value=sites),
            mock.patch("audits.runner._dst_site_kind", return_value="lti"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def listed(self, q):
        res = self.client.get("/audits/dst/", {"q": q})
        self.assertEqual(res.status_code, 200)
        return [s["pole"] for s in res.context["sites"]], res.context["unmatched_terms"]

    def test_a_pasted_list_lists_every_match_and_names_the_misses(self):
        poles, unmatched = self.listed("233201\n255037, 999999\nBainbridge")
        self.assertEqual(poles, ["233201", "255037", "233127"])
        self.assertEqual(unmatched, ["999999"])

    def test_fx_numbers_and_single_terms_still_work(self):
        self.assertEqual(self.listed("fx1168"), (["255037"], []))
        self.assertEqual(self.listed(""), (["233201", "255037", "233127"], []))

    def test_the_box_shows_the_list_and_the_misses(self):
        body = self.client.get("/audits/dst/", {"q": "233201\n999999"}).content.decode()
        self.assertIn(">233201\n999999</textarea>", body)
        self.assertIn("No eligible site for:</strong> 999999", body)
