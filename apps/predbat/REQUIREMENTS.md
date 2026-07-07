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

---

## v20 Redesign Delta (2026-05-02)

This appendix supersedes parts of R1-R52 above. When in conflict, this
section wins. Triggered by today's failure mode: clear morning + cloudy
afternoon caused the plugin to extrapolate `actual_scale` over the whole
day, predicting 16 kWh of overflow that wouldn't materialise (real
forecast: rain by 16:00). Plugin drained battery to 2.8% target and
"manage manually" was needed.

Goals of v20:

1. Use Solcast's day-shape forecast directly instead of clear-sky
   geometry from a single scalar. Solcast already knows about the
   afternoon clouds and rain.
2. Stop chasing 100% SOC at end of day. Drain to overnight need
   (= effective `soc_keep`) so end-of-day excess is exported in the
   evening (high grid value) rather than sitting at 100%.
3. Single drain-target rule: `target = min(curtailment_floor, soc_keep)`.
   Both are "drain to" levels; lower wins.
4. Plugin runs while PV > 0, not until safe_time. Evening drain to
   `soc_keep` happens through the late afternoon.

### Changed Goal

> Prevent grid export exceeding 4kW DNO limit while delivering enough SOC
> by sunset to cover overnight + tomorrow's morning gap. Excess above the
> overnight requirement is exported during the PV window (preferring
> evening for grid value).

### Triage of R1-R52

**Kept unchanged (✅):** R1, R2, R3, R4, R8, R14, R15, R16, R16a, R17, R18,
R26, R27, R28, R29, R30, R34, R35, R36, R37, R38, R44, R47, R48, R49.

**Amended (✏️):**

- **R5** — activation condition becomes "is there work to do?". Plugin
  is Active when `target_soc < soc_max` (i.e. drain target below full)
  AND there is PV (or pre-PV drain conditions per R52 hold). Drop the
  "battery won't reach 100% even with all PV" gate from old R5.
- **R6** — deactivate at `PV ≤ 0.1 kW` (effective sundown), not at
  `safe_time`. After overflow window, plugin continues running to drain
  toward `soc_keep` through the evening.
- **R7** — REMOVED — superseded by R53 (Solcast per-slot is the basis).
- **R9** — same formula shape; `remaining_overflow` is now sourced from
  R53 (Solcast per-slot integral) not solar geometry. Tapered-cap part
  removed (R45 superseded by R57). Result: `curtailment_floor =
  max(0, soc_max − remaining_overflow × OVERFLOW_SAFETY_FACTOR)`.
- **R9a** — strengthened. `effective_load(t) = max(base_load,
  smoothed_loadml(t))` where `smoothed_loadml = rolling_mean(loadml,
  60min)`. The unsmoothed LoadML noise was the v5 failure mode; smoothing
  it lets us safely use Solcast per-slot shape (R53) without re-breaking
  v5.
- **R10** — final clamp becomes
  `target = max(min(curtailment_floor, effective_keep), reserve)` where
  `effective_keep` is `soc_keep` after R26+R48 adjustment. soc_keep is
  no longer added to the `max` clamp directly — it's inside the `min`.
  Reason: on big-overflow days R48 already drops `soc_keep` to ~2.8%
  so the inner `min` correctly drains low. On normal days `soc_keep`
  caps the drain via the inner `min`.
- **R11** — ratchet still applies, but to the OVERFLOW component only.
  When overflow integral falls and `target` switches over to
  `effective_keep` (curtailment_floor exceeds keep), no ratchet on the
  keep component — it can rise/fall freely as Predbat plan changes.
- **R13** — keep concept; integral is now Solcast-shaped (R53).
- **R19** — safe_time now demoted from deactivation trigger to
  diagnostic. Defined as "first time `remaining_overflow_integral = 0`".
  Used for sensor display; not used for control.
- **R20, R21** — keep semantics, but only relevant for the safe_time
  diagnostic now (no functional consequence).
- **R39** — keep concept; integral reference updated to R53.
- **R42** — scale stops being structural. Kept only as a calibration
  knob feeding R58 (live recalibration of next ~30 min of slots).
- **R43** — REPLACED by R58. Old `floor_scale = max(p_scale,
  actual_scale)` collapsed p10/p50/p90 into one number whenever actual
  exceeded any band, destroying the spread that R50 needs.
