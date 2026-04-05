# Curtailment Manager — Definitive Requirements & v10.1 Fix

## Context

v10 was deployed 2026-04-04 as a clean rewrite. The activation (overflow vs headroom) and floor (soc_max - overflow * 1.10) are correct, but the **drain mechanism was removed**. The "managed" state uses `export = min(excess, DNO)` which only exports PV excess — the SIG never discharges the battery toward the floor. On Jul 12 (15.6 kWh overflow, 40% start), this causes 2.49 kWh curtailment from the battery not being at the floor when overflow peaks.

The fix: restore drain via the HA automation's export limit. When SOC > floor + 0.5kWh, set export = DNO. D-ESS with export > PV excess causes the SIG to discharge the battery to maintain the export level.

## Decisions
- **R8 (toggle protection)**: Hysteresis — activate at overflow * 1.1 > headroom, deactivate at overflow * 0.9 <= headroom
- **R7 (actual overflow)**: Trust forecast + energy ratio. No force-activate from actual excess.
- **Requirements**: Save as `apps/predbat/REQUIREMENTS.md` alongside the code.

---

## Changes Required

### 1. Create `apps/predbat/REQUIREMENTS.md`
Permanent reference file. All future changes must check against it. Contains R1-R27 below.

### 2. HA Automation — three-state export control
Current (broken — no drain):
```
export = 0 if soc < floor else min(excess, dno)
```

Fixed:
```
if soc_kwh < target_kwh - 0.5:
    export = 0                          # charge: absorb all PV
elif soc_kwh > target_kwh + 0.5:
    export = dno                        # drain: SIG discharges battery
else:
    export = min(max(excess, 0), dno)   # hold: export PV excess only
```

0.5 kWh margin (symmetric with SOC_MARGIN_KWH) around floor for all three zones.

### 3. Plugin calculate() — hysteresis on activation
```python
ACTIVATE_FACTOR = 1.10   # need 10% more overflow than headroom to activate
DEACTIVATE_FACTOR = 0.90 # need overflow to drop to 90% of headroom to deactivate

if self.was_active:
    if remaining_overflow * DEACTIVATE_FACTOR <= max(headroom, 0):
        return soc_max, "off"
else:
    if remaining_overflow * ACTIVATE_FACTOR <= max(headroom, 0):
        return soc_max, "off"
```

Floor always uses 1.10 safety factor (unchanged).

### 4. Plugin publish() — Active/Off only
Plugin publishes **Active** or **Off** as the phase sensor state. The real-time phase (Charge/Drain/Hold) is determined by the HA automation every 5 seconds and written to `input_text.curtailment_live_phase`. The plugin's 5-min cycle is too slow for accurate phase reporting.

### 5. Simulation — add drain physics
In `_simulate_day_v10`, when SOC > floor + margin:
```python
if mode == "drain":
    # SIG exports at DNO using PV + battery discharge
    export = min(actual_excess + max_discharge_available, dno)
    discharge = max(0, export - max(actual_excess, 0))
    # ... physics
```

### 6. Plugin apply() — export=0 on activate, DNO on deactivate
- **Activate**: set export_limit=0, then D-ESS mode, charge_limit=100, read_only=true. Safe default (absorb PV). HA automation takes over within 5 seconds and sets correct value.
- **Deactivate**: set MSC mode, export_limit=DNO (cleanup reset), read_only=false.

---

## Files to Modify
- `apps/predbat/REQUIREMENTS.md` — new file, permanent requirements reference
- `apps/predbat/curtailment_plugin.py` — hysteresis, diagnostic phase in publish()
- `apps/predbat/tests/test_curtailment.py` — drain in simulation, drain test, updated curtailment bounds
- HA automation `curtailment_manager_dynamic_export_limit` — three-state logic

---

## Definitive Requirements (for REQUIREMENTS.md)

### Goal
Prevent grid export exceeding 4kW DNO limit while minimizing curtailment and filling the battery by sunset.

### Safety
- **R1**: Export never exceeds DNO (4kW). SIG faults at 4.5kW. SMA backstop at 4.25kW.
- **R2**: On error, deactivate cleanly: restore MSC, clear read_only, reset export to DNO.
- **R3**: read_only=true during active control. Predbat must not change inverter settings.
- **R4**: Defer to Predbat charge windows when SOC < soc_keep and charge window active.

