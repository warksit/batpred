---
name: PredHeat as long-term GSHP solution
description: PredHeat (built into Predbat) can replace the cold weather plugin — needs sensors and calibration
type: project
---

PredHeat (`predheat.py`) is Predbat's built-in physics-based heating simulator. It would solve the GSHP load prediction problem at the source — the optimizer natively accounts for heating load via `load_forecast: predheat.heat_energy$external`.

**Why:** Our cold weather plugin works around LoadML's inability to predict GSHP load by boosting SOC keep penalties. PredHeat would eliminate the need by giving the optimizer accurate heating load forecasts.

**How to apply:** When ready to set up, configure in apps.yaml under `predheat:` section.

## What's needed at Mum's

| Item | Status | Notes |
|------|--------|-------|
| `mode: pump` | Config only | |
| `heating_energy` | Have it | `sensor.heat_pump_energy_meter_energy` |
| `external_temperature` | Have it | Via `weather.forecast_home` |
| Internal temp sensor(s) | **NEED TO BUY** | Zigbee sensor in main living area, ~£10-15 |
| Target temperature | Config/helper | GSHP is "dumb", setpoint rarely changes — use `input_number` |
| `heat_cop` | Config | ~3.5 (GSHP has flat COP, ground temp ~10°C year-round) |
| `flow_temp` | Config | ~30-35°C for UFH, can get setting from GSHP display |
| `flow_difference_target` | Config | ~5°C for heat pump UFH |
| `heat_output` | **NEED TO CALCULATE** | Total UFH output in watts at delta 50 |
| `heat_volume` | **NEED TO ESTIMATE** | UFH loops + pipework + buffer tank (key: there IS a buffer tank) |
| `heat_loss_watts` | **CALIBRATE** | Can derive from historical GSHP energy vs outdoor temp |

## Key considerations

- **Buffer tank**: Adds thermal mass. GSHP cycles based on buffer temp, not room temp. Set `heat_volume` to include buffer (100-200L?). PredHeat's thermostat model may not perfectly capture buffer lag.
- **GSHP COP table**: Default is for ASHP (COP drops in cold). GSHP needs flat table: `{-10: 3.5, 0: 3.5, 10: 3.5, 20: 3.5}`
- **GSHP is on/off at 2kW**: PredHeat models this as thermostat hysteresis which should work
- **UFH throughout**: Low flow temps, high thermal mass
- **Template**: See `templates/predheat.yaml` for full config example
- **Docs**: https://springfall2008.github.io/batpred/predheat/
