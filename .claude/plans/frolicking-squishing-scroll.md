# Curtailment Manager v10 — Clean Rewrite Plan

## Context

The curtailment manager has grown to 21 commits of patches on patches. The activation logic has multiple overlapping checks (will_fill, forecast_overflow margins, floor=100% bailouts, sticky activation, hysteresis) that interact badly on moderate/borderline days — either activating unnecessarily (exporting PV that Predbat needs) or failing to deactivate. A clean rewrite from first principles will be simpler and more robust.

## Goal
Prevent grid export exceeding 4kW DNO limit. Only act when there's not enough battery headroom to absorb remaining overflow before PV drops to safe levels.

## Activation: One Check
```
remaining_overflow * 1.10 > available_headroom (soc_max * 0.95 - soc_kw)
```
- More overflow coming (with 10% safety factor) than battery can absorb to 95% → activate
- Otherwise → off, Predbat handles it
- No trajectory simulation needed. No will_fill. No sticky. No hysteresis.
- `remaining_overflow` computed from SCALED forecasts: `pv * energy_ratio`, `load * load_ratio`
  - energy_ratio adjusts PV (>1 = actual ahead of forecast → more overflow)
  - load_ratio adjusts load (<1 = actual lower than predicted → more overflow)
- Overflow summed over entire remaining solar day (no safe_time cutoff — just total overflow)

## Safe Time (Solar Geometry — for 95% cap removal only)
Solar geometry answers ONE question: when can the battery safely go above 95%?

`scale * sin(elevation) < DNO + min_base_load` → PV physically can't spike above DNO after this time.

**Calibration:** day's peak PV / sin(elevation at peak). Updated whenever a new peak is seen.

**Used only to decide when to uncap the floor from 95% to 100%.** Not used for overflow calculation or activation.

Solar geometry kept for:
- 95% → 100% uncap decision (safe_time)
- Tomorrow forecast sensor (no actuals, needs sun curve for release time estimate)
- Diagnostic attributes

## Dynamic Floor
```python
scaled_pv = {m: v * energy_ratio for m, v in pv_step.items()}
scaled_load = {m: v * load_ratio for m, v in load_step.items()}
remaining_overflow = compute_remaining_overflow(scaled_pv, scaled_load, dno, now -> solar_end)
floor = soc_max - remaining_overflow * 1.10  # 10% safety factor
floor = max(floor, soc_keep, reserve)
floor = min(floor, soc_max * 0.95)  # cap at 95% until safe_time (spike headroom)

if past_safe_time:  # solar geometry says PV can't spike above DNO
    floor = min(floor, soc_max)  # uncap to 100%, fill battery
```
Two layers of conservatism:
1. **10% buffer** on overflow estimate — proportional, covers forecast error
2. **95% cap** until safe_time — absolute, covers transient spikes within averaged slots

The 95% cap = 0.9kWh headroom. Absorbs a 6kW spike for ~9 minutes. Enough for cloud-to-clear transitions that the 5-min forecast averages miss.

- Recomputed each cycle (every ~5 min)
- Rises naturally: as overflow absorbed, remaining shrinks, floor rises toward 95%
- Conservative early (morning: big overflow → low floor → big headroom)
- Less conservative late (afternoon: little overflow → floor near 95%)
- After safe_time: cap removed, floor can reach 100%, battery fills from remaining PV

## Binary Control (Two States)
| State | Condition | Export Limit | Battery |
|-------|-----------|-------------|---------|
| **Charge** | SOC < floor | 0 | Absorb all PV |
| **Managed** | SOC >= floor | min(excess, DNO) | Overflow charges, excess exports |

No charge rate. No rate-limiting. The floor IS the control signal.

Hold and Export collapse into one "managed" state — D-ESS with export=DNO. The SIG naturally handles overflow (charges battery) vs excess below DNO (exports).

**HA automation simplified:**
```yaml
export_limit: 0 if soc < floor else min(excess, dno)
```

## Simplified calculate() Flow
```
1. Read forecast data, battery state
2. Get energy_ratio, load_ratio
3. Scale forecasts: pv * energy_ratio, load * load_ratio
4. Compute remaining_overflow (now -> solar_end) from scaled forecasts
5. Compute headroom = soc_max * 0.95 - soc_kw
6. If remaining_overflow * 1.10 <= headroom -> return "off"
7. Compute floor = soc_max - remaining_overflow * 1.10, cap at 95%
8. Check solar geometry: if past safe_time, uncap to 100%
9. Determine phase: charge (SOC < floor) or managed (SOC >= floor)
10. Return floor, phase
```

## Simplified apply() Flow
```
Charge:   D-ESS, export=0, read_only=true
Managed:  D-ESS, export=DNO, read_only=true
Off:      MSC, read_only=false
```

