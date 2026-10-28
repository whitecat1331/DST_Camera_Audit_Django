# Plan integration index — DST Camera Audit

Derived index: order and why only. Each plan file owns its own scope and status; if they disagree, the plan file wins.

Priority axis: workflow & skills → QOL & fixes → features by impact. A hard `Blocked by` overrides it.

## In flight
- `plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md`: 13/16. Phase 6 is built and deployed, waiting on the user's acceptance. Then the final real-audit-day acceptance.

## Features by impact
1. `plans/initial-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md`: Stage `plan`, not started.
   - **Soft order:** after DST_AUDIT_SHARED_REVIEW Phase 6 acceptance. It publishes through that plan's publish + assignment path, and its final acceptance is the unattended version of that plan's real-audit-day acceptance.
   - **Gate:** Phase 0 (the unattended-VPN spike) decides whether the plan is feasible at all.

## Outside `plans/` (misplaced folder, not moved)
These sit in a top-level `in-progress-plans/` instead of `plans/in-progress-plans/`. They are left in place because they belong to other threads' work:
- `in-progress-plans/CBW_FIRMWARE_INVENTORY_REPORT.md`: its schema part was committed for I4 (`dbe111e`); the rest is uncommitted.
- `in-progress-plans/AUGUST_REJECTIONS_CSV_REPORT.md`.

## Change log
- 2026-10-28: index created. Added UNATTENDED_NIGHTLY_DST_AUDIT (new); recorded DST_AUDIT_SHARED_REVIEW status; noted the misplaced top-level plan folder.