- **R46** — REMOVED — its purpose (LoadML phantom
  underestimating overflow) is addressed at source by R9a smoothing
    - R53 Solcast slots. Deactivation rule moves to R6 (PV ≤ 0.1).
- **R50** — operates on per-slot Solcast bands (`pv_estimate10` /
  `pv_estimate` / `pv_estimate90` summed per band), not three copies
  of `max(p_scale, actual_scale)`. Confidence blending unchanged.
- **R52** — pre-PV drain stays. Pre-PV target reformulated:
  `min(soc_keep + buffer, effective_keep)`. The two-stage mechanic
  (coarse pre-PV drain at full DNO, fine post-PV drain) is unchanged.

**Removed (❌):**

- **R7** — see above.
- **R12** — "at safe_time, floor = soc_max, plugin deactivates". Both
  parts gone: floor → effective_keep, plugin runs to PV ≤ 0.1.
- **R45** — tapered cap to 100% at safe_time. The "fill battery before
  MSC handoff" mechanism is exactly the behaviour we're removing.
  Replaced by R57.
- **R46** — see Amended.

### New Requirements

- **R53** (overflow integral source). The remaining-overflow integral
  uses Solcast per-slot pv_estimate kWh, integrated forward from now to
  end of PV. Form:

  ```text
  remaining_overflow = Σ_slots max(0,
                          solcast_slot_kwh
                          − effective_load_kwh(slot)
                          − dno_kwh_per_slot)
  ```

  Per band (R50): the same integral with `pv_estimate10` /
  `pv_estimate` / `pv_estimate90`. The clear-sky `scale × sin(elev)`
  model is no longer used inside the integral. Solcast already encodes
  the day-shape (cloud, rain, ramp), and discarding shape was the
  v18 failure mode.

- **R54** (single drain-target rule). At every plugin cycle:

  ```text
  target_soc = max(min(curtailment_floor, effective_keep),
                   reserve, DEEP_DISCHARGE_FLOOR_KWH)
  ```

    - `curtailment_floor` from R9 (Solcast-shaped via R53).
    - `effective_keep` is `soc_keep` after R26 (plan-time reduction)
    and R48 (live big-overflow relaxation latch).
    - `reserve` is the absolute physical floor (battery/inverter limit).
    - `DEEP_DISCHARGE_FLOOR_KWH = 0.5` — the drain target never
    falls below this regardless of `reserve` or overflow size.
    - `min` because both numbers are "drain TO this level"; lower wins.
    - `max` clamp guarantees we never request below `reserve` nor below
    the deep-discharge floor.

  Trade-off: when `effective_keep < curtailment_floor` (modest overflow
    - low overnight need), the rule drains slightly lower than curtailment
  strictly requires. Accepted in exchange for a single uniform rule
  across the day with no phase switch.

  **Deep-discharge floor (2026-05-19).** On an extreme-overflow day
  `curtailment_floor` (= `overflow_floor`) goes to 0 and R48 has relaxed
  `effective_keep` to 0.5 kWh. The inner `min(0, 0.5)` is 0, and with
  Predbat's `reserve` also 0 the drain target reaches absolute empty —
  observed live 2026-05-19 with the battery at 0.0% SOC. R48 deliberately
  relaxes keep to 0.5 (not 0); the inner `min` must not undo that. The
  `DEEP_DISCHARGE_FLOOR_KWH` (0.5 kWh ≈ 2.8% of soc_max) term in the
  outer `max` keeps a deep-discharge buffer. 0.5 kWh of headroom is
  negligible against a multi-kWh overflow (the battery is slammed full
  mid-day regardless) but protects the cell from a full bottom-out. This
  applies only to the drain target (`compute_drain_above` /
  `sensor.predbat_curtailment_drain_above`); the published `charge_below`
  is separately clamped to `soc_keep`.

- **R55** (overnight target sourced from morning gap).
  `effective_keep` is set in `on_before_plan` (R26) to
  `morning_gap + R55_MARGIN_KWH` where `morning_gap =
  compute_morning_gap(tomorrow_pv, tomorrow_load)` and
  `R55_MARGIN_KWH = 0.5`. R48 may further relax effective_keep on
  big-overflow days via the existing latch (down to 0.5 kWh).
  Published as a sensor (`sensor.predbat_curtailment_overnight_target`)
  for dashboard visibility.

