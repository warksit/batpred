---
description: Review SOC keep performance over a period. Compares planned vs actual minimum SOC, cold weather prediction vs actual GSHP, and curtailment manager influence on the keep floor.
user-invocable: true
---

# SOC Keep Review

Analyze whether `best_soc_keep` is correctly sized by comparing planned vs actual minimum SOC each morning, cold weather prediction accuracy, and curtailment manager's influence on the buffer.

**Usage:** `/soc-keep-review` or `/soc-keep-review 7` (number of days)

**IMPORTANT:**
- All data calls are independent — make them in parallel
- Use ToolSearch to load MCP tools before calling them
- HA tools use **Middlemuir (Mum's)** MCP: `mcp__claude_ai_Middlemuir_Homeassistant_Mums__ha_*`
- Battery: 18.08 kWh
- Tariff: Octopus Cosy (cheap: 04:00-07:00, 13:00-16:00, 22:00-00:00)
- Critical gap: 07:00-13:00 (6 hours between cheap windows)
- GSHP window: ~04:00-08:00 BST (compressor cycles ON ~2kW / OFF ~0W)
- Three plugins modify best_soc_keep: base (user), cold weather (+boost), curtailment (-reduction)

---

## Step 1: Determine Period

Parse argument for number of days (default 7). Compute:
- `start_time`: today minus N days at 00:00:00 UTC
- `end_time`: today at 00:00:00 UTC

---

## Step 2: Pull Data (all in parallel)

### Call 1: ha_get_statistics (hourly SOC)
Entity IDs:
```json
["sensor.sigen_plant_battery_state_of_charge"]
```
Parameters: `start_time`, `end_time`, `period="hour"`, `statistic_types=["min", "mean"]`

### Call 2: ha_get_history (keep + plugin sensors)
Entity IDs:
```json
["sensor.predbat_best_soc_keep", "sensor.predbat_cold_weather_soc_keep_boost", "sensor.predbat_curtailment_solar_offset", "sensor.predbat_cold_weather_prediction", "sensor.predbat_curtailment_phase"]
```
Parameters: `start_time`, `end_time`, `limit=1000`, `minimal_response=false`

### Call 3: ha_get_statistics (load + GSHP actual)
Entity IDs:
```json
["sensor.sigen_plant_consumed_power", "sensor.middlemuir_gshp_daily_energy"]
```
Parameters: `start_time`, `end_time`, `period="day"`, `statistic_types=["mean", "sum", "change"]`

Note: If `sensor.middlemuir_gshp_daily_energy` doesn't exist, use hourly load statistics for 04:00-08:00 to estimate GSHP consumption (load during that window minus ~0.5kW base load).

### Call 4: ha_get_statistics (hourly load detail for GSHP estimation)
Entity IDs:
```json
["sensor.sigen_plant_consumed_power"]
```
Parameters: `start_time`, `end_time`, `period="hour"`, `statistic_types=["mean"]`

---

## Step 3: Compute Daily Metrics

For each day in the period, extract:

### Morning Minimum SOC (07:00-13:00)
From hourly SOC statistics, find the minimum SOC between 07:00 and 13:00. Record the hour it occurred.

### SOC at 07:00
From hourly SOC statistics, get the mean SOC at 07:00. This is what the battery had at the end of the cheap window.

### Effective best_soc_keep
From keep sensor history, find the value active during that night's planning (~03:00). If N/A, note it.

### Cold weather prediction vs actual GSHP
- **Predicted**: `sensor.predbat_cold_weather_prediction` value at ~20:00 the evening before
- **Actual GSHP**: estimated from hourly load 04:00-08:00 minus base load (~0.5kW × 4h = 2kWh). Actual GSHP ≈ total_load_04_08 - 2.0
- **Error**: actual - predicted (positive = underpredict, negative = overpredict)
- **Boost applied**: `sensor.predbat_cold_weather_soc_keep_boost` value

### Curtailment manager influence
- **Solar offset**: `sensor.predbat_curtailment_solar_offset` value (negative = reduced keep)
- **Was curtailment active?**: any non-"Off" `sensor.predbat_curtailment_phase` states during the day
- **If active**: note whether the reduction was appropriate (overflow day with PV covering load early) or inappropriate (reduced keep but PV didn't materialise)

### Headroom
`headroom = morning_min_soc_kwh - effective_keep_kwh`

---

## Step 4: Format Output

```
## SOC Keep Review — last N days

### Configuration
- Base best_soc_keep: X.X kWh (input_number.predbat_best_soc_keep)
- Cold weather boost: +X.X kWh (current)
- Curtailment offset: X.X kWh (current)
- Effective keep: X.X kWh

### Daily Summary
Date        SOC@07  Min SOC  Load 04-08  GSHP pred  GSHP act  CW boost  Curt offset  Keep   Headroom
──────────  ──────  ───────  ──────────  ─────────  ────────  ────────  ───────────  ─────  ────────
2026-03-30   19%     5%      6.9 kWh     4.6 kWh    4.9 kWh   +1.9      0.0          3.9    -2.9 [too low]
2026-03-29   45%    32%      5.2 kWh     4.8 kWh    3.2 kWh   +1.4      0.0          3.4    +3.4 [ok]
...

### Cold Weather Accuracy
- Average prediction error: X.X kWh (positive = underpredicted)
- Worst underpredict: DATE (predicted X.X, actual X.X, error +X.X)
- Worst overpredict: DATE (predicted X.X, actual X.X, error -X.X)
- Prediction R²: X.XX (from sensor attribute)
- Boost correlation with error: does the boost cover the prediction shortfall?

### Curtailment Manager Influence
- Days with active curtailment: X of N
- Days where solar offset reduced keep: X of N
- Any days where curtailment reduced keep AND morning SOC was too low?
  [If yes: curtailment reduction was too aggressive — morning_gap calculation may underestimate]

### SOC Keep Analysis
- Days where min SOC < keep: X of N [buffer was insufficient]
- Days where min SOC < 10%: X of N [dangerously low]
- Average headroom: X.X kWh
- Worst day: DATE with min SOC X% (headroom X.X kWh)

### Recommendations
[Based on data, pick the most relevant:]
- "Increase best_soc_keep by X kWh — headroom was negative on X days"
- "Could reduce best_soc_keep by X kWh — headroom was always > 3 kWh"
- "Cold weather prediction underpredicts by avg X.X kWh — increase prediction multiplier from 0.5 to X.X"
- "Cold weather boost covers prediction error well — no change needed"
- "Curtailment reduced keep on DATE but PV didn't cover morning load — consider adding PV confidence check to morning_gap calculation"
- "Buffer is well-sized — no changes recommended"
```

---

## Step 5: Handle Edge Cases

- **Sensors don't exist for full period**: Keep/boost/offset sensors were created 2026-03-30. For earlier days, report "N/A" for plugin data but still show SOC and load data.
- **No GSHP energy sensor**: Estimate from hourly load. Load 04:00-08:00 minus base load (4h × average overnight load).
- **No data**: Report which sources failed.
- **Today**: Note partial day, exclude from averages.
- **Clock change**: BST started 2026-03-29. Hours before that are GMT — be aware when matching timestamps to local time windows.
