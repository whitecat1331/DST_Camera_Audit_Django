"""Power a site on through its CBW's own logic, never by forcing relays on a scheduled CBW.

On scheduled (solar) CBWs the logic owns TruFix power (FLEET_AUDIT_SCOPE_AND_RELIABILITY
E3/E7). A schedule sets an "on" register ("All TruFix Power") at 06:15; a conditional
task ("M2") then sets each lane's power register, which runs that lane's start-up
(relay on, off 8 s, on, step timers). Writing `relayN=1` instead skips all of that, and
a relay left on after the 18:15 shutdown stops the next morning's start-up (E7: 49 sites
dark all day after run 389).

So DST reads the CBW's config (`io.data`, `schedTasks.data`, `condTasks.data`), derives
the names from it — register numbers and names vary per CBW — and writes what the logic
would (method B, D4): the "on" register, then each lane's power register, through
`customState.json`, which also reaches registers `state.json` hides (D6). A CBW with no
"on" schedule keeps the relay path.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field

import requests

from services.cbw_auth import CbwAuthError, cbw_login
from services.ip_map import DeviceType, pole_to_ip_address

logger = logging.getLogger(__name__)

# condTasks / schedTasks codes (confirmed 2026-10-30 against the low-voltage tasks).
OP_EQ, OP_LT, OP_GT = "1", "2", "5"
OPS = {OP_EQ: "=", OP_LT: "<", OP_GT: ">"}
FUNC_ON, FUNC_SET = "1", "3"  # turn relay on; set register to the operand
IO_RELAY, IO_REGISTER, IO_TIMER = "1", "10", "12"

DEFAULT_VIN_FLOOR = 12.8  # user, 2026-10-30
# When no schedule/M2 compare names the battery input (220080 layout): its usual names.
VIN_KEYS = ("systemVoltage", "vin", "batteryVoltage")


@dataclass(frozen=True)
class Lane:
    power: str  # lane power register (nameCamelCase), e.g. trufixAPower
    relay: str  # the relay its start-up drives, "" when it has no start-up task
    label: str  # "A" / "B" / the register name

    @property
    def has_startup(self) -> bool:
        return bool(self.relay)


@dataclass(frozen=True)
class CbwLogic:
    on_target: str  # register the schedule turns on (allTrufixPower); "" = no schedule
    vin: str  # analog input the logic reads as battery voltage; "" if none found
    lanes: tuple[Lane, ...] = ()
    m2_gate: str | None = None  # "Vin < 12.8", "Vin > 13.3", or None (no gate)

    @property
    def scheduled(self) -> bool:
        return bool(self.on_target)

    @property
    def m2_gate_inverted(self) -> bool:
        return bool(self.m2_gate) and " < " in self.m2_gate


@dataclass
class CbwPowerResult:
    action: str  # turned_on | already_on | battery_low
    method: str  # logic | relays
    lanes: list[str] = field(default_factory=list)  # lanes started (logic) / relays (relays)
    detail: str = ""
    logic: CbwLogic | None = None
    vin: float | None = None
    no_startup: list[str] = field(default_factory=list)  # lanes with power but no start-up task

    @property
    def summary(self) -> str:
        text = f"{self.method} {self.action}"
        if self.lanes:
            text += f" ({', '.join(self.lanes)})"
        return text + (f": {self.detail}" if self.detail else "")


def _rows(text: str) -> list[dict]:
    return [r for r in csv.DictReader(io.StringIO(text or "")) if r.get("enabled") == "1"]


def _actions(row: dict) -> list[tuple[str, str, str]]:
    """[(func, io_index, operand)] for the row's non-empty actions."""
    return [
        (row[f"actionFunc{i}"], row[f"actionIONum{i}"], row[f"actionOperand{i}"])
        for i in (1, 2, 3)
        if row.get(f"actionFunc{i}", "0") != "0"
    ]


