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

---

## Step 1: Determine Date

Parse the argument for a date. If none provided, use today. Compute:

- `start_time`: date at 00:00:00 UTC
- `end_time`: date+1 at 00:00:00 UTC
- `is_today`: true if date == today

---

## Step 2: Pull Data — Two Calls in Parallel

### Call A: ha_get_history (sparse state changes)

One call covering ALL state-change sensors. Defaults are lean: `significant_changes_only=true`, `minimal_response=true`. Only set `minimal_response=false` because we need attributes from the phase sensor for forecast values.

```json
{
  "entity_ids": [
    "sensor.predbat_curtailment_phase",
    "sensor.predbat_curtailment_target_soc",
    "sensor.predbat_curtailment_export_target",
    "sensor.sigen_inverter_running_state",
    "input_text.curtailment_live_phase",
    "sensor.sigen_plant_battery_state_of_charge"
  ],
  "start_time": "<date>T00:00:00+00:00",
  "end_time": "<date+1>T00:00:00+00:00",
  "limit": 1000,
  "minimal_response": false
}
```

This typically returns 5–10 KB. SOC will have many changes but `significant_changes_only` collapses them.

### Call B: ha_get_state (daily totals — single batch)

For **today**, current state contains today's totals. For **past days**, you must compute deltas from history of cumulative sensors instead — see "Past-day adjustments" below.

```json
{
  "entity_id": [
    "sensor.solcast_pv_forecast_forecast_today",
    "sensor.predbat_pv_today",
    "sensor.sigen_plant_daily_third_party_inverter_energy",
    "sensor.curtailment_overflow_energy",
    "counter.voltage_throttle_activations_today",
    "sensor.sig_voltage_throttle_lost_energy",
    "sensor.sigen_plant_pv_power",
    "sensor.sigen_plant_battery_state_of_charge"
  ]
}
```

### Past-day adjustments

If `is_today` is false, replace Call B with a `ha_get_history` for the same entities **at start_time and end_time** (use `limit=2` and rely on first/last values) so you can compute the day's delta for cumulative sensors. Solcast forecast for past days is not retained — note "forecast unavailable for past days" in the PV Accuracy section instead.

---

## Step 3: Compute Metrics (from cheap data first)

### From Call B (states / daily totals)

1. **PV total today**: state of `sensor.sigen_plant_daily_third_party_inverter_energy`
2. **PV forecast today**: state of `sensor.solcast_pv_forecast_forecast_today` (also has p10/p90 in attributes)
3. **PV ratio**: actual / forecast
4. **Voltage throttle activations**: state of `counter.voltage_throttle_activations_today`
5. **Voltage throttle lost energy** (today's session if active, else cumulative): see attributes — `sensor.sig_voltage_throttle_lost_energy` is cumulative; subtract `input_number.voltage_throttle_engage_lost_start` for the current engagement session, or use the daily-reset variant if one exists
6. **Current SOC** (or sunset SOC if past day): state of `sensor.sigen_plant_battery_state_of_charge`

### From Call A (history)

7. **Phase timeline**: state changes of `input_text.curtailment_live_phase` (the live phase, not the plugin phase)
8. **Plugin phase activation window**: first Active → first Off transition
9. **SIG faults**: entries where `running_state` != "Running" — note time + duration to next Running
10. **Adaptive floor trace**: target_soc changes over the day — first value, lowest value, final value
11. **Phase oscillations**: count Charge↔Hold flips per hour (>5/hr suggests instability)

### Inferred / heuristic checks (no slot-by-slot data needed)

12. **Floor verdict (cheap)**:
    - If sunset SOC = 100% **and** voltage throttle lost ≈ 0: floor was correct
    - If sunset SOC < 95% and PV ratio ≥ 0.9: floor was too LOW (drained too much)
    - If voltage throttle activations are high and SOC reached 100% well before sunset: floor was too HIGH (didn't drain enough)
13. **Export verdict**: voltage throttle activation count is the proxy for "did we hit the cap". Don't pull 5-min export statistics just to check max — the throttle counter and SIG faults already tell the story.

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
- Voltage throttle: N activations, X.X kWh lost [✓ none / ⚠ engaged N times]
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
- **Voltage throttle data**: if more than one voltage throttle session today, the cumulative-minus-start-of-session math doesn't give a clean per-session breakdown — just report total daily lost energy (delta of cumulative sensor between start_time and now/end_time).

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
