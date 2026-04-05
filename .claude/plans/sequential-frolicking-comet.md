# Pre-Solar Forced Export for Curtailment Avoidance

## Context

The current curtailment avoidance code creates export windows covering the **solar peak period** (when curtailment occurs). This is ineffective — during peak solar the export limit is already saturated by PV, so discharging battery to grid competes with solar export. The battery needs to be drained **before** solar arrives, when the full export limit is available for battery discharge.

Example (Mum's system: 9 kWh battery, 0.25 kW load, 32 kWh solar, 2 kW export limit):
- **Current approach**: Battery fills by 10am, 6.2 kWh curtailed across 14 slots at 100%
- **Pre-solar export**: Drain 05:00–08:00 at 2 kW, battery peaks at 69%, zero curtailment

## Changes

### 1. Replace curtailment-period windows with pre-solar export windows

**File: `apps/predbat/plan.py`** — `generate_curtailment_export_windows()` (line 846)

Keep the backward-looking ceiling calculation (it correctly computes total expected curtailment). Replace the window generation logic:

**Current**: Finds contiguous periods where ceiling < soc_max (the solar peak) → creates windows there
**New**:
1. `headroom_needed` = `cumulative_curtailed` at minute 0 (total expected curtailment in kWh)
2. Find `curtailment_start` = first minute where ceiling < soc_max (when solar excess begins)
3. `drain_duration_minutes` = `headroom_needed / export_limit_kw * 60`
4. Create one export window: `start = curtailment_start - drain_duration_minutes`, `end = curtailment_start`
5. Clamp start to `minutes_now` (can't start in the past)

The optimizer then decides the actual export limit (0% = full discharge, higher = partial, 100% = skip).

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

- **prediction.py** — already models forced export during export windows correctly
- **execute.py** — already handles `adjust_force_export()` for active windows
- **inverter.py** — already supports forced discharge at configurable rates
- **config.py** — no new config; uses existing `export_limit`, `metric_curtailment_cost`

The entire export window infrastructure (optimizer, simulation, execution) works unchanged — we're just changing **when** the window is placed.

## Files to Modify

1. `apps/predbat/plan.py` — `generate_curtailment_export_windows()` only

## Verification

1. `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
2. `pre-commit run --all-files`
3. Deploy to mum
4. Check log for pre-solar export window creation (should show window before solar, not during)
5. Compare plan: should show Export/Discharge slots pre-dawn instead of passive Demand drain
6. On a sunny day: battery should drain pre-solar, refill with solar, curtailment column = 0
7. Save plan before/after for comparison this time