### Activation
- **R5**: Activate when `remaining_overflow * 1.10 > soc_max * 0.95 - soc_kw`.
- **R6**: Deactivate when `remaining_overflow * 0.90 <= soc_max * 0.95 - soc_kw`. Hysteresis prevents toggling.
- **R7**: Trust forecast + energy ratio for activation. No force-activate from actual excess.
- **R8**: When inactive and no overflow forecast, stay off. Predbat manages normally.

### Floor
- **R9**: `floor = soc_max - remaining_overflow * 1.10` (10% safety factor on overflow estimate).
- **R10**: `floor = max(floor, soc_keep, reserve)` — never drain below household needs.
- **R11**: `floor = min(floor, soc_max * 0.95)` before safe_time — 0.9 kWh spike headroom.
- **R12**: After safe_time, cap removed: `floor = min(floor, soc_max)`. Battery fills to 100%.
- **R13**: Floor rises naturally each cycle as remaining_overflow shrinks.

### Control — Three Behaviors
- **R14**: **Charge** (SOC < floor - 0.5kWh): export=0, battery absorbs all PV.
- **R15**: **Drain** (SOC > floor + 0.5kWh): export=DNO, SIG discharges battery toward floor. THIS IS ESSENTIAL — creates headroom before overflow peaks.
- **R16**: **Hold** (SOC within 0.5kWh of floor): export=min(excess, DNO), battery maintains at floor.
- **R17**: All active states use D-ESS mode. MSC only when off.
- **R18**: HA automation (5-sec) handles real-time export control AND publishes live phase (Charge/Drain/Hold) to `input_text.curtailment_live_phase`. Plugin (5-min) computes floor, sets D-ESS mode, publishes Active/Off.

### Solar Geometry
- **R19**: Safe time from day's peak PV: `scale = peak_pv / sin(elevation_at_peak)`. Safe when `scale * sin(elev) < DNO + 0.5`.
- **R20**: Before peak time, don't compute release — PV may still be rising.
- **R21**: New peak resets safe_time calculation (re-engages 95% cap).

### Forecast Scaling
- **R22**: `energy_ratio = cumulative_actual_pv / cumulative_forecast_pv` with 15% blend ramp.
- **R23**: `load_ratio = cumulative_actual_load / cumulative_forecast_load` with 15% blend.
- **R24**: Scale forecasts before computing overflow: `scaled_pv = forecast * energy_ratio`.

### Key Design Principle — Pre-Overflow is the Only Window
- **R25**: Once PV-load > DNO, we have NO control levers. Battery fills from overflow at whatever rate physics dictates (excess - DNO). The ONLY export option is DNO. All management — drain, charge, hold — must happen BEFORE overflow starts. This is why drain (R15) is essential: the battery must be at the floor when overflow begins.

### Planning
- **R26**: on_before_plan reduces soc_keep on overflow days to morning_gap + margin.
- **R27**: Tomorrow forecast sensor uses same overflow-vs-headroom logic.
- **R28**: Overflow days should result in low morning SOC (max headroom for overflow absorption).

### Tomorrow Sensor
- **R29**: After sunset (PV < 0.1kW), the tomorrow forecast sensor must be published and visible. It shows: total overflow, floor %, will_activate, morning_gap, release time, expected soc_keep. This is the user's primary way to see what's coming tomorrow once today's curtailment is done.
- **R30**: Tomorrow sensor is computed from Predbat's forecast data (pv_forecast_minute_step, load_minutes_step) for the next day's solar window. Cached for 30 minutes to avoid redundant computation.

---

## Verification
1. `pre-commit run --all-files` — clean
2. `cd apps/predbat && python3 tests/test_curtailment.py` — all pass
3. Jul 12 perfect forecast (40% start): curtailment **< 0.5 kWh** (was 2.49)
4. Jul 12 perfect forecast (10% start): curtailment **~0 kWh**
5. All 6 CSV days: max export <= DNO, sunset SOC > 90%
6. Deploy and check `input_text.curtailment_live_phase` shows Drain→Hold→Off sequence
7. Check HA logs: no rapid D-ESS/MSC toggling (hysteresis working)
