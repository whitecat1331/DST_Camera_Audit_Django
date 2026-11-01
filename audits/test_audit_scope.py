"""Phase 2: legacy, PRTG-paused and unmonitored sites are out of the audit's scope."""

from unittest import mock

import httpx
from django.test import SimpleTestCase, TestCase, override_settings

from audits import runner
from cameras.models import Installation, InstallationDevice
from services import ims_client, ims_sync


def installation(ims_id, pole, *, platform="ASE", monitoring="active", hosts=()):
    inst = Installation.objects.create(
        ims_id=ims_id, identifier=f"I-ASE-{pole}", primary_platform=platform, pole_number=pole,
        prtg_monitoring=monitoring,
    )
    for i, (kind, host) in enumerate(hosts):
        InstallationDevice.objects.create(installation=inst, ims_device_id=ims_id * 10 + i, device_type=kind, host=host)
    return inst


class ScopeRuleTests(TestCase):
    def reason(self, inst):
        with mock.patch.object(runner, "_dst_site_kind", return_value="lti"):
            return runner._dst_out_of_scope_reason(inst)

    def test_210071_on_10x_is_kept(self):
        inst = installation(1, "210071", hosts=[("cbw", "10.21.71.177"), ("cpu", "10.21.71.140")])
        self.assertFalse(inst.is_legacy)
        self.assertEqual(self.reason(inst), "")

    def test_233047_with_no_prtg_device_is_out(self):
        self.assertEqual(self.reason(installation(2, "233047", monitoring="unmonitored")), "no PRTG device")

    def test_paused_is_out(self):
        self.assertEqual(self.reason(installation(3, "233048", monitoring="paused")), "PRTG paused")

    def test_legacy_public_hosts_are_out(self):
        inst = installation(4, "100123", hosts=[("cbw", "166.169.75.241"), ("cpu", "166.169.75.242"), ("modem", "10.1.1.1")])
        self.assertTrue(inst.is_legacy)
        self.assertEqual(self.reason(inst), "legacy (no 10.x CBW/CPU host)")

    def test_no_cbw_or_cpu_devices_is_not_called_legacy(self):
        self.assertFalse(installation(5, "233049", hosts=[("modem", "166.1.1.1")]).is_legacy)

    def test_legacy_rule_is_lti_only(self):
        inst = installation(6, "FX1051", platform="DE", hosts=[("cpu", "73.1.2.3")])
        with mock.patch.object(runner, "_dst_site_kind", return_value="de"):
            self.assertEqual(runner._dst_out_of_scope_reason(inst), "")

    def test_unknown_monitoring_from_an_older_ims_is_kept(self):
        self.assertEqual(self.reason(installation(7, "233050", monitoring="")), "")


class RefreshScopeTests(SimpleTestCase):
    @override_settings(DST_PRE_AUDIT_IMS_REFRESH=True)
    def test_unreachable_ims_warns_and_never_raises(self):
        with (
            mock.patch("services.ims_client.refresh_prtg", side_effect=ims_client.IMSClientError("IMS unreachable")),
            mock.patch("services.ims_sync.sync_installations_from_ims", side_effect=RuntimeError("down")),
            mock.patch.object(runner, "_set_progress"),
        ):
            warnings = runner._dst_refresh_scope(1)
        self.assertEqual(len(warnings), 2)
        self.assertIn("used the last synced PRTG state", warnings[0])
        self.assertIn("used the last synced installations", warnings[1])

    @override_settings(DST_PRE_AUDIT_IMS_REFRESH=False)
    def test_can_be_turned_off(self):
        with mock.patch("services.ims_client.refresh_prtg") as refresh:
            self.assertEqual(runner._dst_refresh_scope(1), [])
        refresh.assert_not_called()


@override_settings(IMS_BASE_URL="https://ims.test", IMS_API_TOKEN="tok", IMS_CA_FILE="", IMS_TLS_VERIFY=True)
class RefreshPrtgClientTests(SimpleTestCase):
    def client_for(self, handler):
        transport = httpx.MockTransport(handler)
        real = httpx.Client
        return mock.patch.object(ims_client.httpx, "Client", side_effect=lambda **kw: real(transport=transport, **kw))

    def test_starts_then_polls_until_done(self):
        polls = iter([{"state": "running"}, {"state": "done", "message": "PRTG tree, enclosures and sensors refreshed"}])

        def handler(req):
            assert req.headers["authorization"] == "Bearer tok"
            if req.method == "POST":
                return httpx.Response(202, json={"progress_id": "ext-prtg-1", "started": True})
            assert req.url.params["id"] == "ext-prtg-1"
            return httpx.Response(200, json=next(polls))

        sleeps = []
        with self.client_for(handler):
            msg = ims_client.refresh_prtg(sleep=sleeps.append, poll_seconds=5)
        self.assertEqual(msg, "PRTG tree, enclosures and sensors refreshed")
        self.assertEqual(sleeps, [5])

    def test_a_read_only_token_is_explained(self):
        with self.client_for(lambda req: httpx.Response(401, json={})):
            with self.assertRaisesMessage(ims_client.IMSClientError, "prtg:refresh scope"):
                ims_client.refresh_prtg()

    def test_failure_inside_ims_is_raised(self):
        def handler(req):
            if req.method == "POST":
                return httpx.Response(202, json={"progress_id": "ext-prtg-1"})
            return httpx.Response(200, json={"state": "error", "error": "prtg tree sync: timeout"})

        with self.client_for(handler):
            with self.assertRaisesMessage(ims_client.IMSClientError, "prtg tree sync: timeout"):
                ims_client.refresh_prtg(sleep=lambda s: None)


class SyncStoresMonitoringTests(TestCase):
    def test_sensor_status_and_installation_monitoring_are_stored(self):
        row = {
            "id": 9001, "identifier": "I-ASE-233047", "pole_number": "233047", "primary_platform": "ASE",
            "prtg_monitoring": "paused",
            "devices": [{"id": 1, "device_type": "cbw", "host": "10.3.47.177", "prtg_objid": "5473",
                         "sensors": [{"prtg_objid": "5474", "name": "Ping", "status": "Paused by User"}]}],
        }
        with mock.patch.object(ims_sync, "fetch_ase_installations", return_value=[row]):
            ims_sync.sync_installations_from_ims()
        inst = Installation.objects.get(ims_id=9001)
        self.assertEqual(inst.prtg_monitoring, "paused")
        self.assertEqual(inst.devices.get().sensors.get().status, "Paused by User")