def _conditions(row: dict) -> list[tuple[str, str, str]]:
    """[(io_index, op, value)] for the row's conditions (one or two)."""
    out = [(row["operandIONum1"], row["compareOperator1"], row["operandCompVal1"])]
    if row.get("operandIONum2", "0") != "0":
        out.append((row["operandIONum2"], row["compareOperator2"], row["operandCompVal2"]))
    return out


def _is_measure(idx: str, op: str, io_type: dict) -> bool:
    """A "<"/">" compare on an input that isn't a register, relay or timer: the battery voltage."""
    return op in (OP_LT, OP_GT) and idx != "0" and io_type.get(idx) not in (IO_RELAY, IO_REGISTER, IO_TIMER, None)


def _is_one(value: str) -> bool:
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


def _fmt(value: str) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return value


def parse_logic(io_data: str, sched_data: str, cond_data: str) -> CbwLogic:
    """Derive the on-target, Vin input, lanes and M2 gate from one CBW's config files."""
    io_rows = {r["index"]: r for r in csv.DictReader(io.StringIO(io_data or "")) if r.get("index")}
    name = {k: r.get("nameCamelCase", "") for k, r in io_rows.items()}
    io_type = {k: r.get("ioTypeID", "") for k, r in io_rows.items()}

    on_target = ""
    vin = ""
    for row in _rows(sched_data):
        for func, idx, operand in _actions(row):
            if func == FUNC_SET and _is_one(operand) and io_type.get(idx) == IO_REGISTER:
                on_target = on_target or idx
                if not vin:
                    vin = next((i for i, op, _ in _conditions(row) if _is_measure(i, op, io_type)), "")
    if not on_target:
        return CbwLogic(on_target="", vin=name.get(vin, ""))

    cond_rows = _rows(cond_data)
    # M2: on-target = 1 (plus maybe a Vin gate) → set the lane power registers to 1.
    power_regs: list[str] = []
    m2_gate: str | None = None
    for row in cond_rows:
        conds = _conditions(row)
        if not any(i == on_target and op == OP_EQ and _is_one(v) for i, op, v in conds):
            continue
        sets = [
            idx for func, idx, operand in _actions(row)
            if func == FUNC_SET and _is_one(operand) and idx != on_target and io_type.get(idx) == IO_REGISTER
        ]
        if not sets:
            continue
        power_regs.extend(i for i in sets if i not in power_regs)
        for i, op, v in conds:
            if _is_measure(i, op, io_type):
                vin = vin or i
                m2_gate = f"Vin {OPS.get(op, 'op' + op)} {_fmt(v)}"

    lanes: list[Lane] = []
    for reg in power_regs:
        relay = ""
        for row in cond_rows:
            if not any(i == reg and op == OP_EQ and _is_one(v) for i, op, v in _conditions(row)):
                continue
            ons = [idx for func, idx, _ in _actions(row) if func == FUNC_ON and io_type.get(idx) == IO_RELAY]
            if ons:
                relay = name.get(ons[0], "")
                break
        reg_name = name.get(reg, reg)
        lanes.append(Lane(power=reg_name, relay=relay, label=_lane_label(reg_name)))
    return CbwLogic(on_target=name.get(on_target, ""), vin=name.get(vin, ""), lanes=tuple(lanes), m2_gate=m2_gate)


def _lane_label(reg_name: str) -> str:
    """trufixAPower → A, trufix1CpuReg10 (older layout) → A, trufixB… / trufix2… → B."""
    m = re.match(r"trufix([AB12])", reg_name, re.IGNORECASE)
    return {"A": "A", "1": "A", "B": "B", "2": "B"}[m.group(1).upper()] if m else reg_name


def _value(state: dict, key: str) -> str:
    return str(state.get(key) or "").strip().split(" ")[0]


