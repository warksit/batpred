---
name: GSHP morning heating model (data analysis Mar 2026)
description: Analysis of 23 nights showing temperature+wind model predicts morning GSHP load (R²=0.71). Key features, model coefficients, and validation results.
type: project
---

## GSHP Morning Heating Load Analysis (2026-03-23)

### Key finding
Outdoor temperature alone does NOT predict morning GSHP consumption (R²<0.1).
Adding wind speed and temperature drop yields R²=0.71:

```
heat_kWh = 5.06 - 0.35 × avg_temp(20:00→03:00) + 0.09 × avg_wind(12:00→03:00) + 0.23 × temp_drop(22:00→dawn)
```

### Why these features (first principles)
- **Avg temp 20:00-03:00**: heat loss after heating off (22:00), thermal mass state
- **Avg wind 12:00-03:00**: infiltration while heating on → less stored thermal energy
- **Temp drop 22:00-00:00 → 04:00-07:00**: the actual overnight cooling event
- Wind chill overnight avg alone: R²=0.35 (best single predictor)

### Validation
- LOO-CV: R²=0.58, MAE=0.53 kWh
- Rolling window (self-improving): MAE=0.62 kWh (vs 0.77 for simple mean)
- Train R² improves from 0.62→0.71 as data grows past 15 days

### Data sources
- GSHP energy: `sensor.heat_pump_energy_meter_energy` (HA statistics, 60 days)
- Temperature: `sensor.gw2000a_v2_1_8_outdoor_temperature` (your HA, 4 miles away) or Open-Meteo API
- Wind: Open-Meteo historical API (lat 52.31, lon -1.41)
- GSHP morning load measured as energy delta 04:00→08:00

### GSHP schedule
- Heating off at 22:00, on at ~04:00-05:00 (weather compensation adjusts start)
- First morning cycle: continuous 05:00→~07:30, then cycling
- DHW: 13:15→~15:00 (daily bath, consistent ~3.5 kWh)
- `input_boolean.cold_weather_gshp_heating` for season toggle

### Plan file
`.claude/plans/toasty-dazzling-hanrahan.md` — plugin architecture + cold weather + curtailment updates
