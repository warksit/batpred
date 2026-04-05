---
name: GSHP power sensor entity
description: The heat pump energy/power sensor entity IDs at Mum's HA — use these when checking GSHP status
type: reference
---

- **Power (live)**: `sensor.heat_pump_energy_meter_power` — watts, updates every ~15s. ~9W idle, ~2000W when compressor running.
- **Energy (cumulative)**: `sensor.heat_pump_energy_meter_energy` — kWh, incrementing counter.
- **GSHP heating toggle**: `input_boolean.cold_weather_gshp_heating` — enables/disables the cold weather plugin.

These are available via HA MCP (Middlemuir) — use `ha_get_history` or `ha_get_state` to check GSHP status.