## What to Keep
- **Pure functions (curtailment_calc.py):** `compute_remaining_overflow`, `solar_elevation`, `compute_release_time`, `compute_morning_gap`, `compute_tomorrow_forecast`, `MIN_BASE_LOAD_KW`
- **`on_before_plan`:** soc_keep reduction on overflow days
- **Tomorrow forecast sensor:** `_compute_tomorrow_forecast`, `_publish_tomorrow_forecast`
- **Energy ratio / load ratio:** `_get_energy_ratio`, `_get_load_ratio`
- **Day's peak PV tracker:** replaces rolling 30-min max. Track highest PV reading today. Used for solar geometry scale calibration.
- **Solar geometry:** `_compute_solar_release` simplified — uses day's peak PV for scale. Returns safe_time for 95%→100% uncap decision. Also used by tomorrow sensor.
- **SIG control:** `write_sig`, `_set_read_only`, `apply` (simplified to two active states)
- **Init/cleanup:** re-enable HA automation, clear stale read_only
- **`publish()`** with updated attributes

## What to Remove
- `simulate_soc_trajectory` usage in calculate() — not needed (just compare overflow vs headroom)
- `target_charge_rate` — replaced by binary control
- `_seen_overflow_today` / sticky activation
- `will_fill` / hysteresis / ACTIVATE_THRESHOLD / DEACTIVATE_THRESHOLD
- Multiple overlapping release checks
- `forecast_overflow` margin check
- `floor=100% bailout` check
- `compute_target_soc`, `should_activate`, `compute_overflow_window` (unused pure functions)
- `SAFE_PV_THRESHOLD_KW` constant (solar geometry replaces it for release)

Note: `simulate_soc_trajectory` stays in curtailment_calc.py (used by `on_before_plan` and `compute_tomorrow_forecast`) but is no longer called from `calculate()`.

## HA Automation Update
Current: `export = min(max(excess - target_charge_rate, 0), dno)`
New: `export = 0 if soc < floor else min(excess, dno)`

Reads floor from `sensor.predbat_curtailment_target_soc`. Binary decision based on SOC vs floor.

## HA Entity Cleanup
Remove orphaned entities from old versions:
- `input_number.curtailment_manager_buffer` — old v6 config, not referenced in code
- `input_number.curtailment_manager_buffer_percent` — old v6 config, not referenced in code

Keep:
- `input_boolean.curtailment_manager_enable` — on/off toggle
- `input_text.curtailment_live_phase` — written by HA automation for dashboard
- `automation.curtailment_manager_dynamic_export_limit` — rewrite to binary
- `sensor.predbat_curtailment_phase` — main status
- `sensor.predbat_curtailment_target_soc` — floor target
- `sensor.predbat_curtailment_solar_offset` — on_before_plan output
- `sensor.predbat_curtailment_tomorrow` — tomorrow forecast

## Files to Modify
- `apps/predbat/curtailment_plugin.py` — rewrite `calculate()`, simplify `apply()`, update `publish()`, clean `__init__`
- `apps/predbat/curtailment_calc.py` — update `compute_release_time` (remove headroom param), remove unused functions
- `apps/predbat/tests/test_curtailment.py` — rewrite tests for new logic
- HA automation `curtailment_manager_dynamic_export_limit` — update to binary
- HA helpers — delete orphaned input_number entities

## Validation Framework (Permanent Test)
A simulation test in `tests/test_curtailment.py` that replays all 6 CSV days through the v10 logic:
- For each day: step through 5-min slots, compute activation (overflow vs headroom), floor, phase, export
- Assert: max export <= DNO, zero/minimal curtailment, battery reaches >90% by sunset
- Print summary table: day, total PV, export, curtailed, max export, final SOC, floor at 10:00

This is the regression test: any future change must pass all 6 days. Run before deploying.

## Tomorrow Sensor Alignment
`compute_tomorrow_forecast()` should use the same v10 logic:
- `total_overflow_kwh` from `compute_remaining_overflow()` (same as live)
- `will_activate = total_overflow_kwh > (soc_max - estimated_start_soc)` — overflow vs headroom
  - `estimated_start_soc = morning_gap + margin` (battery at keep level when overflow starts)
- `floor_pct = (soc_max - total_overflow_kwh) / soc_max * 100` (same formula)
- Remove `simulate_soc_trajectory` call from tomorrow forecast (no longer needed)

## Verification
1. Run CSV validation framework — all 6 days pass
2. Today's scenario (moderate, excess < DNO most of day) — plugin stays off
3. Apr 2 scenario (cloudy morning, clear afternoon) — activates when overflow > headroom
4. Jun 19 (65kWh peak) — floor at ~28%, battery absorbs overflow, releases at safe_time
5. Jul 15 (23kWh poor) — plugin never activates
6. Tomorrow sensor shows correct values after sunset
7. Pre-commit clean
8. Deploy and monitor

## Implementation Details (for fresh context after /clear)

### System Constants
- Battery: 18.08 kWh (SIG inverter)
- DNO limit: 4.0 kW export
- SIG hard limit: 4.5 kW (overvoltage fault above this)
- SMA export limit: 4.25 kW (hardware backstop, set in SMA)
- Min base load: 0.5 kW (fridge, standby)
- PREDICT_STEP: 5 minutes
- SOC_MARGIN_KWH: 0.5 (dead band for charge/managed transition)

