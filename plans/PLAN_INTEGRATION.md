# Plan integration index — DST Camera Audit

Derived index: order and why only. Each plan file owns its own scope and status; if they disagree, the plan file wins.

Priority axis: workflow & skills → QOL & fixes → features by impact. A hard `Blocked by` overrides it.

## In flight
- `plans/in-progress-plans/FAST_FLEET_DST_AUDIT.md`: Phases 1–2 accepted (offline skip, parallel LTI); Phase 3 (faster DE lanes) next. It speeds up UNATTENDED_NIGHTLY_DST_AUDIT's halves without changing their order.
- `plans/in-progress-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md`: Phase 0 accepted; Phase 1 (Phase 2 folded) in implementation. The laptop runs remote over the VPN.
- `plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md`: 13/16. Phase 6 is built and deployed, waiting on the user's acceptance. Then the final real-audit-day acceptance.

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
