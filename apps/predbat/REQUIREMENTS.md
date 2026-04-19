# Curtailment Manager — Requirements

All changes to the curtailment manager (curtailment_plugin.py, curtailment_calc.py,
HA automation, tests) MUST be checked against these requirements. Do not remove
features without verifying they are not required here.

## Goal

Prevent grid export exceeding 4kW DNO limit while minimizing curtailment
and filling the battery by sunset.

## Key Design Principle — Pre-Overflow is the Only Window

**R25**: Once PV-load > DNO, we have NO control levers. Battery fills from
overflow at whatever rate physics dictates (excess - DNO). The ONLY export
option is DNO. All management — drain, charge, hold — must happen BEFORE
overflow starts. This is why drain (R15) is essential: the battery must be
at the floor when overflow begins.

## Architecture — Shared Calculation

**R31**: Live calculate() and tomorrow forecast MUST use the same energy
balance function (`_compute_absorption`). One function, two time windows.
If the formula changes, it changes in one place. They must NEVER diverge.

**R32**: The energy balance uses TOTALS, not per-slot overflow:
`battery_must_absorb = remaining_pv - remaining_load - DNO * hours_to_release`
All three (PV, load, export capacity) cover the same window: now to release time.

**R33**: PV total from Solcast remaining sensor (updated from actual conditions).
Per-slot forecast shape used ONLY to estimate the fraction before release time.
Magnitude from Solcast, shape from forecast, both-ways energy_ratio scaling.

## Safety

- **R1**: Export never exceeds DNO (4kW). SIG faults at 4.5kW. SMA backstop at 4.25kW.
- **R2**: On error, deactivate cleanly: restore MSC, clear read_only, reset export to DNO.
- **R3**: read_only=true during active control. Predbat must not change inverter settings.
- **R4**: Defer to Predbat charge windows when SOC < soc_keep and charge window active.

## Activation — "Is there a problem?"

Activation asks: will the total PV excess fill the battery before safe_time?
This is SEPARATE from the floor calculation (which computes the solution).
No export capacity in the activation check — export is what management DOES, not an input.

- **R5**: Activate when `total_excess > headroom` where:
    - `total_excess = remaining_pv - remaining_load` (to safe_time, from Solcast + LoadML)
    - `headroom = soc_max * 0.95 - soc_kw`
- **R6**: Deactivate when `total_excess < headroom * 0.9`. Hysteresis prevents toggling.
- **R7**: Trust forecast + energy ratio for activation. No force-activate from actual excess.
- **R8**: When inactive and no overflow forecast, stay off. Predbat manages normally.

## Floor

- **R9**: `floor = soc_cap - battery_must_absorb * safety_factor` where battery_must_absorb
  comes from the totals-based energy balance (R32). Safety factor = 1.10 (10% buffer).
- **R10**: `floor = max(floor, soc_keep, reserve)` — never drain below household needs.
- **R11**: `floor = min(floor, soc_max * 0.95)` before safe_time — 0.9 kWh spike headroom.
- **R12**: After safe_time, cap removed: `floor = min(floor, soc_max)`. Battery fills to 100%.
- **R13**: Floor rises naturally each cycle as remaining PV and absorption shrink.

## Control — Three Behaviors (HA automation, 5-second cycle)

- **R14**: **Charge** (SOC < floor - 0.5kWh): export=0, battery absorbs all PV.
- **R15**: **Drain** (SOC > floor + 0.5kWh): export=DNO, SIG discharges battery toward floor. THIS IS ESSENTIAL — creates headroom before overflow peaks.
- **R16**: **Hold** (SOC within 0.5kWh of floor): export=min(excess, DNO), battery maintains at floor.
- **R17**: All active states use D-ESS mode. MSC only when off.
- **R18**: HA automation (5-sec) handles real-time export control AND publishes live phase (Charge/Drain/Hold) to `input_text.curtailment_live_phase`. Plugin (5-min) computes floor, sets D-ESS mode, publishes Active/Off. Plugin sets live phase to Off when deactivating.

## Solar Geometry

- **R19**: Safe time from day's peak PV: `scale = peak_pv / sin(elevation_at_peak)`. Safe when `scale * sin(elev) < DNO + 0.5`.
- **R20**: Before peak time, still estimate safe_time for energy balance window. Only block the 95% uncap (don't release before peak).
- **R21**: New peak resets safe_time calculation (re-engages 95% cap).

## Forecast Scaling

- **R22**: `energy_ratio = cumulative_actual_pv / cumulative_forecast_pv` with 15% blend ramp.
- **R23**: `load_ratio = cumulative_actual_load / cumulative_forecast_load` with 15% blend.
- **R24**: energy_ratio scales Solcast remaining both ways (fine-tuning on accurate base). load_ratio scales LoadML load both ways.

## Planning

- **R26**: on_before_plan reduces soc_keep on overflow days to morning_gap + margin. Only reduces if overflow needs the headroom (overflow * safety > headroom with current keep).
- **R27**: on_before_plan uses tomorrow's forecast window overnight (when today's solar < 1 hour remaining).
- **R28**: Overflow days should result in low morning SOC (max headroom for overflow absorption).

## Tomorrow Sensor

- **R29**: Tomorrow forecast waits until today's PV is done (last forecast PV slot < 30 min away). Shows "Pending" with estimated availability time while waiting. Shows clean zeroed attributes when Inactive.
- **R30**: Tomorrow sensor uses SAME `_compute_absorption` function as live (R31). Solcast tomorrow total for PV. Per-slot shape for release fraction. Cached for 30 minutes.

## Testing

- **R34**: Integration tests run ACTUAL plugin.calculate() against CSV data with independent physics simulation. Algorithm bugs cannot hide in reimplemented simulation logic.
- **R35**: Tests must provide Solcast remaining via MockBase sensor overrides to match production behavior. Fallback to per-slot sum is for production resilience, not the primary test path.
- **R36**: TDD — when a flaw is found, write a FAILING test first that demonstrates the flaw. Then fix the code to make the test pass. Never deploy a fix without a test that would have caught the bug.
- **R37**: Never break production code to make tests pass. If tests fail but production is correct, fix the tests.
