---
description: Review curtailment manager performance for a day. Pulls HA history, analyzes phases/SOC/export/PV, and suggests corrections.
user-invocable: true
---

# Curtailment Review

Analyze the curtailment manager's performance for a given day (defaults to today).

**Usage:** `/curtailment-review` or `/curtailment-review 2026-03-26`

**IMPORTANT:**
- All data calls are independent — make them in parallel
- Use ToolSearch to load MCP tools before calling them
- HA tools use **Middlemuir (Mum's)** MCP: `mcp__claude_ai_Middlemuir_Homeassistant_Mums__ha_*`
- Plugin phase: Active, Off. HA automation live phase: Charge, Drain, Hold (in input_text.curtailment_live_phase)
- Legacy (v8) phase names: Charge, Export, Hold, Off
- DNO limit: 4.0 kW, SIG hard limit: 4.5 kW, Battery: 18.08 kWh

---

## Step 1: Determine Date

Parse the argument for a date. If none provided, use today. Compute:
- `start_time`: date at 00:00:00 UTC
- `end_time`: date+1 at 00:00:00 UTC

---

## Step 2: Pull Data (all in parallel)

### Call 1: ha_get_history
Entity IDs (all in one call):
```json
["sensor.predbat_curtailment_phase", "sensor.predbat_curtailment_target_soc", "sensor.predbat_curtailment_overflow_kwh", "sensor.predbat_curtailment_export_target", "sensor.sigen_inverter_running_state"]
```
Parameters: `start_time`, `end_time`, `limit=500`, `minimal_response=false` (need attributes for pv_scale)

### Call 2: ha_get_statistics (5-min granularity)
Entity IDs:
```json
["sensor.sigen_plant_pv_power", "sensor.sigen_plant_consumed_power", "sensor.sigen_plant_grid_export_power", "sensor.sigen_plant_battery_state_of_charge"]
```
Parameters: `start_time`, `end_time`, `period="5minute"`, `statistic_types=["mean", "max", "min"]`

### Call 3: ha_get_states (current/today values for PV comparison)
```json
["sensor.solcast_pv_forecast_forecast_today", "sensor.predbat_pv_today", "sensor.sigen_plant_daily_third_party_inverter_energy"]
```
Note: These show TODAY's values. For historical days, use the PV statistics total instead.

---

## Step 3: Compute Metrics

### From statistics (5-min data):
1. **Total PV**: sum of (mean PV × 5/60) for all slots with PV > 0
2. **Peak PV**: max of all max PV values, note the time
3. **Total export**: sum of (mean export × 5/60)
4. **Max export**: max of all max export values — **flag if > 4.0 kW**
5. **SOC trace**: min/max per hour, find daily low and high with times
6. **Sunset SOC**: SOC at the last 5-min slot where mean PV > 0.1

### From history:
7. **Phase timeline**: list all state changes with timestamps
8. **SIG faults**: count entries where running_state != "Running", note times and durations
9. **Adaptive floor**: first target_soc value after curtailment activates
10. **PV scale**: from phase sensor attributes (pv_scale), track peak value

### Computed:
11. **Actual overflow**: sum of max(0, mean_pv - mean_load - 4.0) × 5/60 for overflow slots
12. **Post-overflow energy**: sum of max(0, mean_pv - mean_load) × 5/60 for slots after last overflow slot
13. **Ideal floor**: 18.08 - actual_overflow - post_overflow_energy
14. **Floor error**: adaptive_floor - ideal_floor (positive = too high, negative = too low)

---

## Step 4: Format Output

```
## Curtailment Review — [date]

### Performance
- Export: max X.X kW [✓ within DNO / ⚠ exceeded DNO at HH:MM]
- SIG faults: [0 ✓ / ⚠ N faults — list times and durations]
- Sunset SOC: XX% (XX.X kWh) [✓ >90% / ⚠ below 90%]
- PV total: XX.X kWh (peak X.X kW at HH:MM)

### Phase Timeline
HH:MM  Phase    SOC%  Target%  Overflow kWh
────── ──────   ────  ───────  ────────────
[each phase transition from history, with nearest SOC/target values]

### Floor Analysis
- Adaptive floor: XX% (X.X kWh)
- Actual overflow absorbed: X.X kWh
- Post-overflow energy available: X.X kWh
- Ideal floor would have been: XX% (X.X kWh)
- Verdict: [✓ floor was correct (within 5%) / ⚠ floor was X% too high — battery didn't reach 100% / ⚠ floor was X% too low — SIG fault risk]

### PV Accuracy
- Solcast forecast: XX.X kWh | Actual: XX.X kWh
- Ratio: X.Xx [✓ within 20% / ⚠ significant error]
- PV scale factor: peak X.Xx at HH:MM [or "not activated"]

### Suggestions
[For each issue found, a specific actionable suggestion. Examples:]
- "⚠ Export reached X.X kW at HH:MM — floor was too high. Actual overflow was X.X kWh vs forecast X.X kWh"
- "⚠ Sunset SOC was XX% — post-overflow energy was X.X kWh less than forecast. Consider reducing post_credit_scale"
- "⚠ SIG fault at HH:MM — battery was XX% when overflow started. Floor should have been XX% lower"
- "⚠ Phase oscillated N times between Export/Hold — intermittent cloud causing rapid transitions"
- "⚠ PV was X.Xx of forecast — calibration may need attention"
- [Or if no issues:] "✓ No issues — zero curtailment, XX% sunset SOC, floor was within X% of ideal"
```

---

## Step 5: Handle Edge Cases

- **No curtailment activity**: If phase was "Off" all day, report "Curtailment manager was inactive — no overflow detected" and skip floor/phase analysis. Still show PV summary and SOC range.
- **Missing data**: Report which source failed, present what's available.
- **Date is today**: Note that the day is still in progress and results are partial.
