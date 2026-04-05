---
name: Curtailment v5 March 18 Test
description: Curtailment plugin v5 deployed 2026-03-17 evening — analyse results on Mar 18 high-PV day
type: project
---

# Curtailment v5 — March 18 Live Test

## Status
- **Deployed**: 2026-03-17 ~22:40 to Mum's HA
- **Uncommitted changes**: curtailment_plugin.py and curtailment_calc.py have post-commit changes (buffer, logging, removed unused import, 23:00 cutoff)
- **HA automation updated**: reads `export_target` sensor + `dno_limit` attribute
- **HA helpers**: `input_boolean.curtailment_manager_enable` (on), `input_number.curtailment_manager_buffer` (1.0 kWh)
- **Deleted**: `input_number.curtailment_manager_dno_limit` (duplicate — reads from apps.yaml export_limit)

## Forecast for Mar 18
- Solcast: 56.8 kWh PV, peak 8 kW
- Predbat plan (cloud-scaled): 48.7 kWh
- Yesterday accuracy: Solcast 47.7 predicted vs 43.4 actual (91%)
- Overflow: 4.3 kWh across 7 slots (10:00–12:30, 15:00)
- Target SOC with 1kWh buffer: 12.8 kWh (71%)

## Expected SOC trajectory
| Time | SOC | Phase | Notes |
|------|-----|-------|-------|
| 07:00 | 19% | charge | Plugin activates, D-ESS, export=0 |
| 09:30 | 71% | hold | Reaches target, export tracks PV-load |
| 10:00 | 73% | hold | Overflow starts, battery absorbs |
| 12:30 | 93% | hold | Last big overflow slot (7% headroom) |
| 15:00 | 94% | hold | Final overflow slot |
| 15:30 | 94% | off | Plugin deactivates, MSC resumes |
| 17:00 | 100% | MSC | Afternoon PV fills remaining |

## How to analyse

### 1. Check logs for errors/phase transitions
```bash
ssh hassio@100.110.70.80 "bash -l -c 'ha apps logs 6adb4f0d_predbat'" 2>&1 | grep -i curtailment
```
Look for:
- `PHASE` lines — should see: none→charge→hold→off
- Any `ERROR` or `WARNING` lines
- `real-time activation` — means forecast missed overflow (investigate)

### 2. Fetch actual sensor data via HA MCP
Compare these against the predicted trajectory:
- `sensor.predbat_curtailment_phase` — history shows phase transitions
- `sensor.predbat_curtailment_target_soc` — should start ~71%, rise during day
- `sensor.predbat_curtailment_overflow_kwh` — should decrease to 0
- `sensor.sigen_plant_battery_state_of_charge` — actual SOC vs predicted table above
- `sensor.sigen_plant_grid_export_power` — MUST stay ≤ 4 kW all day
- `sensor.sigen_plant_daily_third_party_inverter_energy` — actual PV total
- `sensor.sigen_plant_daily_grid_export_energy` — total export

### 3. Success criteria
1. Zero curtailment (no SMA power limiting events)
2. Export never exceeds 4 kW
3. Plugin activates/deactivates cleanly (no stuck read_only)
4. Battery reaches ~100% by end of day
5. No errors in Predbat logs

### 4. Things that might need fixing
- **Phase oscillation**: if SOC bounces around target, increase SOC_MARGIN_KWH (currently 0.5)
- **Buffer too small**: if battery hits 100% during overflow, increase buffer
- **Buffer too large**: if battery never reaches 100% by evening, decrease buffer
- **Early activation noise**: plugin may activate at dawn when overflow is far away — harmless but verbose
- **HA automation not firing**: check automation traces in HA if export_limit isn't changing
- **Forecast way off**: note the ratio (actual/forecast) for calibrating buffer

## Detailed plan file
See `.claude/plans/curtailment-v5-mar18-test.md` for full analysis with overflow table.
