# Pre-Solar Forced Export for Curtailment Avoidance

## Context

The current curtailment avoidance code creates export windows covering the **solar peak period** (when curtailment occurs). This is ineffective — during peak solar the export limit is already saturated by PV, so discharging battery to grid competes with solar export. The battery needs to be drained **before** solar arrives, when the full export limit is available for battery discharge.

Example (Mum's system: 18 kWh battery, ~0.5 kW base load, 32 kWh solar, 4 kW export limit):
- **Current approach (passive)**: Penalise curtailment cost + lower SOC keep floor → optimizer picks lower overnight charge → battery drains via load only (~0.5 kW). Not fast enough — battery fills early, energy curtailed.
- **Pre-solar export (active)**: Force discharge to grid at export limit before solar arrives → battery starts low → solar refills without hitting 100% → zero curtailment.

### Modelling results (from this session)

Using simplified 9 kWh battery, 0.25 kW load, 32 kWh solar, 2 kW export limit:
- **No export limit**: 26 kWh exported freely, no curtailment
- **With 2 kW limit, no pre-drain**: Battery fills at 10am, 6.2 kWh curtailed across 14 slots
- **With 2 kW limit + forced export from 05:00**: Battery drains to 0% by 08:00, peaks at 69.4%, **zero curtailment**

Saved plan comparisons in `memory/plan-baseline.json` and `memory/plan-curtailment.json`.

### What already exists on branch `curtailment-avoidance`

The branch (committed, merged into `mum`, deployed) has:
- `metric_curtailment_cost` (21.5 p/kWh) — penalty in prediction.py simulation
- `curtailment_margin` (30%) — inflates PV forecast for conservative planning
- `best_soc_keep_solar_offset` (2.6 kWh) — reduces SOC keep floor on sunny days (3.6 → 1.0 kWh)
- `export_limit` (4000 W) — grid export limit config
- Curtailment column in plan table web UI
- `generate_curtailment_export_windows()` — creates windows during solar peak (to be replaced)
- `max_soc_ceiling` — backward-looking cumulative curtailment algorithm (keep this)
- Curtailment tracking in prediction.py (keep this — needed for cost penalty)

### Key findings from this session

1. **`best_soc_keep` is a soft penalty** (prediction.py:1093), not a hard floor. With weight=0.5, optimizer went to 3% despite 6% effective keep. This is fine if ML load prediction is accurate.
2. **Passive drain is too slow** — at 0.5 kW load, draining 3 kWh takes 6 hours. Forced export at 4 kW drains it in 45 minutes.
3. **Curtailment-period windows are useless** — during peak solar, the export limit is saturated by PV, so battery can't discharge to grid anyway. Windows must be placed BEFORE solar.
4. **Keep `best_soc_keep_solar_offset`** — it relaxes the floor constraint so the optimizer is willing to accept low SOC plans. The pre-solar window provides the mechanism to get there fast.

### Current HA entity values (Mum's system)

- `metric_curtailment_cost`: 21.5 p/kWh
- `curtailment_margin`: 30%
- `best_soc_keep_solar_offset`: 2.6 kWh
- `best_soc_keep`: 3.6 kWh
- `best_soc_keep_weight`: 0.5
- `best_soc_min`: 0
- `export_limit` in apps.yaml: 4000 (W)

## Progress

### Completed
1. **Removed ceiling logic from prediction.py** — 3 blocks that forced discharge when SOC > ceiling. This caused SOC inflation (test 12 caught it) and was the wrong approach (prediction shouldn't force actions, that's the optimizer's job).
2. **Removed tests 9 & 10 from test_curtailment.py** — tested the removed ceiling behavior (headroom charging, ceiling discharge during hold). Renumbered 11→9, 12→10.
3. **Removed ceiling override from execute.py** — the `ceiling_kw`/`ceiling_override` logic that bypassed charge hold when SOC exceeded ceiling. Hold branch now goes straight to "Hold charging".
4. **Removed ceiling tests from test_execute.py** — `ceiling_hold_override` and `ceiling_hold_no_override` tests, plus `max_soc_ceiling` parameter from `run_execute_test`.
5. **All tests pass, pre-commit clean.**

### Remaining
1. Rewrite `generate_curtailment_export_windows()` in plan.py for pre-solar window placement
2. Write window placement test (TDD: test first, then implement)
3. Run full test suite, commit
4. Merge into mum, deploy

### What's been removed vs kept
- **Removed**: ceiling-based forced discharge in prediction.py and execute.py (wrong approach)
- **Kept**: `max_soc_ceiling` computation in plan.py (still used for `generate_curtailment_export_windows`)
- **Kept**: curtailment tracking/cost in prediction.py (needed for optimizer penalty)
- **Kept**: `generate_curtailment_export_windows()` in plan.py (to be rewritten, not removed)

## Changes

### 1. Replace curtailment-period windows with pre-solar export windows

**File: `apps/predbat/plan.py`** — `generate_curtailment_export_windows()` (line 846)

Keep the backward-looking ceiling calculation (it correctly computes total expected curtailment). Replace the window generation logic:

**Current** (lines 877-889): Finds contiguous periods where ceiling < soc_max (the solar peak) → creates windows there
**New**:
1. `headroom_needed` = `cumulative_curtailed` at minute 0 (total expected curtailment in kWh)
2. Find `curtailment_start` = first minute where ceiling < soc_max (when solar excess begins)
3. `drain_duration_minutes` = `headroom_needed / export_limit_kw * 60`
4. Create one export window: `start = curtailment_start - drain_duration_minutes`, `end = curtailment_start`
5. Clamp start to `minutes_now` (can't start in the past)

The optimizer then decides the actual export limit (0% = full discharge, higher = partial, 100% = skip it).

```python
# Calculate headroom needed and when curtailment starts
headroom_needed = cumulative_curtailed  # kWh at minute 0, already computed
curtailment_start = None
for m in range(0, self.forecast_minutes, step):
    ceiling = max_soc_ceiling.get(m, self.soc_max)
    if ceiling < self.soc_max - 0.1:
        curtailment_start = m
        break

if curtailment_start is not None and headroom_needed > 0.1:
    export_limit_kw = self.export_limit / (60 / step)  # kW per step → kW
    drain_minutes = (headroom_needed / export_limit_kw) * 60  # convert hours to minutes
    window_start_rel = max(curtailment_start - drain_minutes, 0)
    window_end_rel = curtailment_start
    # ... convert to absolute, check overlaps, create window
```

### 2. No other file changes needed

- **prediction.py** — already models forced export during export windows correctly (lines 619-841)
- **execute.py** — already handles `adjust_force_export()` for active windows (lines 356-422)
- **inverter.py** — already supports forced discharge at configurable rates via `adjust_discharge_rate()`
- **config.py** — no new config; uses existing `export_limit`, `metric_curtailment_cost`
- **`best_soc_keep_solar_offset`** — keep as-is, complements the export window

The entire export window infrastructure (optimizer, simulation, execution) works unchanged — we're just changing **when** the window is placed.

### How the optimizer handles the window

`optimise_export()` (plan.py:1689) tests these export limits per window:
- `100.0` = disabled (don't export)
- `99.0` = freeze (no charging, passive discharge only)
- `0.0` = full discharge to reserve
- `0.3, 0.5, 0.7` = partial discharge at fractional rates (if `set_export_low_power`)

It runs dual simulations (medium + PV10 forecast) for each option and picks lowest cost. The `metric_curtailment_cost` penalty makes it prefer draining when curtailment is expected.

During execution, prediction.py clips battery discharge so grid export never exceeds `export_limit` (line 823-841).

## Files to Modify

1. `apps/predbat/plan.py` — `generate_curtailment_export_windows()` only (lines 846-921)

## Verification

1. `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
2. `pre-commit run --all-files`
3. Deploy to mum (merge into mum branch first, deploy all files)
4. Check log for pre-solar export window creation (should show window before solar, not during)
5. **Save plan before/after** — use `mcp__PredbatMCP__get_plan` and save to memory/
6. Compare plan: should show Export/Discharge slots pre-dawn instead of passive Demand drain
7. On a sunny day: battery should drain pre-solar, refill with solar, curtailment column = 0

## Pending / Related

- **ML load prediction review** — user checking LoadML chart on Predbat dashboard to assess GSHP/temperature accuracy. If ML is good, comfortable with lower SOC targets.
- **Upstream PR** — curtailment-avoidance branch not yet PR'd to springfall2008/batpred
