# Curtailment Review Skill

## Context

The curtailment manager v6 was deployed 2026-03-27 with adaptive floor, PV scaling, and time-aware phases (Charge/Export/Hold/Off). We need a `/curtailment-review` skill to analyze each day's performance and suggest corrections.

## File to create
`.claude/skills/curtailment-review/SKILL.md`

## Data to pull (all in parallel)

1. `ha_get_history` — phase transitions, SIG faults, export limit changes:
   - `sensor.predbat_curtailment_phase` (Charge/Export/Hold/Off)
   - `sensor.predbat_curtailment_target_soc` (adaptive floor trace)
   - `sensor.predbat_curtailment_overflow_kwh` (overflow estimate trace)
   - `sensor.predbat_curtailment_export_target` (export target)
   - `sensor.sigen_inverter_running_state` (SIG faults)

2. `ha_get_statistics` (5-min, mean+max) — PV, load, export, SOC:
   - `sensor.sigen_plant_pv_power`
   - `sensor.sigen_plant_consumed_power`
   - `sensor.sigen_plant_grid_export_power`
   - `sensor.sigen_plant_battery_state_of_charge`

3. `ha_get_states` — Solcast forecast for comparison:
   - `sensor.solcast_pv_forecast_forecast_today`
   - `sensor.predbat_pv_today`
   - `sensor.sigen_plant_daily_third_party_inverter_energy`

## Analysis sections

1. **Performance** — max export, SIG faults, sunset SOC, curtailment kWh
2. **Timeline** — phase transitions with SOC/PV/export at each change
3. **Floor analysis** — adaptive floor vs ideal floor (computed from actual overflow + post-overflow)
4. **PV accuracy** — actual vs forecast, PV scale factor trace
5. **Suggestions** — actionable items or "no changes needed"

## Output format

```
## Curtailment Review — [date]

### Performance
- Export: max X.X kW [checkmark within DNO / warning exceeded DNO at HH:MM]
- SIG faults: [0 / N faults]
- Sunset SOC: XX% [checkmark / warning below 90%]

### Timeline
HH:MM  Phase    SOC%  Target%  PV kW  Export kW
[key transitions]

### Floor Analysis
- Adaptive floor: XX% (X.X kWh)
- Actual overflow absorbed: X.X kWh
- Post-overflow charged: X.X kWh
- Ideal floor: XX% | Verdict: [correct / too high / too low]

### PV Accuracy
- Forecast: XX.X kWh | Actual: XX.X kWh | Ratio: X.Xx
- PV scale: [peak X.Xx at HH:MM / not activated]

### Suggestions
[items or "No changes needed"]
```

## Verification
1. Run on March 26 (known failure day — should flag SIG faults + floor too high)
2. Run on March 25 (Environmental Abnormality fault)
3. Run on today (first v6 live test)
4. Run on a non-overflow day (should report "inactive")
