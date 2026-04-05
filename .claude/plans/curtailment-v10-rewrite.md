# Curtailment Manager v10 — Clean Rewrite Plan

## Goal
Prevent grid export exceeding 4kW DNO limit. Only act when there's not enough battery headroom to absorb remaining overflow before PV drops to safe levels.

## Activation: One Check
```
remaining_overflow_until_safe_time > available_headroom (soc_max - soc_kw)
```
- More overflow coming than battery can absorb → activate
- Otherwise → off, Predbat handles it
- No trajectory simulation needed. No will_fill. No sticky. No hysteresis.
- `remaining_overflow` from `compute_remaining_overflow()` scaled by `energy_ratio`
- `safe_time` from solar geometry (see below)

## Safe Time (Solar Geometry)
When does PV-load drop below DNO permanently?

**Calibration of scale = peak_pv / sin(elevation):**
- Before significant PV (< 2kWh produced): use FORECAST peak PV to estimate scale
- After significant PV: use rolling 30-min max from actuals, but only trust it after peak time
- Declining side: actuals are reliable

**Crossing:** scan for `scale * sin(elevation) < DNO + min_base_load`

**Safe time = crossing time.** No headroom subtraction — the floor handles timing.

**For tomorrow forecast sensor:** use forecast peak PV for scale (no actuals available).

## Dynamic Floor
```python
remaining_overflow = compute_remaining_overflow(now → safe_time) * energy_ratio
floor = soc_max - remaining_overflow
floor = max(floor, soc_keep, reserve)
```
- Recomputed each cycle (every ~5 min)
- Rises naturally: as overflow gets absorbed and safe_time approaches, remaining shrinks, floor rises
- Conservative early (morning: big overflow ahead → low floor → big headroom)
- Less conservative late (afternoon: little overflow left → high floor → nearly full)
- After safe_time: floor = soc_max (100%), absorb all remaining PV

## Binary Control (Three States)
| State | Condition | Export Limit | Battery |
|-------|-----------|-------------|---------|
| **Charge** | SOC < floor | 0 | Absorb all PV |
| **Hold** | SOC ≈ floor | min(excess, DNO) | Only overflow charges |
| **Export** | SOC > floor | min(excess, DNO) | Discharge toward floor |

No charge rate. No rate-limiting. The floor IS the control signal.

**HA automation simplified:**
```yaml
export_limit: 0 if soc < floor else min(excess, dno)
```

## What to Keep
- **`curtailment_calc.py` pure functions:** `compute_remaining_overflow`, `solar_elevation`, `compute_release_time`, `compute_morning_gap`, `compute_tomorrow_forecast`, `MIN_BASE_LOAD_KW`
- **`on_before_plan`:** soc_keep reduction on overflow days (separate from main control)
- **Tomorrow forecast sensor:** `_compute_tomorrow_forecast`, `_publish_tomorrow_forecast`
- **Energy ratio / load ratio:** `_get_energy_ratio`, `_get_load_ratio`
- **Rolling PV max:** `_get_rolling_pv_max` (for solar geometry calibration)
- **Solar geometry release:** `_compute_solar_release` (simplified — returns safe_time, no headroom subtraction)
- **SIG control:** `write_sig`, `_set_read_only`, `apply` (simplified)
- **Init/cleanup:** re-enable HA automation, clear stale read_only
- **`publish()`** with updated attributes

## What to Remove
- `simulate_soc_trajectory` — not needed for activation (just compare overflow vs headroom)
- `target_charge_rate` — replaced by binary control
- `_seen_overflow_today` / sticky activation
- `will_fill` / hysteresis / ACTIVATE_THRESHOLD / DEACTIVATE_THRESHOLD
- Multiple overlapping release checks (released + fallback + will_fill guard)
- `forecast_overflow` margin check
- `floor=100% bailout` check
- `compute_target_soc`, `should_activate`, `compute_overflow_window` (unused pure functions in calc.py)
- `SAFE_PV_THRESHOLD_KW` constant (no longer used for release — solar geometry replaces it)

## Simplified calculate() Flow
```
1. Read forecast data, battery state
2. Get energy_ratio, load_ratio, actual_pv, actual_load
3. Compute safe_time from solar geometry
4. Compute remaining_overflow (now → safe_time) × energy_ratio
5. Compute headroom = soc_max - soc_kw
6. If remaining_overflow <= headroom → return "off"
7. Compute floor = soc_max - remaining_overflow
8. Determine phase: charge (SOC < floor), hold (at floor), export (SOC > floor)
9. Return floor, phase
```

## Simplified apply() Flow
```
Charge phase: D-ESS, export=0, read_only=true
Hold phase:   D-ESS, export=DNO, read_only=true  (or HA automation)
Export phase:  D-ESS, export=DNO, read_only=true
Off phase:    MSC, read_only=false
```

Hold and Export both set export=DNO. The only difference from the battery's perspective is whether overflow charges it (hold, at floor) or it discharges (export, above floor). The SIG handles this naturally in D-ESS — we just set the export limit.

Actually: **hold and export can be the same.** D-ESS with export=DNO. If SOC > floor, SIG discharges to meet export limit. If SOC at floor, overflow charges. We just need charge (export=0) and hold/export (export=DNO).

So really two states: **charge** (export=0) and **managed** (export=DNO).

## HA Automation Update
Current: `export = min(max(excess - target_charge_rate, 0), dno)`
New: `export = 0 if soc < floor else min(excess, dno)`

The automation triggers on PV/load/SOC changes. Reads floor from `sensor.predbat_curtailment_target_soc`. Binary decision.

## Files to Modify
- `curtailment_plugin.py` — rewrite `calculate()`, simplify `apply()`, update `publish()`, clean `__init__`
- `curtailment_calc.py` — remove unused functions, update `compute_release_time` (no headroom param)
- `tests/test_curtailment.py` — rewrite tests for new logic
- HA automation `curtailment_manager_dynamic_export_limit` — update to binary

## Testing
1. Run against all 6 CSV days — zero curtailment, max export ≤ 4kW
2. Today's scenario (moderate, excess < DNO most of day) — plugin stays off
3. Apr 2 scenario (cloudy morning, clear afternoon) — activates when overflow > headroom
4. Jun 19 (65kWh peak) — floor at ~28%, battery absorbs overflow, releases at safe_time
5. Jul 15 (23kWh poor) — plugin never activates
6. Pre-commit clean
7. Deploy and monitor

## Risk
If remaining_overflow is underestimated (forecast too low, energy_ratio < 1 on a day that clears up), the plugin doesn't activate early enough. Mitigation: energy_ratio responds within 1-2 cycles as actual PV data comes in. The check runs every 5 minutes.

If we leave it too late and the battery fills, the SMA 4.25kW export limit is the hardware backstop.
