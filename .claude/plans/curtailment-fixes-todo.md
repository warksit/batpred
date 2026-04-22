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

### Bug 8: Relaxed soc_keep while PV covers load (one-way ratchet)

**Rationale:** The purpose of `soc_keep` is to reserve battery for short-term
load without grid import. When PV clearly exceeds load, that reservation
isn't needed *right now* — we could use the capacity for overflow absorption.
Once the battery has recovered the reserve, lock it.

**Logic:**
```python
RELAXED_KEEP_KWH = 0.5
PV_MARGIN_KW = 1.0  # PV must exceed load by this to be "clearly covering"

# One-way ratchet: once battery reaches full reserve, lock it for rest of day
if soc_kw >= soc_keep_base and not self._keep_recovered:
    self._keep_recovered = True

# Determine effective keep for the floor clamp
if (actual_pv - actual_load) > PV_MARGIN_KW and not self._keep_recovered:
    effective_keep = min(soc_keep_base, RELAXED_KEEP_KWH)
else:
    effective_keep = soc_keep_base

floor = max(overflow_floor, max(effective_keep, reserve))
```

`_keep_recovered` persists via state file (R47), resets on day rollover.

**Benefit on big-overflow days:** ~1 kWh of extra overflow absorption in the
morning before battery reaches reserve. On today's overflow (~23 kWh vs
~14 kWh absorbable room at 8% floor), that's meaningful.

**Risk:** if PV crashes before battery recovers (PV spike then cloud),
battery could be at 0.5 kWh with no reserve for evening load. Mitigations:
the PV_MARGIN_KW hysteresis requires sustained PV > load, and the lock fires
the moment SOC hits `soc_keep_base`. Still worth testing with cloudy-morning
scenarios before deployment.

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
