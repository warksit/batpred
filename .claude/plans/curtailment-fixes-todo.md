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

### Bug 7: Ratchet captures soc_keep inflation

**Symptom (2026-04-22):** Cold weather plugin boosts `best_soc_keep` during
04:00–07:00 (GSHP protection). At 07:00 the boost drops (3.4 → 1.5 kWh), but
the curtailment ratchet has already captured 3.4 as the floor and holds it,
artificially restricting battery ~2 kWh for the rest of the day.

**Fix:** Ratchet only the overflow-derived floor, not the soc_keep-clamped
floor. Structure:

```python
overflow_floor = soc_max - remaining_overflow * OVERFLOW_SAFETY_FACTOR
overflow_floor = min(overflow_floor, soc_max * 0.9)  # R45 cap

# Ratchet only the overflow reservation
scale_rose = floor_scale > self._last_floor_scale + 0.01
if self._floor_ratchet is not None and not scale_rose:
    overflow_floor = max(overflow_floor, self._floor_ratchet)
self._floor_ratchet = overflow_floor

# Apply dynamic clamps AFTER ratchet
floor = max(overflow_floor, max(soc_keep, reserve))
floor = min(floor, soc_max)
```

soc_keep and reserve become dynamic clamps — when they drop (cold boost ends,
GSHP cycle ends), floor can drop too. When they rise, floor rises. The
real "no, we've committed this headroom" is the overflow ratchet only.

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

## Key insight: never deploy mid-day

Restart risk is partly mitigated by R47 persistence, but deploys still
interrupt the plugin cycle and reset caches (`_cached_keep`, etc.).
Rule of thumb: only deploy when plugin is Off (overnight).
