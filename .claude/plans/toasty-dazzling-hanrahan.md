# Plan: /tomorrow skill

## Context

With both plugins (curtailment + cold weather) now deployed and influencing
Predbat's overnight planning, it's useful to have a quick overview of what's
expected tonight and tomorrow. The skill pulls live data from Predbat MCP and
mum's HA MCP, presents a compact dashboard, and flags any risks.

## Skill location

`~/.claude/skills/tomorrow/SKILL.md` (global skill, not project-specific)

## What it does

Invoked as `/tomorrow`. Pulls live data and outputs a dashboard summary:

```
## Battery Briefing

### Now
- Battery: 68% (12.3 / 18.1 kWh)
- Mode: Demand | Read-only: No
- PV: 0 kW | Load: 0.5 kW | Grid: importing 0.5 kW

### Today So Far
- PV generated: 15.2 kWh (Solcast forecast was 19.4)
- Grid import: 3.2 kWh | Grid export: 8.1 kWh
- Curtailment: Absorbing 06:30→11:20, Tracking 11:20→13:20, Off since 13:20
- SOC low: 22% at 07:15 | SOC high: 96% at 15:30
- Cold weather: predicted 3.5 kWh, actual GSHP 04-08: 4.1 kWh (boost was 0)

### Tonight
- best_soc_keep: 2.0 kWh (base) + cold weather boost: 0.0 kWh
- Cold weather prediction: 3.5 kWh (rolling_avg=4.5, boost=0)
- Charge window: 04:00-07:00 (Cosy)
- Planned charge target: 40%

### Tomorrow
- Solcast: 19.4 kWh | Peak: 2.9 kW at 12:00
- Weather: 12°C, wind 8 km/h, partly cloudy
- Overflow forecast: 2.3 kWh
- Curtailment target SOC: 87%
- Solar offset: 1.5 kWh reduction (morning gap: 0.8 kWh)

### Risks
⚠ Cold weather boost active (2.1 kWh) — colder than recent average
⚠ Curtailment expected — battery will be drained before solar peak
✓ No issues detected
```

## Data sources

| Data | Source | Tool |
|------|--------|------|
| Battery SOC, mode, power | Predbat MCP | `get_status` |
| Charge windows, plan rows, SOC profile | Predbat MCP | `get_plan` |
| Cold weather prediction + boost | HA MCP | `ha_get_state` → `sensor.predbat_cold_weather_prediction` |
| Curtailment phase/target/overflow | HA MCP | `ha_get_state` → `sensor.predbat_curtailment_*` |
| Curtailment solar offset | HA MCP | `ha_get_state` → `sensor.predbat_curtailment_solar_offset` |
| best_soc_keep config | HA MCP | `ha_get_state` → `input_number.predbat_best_soc_keep` |
| Solcast today + tomorrow | HA MCP | `ha_get_state` → `sensor.solcast_pv_forecast_forecast_*` |
| Today's actual PV | HA MCP | `ha_get_state` → `sensor.sigen_plant_daily_third_party_inverter_energy` |
| Today's grid import/export | HA MCP | `ha_get_state` → `sensor.octopus_energy_*_accumulative_*` |
| Today's SOC min/max | HA MCP | `ha_get_statistics` → `sensor.sigen_plant_battery_state_of_charge` (today, hourly) |
| Today's curtailment history | HA MCP | `ha_get_history` → `sensor.predbat_curtailment_phase` |
| Weather forecast | HA MCP | `ha_get_state` → `weather.forecast_home` |

## Risk flagging logic

- **Cold weather boost > 0**: flag with prediction vs rolling_avg
- **Curtailment overflow > 0**: flag expected draining
- **SOC at 07:00 < 20%**: flag low morning SOC
- **No charge window planned**: flag if best_soc_keep > 0 but no charge window
- **Solcast tomorrow < 5 kWh**: flag poor solar day
- **GSHP heating off but cold forecast**: flag potential miss

## Skill structure

The SKILL.md instructs Claude to:
1. Call `mcp__PredbatMCP__get_status` for current state
2. Call `mcp__PredbatMCP__get_plan` for charge windows and plan rows
3. Call `ha_get_state` for each plugin sensor and config value
4. Call `ha_get_state` for Solcast and weather
5. Format as compact dashboard
6. Apply risk checks and flag any concerns

All calls can be made in parallel (no dependencies between them).

## Files to create

| File | Description |
|------|-------------|
| `~/.claude/skills/tomorrow/SKILL.md` | Skill definition |

## Verification

- Run `/tomorrow` and check output includes all sections
- Check data matches what's visible in HA dashboard
- Test with MCP disconnected — should report connection issue gracefully
