# Curtailment Manager — Requirements

All changes to the curtailment manager (curtailment_plugin.py, curtailment_calc.py,
HA automation, tests) MUST be checked against these requirements. Do not remove
features without verifying they are not required here.

## Goal

Prevent grid export exceeding 4kW DNO limit while minimizing curtailment
and filling the battery by sunset.

## Key Design Principle — Solar Geometry is the Ground Truth

**R25**: The overflow window and its energy are derived from the solar geometry
curve, not from the forecast per-slot scan. The smooth solar curve (scale × sin(elev))
defines when overflow is possible and how much headroom is needed. Forecast per-slot
data is too noisy (cloud calibration, Predbat scaling) to be trusted for this.

Scale is initialised from Solcast p90 forecast peak (worst-case near-perfect day).
Once actual peak PV is observed, scale updates — but only if it raises the floor
(reduces headroom). The floor can never be lowered mid-day; headroom already
reserved cannot be reclaimed.

## Safety

- **R1**: Export never exceeds DNO (4kW). SIG faults at 4.5kW. SMA backstop at 4.25kW.
- **R2**: On error, deactivate cleanly: restore MSC, clear read_only, reset export to DNO.
- **R3**: read_only=true during active control. Predbat must not change inverter settings.
- **R4**: Defer to Predbat charge windows when SOC < soc_keep and charge window active.

## Activation

- **R5**: Activate when BOTH conditions are true:
  1. `remaining_overflow > 0` — solar geometry curve (R9) predicts overflow.
  2. `solcast_remaining - load_remaining > (soc_max - soc_kw)` — total PV exceeds
     what is needed to fill the battery. If the battery won't reach 100% even with
     all the PV, the overflow energy is needed for charging — do not activate.
- **R6**: Deactivate at safe_time (R19): restore MSC, hand back to Predbat.
  Predbat MSC fills the battery with remaining post-safe_time PV.
- **R7**: No activation from per-slot forecast scan. Solar geometry and Solcast p90 only.
- **R8**: When inactive, Predbat manages normally.

## Scale — Worst-Case Clear Day

- **R42**: At activation, derive scale from Solcast p90 forecast:
  `scale = p90_peak_kw / sin(elevation at p90_peak_time)`
  This represents a near-perfect solar day — the worst case for overflow headroom.
- **R43**: Once actual peak PV is observed during the day, update scale:
  `scale = actual_peak_kw / sin(elevation at actual_peak_time)`
  Only apply if this raises the floor (i.e. actual_peak < p90_peak). If actual
  peak exceeds p90 (rare clear-sky burst), floor stays — headroom already reserved.
- **R44**: Before today's peak is observed, use yesterday's scale as fallback if
  Solcast p90 is unavailable. Scale changes slowly day-to-day (~1° elevation per day).

## Floor — Solar Geometry Integral

- **R9**: `remaining_overflow = ∫ max(0, scale × sin(elev(t)) - base_load - DNO) dt`
  integrated from now to safe_time (R19). Evaluated each 5-minute plugin cycle.
  `floor = soc_max - remaining_overflow × 1.25`
- **R10**: `floor = max(floor, soc_keep, reserve)` — never drain below household needs.
- **R11**: Floor ratchet — floor can only rise, never fall. Once headroom is reserved
  it cannot be reclaimed mid-day. Reset on deactivation.
- **R12**: At safe_time, remaining_overflow = 0, floor = soc_max. Plugin deactivates.
- **R13**: Floor rises naturally each cycle as the integral shrinks (time passing,
  sin(elev) falling). Rises faster on cloudy days (actual peak < p90 → scale updates
  down → integral smaller → floor higher sooner).

## Control — Two Behaviours (HA automation, 5-second cycle)

- **R14**: **Drain** (SOC > floor + 0.5kWh): export = DNO. SIG discharges battery
  toward floor. Creates headroom before overflow window.
- **R15**: **Hold** (SOC ≤ floor + 0.5kWh): export = min(excess, DNO). Battery
  absorbs overflow above DNO naturally. Sub-DNO PV is exported, not stored.
- **R16**: No Charge mode. Battery charges from overflow absorption in Hold, not
  from a forced zero-export state.
- **R17**: All active states use D-ESS mode. MSC only when off (R6).
- **R18**: HA automation (5-sec) handles real-time export control AND publishes live
  phase (Drain/Hold) to `input_text.curtailment_live_phase`. Plugin (5-min) computes
  floor, sets D-ESS mode, publishes Active/Off. Plugin sets live phase to Off on deactivation.

## Solar Geometry — Safe Time

- **R19**: Safe time = when `scale × sin(elev) < DNO + base_load`. No curtailment
  risk beyond this point. Computed each cycle from current scale.
  `base_load` = 0.5 kW (minimum household load that offsets PV before grid sees it).
- **R20**: Before today's actual peak is observed, safe_time is estimated from p90
  scale. Once actual peak seen and scale updates, safe_time recalculates.
- **R21**: Safe_time only moves later (more conservative) until actual peak is
  confirmed. Cannot move earlier until scale is updated downward from actual peak.

## Planning

- **R26**: on_before_plan reduces soc_keep on overflow days to morning_gap + margin.
  Uses tomorrow's Solcast p90 peak to determine if overflow is expected.
- **R27**: on_before_plan uses tomorrow's forecast window overnight (when today's
  solar < 1 hour remaining).
- **R28**: Overflow days should result in low morning SOC (max headroom for overflow).

## Tomorrow Sensor

- **R29**: Tomorrow sensor shows expected overflow energy (from p90 scale integral)
  and estimated safe_time. Available after today's PV is done. Shows "Pending" while
  waiting. Shows zeroed attributes when Inactive.
- **R30**: Tomorrow sensor uses same solar geometry calculation as live (R9/R19).
  Solcast p90 tomorrow peak for scale. Cached for 30 minutes.

## Floor Stability

- **R39**: Floor ratchet (R11): floor never falls within a day. No separate rate
  limit needed — the integral naturally falls smoothly as sin(elev) decreases.
  Safe_time only moves later until scale is confirmed (R21).

## Testing

- **R34**: Integration tests run ACTUAL plugin.calculate() against CSV data with
  independent physics simulation. Algorithm bugs cannot hide in reimplemented logic.
- **R35**: Tests must provide Solcast p90 peak via MockBase sensor overrides.
  Scale derivation must be testable with known p90 inputs.
- **R36**: TDD — when a flaw is found, write a FAILING test first. Then fix the
  code. Never deploy a fix without a test that would have caught the bug.
- **R37**: Never break production code to make tests pass. If tests fail but
  production is correct, fix the tests.
