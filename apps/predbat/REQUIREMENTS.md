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
- **R43**: `floor_scale = max(p90_scale, actual_scale)` — asymmetric use of actual_scale
  for the floor:
    - When `actual > p90` (day sunnier than forecast): use actual_scale. Bigger overflow
    estimate → lower floor → more drain → safer. Protects against the 10% of days that
    exceed the p90 forecast.
    - When `actual < p90` (day cloudier): keep p90_scale. Afternoon could still clear,
    and using a low actual_scale at peak hour would drop the floor (violating R11 spirit)
    and under-provision for a late-day p90 outcome.
  Previously the floor always used p90_scale. This under-estimated overflow on sunny
  days where actual PV exceeded p90, risking DNO breach.
  actual_scale also drives safe_scale (R21): cloudy day → earlier safe_time → earlier
  MSC handoff to recover battery; clear day → later safe_time (conservative).
- **R44**: Before today's peak is observed, use yesterday's scale as fallback if
  Solcast p90 is unavailable. Scale changes slowly day-to-day (~1° elevation per day).

## Floor — Solar Geometry Integral

- **R9** (v19 tapered cap): `remaining_overflow = ∫ max(0, scale × sin(elev(t))
    - effective_load(t) - DNO) dt` integrated from now to safe_time (R19).
  Evaluated each 5-minute plugin cycle.

  ```text
  buffer_kwh       = min(MAX_RESERVED_KWH, remaining_overflow)  # MAX_RESERVED_KWH = 1.8
  max_target_soc   = soc_max - buffer_kwh
  overflow_floor   = max_target_soc - remaining_overflow × OVERFLOW_SAFETY_FACTOR
  ```

  Safety factor = 1.2 reserves 20% extra headroom against forecast error *during*
  overflow. The tapered cap (R45) only binds when `remaining_overflow ≥ 1.8 kWh`
  (peak of day); near safe_time the buffer tapers toward 0, `max_target_soc`
  approaches soc_max, and the battery fills to ~100% before handoff to MSC.
- **R9a**: `effective_load(t) = max(base_load, loadml_forecast(t))` — the overflow
  integral MUST use Predbat's LoadML per-slot forecast with `base_load` (0.5 kW) as
  a floor. LoadML already learns regular daytime loads (DHW cycle, EV charging,
  cooking). Those absorb PV directly and reduce the overflow needing export headroom.
  Reason: with only the 0.5 kW flat constant, the formula overestimated overflow by
  1–2 kW × ~10 daylight hours on normal days, forcing unnecessary drain and lower
  sunset SOC. See also `feedback_use_loadml_for_floor.md`.
- **R10**: `floor = max(floor, effective_keep, reserve)` — never drain below
  household needs. `effective_keep` is normally `soc_keep` but can be relaxed
  to 0.5 kWh under R48 conditions.
- **R48**: Relaxed soc_keep on big-overflow mornings. When BOTH (a) the forecast
  overflow × safety_factor exceeds room available with base keep, AND (b) PV
  currently exceeds load by ≥ 0.5 kW, use `effective_keep = 0.5 kWh` instead
  of `soc_keep`. **Two-phase recovered latch** — battery must first be observed
  BELOW `soc_keep` this day (sets `_keep_drained_today = True`) before
  `_keep_recovered = True` can latch on SOC rising back to `soc_keep`. Without
  the drain-first guard, the latch fires at midnight rollover when battery is
  at 100% overnight, defeating R48 on every real morning.
  **Engagement latch** (`_r48_engaged_today`) — once R48's first-fire conditions
  are met today, latch on so subsequent cycles use relaxed keep regardless of
  pv_covering oscillation around the 0.5 kW threshold. Avoids effective_keep
  toggling 0.5 ↔ 1.5 kWh in cloudy mornings (5 toggles observed 2026-04-25
  06:11–09:58 BST before this fix). Engagement latch clears when
  `_keep_recovered = True` (drain cycle complete). All three flags persisted
  via state file; reset on day rollover.
