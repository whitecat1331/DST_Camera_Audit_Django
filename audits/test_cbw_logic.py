"""Phase 3: power scheduled (solar) CBWs on through their own logic, never by forcing relays.

Fixtures are real configs (2026-10-30): 220116 (standard layout, M2 "Vin > 13.3"),
220084 (hidden registers, M2 "Vin < 12.8"), 220122 (no M2 gate, lane A only),
220080 (older names: trufixOn → trufix1CpuReg10), 233009 (the schedule switches the relay itself).
"""

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from audits import runner
from services import cbw_auth, cbw_logic
from services.cbw_logic import CbwLogic, CbwPowerResult, parse_logic, power_on_cbw

FIX = Path(__file__).parent / "fixtures" / "cbw"
HOST = "10.1.116.177"


def config(pole: str) -> tuple[str, str, str]:
    return tuple((FIX / f"{pole}_{k}.data").read_text() for k in ("io", "schedTasks", "condTasks"))


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.ok = status < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class FakeCbw:
    """A logged-in CBW: serves the config files and a customState/state, records every GET."""

    def __init__(self, pole: str, state: dict, relays: dict | None = None):
        self.files = dict(zip(("io.data", "schedTasks.data", "condTasks.data"), config(pole)))
        self.state = dict(state)
        self.relays = relays or {"relay1": "1", "relay2": "1"}
        self.gets: list[str] = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        path = url.split(f"http://{HOST}/", 1)[1]
        if path in self.files:
            return Resp(text=self.files[path])
        if path.startswith("customState.json"):
            return Resp(payload=self.state)
        if path.startswith("state.json"):
            return Resp(payload=self.relays)
        if path == "setup.html":
            return Resp()
        return Resp(404)

    @property
    def writes(self) -> list[str]:
        return [u.split("/", 3)[3] for u in self.gets if "?" in u]


def power_on(cbw: FakeCbw, **kw) -> CbwPowerResult:
    session = mock.MagicMock()
    session.get.side_effect = cbw.get
    login = cbw_auth.CbwLogin(host=HOST, index=1, session=session, response=Resp())
    with (
        mock.patch.object(cbw_logic, "pole_to_ip_address", return_value=HOST),
        mock.patch.object(cbw_logic, "cbw_login", return_value=login) as login_mock,
    ):
        result = power_on_cbw("220116", "admin", ["p"], **kw)
    assert login_mock.call_args.kwargs["path"] == "/setup.html"  # the config needs the manager password
    return result


class ParseLogicTests(SimpleTestCase):
    def test_standard_layout(self):
        logic = parse_logic(*config("220116"))
        self.assertEqual(logic.on_target, "allTrufixPower")
        self.assertEqual(logic.vin, "systemVoltage")
        self.assertEqual([(lane.power, lane.relay) for lane in logic.lanes],
                         [("trufixAPower", "trufixA1Relay"), ("trufixBPower", "trufixB2Relay")])
        self.assertEqual(logic.m2_gate, "Vin > 13.3")  # compare code 5
        self.assertFalse(logic.m2_gate_inverted)

    def test_hidden_register_layout_has_names_and_an_inverted_gate(self):
        logic = parse_logic(*config("220084"))
        self.assertEqual(logic.on_target, "allTrufixPower")  # localIONum 0: not in state.json
        self.assertEqual(logic.m2_gate, "Vin < 12.8")  # compare code 2
        self.assertTrue(logic.m2_gate_inverted)

    def test_lane_without_start_up_logic(self):
        logic = parse_logic(*config("220122"))
        self.assertIsNone(logic.m2_gate)
        self.assertEqual([(lane.label, lane.has_startup) for lane in logic.lanes], [("A", True), ("B", False)])

    def test_older_layout_lane_registers_map_to_lanes(self):
        logic = parse_logic(*config("220080"))
        self.assertEqual(logic.on_target, "trufixOn")
        self.assertEqual([(lane.label, lane.power, lane.relay) for lane in logic.lanes],
                         [("A", "trufix1CpuReg10", "trufix1(on)Startup"), ("B", "trufix2CpuReg20", "")])

    def test_schedule_that_switches_the_relay_has_no_lanes(self):
        logic = parse_logic(*config("233009"))
        self.assertTrue(logic.scheduled)
        self.assertEqual(logic.lanes, ())

    def test_no_on_schedule_is_unscheduled(self):
        io_data, _sched, cond = config("220116")
        self.assertFalse(parse_logic(io_data, "", cond).scheduled)


OFF = {"allTrufixPower": "0", "trufixAPower": "0", "trufixBPower": "0", "systemVoltage": "13.9 V"}


