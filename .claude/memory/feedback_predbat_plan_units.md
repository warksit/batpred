---
name: CRITICAL — Predbat plan values are energy per slot, not power
description: STOP AND READ before comparing ANY Predbat data to live power — plan/forecast values are kWh per 30-min slot, NOT kW. Multiply by 2 to get kW. This mistake has been made repeatedly.
type: feedback
---

## CRITICAL UNIT CONVERSION — READ BEFORE COMPARING FORECAST TO LIVE DATA

Predbat plan/forecast values (pv_forecast, load_forecast, pv_forecast_minute_step, load_minutes_step, etc.) are **kWh energy per slot**, NOT kW power.

- **Plan rows** (30-min slots): multiply by **2** to get kW
- **5-min step data** (minute_step dicts): multiply by **12** to get kW
- **Live sensor data** (pv_power, load_power, grid_power, battery_power): already in **Watts or kW**

### Example
- `pv_forecast: 3.19` in a plan row = 3.19 kWh per 30 min = **6.38 kW**
- Live `pv_power: 6700` = **6.7 kW**
- These MATCH. Do NOT say "forecast is half of actual" — that is the unit error.

### The recurring mistake
Seeing forecast value 3.19 and live value 6.7, then concluding "forecast is predicting half the actual PV" or "calibration is wrong". This is WRONG — the forecast is in kWh/slot, the live reading is in kW. They are different units.

**Why:** User has corrected this same error multiple times across conversations. It leads to incorrect diagnoses (blaming calibration, Solcast, etc.) when the forecast is actually accurate.

**How to apply:** EVERY time you compare Predbat forecast data to live power readings, you MUST convert to the same unit first and show the conversion explicitly. If you find yourself concluding "forecast is X times too low/high", STOP and check whether you forgot the unit conversion.