- **R11**: Floor ratchet applies to the OVERFLOW-DERIVED floor only, not the
  final floor after soc_keep/reserve clamps. `soc_keep` and `reserve` are
  DYNAMIC — when cold weather boost ends or on_before_plan reduces keep, the
  final floor follows. Only the `soc_max - overflow × safety_factor` component
  ratchets (the actual headroom reservation we've committed to).
  Exception: ratchet is bypassed when `floor_scale` increased from previous
  cycle (R43 triggered — sunnier than forecast, more headroom needed, allow
  floor to drop). Reset on deactivation.
- **R46**: Deactivation uses `safe_time`, not the forecast integral. Plugin goes
  Off only when `now >= safe_time` (solar geometry past overflow threshold) or
  when the battery-fill check fails. The LoadML-driven integral can under-
  estimate overflow (phantom afternoon load pushes predicted overflow to zero
  even while sun is still above threshold), which would cause premature
  deactivation and lose R45 protection during the last chunk of the overflow
  window. Activation still uses the integral (need forecast confidence to
  start draining the battery). R25/R19 solar geometry is ground truth.
- **R47**: Persist state `{date, peak_pv_kw, peak_pv_time, floor_ratchet,
  last_floor_scale}` to `curtailment_state.json` under `config_root`. Load
  on plugin init if date matches today; ignore if stale. Prevents restarts
  from losing observed peak_pv (and therefore actual_scale → safe_scale →
  safe_time) mid-day. Test environments without `config_root` skip
  persistence to avoid cross-test pollution.
- **R45** (v19): Reserved headroom = `min(effective_max_reserved, remaining_overflow)`.
  `MAX_RESERVED_KWH = 1.8` (10% of soc_max) — same ceiling as the previous
  hardcoded 90% cap. `effective_max_reserved` is normally `MAX_RESERVED_KWH`
  but may be reduced by R49 on confirmed-cloudy afternoons. The reservation
  tapers with `remaining_overflow`:
    - Peak overflow (`remaining ≥ effective_max_reserved`): buffer clamps at
    `effective_max_reserved`, target is `soc_max - effective_max_reserved`.
    Full CLS safety during the window where LoadML over-prediction could
    inflate real overflow.
    - Tail of overflow (`remaining < effective_max_reserved`): buffer =
    `remaining`, target rises toward 100%. Physical PV is already near
    DNO+load so LoadML surprise is bounded by the PV curve itself.
    - At safe_time (`remaining = 0`): buffer = 0, target = soc_max. Battery full
    before MSC handoff. Avoids the old trade-off of ending 92–95% on thin
    post-release tail days where MSC can't refill the 10% reserve from sparse
    evening PV.
- **R50** (v21 confidence-weighted overflow): the floor formula uses a
  confidence-weighted blend of three forecast bands instead of always-p90.
  Solcast publishes pv_estimate10 / pv_estimate (P50) / pv_estimate90 per
  slot, plus an `analysis.confidence` value (0..1). The plugin computes
  three overflow integrals using each band's scale, then blends them by
  confidence:

  ```text
  p10_scale = max(p10_peak / sin(elev_at_peak), actual_scale)   # R43 still applies
  p50_scale = max(p50_peak / sin(elev_at_peak), actual_scale)
  p90_scale = max(p90_peak / sin(elev_at_peak), actual_scale)

  overflow_p10 = ∫ max(0, p10_scale × sin(elev) − load − DNO) dt
  overflow_p50 = ∫ max(0, p50_scale × sin(elev) − load − DNO) dt
  overflow_p90 = ∫ max(0, p90_scale × sin(elev) − load − DNO) dt

  c = clamp(confidence, 0, 1)
  HIGH = input_number.curtailment_confidence_high   (default 0.85)
  LOW  = input_number.curtailment_confidence_low    (default 0.60)

  if c >= HIGH:           expected = overflow_p90        # pre-R50 behaviour
  elif c >= LOW:          t = (c − LOW) / (HIGH − LOW)
                          expected = (1−t)*p50 + t*p90
  else:                   t = c / LOW
                          expected = (1−t)*p10 + t*p50
  ```

  `expected` then substitutes for `remaining_overflow` in R9's floor
  formula. R45 buffer, R49 buffer reduction, R11 ratchet, R43 actual_scale
  promotion all still apply on top of the blended estimate.

  Why: at low confidence, the p90 forecast isn't trustworthy and committing
  to p90 drain wastes battery on round-trip losses. The blend leans toward
  pessimistic estimates when forecast quality is low.

  Reference incident: 2026-04-28. Plugin drained ~9.5 kWh on a forecast
  where Solcast reported confidence 0.69 and spread 25 kWh (P10=14, P50=31,
  P90=49). Day delivered ~5 kWh PV. Round-trip loss ~1.4 kWh + import cost.
  Battery hit 1.9%. Under R50 with c=0.69, expected ≈ 0.36 × p90 + 0.64 ×
  p50 — drains modestly, doesn't bottom-out battery.

  Default fallback: when Solcast doesn't expose `analysis.confidence`
  (test environments, data unavailable), treat as 0.9 → use overflow_p90
  exactly as pre-R50. R50 only changes behaviour when real confidence data
  is present and below HIGH threshold.

  Tunable thresholds via two input_number helpers (curtailment_confidence
  _high, curtailment_confidence_low) exposed on the dashboard. Constraint
  enforced in plugin: 0 ≤ low < high ≤ 1.

- **R52** (v22 pre-PV drain timing): activate the plugin BEFORE sunrise on
  confirmed-overflow days so we drain at full DNO rate while drain capacity
  is uncontested by PV. Two-stage drain:
    - Pre-PV: target = `soc_keep + buffer_pct × soc_max` (default 20%)
    - Post-PV: target = R50 floor (existing behaviour)

  Decision flow inside the existing "no PV yet" early return:

  ```text
  if input_boolean.gshp_ch_active is on:
      # Winter — protect overnight battery for heat pump load
      Off
  if overflow_p90 < 1 kWh:
      # No meaningful overflow forecast → no need to drain
      Off
  if SOC ≤ target_at_pv_start:
      # Already at/below pre-PV target
      Off

  pv_start_utc = compute_pv_start_time(p90_scale, ..., threshold=0.5 kW)
  drain_amount_kwh = SOC_now − target_at_pv_start
  drain_minutes = drain_amount / DNO × 60
  drain_start_utc = pv_start_utc − drain_minutes

  if now < drain_start_utc:
      Off (waiting; not enough time would have been wasted)
  else:
      Active, target = target_at_pv_start (pre-PV drain phase)
  ```

  After PV starts (`actual_pv ≥ 0.1`), normal flow resumes — R50 floor calc
  applies and battery drains further to the deeper R50 floor. The pre-PV
  drain only handles the FIRST stage (high SOC → target_at_pv_start).

  Why two-stage: pre-PV drain rate is 4 kW (DNO uncontested). Post-PV drain
  rate falls as PV ramps (PV uses DNO bandwidth). Splitting the drain target
  exploits this — coarse drain pre-PV, fine drain post-PV.

  Helpers:
    - `input_boolean.gshp_ch_active` — central heating active flag (manual
      toggle in pump room, or HA dashboard tile).
    - `input_number.curtailment_pre_pv_buffer_pct` — buffer above soc_keep
      (default 20, range 0-50).

  Reference incident: 2026-04-29. Plugin activated only at first PV (~05:12
  BST), drained from 70% → 24% during 05:12-08:12 BST. Should have started
  ~03:30 BST and finished pre-PV drain at PV start (~05:00 BST), with
  remainder draining post-PV — overall same total drain but no wasted
  capacity in the first 1.5 hours.

  Why the buffer (not drain to 0% pre-PV): if PV is delayed by clouds,
  battery has 3.6 kWh = 7h of base load buffer. Without it, plugin could
  drain to 0% then bleed via base load before sun arrives.

- **R49** (v20 dynamic buffer reduction): on confirmed-cloudy afternoons,
  scale `effective_max_reserved` down to `max(0.5, MAX_RESERVED_KWH × 0.7)`
  = 1.26 kWh. Reduction fires only when ALL hold:
    1. `minutes_now ≥ 14:00 local` — DHW typically done, peak likely past.
    2. `solcast_so_far > 10 kWh` — enough sunlight elapsed to make ratios
       statistically meaningful.
    3. `cumulative_ratio = SIG_DAILY_PV / (SOLCAST_TODAY − SOLCAST_REMAINING)
       < 0.9` — actual PV tracking ≥10% under forecast for the whole day.
    4. `recent_ratio = (Δ actual PV last 60 min) / (Δ solcast_so_far last
       60 min) < 0.95` — the most recent hour confirms the trend. Without
       this gate, the reduction would mis-fire when clouds clear after 15:00
       (cumulative still low, but afternoon will deliver).
  Why: Solcast over-forecasted today → the headroom we're reserving for an
  overflow that isn't materialising is wasted SOC. Reducing buffer raises
  max_target_soc by ~3%, letting the battery aim higher rather than ending
  the day with avoidable shortfall. The 0.7× factor (not 0.5×) and the 0.5
  kWh floor keep some safety margin against late-afternoon clearing. Reason
  for codifying: 2026-04-26 the day under-delivered on PV; with full 1.8
  kWh buffer the plugin held at ~93% target while battery was actually
  going to fill to 100% — user manually overrode to Charge. This rule lets
  the plugin decide automatically.
  PV history is kept in-memory only (rolling 75-min window) — after a plugin
  restart we wait one hour before recent_ratio is available. Cumulative
  ratio still works immediately on restart, but the gate requires both.
- **R12**: At safe_time, remaining_overflow = 0, floor = soc_max. Plugin deactivates.
- **R13**: Floor rises naturally each cycle as the integral shrinks (time passing,
  sin(elev) falling). Rises faster on cloudy days (actual peak < p90 → scale updates
  down → integral smaller → floor higher sooner).

## Control — Three Phases (HA automation, 5-second cycle)

Phase selection uses **Schmitt-trigger hysteresis**: the OUT transition (entering
Drain or Charge from Hold) requires SOC to exceed an outer threshold;
the IN transition (returning to Hold) only requires SOC to cross the target.
Drain and Charge therefore run **all the way to target**, not just to the
hysteresis edge — this avoids stopping short of target and re-entering on the
next minor SOC drift.

- **R14**: **Drain** (active when current_phase=Drain): export = DNO. SIG
  discharges to grid toward `target_kwh`. Exit to Hold when `SOC ≤ target`
  (drains all the way to target before yielding).
- **R15**: **Hold** (entry / steady state): export = min(excess, DNO). Battery
  absorbs overflow above DNO naturally.
    - Exit to Drain when `SOC > target + OUTER_THRESHOLD_KWH`
    - Exit to Charge when `SOC < target − OUTER_THRESHOLD_KWH`
- **R16**: **Charge** (active when current_phase=Charge): export = 0.
  Battery charges from sub-DNO PV toward `target_kwh`. Exit to Hold when
  `SOC ≥ target` (charges all the way to target).
- **R16a**: `OUTER_THRESHOLD_KWH = 0.18 kWh` (≈1% of soc_max). Sized to be
  robust to Sigen SOC 0.1% quantisation (~0.018 kWh), so SOC noise alone
  cannot pop us out of Hold. Tighter than the original 0.5 kWh design — the
  Schmitt run-to-target behaviour means tighter outer threshold no longer
  causes flap, because once Drain/Charge is engaged it commits to the target.
- **R17**: All active states use D-ESS mode. MSC only when off (R6).
- **R18**: HA automation (5-sec) handles real-time export control AND publishes live
  phase (Charge/Drain/Hold/Off, plus Manual Charge/Hold/Drain when override is set)
  to `input_text.curtailment_live_phase`. Plugin (5-min) computes floor, sets
  D-ESS mode, publishes Active/Off. Plugin sets live phase to Off on deactivation.
- **R38**: Plugin `export_target` sensor publishes:
    - `-2` when plugin is Off (signals MSC handoff to yaml).
    - `dno_limit_kw` when plugin is Active.
  The yaml uses this as the cap fed into the Hold/Drain `new_limit` calc and
  uses `< 0` as the Off detector. Plugin does NOT publish 0.0 to signal Charge:
  the yaml Hold path would interpret `export_cap=0` as "clamp Hold to 0",
  defeating Hold semantics. Charge/Hold/Drain phase selection is done in the
  yaml from SOC vs target (Schmitt-trigger, R14-R16) — plugin has no
  override on phase. Plugin's role is "publish the cap the yaml should
  enforce when it decides to export".

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
