# Plan integration index — DST Camera Audit

Derived index: order and why only. Each plan file owns its own scope and status; if they disagree, the plan file wins.

Priority axis: workflow & skills → QOL & fixes → features by impact. A hard `Blocked by` overrides it.

## In flight
- `plans/in-progress-plans/FAST_FLEET_DST_AUDIT.md`: Phases 1–3 accepted (offline skip, parallel LTI, ~20 s DE lanes); Phase 5 full-fleet timed run next. It speeds up UNATTENDED_NIGHTLY_DST_AUDIT's halves without changing their order.
- `plans/in-progress-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md`: Phase 0 accepted; Phase 1 (Phase 2 folded) in implementation. The laptop runs remote over the VPN.
- `plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md`: 13/16. Phase 6 is built and deployed, waiting on the user's acceptance. Then the final real-audit-day acceptance.

## QOL & fixes
- `plans/in-progress-plans/FLEET_AUDIT_SCOPE_AND_RELIABILITY.md`: **in flight** (promoted 2026-10-30). Fixes from run 389 (the de-facto FAST_FLEET Phase 5 run).
  - Execution order: Phase 0 → 1 → 4 → 3 → 4b → 2 → 5.
  - Phase 2 needs an IMS-repo change. The D1 IMS host fix is done (2026-10-30). All decisions D1–D6 are made.
  - Phase 4b touches DST_AUDIT_SHARED_REVIEW's review page; it lands after or with that plan's Phase 6 acceptance.
  - FAST_FLEET Phase 5's acceptance should be re-run after this plan's Phase 5.

## Features by impact
(none waiting)

## Outside `plans/` (misplaced folder, not moved)
These sit in a top-level `in-progress-plans/` instead of `plans/in-progress-plans/`. They are left in place because they belong to other threads' work:
- `in-progress-plans/CBW_FIRMWARE_INVENTORY_REPORT.md`: its schema part was committed for I4 (`dbe111e`); the rest is uncommitted.
- `in-progress-plans/AUGUST_REJECTIONS_CSV_REPORT.md`.

## Change log
- 2026-10-28: index created. Added UNATTENDED_NIGHTLY_DST_AUDIT (new); recorded DST_AUDIT_SHARED_REVIEW status; noted the misplaced top-level plan folder.
- 2026-10-28: UNATTENDED_NIGHTLY_DST_AUDIT revised (MFA → pre-connected VPN, release instead of connect; one-time schedule).
- 2026-10-28: UNATTENDED_NIGHTLY_DST_AUDIT promoted to in-progress (user: remote over the VPN, start implementation); Phase 2 folded into Phase 1. It runs alongside DST_AUDIT_SHARED_REVIEW's Phase 6 acceptance rather than after it, by the user's direction.
- 2026-10-28: added FAST_FLEET_DST_AUDIT (new, Stage: plan) after run 196 projected over a day for the full fleet.
- 2026-10-29: FAST_FLEET_DST_AUDIT promoted to in-progress (user: "start implementation"); Phase 1 accepted for LTI.
- 2026-10-29: FAST_FLEET_DST_AUDIT Phase 2 accepted.
- 2026-10-29: FAST_FLEET_DST_AUDIT Phase 3 accepted; Phase 4 not needed on the projection.
- 2026-10-29: FAST_FLEET_DST_AUDIT Phase 4b (live publish) implemented; its live acceptance waits on the next small LTI + DE test.
- 2026-10-29: PER_DEVICE_TIME_CHECK planned (per-capture timestamps, site timezone, per-device drift and pass/fail); in progress, Phase 1 next. Phase 3 needs the user's go-ahead (shared DB + review deploy).
- 2026-10-30: added FLEET_AUDIT_SCOPE_AND_RELIABILITY (draft) from the run-389 cross-reference.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: OvrC root cause found (no per-FX isolation; FX1031 not an OvrC customer aborted the pass). Phase 1 rewritten; solar CBW power-on investigation started.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: solar root cause found. CBW logic owns TF power through registers, and DST's relay writes skip it. D4 decided (Option B). The register write was proven live on 220116; the off→on test then passed (+42 s), no restore (user), Vin floor 12.8 V.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: E4 root cause found. The CBW returns 429 after two failed logins and the password loops skip ahead past the correct index-2 password. All 19 poles are recoverable; Phase 4 rewritten.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: user decision. Legacy and PRTG-paused sites are skipped and left out of the report entirely (Phase 2b).
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: added E6 and Phase 4b. The review table must fit the page with no sideways scroll; the Notes column was off-page on run 389. It touches DST_AUDIT_SHARED_REVIEW's review page, so it lands after or alongside that plan's Phase 6 acceptance.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY promoted from draft to plan and moved to in-progress (user: "start implementation"). Checklist added; Phase 4 is now gated and runs before Phase 3.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY decisions made. D1: 210017/210036 aren't legacy; their IMS hosts get fixed to 10.21.x. D2: unmonitored sites are skipped and hidden. D3: a real auth failure skips the CBW but VNC is tried. D5: Option A, report the off-norm M2 gates.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY Phase 1 accepted. The full 283-FX OvrC pass took 51 min with every FX read or given a reason. A wrong-site match (FX140 → FX1400) was found during acceptance and fixed. Phase 4 is next.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY Phase 4 accepted. A shared lockout-aware `cbw_login` is in place and D3 is wired (a CBW that rejects every password is skipped while VNC is still tried). In the live read-only check all 19 E4 poles logged in, and on the second pass each took one request using its remembered password. Phase 3 is next.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: new open decision D6. 22 of the 145 scheduled solar CBWs (220084 layout) don't expose their logic registers in `state.json`, so method B can't be written to them that way.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY D6 decided: C then B (spike a write path on one of the 22; until then report them as "no on-demand power-on").
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY D6 spike: `customState.json` writes powered on 220084 (hidden-register layout); VNC was up within about 8 min. 220116 never read as off, so it wasn't tested.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY D1 data fix done. IMS DEV hosts for 210017/210036 were set to PRTG's 10.21.x (IMS had a stale copy from an older PRTG sync).
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: E7 found (run 1220 solar misses). DST's evening `relayN=1` writes keep the TF relay on, so the CBW's 06:15 start-up (R1, which needs the relay off) never fires. Proven by a relay-off write on 3 sites (all up within about 47 s), then 37 more recovered (34 back on VNC within 4 min; 3 are TF-computer problems). D4/D5 compare operators corrected (code 2 is "<"). Hold: no evening or weekend LTI runs until Phase 3 replaces relay forcing.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY: Phases 3, 4b and 2 implemented (user: "implement what is left and we will run acceptance again"); none accepted yet. Phase 3: `power_on_cbw` goes through each CBW's own logic, never forces relays on scheduled CBWs, and adds a VNC boot wait and a CBW report. Phase 4b: the review table fits the page with no sideways scroll at 1280–1920 px. Phase 2: IMS sensor status, `prtg_monitoring` and `prtg-refresh`; DST does a pre-audit refresh and drops out-of-scope sites. Open: the Malibu (7000xx, all paused) exclusion needs the user's confirmation. The E7 hold stays until Phase 3 is accepted.
- 2026-10-30: FLEET_AUDIT_SCOPE_AND_RELIABILITY Phases 3, 4b and 2 accepted (user: "acceptance passed, redeploy"). The E7 hold is lifted. Only Phase 5 (full-fleet re-run) remains; the Malibu exclusion still needs the user's confirmation.
