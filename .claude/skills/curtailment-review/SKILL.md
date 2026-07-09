---
description: Review curtailment manager performance for a day. Pulls HA history, analyzes phases/SOC/export/PV, and suggests corrections.
user-invocable: true
---

# Curtailment Review

Analyze the curtailment manager's performance for a given day (defaults to today).

**Usage:** `/curtailment-review` or `/curtailment-review 2026-03-26`

**IMPORTANT:**

- HA tools use **Middlemuir (Mum's)** MCP: `mcp__claude_ai_Middlemuir_Homeassistant_Mums__ha_*`
- All data calls in Step 2 are independent — make them in parallel in a single message
- **Be lean.** Default to daily-aggregated sensors; only pull 5-min statistics if a problem is detected and needs timing analysis (Step 3)
- DNO limit: 4.0 kW, SIG hard limit: 4.5 kW, Battery: 18.08 kWh, Latitude: 52.3°N
- Plugin phase: Active, Off. HA automation live phase: Charge, Drain, Hold (in `input_text.curtailment_live_phase`)
- **NEVER request history with `minimal_response=false` across more than one day, and never include `sensor.predbat_curtailment_phase` in a `minimal_response=false` history call.** Its attributes embed a large nested forecast blob that repeats on every state change — a 2-day, 7-entity call returned 383 KB and overflowed the token limit (observed 2026-06-14). Pull phase *attributes* from a single `ha_get_state` snapshot, not from history.
- **Multi-day requests** ("last couple of days", "this week"): loop Step 2 **one day at a time**, never widen a single history call to span the range.
- **If a history call still overflows** and is saved to a file, don't re-read it whole — extract the series you need with `jq` (e.g. `jq -r '.data.entities[] | select(.entity_id=="input_text.curtailment_live_phase") | .states[] | "\(.last_changed) \(.state)"'`).

---

## Step 1: Determine Date(s)

Parse the argument. If none provided, use today. If it names a range ("last
couple of days", "last 3 days", "this week"), expand it to an explicit list of
dates and **run Steps 2–4 once per date** (oldest first), then write one
combined report. Do NOT widen a single history call to span the range.

For each date compute:

- `start_time`: date at 00:00:00 UTC
- `end_time`: date+1 at 00:00:00 UTC
- `is_today`: true if date == today

---

## Step 2: Pull Data — Four Calls in Parallel

### Call A: ha_get_history (sparse state changes)

One call covering the light state-change sensors. Keep it lean:
`significant_changes_only=true`, **`minimal_response=true`**. These sensors carry
all the timeline we need in their `state` values; we do NOT need their
attributes, so do not pay for them.

```json
{
  "entity_ids": [
    "input_text.curtailment_live_phase",
    "sensor.predbat_curtailment_drain_above",
    "sensor.predbat_curtailment_charge_below",
    "select.sigen_plant_remote_ems_control_mode",
    "input_select.predbat_requested_mode",
    "sensor.sigen_inverter_running_state"
  ],
  "start_time": "<date>T00:00:00+00:00",
  "end_time": "<date+1>T00:00:00+00:00",
  "limit": 1000,
  "minimal_response": true
}
```

This typically returns 5–10 KB for one day. The drain_above / charge_below
series let you verify the deep-discharge floor (≥ 0.5 kWh while plugin Active);
the EMS-mode / requested-mode series let you verify no MSC-clobber regression.

**Do NOT include `sensor.sigen_plant_battery_state_of_charge` here** — even with
`significant_changes_only` it returned 142 KB and hit the 1000-row limit at
midday, losing the afternoon trace (observed 2026-07-09). SOC comes from Call D
instead; only pull raw SOC history for a narrow window (< 1 h) if a specific
floor-crossing needs second-level timing.

**Phase-sensor attributes** (floor_pct, overflow_kwh, safe_time, floor_source,
effective_keep_kwh, confidence) come from a single current `ha_get_state` of
`sensor.predbat_curtailment_phase` (it's in Call B) — never from a
`minimal_response=false` history call. For a *past* day you lose the intraday
trace of these attributes; that's acceptable — note "phase attributes are
end-of-window snapshot only" rather than pulling the heavy history.

### Call B: ha_get_state (daily totals — single batch)

For **today**, current state contains today's totals. For **past days**, you must compute deltas from history of cumulative sensors instead — see "Past-day adjustments" below.

```json
{
  "entity_id": [
    "sensor.solcast_pv_forecast_forecast_today",
    "sensor.predbat_pv_today",
    "sensor.sigen_plant_daily_third_party_inverter_energy",
    "sensor.sigen_plant_daily_grid_import_energy",
    "sensor.sigen_plant_daily_grid_export_energy",
    "counter.voltage_throttle_activations_today",
    "sensor.sigen_plant_pv_power",
    "sensor.sigen_plant_battery_state_of_charge",
    "sensor.predbat_curtailment_phase"
  ]
}
```

`sensor.predbat_curtailment_phase` here is the **single** place to read the
phase attributes (floor_pct, overflow_kwh/p10/p50/p90, safe_time, floor_source,
effective_keep_kwh, p10_recovery_floor_kwh, confidence). This snapshot replaces
any need to pull phase-attribute history. Daily grid import/export are the
clearest "did the day go well" signal — near-zero import on an overflow day is
the success marker.

### Call C: ha_get_history statistics (day-deltas for lifetime-cumulative sensors)

`sensor.curtailment_overflow_energy` and `sensor.sig_voltage_throttle_lost_energy`
are **lifetime-cumulative** (`state_class: total`, hundreds of kWh) — their current
state is useless for a daily figure. Get the day's delta directly (works for today
AND past days, tiny response):

```json
{
  "source": "statistics",
  "entity_ids": ["sensor.curtailment_overflow_energy", "sensor.sig_voltage_throttle_lost_energy"],
  "period": "day",
  "statistic_types": ["change"],
  "start_time": "<date>T00:00:00+00:00",
  "end_time": "<date+1>T00:00:00+00:00"
}
```

`change` = the day's overflow kWh and throttle-lost kWh. (Observed 2026-07-04:
raw states were 767 / 439 kWh cumulative; day changes were 10.87 / 5.07.)

### Call D: ha_get_history statistics (SOC hourly)

SOC min/peak/sunset come from hourly statistics — 24 tiny rows, works for any date:

```json
{
  "source": "statistics",
  "entity_ids": ["sensor.sigen_plant_battery_state_of_charge"],
  "period": "hour",
  "statistic_types": ["mean", "min", "max"],
  "start_time": "<date>T00:00:00+00:00",
  "end_time": "<date+1>T00:00:00+00:00"
}
```

Sunset SOC = mean of the hour containing sunset (~20:00 UTC midsummer at 52.3°N).
Day min = min over the day (check its hour against the phase timeline: pre-PV
near-zero is by design on big-overflow days).

### Past-day adjustments

If `is_today` is false, replace Call B's daily-total sensors with a `ha_get_history`
**at start_time and end_time** (use `limit=2` and rely on first/last values) so you
can compute the day's delta for the `daily_*` cumulative sensors. Call C already
handles the two lifetime-cumulative sensors for any date. Solcast forecast for past
days is not retained — note "forecast unavailable for past days" in the PV Accuracy
section instead.

---

## Step 3: Compute Metrics (from cheap data first)

### From Call B (states / daily totals)

1. **PV total today**: state of `sensor.sigen_plant_daily_third_party_inverter_energy`
2. **PV forecast today**: state of `sensor.solcast_pv_forecast_forecast_today` (also has p10/p90 in attributes)
3. **PV ratio**: actual / forecast
4. **Voltage throttle activations**: state of `counter.voltage_throttle_activations_today`
5. **Voltage throttle lost energy + curtailment overflow (day)**: the `change` values from Call C — never the raw cumulative states
6. **Current SOC** (today: state of `sensor.sigen_plant_battery_state_of_charge`; sunset/min/peak SOC for any date: Call D hourly stats)

### From Call A (history)

7. **Phase timeline**: state changes of `input_text.curtailment_live_phase` (the live phase, not the plugin phase)
8. **Plugin phase activation window**: first Active → first Off transition
9. **SIG faults**: entries where `running_state` != "Running" — note time + duration to next Running
10. **Split-threshold floor trace**: min `drain_above` and min `charge_below` over the day. **Both must stay ≥ 0.5 kWh while the plugin is Active** (the deep-discharge floor, `DEEP_DISCHARGE_FLOOR_KWH`). A value of 0.0 only legitimately appears while the plugin is Off (charge_below publishes 0.0 when inactive); an Active-state value below 0.5 is a regression. On big-overflow days expect `drain_above` to sit at exactly 0.50.
11. **EMS-clobber check**: scan `select.sigen_plant_remote_ems_control_mode` vs `input_select.predbat_requested_mode`. A mode the Predbat mapper set (Command Charging / Command Discharging (PV First)) must NOT flip back to Maximum Self Consumption within a few seconds while the plugin is Off — that's the 2026-06-11 clobber regression. Curtailment's own `Command Discharging (ESS First)` → MSC restore at deactivation is expected and fine.
12. **Phase oscillations**: count Charge↔Hold flips per hour (>5/hr suggests instability)

(SOC minimum: if the battery hit near-0% overnight, check whether it was the curtailment plugin or **Predbat's own pre-dawn discharge** — `requested_mode = Discharging` → `Command Discharging (PV First)`. The plugin's drain floor does not govern Predbat's separate discharge plan.)

### Inferred / heuristic checks (no slot-by-slot data needed)

13. **Floor verdict (cheap)**: the v20 design drains to overnight need, not 100% — do NOT treat sunset SOC < 100% as failure. Instead:
    - Near-zero daily grid import on an overflow day **and** overflow_kwh ≈ 0 (no DNO breach): floor/drain was correct ✓
    - High daily import on a day with PV ratio ≥ 0.9: drained too LOW / recovered too late
    - Curtailment overflow > 0 or a SIG fault: drained too little / cap breached
14. **Export verdict**: voltage throttle activation count is the proxy for "did we hit the cap". A busy 50+ kWh export day will show dozens of activations — that's normal, not a fault. Don't pull 5-min export statistics just to check max — the throttle counter and SIG faults already tell the story.
    - **`sig_voltage_throttle_lost_energy` is a misnomer — it is DEFERRED export, not lost generation** (established 2026-07-09). The throttle caps `number.sigen_plant_grid_export_limitation` (SIG grid export); the SMA keeps generating and the surplus charges the battery, which exports it later. True cost ≈ round-trip loss only (~10% × 12p ≈ 1.2p/kWh); FIT generation is unaffected. Report it as "X kWh deferred through battery (~Yp round-trip cost)". It is only genuinely lost if the battery was FULL while the throttle was engaged — check Call D's SOC peak before calling it a loss.

### Step 3b — OPTIONAL deeper pull (only if Step 3 flagged an issue)

Only if Step 3 identified an issue requiring slot timing (e.g., "when did peak PV happen?", "how long was overflow?"):

```json
{
  "source": "statistics",
  "entity_ids": ["sensor.sigen_plant_pv_power"],
  "period": "5minute",
  "statistic_types": ["mean", "max"],
  "start_time": "<date>T00:00:00+00:00",
  "end_time": "<date+1>T00:00:00+00:00",
  "limit": 300
}
```

Single entity, single stat = ~16 KB instead of 63 KB. Skip this entirely for normal-day reviews.

---

## Step 4: Format Output

```text
## Curtailment Review — [date]

### Performance
- PV today: XX.X kWh actual vs YY.Y kWh forecast (ratio Z.ZZx) [✓/⚠]
- Sunset SOC (or current SOC if today): XX.X% [✓ ≥95% / ⚠ below]
- SIG faults: [0 ✓ / ⚠ N — list HH:MM and duration]
- Voltage throttle: N activations, X.X kWh deferred via battery (~Yp round-trip) [✓ none-or-deferred / ⚠ battery was full while throttled — genuinely lost]
- Phase oscillations (Charge↔Hold): N flips [✓ stable / ⚠ unstable in HH:MM-HH:MM]

### Phase Timeline
HH:MM  Phase         Target%  Note
─────  ────────────  ───────  ────
[live phase changes; pull Target% from nearest target_soc state]

### Floor Analysis
- First adaptive floor: XX% (X.X kWh) at HH:MM
- Lowest target: XX% (X.X kWh) at HH:MM
- Final target: XX% (X.X kWh) at HH:MM
- Verdict: [✓ floor correct / ⚠ too low — sunset SOC X% / ⚠ too high — N voltage throttle events]

### PV Accuracy
- Solcast: XX.X kWh, p10 YY.Y, p90 ZZ.Z, confidence C
- Actual: XX.X kWh (ratio R.RRx) [✓ within 20% / ⚠ off]
- [If past day:] Solcast forecast not retained for past days

### Suggestions
[Concrete, actionable. If nothing:] "✓ Day went as planned"
```

---

## Step 5: Edge Cases

- **No curtailment activity** (phase Off all day): report "Curtailment manager inactive — no overflow detected" and skip floor/phase analysis. Still report PV total, sunset SOC, voltage throttle (if any).
- **Today, mid-day**: state day is in progress; skip sunset SOC (use "current SOC: X%").
- **Missing data**: report which call failed and present what you have.
- **Voltage throttle data**: Call C's day `change` is the total daily lost energy — don't attempt per-session breakdowns from the cumulative sensor.

---

## Step 6: Suggest Skill Self-Improvements

Before ending the review, briefly reflect: did anything in this run point to an improvement to *this skill*?

Examples:

- A new daily sensor exists that would replace a heavy history call → propose adding it
- A metric was computed but never used in any verdict → propose dropping the metric
- The deep statistics pull was triggered but didn't change the verdict → tighten the trigger condition
- A new failure mode is observed (e.g., SIG comms fault distinct from voltage trip) → propose adding it to the "SIG faults" reporting

If you spot something, end the response with:

```text
### Skill self-improvement
- [Specific, actionable suggestion to .claude/skills/curtailment-review/SKILL.md]
```

If nothing — say nothing. Don't fabricate suggestions.