- **R56** (plugin active while PV > 0). The plugin is Active for the
  whole PV window (R52 pre-PV drain → through PV peak → through
  late-afternoon drain to `effective_keep`) until `pv_power ≤ 0.1 kW`.
  After PV stops, plugin deactivates and Predbat MSC takes over for
  overnight. Drain mode through the late afternoon will pull from
  battery to grid (round-trip cost) — accepted because evening kWh has
  higher grid value than midday curtailment, so net positive.

- **R57** (no 100% chase). Plugin never targets `soc_max` as the drain
  target. End-of-day SOC ≈ `effective_keep` on most days. Battery only
  reaches 100% if PV physically overcharges past the cap (e.g. a true
  no-load mid-day with battery already at `effective_keep`). R45
  superseded.

- **R58** (actual_scale as live calibration only). `actual_scale` is
  applied as a multiplier to the next 30 min of Solcast pv_estimate
  slots, capped at 1.5×. Beyond 30 min, Solcast slots are used as-is
  (preserving day-shape). Replaces R43's global override which
  collapsed p10/p50/p90 to a single value whenever actual exceeded p90.

  ```text
  if actual_scale > 0 and within next 30 min:
      slot_kwh_used = solcast_slot_kwh × min(1.5, actual_scale_ratio)
  else:
      slot_kwh_used = solcast_slot_kwh
  ```

  where `actual_scale_ratio = actual_pv_last_30min / solcast_last_30min`.

### Order of work (TDD)

For each item, write a FAILING test first (R36), then code, then
verify all existing tests still pass (R37 — never break production).

1. **R9a smoothing** (foundation for R53). Test: noisy LoadML with
   1 kW transient should not change the integral by more than 5%.
2. **R53 per-slot integral**. Test fixture: today's actual data
   (clear morning, rain afternoon). Old code returns ~16 kWh;
   new code should return < 2 kWh.
3. **R55 overnight target sensor**. Test: with mild overnight forecast
   `morning_gap = 4 kWh`, sensor publishes `4.5 kWh / 25%`.
4. **R54 single rule**. Test matrix from triage examples 1-4:
   target should be `min(curt, keep)` clamped above reserve.
5. **R57 / R45 removal**. Test: plugin never targets `soc_max` after
   `remaining_overflow → 0`. Target falls to `effective_keep`.
6. **R56 plugin active until PV=0**. Test: at 16:00 with overflow=0,
   plugin still Active and Drain mode if SOC > effective_keep.
   Plugin Off at PV=0.
7. **R58 actual_scale live calibration**. Test: `actual_scale=2.0`
   only multiplies next 30 min of Solcast slots; remaining-day shape
   preserved. Cap at 1.5× respected.
8. **R50 per-slot bands**. Test: p10 / p50 / p90 overflow integrals
   produce DIFFERENT values when fed Solcast bands with realistic
   spread (not collapsed by R43, which is removed).

### Items still flagged for discussion

- **R49** kept for now (user decision). Re-evaluate after R53 +
  R50-on-bands ship — if they fully address the "Solcast over-
  forecasted today" failure mode, R49 becomes redundant.
- **R48** kept for now (user decision). The relaxed-keep latch is
  what makes target=2.8% work on huge-overflow days under R54.
- **Round-trip loss in evening drain** (R56). Empirical question:
  on a no-overflow but high-SOC day, is evening drain from battery
  to grid actually net-positive? Worth instrumenting after deploy.

---

## Proposed additions (2026-05-06, pure functions tested, plugin wiring deferred)

After investigating today's curtailment performance, two gaps identified
in R54. Pure helper functions added to `curtailment_calc.py` with full
unit-test coverage; plugin integration is a follow-up change.

### R59 — P10 recovery floor (lower bound on R54 floor)

The current R54 formula:

```text
target = max(min(curt_floor, effective_keep), reserve)
```

ensures we drain to *at least* `reserve`, but doesn't ensure we'll
actually recover to `overnight_target` by sundown on a worst-case (P10)
PV day. R55 sources `effective_keep` from *tomorrow's* morning gap,
not from *today's remaining* PV runway. So on a confirmed-overflow day
where R48 relaxes effective_keep to 0.5 kWh, we drain to 0.5 and
*assume* PV will refill — if the day delivers P10 instead of P50, we
end below overnight target.

**R59**: add a P10 recovery lower bound:

