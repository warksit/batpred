# Curtailment Fixes TODO (2026-04-21, post-incident)

Captured from live debugging. Do NOT deploy mid-day — every deploy restarts
Predbat, which resets `_peak_pv` and `_floor_ratchet`, which today made things
worse.

## Key realisation: Bug 2 is foundational

Today's plugin-deactivates-too-early problem looked like it was about the
integral/LoadML check, but the root cause was actually the **wrong
`safe_time`** — because a mid-day restart reset `_peak_pv` to 0, `safe_scale`
fell back to p90_scale (R21 requires peak confirmed), and safe_time computed
from p90 gave 16:37 BST instead of the actual-scale-based ~17:20 BST.

Fix Bug 2 first. Without persistent state, every other rule in the plugin
that depends on observed peak (R21 safe_scale, R43 floor_scale, R11 ratchet)
silently resets on restart.

## Bug 2: Persist in-memory state to disk (fix FIRST)

**Symptom:** Every deploy resets `_peak_pv`, `_peak_pv_time`, `_floor_ratchet`
to zero/None. Mid-day restarts lose:
- Tracked peak PV → actual_scale unusable → safe_scale reverts to p90_scale
  → safe_time too early → plugin deactivates 43 min early
- Floor ratchet → allows floor to drop (sometimes helps, sometimes hurts)

**Fix:** Persist to a small JSON file keyed by today's date.

- File path: `/addon_configs/6adb4f0d_predbat/curtailment_state.json`
- Schema:
  ```json
  {
    "date": "2026-04-21",
    "peak_pv_kw": 9.29,
    "peak_pv_time": 754,
    "floor_ratchet": 14.84
  }
  ```
- Write: after any update to these fields (end of calculate())
- Read: on plugin `__init__`, only if `date` matches today
- On date change: ignore file (fresh state for new day)

## Bug 1: Deactivation uses LoadML-polluted integral

**Symptom (today 16:20 BST):** Plugin `phase=Off` while PV 5–6 kW, load 0.5 kW,
still overflowing. Was the LoadML-inflated integral that pushed
`remaining_overflow` below the 0.1 kWh threshold.

**Fix (depends on Bug 2 being done first):** Deactivate based on `safe_time`
rather than the LoadML-polluted integral. `safe_time` (R19) is the moment
solar geometry alone can no longer produce overflow — that's the ground truth
(R25). With Bug 2 fixed, `safe_time` is computed from the correctly-persisted
actual_scale and is reliable.

```python
past_safe_time = utc_hours >= safe_utc

if past_safe_time or not will_fill:
    return soc_max, "off"
```

No integral check, no live sensor check. Just "has the sun dropped below the
geometric threshold?" LoadML still drives the floor calculation (R9/R9a) but
NOT the active/inactive decision.

If doing Bug 1 WITHOUT Bug 2: use a fallback safety net — e.g. keep active
also if a rolling max of `sensor.curtailment_net_pv_surplus_kw` over the last
10 min exceeds DNO. But this is a workaround; real fix is persisting state.

## Bug 3: R11 ratchet blocks R43 safety benefit

When actual_scale > p90_scale (sunnier than forecast), R43 wants to lower the
floor (more headroom). R11 ratchet holds floor up → R43 benefit is nullified
within the same day.

**Fix:** allow floor to DECREASE when `floor_scale` increases from the
previous cycle (i.e., R43 is kicking in). Track `_last_floor_scale` alongside
`_floor_ratchet`. If `floor_scale > _last_floor_scale`, bypass the ratchet.

## LoadML is fine

LoadML is generally accurate (mae_kwh ~0.003 per training stats). Today's
~1 kW over-prediction was small. Not worth building a LoadML trust gate —
the existing safety factor (1.1) and R45 90% cap are the right pressure
valves.

## Deploy plan for these fixes

Overnight, in order:
1. **Bug 2 first** (state persistence) — foundation for the others
2. **Bug 1** (safe_time-based deactivation) — depends on Bug 2
3. **Bug 3** (R43-aware ratchet) — independent of the above but test after 1+2
4. Run integration tests after each
5. Deploy all together while plugin is Off
6. Verify tomorrow morning: sensor populates, target sensible, no early
   deactivation when PV still above DNO + load

## Bug 4: `on_before_plan` "today is done" heuristic is arbitrary

Currently uses hardcoded `23*60 - minutes_now < 60` — i.e., switch to tomorrow's
forecast at 23:00 BST. This is sunset + 2.5h in April, but sunset + 7.5h in
winter. Fine for today (cheap rate doesn't start until 04:00 so plenty of
headroom), but sub-optimal on short winter days where overnight cheap-rate
windows are tight.

**Fix:** Switch to tomorrow's forecast once PV has been effectively 0 for
30 minutes. Observation-based rather than clock-based.

Implementation: track consecutive cycles where `actual_pv < 0.1 kW`. On the
6th consecutive cycle (30 min × 5-min cycles), switch to tomorrow's window.
Reset counter when PV returns.

Not urgent — only matters in winter.

## Bug 6: R4 defer-to-Predbat-charge-window flips without hysteresis

**Symptom (Wed 06:59 BST):** Plugin flipped Active→Off→Active over 3 minutes.
Cause: R4 triggered when SOC (3.3 kWh) was 0.1 kWh below `best_soc_keep`
(3.4 kWh). Log: `Curtailment: deferring to charge window (SOC 3.3 < keep 3.4)`.
SOC nudged above keep 3 min later → plugin re-activated.

Not dangerous, but unnecessary flicker. Shows up as a spurious
`target_soc: 100%` transient and a brief MSC switch.

**Fix:** Add ±0.2 kWh hysteresis to the R4 defer check, same as the
Charge/Hold/Drain split uses:
- Defer when `soc_kw < soc_keep - 0.2`
- Release when `soc_kw >= soc_keep + 0.2`

Stops single-sample wobbles at the threshold triggering mode changes.

## Bug 5: Diagnostic visibility gaps

Post-incident analysis of today was harder than it needed to be. Several key
values had to be reverse-engineered from cumulative sensors and indirect signals.

**Add to `sensor.predbat_curtailment_phase` attributes:**
- `peak_pv_kw` — observed daily peak (drives actual_scale)
- `peak_pv_time` — minutes-since-midnight of observed peak
- `actual_scale` — derived peak_pv / sin(elev_at_peak); shows R43's input
- `last_decision` — short string like "active: overflow=4.2, will_fill=true" or
  "off: past_safe_time"

**Log line on phase transition:**
Today's PHASE log line already has most of this, but add actual_scale,
peak_pv, and the deactivation reason explicitly.

**Rotate state file per day** (cheap postmortem data):
Instead of overwriting `curtailment_state.json` each day, also write to
`curtailment_state_{YYYY-MM-DD}.json`. Keep last N days. Lets us replay
any day's scale/ratchet evolution later.

**Daily summary sensor** (optional, nice-to-have):
At safe_time, publish `sensor.predbat_curtailment_daily_summary` with
`{peak_pv, actual_overflow, floor_max_reached, sunset_soc, activation_time,
deactivation_time}`. Mirrors what `/curtailment-review` computes. Useful
for at-a-glance daily report.

None of this affects operation — pure visibility. Add after a few days of
stable running so the diagnostics actually catch interesting behaviours.

## Key insight: never deploy mid-day

Every restart resets in-memory state. Once state is persisted (Bug 2), this
matters less — but still safer to deploy at night when plugin is Off anyway.
Rule of thumb: only deploy when plugin is Off.
