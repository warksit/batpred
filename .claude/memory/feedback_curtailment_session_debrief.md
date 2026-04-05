---
name: Curtailment export window session debrief
description: Lessons from failed attempt to get curtailment export windows through the Predbat optimizer (2026-03-12)
type: feedback
---

## What happened

Attempted to fix curtailment export windows being discarded by the optimizer. Made 4 successive patches, each revealing another blocking layer, broke 3 working features, deployed broken code to production multiple times.

## Key mistakes

1. **Chasing symptoms without understanding the system.** Never traced the full export window lifecycle before coding:
   - `high_export_rates` → `export_window_best` → levelling pass (price thresholds) → overlap filtering (`calculate_export_oncharge`) → `allow_this_export_window()` → `discard_unused_export_slots()`
   - Each layer is designed to filter non-profitable exports. A 0p curtailment window gets blocked at every stage.

2. **Misread logs and reported incorrect conclusions.** Told the user "all selected 0.0" (no overnight charging) based on individual window tweak results, when the levelling pass had already set 7-9 kWh. This wasted time and eroded trust.

3. **Deployed untested changes to production repeatedly.** Should have worked on a branch and tested properly before deploying once.

4. **Broke working features.** Edited plan.py and fetch.py which contained working code for: HoldChrg target fix, low SOC for solar days, curtailment cost penalty. All broken by the edits.

5. **Fundamental logic error in the final approach.** Injecting the export window AFTER the optimizer meant the target (48%) was calculated assuming a full battery, but the optimizer had already reduced overnight charging to ~20%. Export target above current SOC = nonsensical plan.

## Predbat export optimizer architecture (must understand before touching)

The export window pipeline has ~5 filtering layers:
1. `high_export_rates` → `export_window_best` (only profitable export rate windows)
2. `optimise_charge_limit_price_threads()` levelling pass — filters by price threshold, `calculate_export_oncharge` overlap check
3. `allow_this_export_window()` — blocks windows overlapping charge windows
4. `optimise_export()` — requires metric AND cost improvement (cost guard)
5. `discard_unused_export_slots()` — removes windows with limit==100 (disabled)

**A 0p curtailment export window gets blocked at EVERY stage.** Patching individual stages doesn't work — the architecture assumes export windows are profitable.

## What actually works

- `metric_curtailment_cost` in prediction.py simulation DOES influence the charge optimizer — it reduces overnight charging on sunny days
- The problem is specifically forcing a pre-solar forced EXPORT, not the curtailment cost itself

## Right approach (not yet implemented)

Don't fight the export optimizer. Options:
1. **First-class curtailment support** — add curtailment as a concept the optimizer understands natively, not bolted-on export windows
2. **Charge-side solution** — instead of forcing exports, cap charging via lower `best_soc_max` on sunny days or teach the charge optimizer about "free solar kWh available"
3. **Dynamic target** — if an export window IS used, its target must account for the planned SOC at window start, not assume a full battery
4. **Throttled charging mode** — use Mode 4 (Charge PV First) with a low charge rate to slow battery fill during peak solar, rather than pre-emptive export

## Process lessons

- Understand the full pipeline before coding (read, trace, diagram)
- Work on a branch, not production
- Test properly before deploying
- If a fix reveals another blocker, STOP and reassess the approach — don't keep patching
- Never report log analysis conclusions without double-checking the full context
- Have a clear rollback plan before deploying

## Deployment state after rollback

- Mum's HA: `mum-no-curtailment` (lost curtailment-v2 features too)
- Local repo: `curtailment-v2` branch with broken changes in git stash
- To restore curtailment-v2 (without this session's changes): deploy from the committed branch, NOT from stash