### SIG Entity Names
```python
SIG_EMS_MODE = "select.sigen_plant_remote_ems_control_mode"
SIG_EXPORT_LIMIT = "number.sigen_plant_grid_export_limitation"
SIG_CHARGE_LIMIT = "number.sigen_plant_ess_charge_cut_off_state_of_charge"
SIG_PV_POWER = "sensor.sigen_plant_pv_power"
SIG_LOAD_POWER = "sensor.sigen_plant_consumed_power"
SIG_DAILY_PV = "sensor.sigen_plant_daily_third_party_inverter_energy"
```

### D-ESS Mode Behaviour
- "Command Discharging (ESS First)" = D-ESS mode
- SIG respects the export limit setting
- If excess > export_limit: battery absorbs overflow (physics)
- If excess < export_limit: SIG exports excess, battery idle or discharges
- "Maximum Self Consumption" = MSC mode (Predbat's normal)

### Energy Ratio Computation (keep as-is from _get_energy_ratio)
- `actual_produced` from `sensor.sigen_plant_daily_third_party_inverter_energy`
- `forecast_produced` from `sensor.predbat_pv_today` attributes (totalCL - remainingCL)
- 10% blend: `energy_ratio = 1.0 + (raw_ratio - 1.0) * blend` where blend ramps from 0→1 over first 10% of daily forecast
- Fallback to raw Solcast if calibrated attributes unavailable

### Load Ratio Computation (keep as-is from _get_load_ratio)
- `actual_load_total` from `sensor.sigen_plant_daily_load_consumption`
- `predicted_so_far` from `predbat.load_energy_adjusted` attribute `today_so_far`
- Same 10% blend as energy ratio
- Returns 1.0 if too early (< 2kWh predicted so far)

### Solar Geometry (keep solar_elevation and compute_release_time from curtailment_calc.py)
- `solar_elevation(lat, lon, utc_hours, doy)` — Spencer 1971, ~1 degree accuracy
- `compute_release_time(scale, lat, lon, doy, threshold_kw, current_utc_hours)` — scan for crossing
- Location from `zone.home` entity (lat/lon attributes)
- Remove headroom_kwh parameter from compute_release_time — just find the crossing time

### Rolling PV Max (keep _get_rolling_pv_max)
- 30-min window of (minutes_now, pv_kw) tuples
- Returns (max_value, time_of_max)
- Used for solar geometry scale calibration on declining side

### Forecast Data Format
- `pv_forecast_minute_step`: dict {minutes_from_now: kWh_per_step}
- `load_minutes_step`: dict {minutes_from_now: kWh_per_step}
- Both are kWh per 5-min step (not kW) — multiply by 12 to get kW
- `forecast_minutes`: how far ahead the forecast extends (typically 1800 = 30h)
- `solar_end = min(forecast_minutes, max(5, 23*60 - minutes_now))`

### on_before_plan (keep as-is)
- Reduces best_soc_keep on overflow days to morning_gap + 0.5 margin
- If overflow > morning_gap: keep = reserve (0) — battery refills from overflow anyway
- Uses simulate_soc_trajectory to check will_fill (this is the planning check, separate from real-time)
- Caches result (30 min overnight, always recalc morning, skip afternoon)

### HA Automation Entity
- `automation.curtailment_manager_dynamic_export_limit`
- Triggers: PV power, consumed power, SOC, target_soc changes
- Conditions: curtailment phase != Off, EMS mode = D-ESS
- Currently: `export = min(max(excess - target_charge_rate, 0), dno)`
- New: `export = 0 if soc_kwh < target_kwh else min(excess, dno)`
  - `soc_kwh = soc_pct / 100 * 18.08`
  - `target_kwh` from `sensor.predbat_curtailment_target_soc` attribute

### Deploy Method
```bash
for f in curtailment_calc.py curtailment_plugin.py; do
  ssh hassio@100.110.70.80 "sudo cp /dev/stdin /addon_configs/6adb4f0d_predbat/$f" < apps/predbat/$f
done
ssh hassio@100.110.70.80 "sudo touch /addon_configs/6adb4f0d_predbat/plugin_system.py"
```

### Test Patterns
- MockBase class in test_curtailment.py provides: pv_forecast_minute_step, load_minutes_step, soc_kw, soc_max, minutes_now, now_utc, zone.home lat/lon
- MockBase converts raw kW inputs to kWh/step internally (PLUGIN_STEP / 60.0 factor)
- CSV days in tests/data/curtailment/: Jun19, Jul12, Jul9, Jul1, Jul15, May30
- `_load_csv_to_forecasts(filepath, watts=bool)` loads CSV to forecast dicts

### Pre-commit Checks (MANDATORY before commit)
- `pre-commit run --all-files`
- `cd apps/predbat && python3 tests/test_curtailment.py --quick`

## Risk
If remaining_overflow is underestimated (forecast too low, energy_ratio < 1 on a day that clears up), the plugin doesn't activate early enough. Mitigation: energy_ratio responds within 1-2 cycles as actual PV data comes in. The check runs every 5 minutes.

If we leave it too late and the battery fills, the SMA 4.25kW export limit is the hardware backstop.
