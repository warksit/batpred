# Curtailment Fixes TODO

Captured across multiple debugging sessions. Do NOT deploy mid-day — every
deploy restarts Predbat, which resets state (now partly mitigated by R47
persistence, but safer to deploy overnight).

## Open items (ordered by priority)

### Bug 6: R4 defer-to-charge-window needs hysteresis

**Symptom (2026-04-22 06:59 BST):** Plugin flipped Active→Off→Active over
3 minutes because SOC (3.3 kWh) was 0.1 kWh below `best_soc_keep` (3.4 kWh).
Log: `Curtailment: deferring to charge window (SOC 3.3 < keep 3.4)`.

Not dangerous but causes a spurious `target_soc: 100%` transient and a brief
MSC switch.

**Fix:** Add ±0.2 kWh hysteresis to the R4 defer check:
- Defer when `soc_kw < soc_keep - 0.2`
- Release when `soc_kw >= soc_keep + 0.2`

### Bug 8: Relaxed soc_keep (two conditions, one-way ratchet)

**Rationale:** `soc_keep` reserves battery for short-term load without grid
import. Safe to relax ONLY when BOTH:
1. The forecast overflow won't fit in the available room → we NEED the room
2. PV is currently above load → dropping keep won't force grid import

Once SOC has recovered to base keep, lock it back for the rest of the day.

**Logic:**
```python
RELAXED_KEEP_KWH = 0.5
PV_MARGIN_KW = 0.5  # PV must exceed load by this for "safe to relax"

# Condition 1: forecast overflow exceeds room with normal keep + R45 cap
room_with_base = (soc_max * 0.9) - soc_keep_base
needs_room = remaining_overflow * OVERFLOW_SAFETY_FACTOR > room_with_base

# Condition 2: PV currently covers load (dropping keep won't cause import)
pv_covering = (actual_pv - actual_load) > PV_MARGIN_KW

# One-way ratchet: once SOC reaches base keep, lock for rest of day
if soc_kw >= soc_keep_base:
    self._keep_recovered = True

# Effective keep
if needs_room and pv_covering and not self._keep_recovered:
    effective_keep = RELAXED_KEEP_KWH
else:
    effective_keep = soc_keep_base

floor = max(overflow_floor, max(effective_keep, reserve))
```

`_keep_recovered` persists via state file (R47); reset on day rollover.

**Benefit:** On big overflow days, ~1 kWh extra headroom during the morning
drain window. Zero effect on moderate/small days (needs_room=False) or
pre-dawn (pv_covering=False).

**Risk:** if PV crashes right after the relax triggers and before SOC
recovers, battery sits at 0.5 kWh with reduced afternoon cushion. Both
conditions must flip to False for relax to end; but `_keep_recovered`
effectively latches the restored 1.5 once SOC reaches it. Worth testing
cloudy-morning and mid-day-cloud scenarios before deploy.

### Bug 9: LoadML reliability monitoring

**Motivation:** Curtailment formula (R9a) trusts LoadML for per-slot load
prediction. Multiple safety layers protect against LoadML errors (R45 cap,
1.1 safety factor, R43 max-scale) but we currently have NO visibility when
LoadML is going sideways. The ~3 kW flip between yesterday's and today's
afternoon forecast was only caught because the user spotted it.

**Minimum: a "LoadML trust" sensor**

Publish `sensor.predbat_load_ml_accuracy` with attributes:
- `yesterday_mae_kwh` — mean absolute error between yesterday morning's
  LoadML forecast and yesterday's actual load, bucketed per hour
- `yesterday_peak_error_kwh` — worst single-slot error
- `forecast_vs_historical_divergence_pct` — today's forecast vs a simple
  rolling 7-day average (exposes when ML drifts away from typical pattern)
- `status` — string: "normal" | "elevated_error" | "suspicious" based on
  thresholds (e.g. MAE > 0.5 kWh/slot → suspicious)

**Nice-to-have: automatic safety factor bump**

If `status != "normal"`, curtailment plugin auto-increases
`OVERFLOW_SAFETY_FACTOR` from 1.1 → 1.3 for the day. Self-healing — when
LoadML is unreliable the floor gets more conservative automatically.

**Implementation notes:**
- Error computation: store yesterday's LoadML forecast snapshot at
  end-of-day, compare to actual load history today.
- Divergence: don't need to re-implement rolling average — Predbat already
  uses `days_previous` averaging internally; query that.
- Alert via HA notification when status flips to "suspicious".

### Bug 4: `on_before_plan` clock-based heuristic

Current: switch to tomorrow's forecast at 23:00 BST hardcoded. Fine in April,
wastes 7+ hours in winter.

**Fix:** Switch after PV has been < 0.1 kW for 30 min (observation-based).
Not urgent — winter improvement.

### Bug 5: Diagnostic visibility gaps

**Add to phase sensor attributes:**
- `peak_pv_kw` — observed daily peak
- `peak_pv_time` — minutes-since-midnight
- `actual_scale` — derived from peak_pv
- `last_decision` — short reason string

**Rotate state file per day** (cheap postmortem): write also to
`curtailment_state_{YYYY-MM-DD}.json`, keep last N days.

**Optional daily summary sensor**: publish
`{peak_pv, actual_overflow, floor_max, sunset_soc, activation_time,
deactivation_time}` at safe_time.

Zero operational impact — add after a few stable days.

## Completed items

- **Bug 1** (done 2026-04-21 eve): `safe_time`-based deactivation (R46). Uses
  solar geometry ground truth instead of LoadML-polluted integral.
- **Bug 2** (done 2026-04-21 eve): State persistence (R47).
  `curtailment_state.json` survives restarts, keyed by date.
- **Bug 3** (done 2026-04-21 eve): R11 ratchet bypass when `floor_scale`
  rises (R43-triggered safety path).
- **Day-rollover reset** (done 2026-04-21 eve): If `_state_date != today`
  at start of calculate(), reset in-memory daily state.
- **Bug 7** (done 2026-04-22 morning): Ratchet only the overflow-derived
  floor; soc_keep/reserve applied as dynamic clamps after ratchet. Fixes
  battery over-reservation when cold weather boost ends mid-day.

## Key insight: never deploy mid-day

Restart risk is partly mitigated by R47 persistence, but deploys still
interrupt the plugin cycle and reset caches (`_cached_keep`, etc.).
Rule of thumb: only deploy when plugin is Off (overnight).
