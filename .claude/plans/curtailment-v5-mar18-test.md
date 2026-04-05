## Curtailment v5 — March 18 Test Day Analysis

### Pre-deploy predictions (evening Mar 17)

**Forecast**: 48.7 kWh PV (Predbat plan), 56.8 kWh (Solcast raw)
**Yesterday accuracy**: Solcast predicted 47.7, actual was 43.4 (91%)
**Peak PV excess**: 6.1 kW at 12:30 (well above 4 kW DNO limit)
**Total overflow**: 4.3 kWh across 7 slots
**Buffer**: 1.0 kWh (configurable via input_number.curtailment_manager_buffer)
**Target SOC**: 12.8 kWh (71%) — down from 78% without buffer

### Expected timeline

| Time | SOC | Phase | What happens |
|------|-----|-------|-------------|
| ~07:00 | 19% | charge | Plugin activates, D-ESS mode, export=0 |
| ~09:30 | ~71% | hold | SOC reaches target, export tracks PV-load |
| 10:00 | 73% | hold | First overflow — battery absorbs 0.46 kWh |
| 10:30 | 77% | hold | Battery absorbs 0.70 kWh |
| 11:00 | 80% | hold | Battery absorbs 0.56 kWh |
| 11:30 | 81% | hold | Battery absorbs 0.16 kWh |
| 12:00 | 87% | hold | Battery absorbs 1.13 kWh |
| 12:30 | 93% | hold | Battery absorbs 1.05 kWh (7% headroom) |
| 13:00–14:30 | ~93% | hold | No overflow, export < 4kW, battery ~idle |
| 15:00 | 94% | hold | Last overflow — battery absorbs 0.22 kWh |
| ~15:30 | 94% | off | Plugin deactivates, restores MSC |
| 15:30–17:00 | 94→100% | MSC | Predbat fills remaining 6% from sub-4kW excess |

### What to check tomorrow

**HA entities to monitor:**
- `sensor.predbat_curtailment_phase` — should go charge→hold→off
- `sensor.predbat_curtailment_target_soc` — should start ~71%, rise during day
- `sensor.predbat_curtailment_overflow_kwh` — should decrease to 0
- `sensor.predbat_curtailment_export_target` — 0 (charge), -1 (hold), then -1 (off)
- `sensor.sigen_plant_battery_state_of_charge` — actual SOC vs predicted
- `sensor.sigen_plant_grid_export_power` — MUST stay ≤ 4 kW all day
- `sensor.sigen_plant_daily_third_party_inverter_energy` — actual PV total
- `sensor.sigen_plant_daily_grid_export_energy` — total export
- `select.sigen_plant_remote_ems_control_mode` — D-ESS when active, MSC when off

**Success criteria:**
1. Zero curtailment (no SMA power limiting)
2. Export never exceeds 4 kW
3. Plugin activates/deactivates cleanly
4. Battery reaches ~100% by end of day
5. No errors in Predbat logs

**Potential issues to watch:**
- Plugin activates too early (before dawn PV is meaningful) — harmless but noisy
- Phase oscillation at target boundary — SOC_MARGIN_KWH (0.5) should prevent this
- HA automation not responding to export_target changes — check automation traces
- Forecast error: if actual PV >> forecast, buffer may not be enough
- Forecast error: if actual PV << forecast, plugin holds SOC too low unnecessarily

### How to check logs
```bash
ssh hassio@100.110.70.80 "bash -l -c 'ha apps logs 6adb4f0d_predbat'" 2>&1 | grep -i curtailment
```

### How to disable in emergency
Set `input_boolean.curtailment_manager_enable` to OFF in HA dashboard.
Plugin will deactivate, restore MSC mode, clear read_only.