class PowerOnViaLogicTests(SimpleTestCase):
    def test_off_site_gets_the_on_register_then_the_lane_registers_never_relays(self):
        cbw = FakeCbw("220116", OFF)
        result = power_on(cbw)
        self.assertEqual((result.action, result.method, result.lanes), ("turned_on", "logic", ["A", "B"]))
        self.assertEqual(cbw.writes, ["customState.json?allTrufixPower=1",
                                      "customState.json?trufixAPower=1&trufixBPower=1"])
        self.assertFalse(any("relay" in w.lower() for w in cbw.writes))

    def test_e7_stuck_site_relay_left_on_is_started_by_its_lane_register(self):
        # Run 1220: All TruFix Power 1, TF A Power 0, A1 relay forced on by DST.
        cbw = FakeCbw("220116", {**OFF, "allTrufixPower": "1", "trufixBPower": "1", "trufixA1Relay": "1"})
        result = power_on(cbw)
        self.assertEqual(result.lanes, ["A"])
        self.assertEqual(cbw.writes, ["customState.json?trufixAPower=1"])

    def test_hidden_registers_are_written_through_custom_state(self):
        cbw = FakeCbw("220084", OFF)
        power_on(cbw)
        self.assertEqual(cbw.writes[0], "customState.json?allTrufixPower=1")

    def test_lane_with_no_start_up_logic_is_not_written(self):
        cbw = FakeCbw("220122", OFF)
        result = power_on(cbw)
        self.assertEqual(cbw.writes[-1], "customState.json?trufixAPower=1")
        self.assertEqual(result.no_startup, ["B"])

    def test_battery_below_the_floor_writes_nothing(self):
        cbw = FakeCbw("220116", {**OFF, "systemVoltage": "12.6 V"})
        result = power_on(cbw)
        self.assertEqual((result.action, result.vin), ("battery_low", 12.6))
        self.assertIn("Vin 12.6 V < 12.8 V", result.detail)
        self.assertEqual(cbw.writes, [])

    def test_already_on_writes_nothing(self):
        cbw = FakeCbw("220116", {**OFF, "allTrufixPower": "1", "trufixAPower": "1", "trufixBPower": "1"})
        self.assertEqual(power_on(cbw).action, "already_on")
        self.assertEqual(cbw.writes, [])

    def test_relay_schedule_layout_keeps_the_relay_path(self):
        cbw = FakeCbw("233009", {"systemVoltage": "14.3"}, relays={"relay1": "0", "relay2": "0"})
        result = power_on(cbw)
        self.assertEqual((result.action, result.method), ("turned_on", "relays"))
        self.assertEqual(cbw.writes, ["state.json?relay1=1&relay2=1"])

    def test_older_layout_reads_vin_by_its_usual_name(self):
        cbw = FakeCbw("220080", {"trufixOn": "0", "trufix1CpuReg10": "0", "trufix2CpuReg20": "0",
                                 "systemVoltage": "12.1"})
        result = power_on(cbw)
        self.assertEqual((result.action, result.vin), ("battery_low", 12.1))
        self.assertEqual(cbw.writes, [])

    def test_unscheduled_cbw_keeps_the_relay_path(self):
        cbw = FakeCbw("220116", OFF, relays={"relay1": "0", "relay2": "1"})
        cbw.files["schedTasks.data"] = ""
        result = power_on(cbw)
        self.assertEqual((result.action, result.method), ("turned_on", "relays"))
        self.assertEqual(cbw.writes, ["state.json?relay1=1&relay2=1"])


def logic_result(action="turned_on", lanes=("A",), gate="Vin < 12.8", vin=13.9, no_startup=()):
    logic = CbwLogic(on_target="allTrufixPower", vin="systemVoltage", m2_gate=gate)
    return CbwPowerResult(action=action, method="logic", lanes=list(lanes), logic=logic, vin=vin,
                          no_startup=list(no_startup))