def _float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def read_logic(session: requests.Session, ip: str, timeout: float = 10) -> CbwLogic:
    texts = []
    for page in ("io.data", "schedTasks.data", "condTasks.data"):
        r = session.get(f"http://{ip}/{page}", timeout=timeout)
        r.raise_for_status()
        texts.append(r.text)
    return parse_logic(*texts)


def power_on_cbw(
    pole: str,
    username: str,
    passwords: list[str],
    *,
    vin_floor: float = DEFAULT_VIN_FLOOR,
    timeout: float = 10,
) -> CbwPowerResult:
    """Power a pole's CBW on: through its logic when it has an "on" schedule, else relays.

    Raises CbwAuthError (no manager password got in: DST can't read the logic, so it
    never falls back to forcing relays) or CbwUnreachable. Never turns anything off.
    """
    ip = pole_to_ip_address(str(pole).strip(), DeviceType.CBW)
    # The config files need the manager password, which setup.html checks.
    login = cbw_login(ip, username, passwords, path="/setup.html", timeout=timeout)
    session = login.session
    try:
        logic = read_logic(session, ip, timeout)
        # No "on" schedule, or one that switches the TF relay itself (233009 layout:
        # relay on 06:17, off 18:16, no lane registers): relays are what it uses.
        if not logic.scheduled or not logic.lanes:
            from services.cbw_relays import relays_on

            r = session.get(f"http://{ip}/state.json", timeout=timeout)
            r.raise_for_status()
            relay = relays_on(session, ip, r.json(), timeout=timeout)
            return CbwPowerResult(action=relay.action, method="relays", lanes=relay.relays, logic=logic)
        return _power_on_via_logic(session, ip, logic, vin_floor, timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"CBW power-on failed for pole {pole} ({ip}): {type(exc).__name__}") from exc
    finally:
        session.close()


def _power_on_via_logic(
    session: requests.Session, ip: str, logic: CbwLogic, vin_floor: float, timeout: float
) -> CbwPowerResult:
    url = f"http://{ip}/customState.json"
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    state = r.json()
    if logic.on_target not in state:
        raise RuntimeError(f"CBW {ip}: {logic.on_target} not in customState.json")

    vin_key = logic.vin or next((k for k in VIN_KEYS if k in state), "")
    vin = _float(_value(state, vin_key)) if vin_key else None
    startable = [lane for lane in logic.lanes if lane.has_startup and lane.power in state]
    no_logic = [lane.label for lane in logic.lanes if not lane.has_startup]
    note = f"lane {', '.join(no_logic)} has no start-up logic" if no_logic else ""

    on_now = _is_one(_value(state, logic.on_target))
    to_start = [lane for lane in startable if not _is_one(_value(state, lane.power))]
    if on_now and not to_start:
        return CbwPowerResult(action="already_on", method="logic", detail=note, logic=logic, vin=vin, no_startup=no_logic)
    if vin is not None and vin < vin_floor:
        return CbwPowerResult(
            action="battery_low", method="logic", detail=f"battery low (Vin {vin:g} V < {vin_floor:g} V)",
            logic=logic, vin=vin, no_startup=no_logic,
        )
    if not startable:
        raise RuntimeError(f"CBW {ip}: no lane start-up found in its logic")

    if not on_now:
        session.get(f"{url}?{logic.on_target}=1", timeout=timeout).raise_for_status()
    if to_start:
        query = "&".join(f"{lane.power}=1" for lane in to_start)
        session.get(f"{url}?{query}", timeout=timeout).raise_for_status()
    logger.info("[CBW] power on via logic %s on=%s lanes=%s vin=%s", ip, logic.on_target,
                [lane.power for lane in to_start], vin)
    return CbwPowerResult(
        action="turned_on", method="logic", lanes=[lane.label for lane in to_start], detail=note,
        logic=logic, vin=vin, no_startup=no_logic,
    )


__all__ = ["CbwAuthError", "CbwLogic", "CbwPowerResult", "Lane", "parse_logic", "power_on_cbw"]