```text
p10_charging_potential = max(0, p10_pv_remaining_kwh - load_remaining_kwh)
p10_recovery_floor = max(0, overnight_target_kwh - p10_charging_potential)

target_soc = max(reserve, p10_recovery_floor, min(curt_floor, effective_keep))
```

Pure function `compute_p10_recovery_floor()` — passes seven unit
tests covering huge-runway / no-runway / partial / load>PV / zero-target /
today's actual data / combined-with-R54.

Behaviour:

- Sunrise (lots of P10 PV ahead): p10_recovery ≈ 0 → outer max yields
  inner min (no change vs current)
- Mid-afternoon (less ahead): rises, starts capping how low keep can go
- Sunset (P10 PV → 0): p10_recovery → overnight_target → forces SOC up
  to target by sunset (replaces R57's "drain to keep, hope PV refills")

### R60 — effective export cap for overflow integral

The overflow integral asks "how much PV will exceed our export ability?"
and uses `dno_limit=4.0` as the export ceiling. But the voltage throttle
constrains real export below DNO whenever grid voltage rises. Reference:
on 2026-05-06 between 14:50 and 15:50 BST mean export was 2.92 kW —
27% under DNO. Forecast overflow using DNO=4.0 understates actual
curtailment by the same ratio.

**R60**: feed the overflow integral a smoothed effective DNO instead:

```text
effective_dno = compute_effective_export_cap(
    today_samples_kw,        # rolling 30-min cap readings during PV>load
    yesterday_avg_kw,        # persisted across days
    dno_kw=4.0,
    min_samples=10,
    hard_floor_kw=2.0,
)
```

Three-regime fallback:

1. ≥ min_samples today → today's mean (clamped [hard_floor, DNO])
2. else yesterday's daytime mean
3. else DNO (cold start, no persisted data)

**Why both regimes**: at 06:00 BST we have no today data, so use
yesterday's. By midday today's data dominates — yesterday is ignored.

**Why hard_floor**: a single bad voltage hour shouldn't predict
"no export at all" tomorrow. 2.0 kW floor preserves *some* DNO
contribution to the forecast.

Pure function `compute_effective_export_cap()` — passes eight unit tests
covering all three regimes plus clamps.

### Plugin wiring (done 2026-05-06)

Both R59 and R60 wired into `curtailment_plugin.py`:

- **State**: `_cap_samples` (deque, last 6 = 30 min), `_cap_samples_full_day`
  (list, full-day samples), `_yesterday_cap_avg` (float, persisted),
  `_effective_dno` (float, computed each cycle), `_p10_recovery_floor`
  (float, computed each cycle).
- **Sampling**: `voltage_throttle_filtered_cap` read each cycle. Filtered
  to `actual_pv > 0.5 kW` so idle hours don't dilute the daytime mean.
- **State persistence**: yesterday_cap_avg, cap_samples,
  cap_samples_full_day round-trip through `_load_state` / `_save_state`.
- **Day rollover** (`_reset_for_new_day`): rolls today's full-day mean
  into `_yesterday_cap_avg`, clears today's lists.
- **Today's overflow integral**: passes `self._effective_dno` to all three
  `_compute_overflow_band` calls in calculate() and
  `_publish_forecast_overflow`.
- **R54 floor formula** updated to:

  ```text
  floor = max(reserve, p10_recovery, min(overflow_floor, effective_keep))
  ```

- **Tomorrow's forecast** uses `compute_effective_export_cap` against
  `_cap_samples_full_day` (today's just-completed daytime mean) with
  yesterday fallback. `excess` now subtracts realistic exportable_kwh
  before comparison to headroom — was previously assuming all PV-load
  could exit (over-optimistic).
- **Diagnostic attributes** on `sensor.predbat_curtailment_phase`:
  `effective_dno_kw`, `p10_recovery_floor_kwh`, `yesterday_cap_avg_kw`,
  `cap_samples_today`. Tomorrow sensor adds `exportable_kwh` and
  `tomorrow_eff_dno_kw`.

All 138 curtailment tests pass (15 new + existing). Pure-function unit
tests cover the math; integration tests cover the floor formula change
through real-day CSV fixtures.

## Day-Shape Scenario Test Matrix (2026-05-08)

Every proposed change to charge_below / drain_above / phase logic must
be reasoned through these five canonical day shapes before merging.
Asymmetric days (sunny→cloudy, cloudy→sunny) are the critical guard
cases — naive blending and naive past-tracking ratios both fail there.

**Design choice 2026-05-11:** charge_below uses Solcast P10 (pessimistic)
remaining estimate directly. Reverts the 2026-05-08 P50 choice.

Rationale: once SOC crosses overnight_target the Hold/Drain logic exports
the surplus, so over-charging by a kWh or two costs at most one battery
round-trip (~10%). Under-charging costs the full overnight import bill
plus comfort risk. Asymmetric cost → choose the defensive quantile.

We do NOT apply a calibration ratio (last 30 min actual / solcast):

- The past doesn't predict the future on shape-changing days
  (sunny→cloudy invalidates a high morning ratio for the afternoon)
- Solcast already revises P10 through the day as actual conditions
  clarify; layering a ratio on top second-guesses Solcast's own
  time-aware model

For each day shape, document:

1. Expected SOC trajectory
2. Expected charge_below trajectory
3. Expected drain_above trajectory
4. Pitfalls (what the wrong logic would do)

### Scenario 1 — On-forecast day (Solcast P50 ≈ actual, P10 below)

- Profile: PV tracks Solcast P50 ±10% all day.
- charge_below: moderate early (P10 is conservative; floor reflects
  pessimistic outlook). Eases as P10 PV remaining shrinks toward
  sunset and actual PV is banked.
- drain_above: tracks `min(overflow_floor, effective_keep)`. On a
  bright day overflow_floor wins (low value); on a normal day
  effective_keep wins (= overnight target).
- Phase: morning Charge to charge_below as a safety buffer, then
  Hold/Drain as actual PV exceeds the P10 line.
- Pitfall: small extra round-trip on days that turn out P50+. Cost
  is ~10% of the over-charged slice — accepted as insurance.

### Scenario 2 — Under-forecast day (actual < Solcast)

- Profile: clouds materialise that Solcast didn't predict. Solcast P10
  remaining revises down through the morning to catch up.
- charge_below: P10 already pessimistic, so morning floor is already
  high enough to force-charge well before deficit becomes critical.
  As P10 revises down further the floor rises more, but most of the
  defensive charging happened earlier.
- drain_above: stays at curtailment-buffer floor — overflow probably
  won't materialise.
- Phase: morning Charge ensures SOC reaches target even on worst-case
  forecast.
- Pitfall: minimal — P10 is designed for this case.

### Scenario 3 — Over-forecast day (actual > Solcast)

- Profile: clearer than Solcast predicted. P10 remaining stays low
  (conservative) until Solcast revises up.
- charge_below: P10-based, so it stays elevated even as actual PV
  pours in. Some "wasted" defensive charging happens, but once SOC
  crosses target the Drain phase exports the surplus.
- drain_above: as actual overflow develops, overflow_floor lowers
  (R50 confidence weighting on the curtailment side handles this).
  drain_above tracks the overflow buffer requirement.
- Phase: morning Charge to P10 floor; Drain as overflow develops and
  SOC exceeds target.
- Pitfall: defensive charging cost ~10% on the over-charged slice.
  Accepted.

### Scenario 4 — Sunny morning, cloudy afternoon (front-loaded)

- Profile: clear sunrise → high PV early → clouds 11:00-13:00 → little
  afternoon. Solcast P10 should reflect this from sunrise.
- charge_below: starts elevated (P10 already accounts for afternoon
  clouds). Falls as morning PV banks.
- drain_above: drain target tracks overflow_floor. Drain may fire
  morning when battery fills from sunny-morning surplus.
- Phase: morning Charge to P10 floor; Hold/Drain midday; SOC already
  comfortable for afternoon clouds.
- Pitfall: a calibration ratio approach would say "ratio=1.3 morning,
  trust = scale up afternoon forecast" — wrong, the afternoon clouds
  are already in the forecast. Direct P10 avoids this trap.

### Scenario 5 — Cloudy morning, sunny afternoon (back-loaded)

- Profile: clouds sunrise → low PV early → clears 11:00 → high PV
  afternoon. Solcast P10 should reflect this from sunrise.
- charge_below: starts elevated (P10 won't promise the afternoon
  recovery — it assumes worst case). Triggers morning Charge from
  grid to guarantee target.
- drain_above: rises as Solcast says afternoon overflow likely;
  morning Hold ensures battery has room.
- Phase: morning Charge to P10 floor; Drain mid/late afternoon if
  overflow develops.
- Pitfall: a calibration ratio would say "ratio=0.3 morning, scale
  down afternoon forecast" — wrong, the afternoon sun is already in
  the forecast. Direct P10 avoids this trap.
- 2026-05-08 was this shape. With P10 we'd eager-charge morning →
  round-trip drain afternoon (cost ~£0.10-0.20 + battery wear). The
  alternative — P50-direct — gave 0 morning floor but accepted ending
  below overnight target if actual undershot P50. P10 chooses the
  defensive bet.

### Test Coverage Required

- `test_curtailment.py` pure-function tests: P10 with deficit / surplus /
  zero target / load > P10, plus regression guard that P50 is ignored. ✓
- Integration scenario tests: each of the 5 day shapes simulated end-
  to-end against expected SOC trajectory. **TODO**.
- Real-day CSV fixtures: capture 2026-05-08 (under-forecast cloudy)
  for regression. **TODO**.

### soc_keep floor (added 2026-05-08)

The published `charge_below` sensor is clamped to be ≥ `soc_keep`. Even
when forecast says we'll comfortably exceed overnight target without
intervention, charge_below should never tell the HA automation that
SOC below soc_keep is acceptable — soc_keep represents the minimum
acceptable SOC for comfort/safety, regardless of forecast.

This clamp is applied at publish time only — the R54 floor input
(`_p10_recovery_floor`) is not clamped, so R48's effective_keep
relaxation still works on big-overflow days (where intentionally
allowing SOC < soc_keep absorbs more PV). Two separate concepts:

- `_p10_recovery_floor`: pure forecast-derived recovery requirement
  (input to R54 outer max for drain target)
- Published `charge_below`: clamped to soc_keep (defines what the HA
  automation will force-charge to recover)

### Deep-discharge floor on charge_below (added 2026-06-04)

The soc_keep clamp above evaporates when `on_before_plan` (R26)
relaxes `best_soc_keep` toward 0 on sunny-tomorrow days. On those
days `charge_below = max(p10_recovery=0, soc_keep=0) = 0`, so
`charge_target = min(charge_below, drain_above) = 0`. With SOC = 0
the YAML stays in Hold and exports surplus PV to grid — leaving no
buffer for a load transient. Observed 2026-06-04: battery at 0%,
PV-load surplus 5 kW exporting, kettle (~3 kW) caused a sub-second
grid touch.

Symmetric fix: `compute_charge_below` floors at the same
`DEEP_DISCHARGE_FLOOR_KWH = 0.5` constant used by R54's drain target.

```text
charge_below = max(p10_recovery_floor, soc_keep, DEEP_DISCHARGE_FLOOR_KWH)
```

With SOC = 0 and `charge_below = 0.5`, `charge_target = 0.5` and the
phase flips to Charge: `export = 0`, all PV is directed to battery
until SOC reaches 0.5 kWh. A load transient at low SOC is now
absorbed by redirecting PV (already on-site) rather than from the
grid (round-trip via the export commitment).

Invariant: the system never reports a target SOC below
`DEEP_DISCHARGE_FLOOR_KWH` on either threshold, regardless of how
optimistic the forecast or how low `soc_keep` is.

### Why we accept this design's failure modes

The trade (P10 choice, 2026-05-11):

- **Best case (P10-or-worse day):** overnight target guaranteed,
  no expensive evening grid-fill.
- **Worst case (clearer than P10 day):** defensive charging that
  later round-trips out as Drain-phase export — ~10% efficiency
  loss on the over-charged slice + minor cycle wear.
- **Asymmetric cost calculus:** under-charging costs the full
  overnight import bill plus comfort risk; over-charging costs
  one round-trip on a small slice. Defensive bet wins.

Empirically on 2026-05-08 (cloudy morning, sunny afternoon shape):
P10 triggered ~6 kWh round-trip drain afternoon (£0.10-0.20 + cycle).
P50 would have skipped the morning charge but accepted ending below
overnight target on worse-than-P50 days. We prefer the round-trip
cost over the import-bill exposure.

### R61 — no-surplus drain hold (Option A, 2026-06-15)

The drain target (`effective_keep`) must never request draining below
the CURRENT SOC while PV is not covering load (no genuine surplus):

```text
effective_keep = apply_no_surplus_drain_hold(effective_keep, soc_kw, pv_covering)
# i.e. if not pv_covering: effective_keep = max(effective_keep, soc_kw)
```

**Why.** The R55 overnight target legitimately shrinks toward sunrise
(less battery needed as the morning nears). But `compute_morning_gap`
declares "sunrise" when PV crosses ~0.3 kW sustained — hours before PV
actually exceeds load. Draining to that collapsed target empties the
battery before PV relieves it. Observed 2026-06-15: plugin activated
05:11 BST with target ≈ 0.3 kWh, drained 7.6% → 2.7% to grid, then
imported at standard rate once empty; PV did not exceed load until
~08:44.

**Principle.** Draining exists to make room for *surplus* PV (R25/R52).
With no surplus there is nothing to make room for — hold at (at least)
current SOC. No Drain fires; the battery still covers load naturally in
Hold. Once `pv_covering` becomes true, normal drain-to-target resumes.

**Interaction with R52.** Pre-PV drain is an intentional pre-sunrise
drain on confirmed-overflow days; it uses a separate, separately-gated
path (`_pre_pv_drain_decision`) and does NOT flow through this guard.

Tests: `test_no_surplus_hold_dawn_collapse`,
`test_no_surplus_hold_target_above_soc_unchanged`,
`test_no_surplus_hold_surplus_allows_drain`.

Known residual (future work): the root cause — morning_gap's sunrise
boundary (PV ≥ 0.3 kW instead of PV ≥ load) — remains; the collapsed
pre-dawn overnight_target still feeds the published Off-path target and
R59's recovery input. Fix tracked in master-plan-jul-2026 Phase 1.5.

### R62 — forecast-driven pre-PV drain target (autonomous, 2026-07-07)

R52's pre-PV drain target was `soc_keep + PRE_PV_BUFFER_PCT% × soc_max` — a
static knob. On 2026-07-07 (63 kWh forecast @ 0.92 confidence, ~17-20 kWh
above the effective cap vs 17.6 kWh total battery headroom) it would have
stopped draining at 3.6 kWh, stranding ~3 kWh of headroom on exactly the day
it mattered. The plugin must size the pre-PV drain from the forecast itself —
no manual helper-tweaking the night before.

```text
legacy         = soc_keep + buffer_pct% × soc_max        (unchanged knob)
overflow_floor = max(0, (soc_max − min(1.8, overflow)) − overflow × 1.2)
target         = min(legacy, max(reserve,
                                 DEEP_DISCHARGE_FLOOR + dawn_load,
                                 overflow_floor))
```

- `overflow` = R50 confidence-blended overflow integral against the R60
  effective cap (both already computed pre-dawn by _publish_forecast_overflow).
  Low confidence shrinks overflow → target returns to legacy: the formula can
  only be MORE aggressive than R52, never less.
- `dawn_load` = forecast house load from PV-start until PV covers load — the
  R61 window where the battery still carries the house. Near-zero, never zero.
- Implemented in `compute_pre_pv_target()` (curtailment_calc.py).

**Companion fix (same date):** the pre-PV activation branch now stamps
`_effective_keep_kwh` / `_overflow_floor_kwh` with the pre-PV target and
clears `_p10_recovery_floor`. Previously publish() derived `drain_above` from
YESTERDAY EVENING'S values (e.g. 14.95 kWh after an R61 dusk hold), so the HA
automation would refuse to drain below yesterday's level — pre-PV drain fired
but silently did nothing.

Tests: `test_R62_pre_pv_target_*` (pure), updated `test_R52_pre_pv_drain_*`,
`test_R62_pre_pv_publish_thresholds_not_stale` (stale-leak regression).

Known open items (Phase 1.4): tomorrow-sensor `exportable = eff_dno ×
window` linearisation ignores the battery-absorb timing constraint
(display-only).

**R61 dusk behaviour — DECIDED intentional (2026-07-08).** The no-surplus
hold also stops the R56 late-afternoon drain once evening PV < load. Under
the flat 12p export tariff this is economically CORRECT: dusk drain earns
nothing over exporting tomorrow (flat rate), while tomorrow's overflow room
is R52/R62's job — decided pre-dawn with a fresher forecast. R56's "evening
kWh has higher grid value" rationale belonged to the old deemed-£0 tariff.
Do not "fix" the dusk asymmetry; revisit only if the export tariff becomes
time-of-use.