@override_settings(DST_POWER_ON_CONCURRENT=1)
class RunnerTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dst-logic-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_pass(self, results: dict):
        sites = [SimpleNamespace(pole_number=p, identifier=p, ims_id=1) for p in results]
        run = runner._DstRun(1, SimpleNamespace(), len(sites), self.dir)

        def power(inst):
            r = results[inst.pole_number]
            return inst.pole_number, None, r.action == "turned_on", r

        with (
            mock.patch.object(runner, "_power_on_lti_site", side_effect=power),
            mock.patch.object(runner, "_set_progress"),
            mock.patch("audits.models.AuditJob.objects"),
            mock.patch.object(runner, "_raise_if_cancelled"),
        ):
            turned, failed = runner._dst_power_on_pass(run, sites, "power-on")
        return run, sites, turned, failed

    def test_logic_sites_wait_on_their_lanes_not_the_fixed_settle(self):
        run, _sites, turned, failed = self.run_pass({"220001": logic_result(lanes=("A", "B"))})
        self.assertFalse(turned)  # no relays were turned on, so no fixed 90 s settle
        self.assertEqual(failed, [])
        self.assertEqual(run.boot_hosts, ["10.1.1.140", "10.1.1.141"])

    def test_battery_low_is_reported_not_offline(self):
        run, sites, _t, failed = self.run_pass({"220007": logic_result(action="battery_low", vin=12.4)})
        self.assertEqual(failed, [])
        self.assertEqual(run.boot_hosts, [])
        report = runner._dst_cbw_report(run, sites)
        self.assertIn("CBW report: battery low, not powered on: 1: 220007 (12.4 V)", report)

    def test_report_groups_inverted_and_missing_gates_and_lanes_without_logic(self):
        run, sites, _t, _f = self.run_pass({
            "220001": logic_result(),
            "220084": logic_result(),
            "220122": logic_result(gate=None, no_startup=("B",)),
            "220116": logic_result(gate="Vin > 13.3"),
        })
        report = runner._dst_cbw_report(run, sites)
        self.assertIn("CBW report: M2 gate 'Vin < 12.8' is inverted (never passes on a healthy battery): 2: "
                      "220001, 220084", report)
        self.assertIn("CBW report: M2 has no battery gate: 1: 220122", report)
        self.assertIn("CBW report: lane power with no start-up logic (not started): 1: 220122 lane B", report)
        self.assertFalse(any("220116" in line for line in report))

    def test_report_flags_ims_power_type_against_the_cerbo(self):
        sites = [
            SimpleNamespace(pole_number="233001", identifier="a", ims_id=1, is_solar=True, power_type="AC"),
            SimpleNamespace(pole_number="233002", identifier="b", ims_id=2, is_solar=False, power_type="Solar"),
            SimpleNamespace(pole_number="233003", identifier="c", ims_id=3, is_solar=True, power_type="Solar"),
        ]
        run = runner._DstRun(1, SimpleNamespace(), 3, self.dir)
        report = runner._dst_cbw_report(run, sites)
        self.assertEqual(report, ["CBW report: IMS power_type disagrees with the Cerbo (fix in IMS): 2: "
                                  "233001 (AC, has Cerbo), 233002 (Solar, no Cerbo)"])

    @override_settings(DST_SOLAR_BOOT_TIMEOUT_SECONDS=30)
    def test_boot_wait_ends_when_every_lane_answers(self):
        answers = iter([False, True, True])  # 2nd host up at once, 1st on the second poll
        with (
            mock.patch.object(runner, "_vnc_open", side_effect=lambda h: h == "b" or next(answers)),
            mock.patch.object(runner, "_set_progress"),
            mock.patch("audits.models.AuditJob.objects"),
            mock.patch.object(runner, "_raise_if_cancelled"),
            mock.patch.object(runner.time, "sleep") as sleep,
        ):
            down = runner._dst_wait_for_boot(1, ["a", "b"], "LTI")
        self.assertEqual(down, [])
        self.assertEqual(sleep.call_count, 1)


class GridFallbackTests(SimpleTestCase):
    def site(self, solar):
        return SimpleNamespace(pole_number="220061", identifier="x", ims_id=1, is_solar=solar)

    def test_grid_site_without_manager_password_still_gets_relays(self):
        relay = SimpleNamespace(action="turned_on", relays=["relay1"])
        with (
            mock.patch("services.cbw_logic.power_on_cbw", side_effect=cbw_auth.CbwAuthError("h", "auth_failed", "x")),
            mock.patch("services.cbw_relays.turn_all_relays_on", return_value=relay) as relays,
        ):
            summary, err, changed, power = runner._power_on_lti_site(self.site(False))
        self.assertEqual((err, changed, power), (None, True, None))
        self.assertIn("no manager password", summary)
        relays.assert_called_once()

    def test_solar_site_without_manager_password_is_never_relay_forced(self):
        with (
            mock.patch("services.cbw_logic.power_on_cbw", side_effect=cbw_auth.CbwAuthError("h", "auth_failed", "x")),
            mock.patch("services.cbw_relays.turn_all_relays_on") as relays,
        ):
            with self.assertRaises(cbw_auth.CbwAuthError):
                runner._power_on_lti_site(self.site(True))
        relays.assert_not_called()
