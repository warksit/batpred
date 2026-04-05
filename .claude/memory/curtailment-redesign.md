---
name: Curtailment manager redesign plan
description: Iterative target SOC approach for curtailment management — modelling validated across 6 days, ready for implementation
type: project
---

## Validated Algorithm: Iterative Target SOC (v5)

**Core rule, recalculated every Predbat slot (5 min):**
1. `remaining_overflow` = sum of max(0, forecast_excess - DNO_limit) × slot_hours for all future slots
2. `target_soc` = battery_max (100%) - remaining_overflow
3. If SOC > target: export from battery to bring SOC toward target (capped at DNO limit)
4. If SOC < target: charge from PV toward target, export remainder
5. If excess > DNO limit: always export DNO limit, battery absorbs overflow
6. If excess < 0: battery covers load (self-consumption, always allowed)

**Activation:** any day where forecast overflow > 0 (any slot with excess > 4kW)
**Deactivation:** not needed — target SOC naturally rises to 100% as overflow is absorbed

**Key properties:**
- SOC stays as HIGH as possible — only drains what's needed for expected overflow
- Target SOC rises through the day as overflow is absorbed
- No phases, no floor, no deactivation time — one rule, recalculated each slot
- Battery reaches 100% just as curtailment risk ends
- Cloudy day (45 kWh): dawn target = 80% (barely drains), not 5%
- Peak day (68 kWh): dawn target = 14% (drains significantly — lots of overflow to absorb)

## Modelling Results (v5 — model at /tmp/curtailment_model_v5.py)

| Day | PV | Overflow | Dawn Target | MSC Curtailed | Plugin Curtailed | End SOC | +£/day |
|-----|---:|---:|---:|---:|:---:|---:|---:|
| Jun 19 (peak) | 66 kWh | 13.0 kWh | 28% | 11.6 kWh | **0** | 93% | +£3.38 |
| Jul 12 (peak) | 68 kWh | 15.5 kWh | 14% | 14.1 kWh | **0** | 92% | +£4.15 |
| Jul 9 (cloudy) | 45 kWh | 3.6 kWh | 80% | 3.0 kWh | **0** | 91% | +£0.71 |
| Jul 15 (poor) | 23 kWh | 0.0 kWh | OFF | 0 | 0 | 83% | — |
| Jul 1 (moderate) | 39 kWh | 1.3 kWh | 93% | 0.3 kWh | **0** | 87% | -£0.11* |
| May 30 | 47 kWh | 3.8 kWh | 79% | 2.6 kWh | **0** | 92% | +£0.60 |

*Jul 1 apparent loss is because model doesn't value stored energy (87% vs 77% end SOC)

## Overflow Calculation

`overflow = sum of max(0, (PV_forecast - load_forecast) - DNO_limit) × slot_hours`

This is the TOTAL kWh the battery must absorb to prevent curtailment. NOT the same as MSC curtailment (which is overflow AFTER battery is already full). On 68 kWh day: 15.5 kWh overflow (MSC only curtails 14.1 because it absorbs some before filling).

## Implementation Notes

- **SOC target = 100%** once PV trend < DNO limit (safe to be full, excess exports within limit)
- **SIG hard shutdown** if export >4.5kW even briefly — this is the primary reason to manage ALL curtailment, not just large amounts
- **Pre-drain at dawn** is forecast-based (need to know curtailment will happen before it does)
- **Deactivation** is real-time (target SOC naturally reaches 100% as overflow is consumed)
- **Predbat SOC forecast** will be wrong during D-ESS (doesn't know about forced export). Plugin overrides Predbat during active management. After target reaches 100%, Predbat resumes with actual SOC.
- **No sliding floor needed** — the overflow forecast replaces it entirely

## Previous Approaches (Superseded)

- v1: Simple on/off with PV > 4kW threshold — missed forecast-based activation
- v2: Forecast D-ESS from dawn to sunset — too aggressive, no deactivation
- v3: Two-pass deactivation with sliding SOC floor — worked but over-drained battery
- v4: Adaptive export cap — failed to pre-drain, ramp-to-fill broken

## Modelling Data Files

Six validation days:
- `/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_06_19.csv` — 65 kWh peak (Watts)
- `/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_12.csv` — 68 kWh peak (kW)
- `/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_09.csv` — 45 kWh cloudy (kW)
- `/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_15.csv` — 23 kWh poor (kW)
- `/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_01.csv` — 39 kWh moderate (kW)
- `/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_05_30.csv` — 47 kWh (kW)

CSV format: semicolon-delimited, 15-min intervals. June file Watts, all others kW.
Battery: 18.08 kWh, max charge/discharge 5.5kW, DNO limit 4kW.
Financials: 19p/kWh FIT generation, 12p/kWh export, 24.5p/kWh import.

## Current Deployed State (2026-03-17)

- Plugin: `apps/predbat/curtailment_plugin.py` — live on Mum's HA (OLD design, needs rewrite)
- HA automation: `curtailment_manager_dynamic_export_limit` — deployed
- HA input helpers: enable(on), threshold(0), dno_limit(4kW), max_soc(90%)
- SMA Home Manager export limit: 4kW
- Branch: `curtailment-manager`

## Move best_soc_keep_solar_offset to plugin (PARKED)

Changes in commit 3d02676 should be reverted from core Predbat files and re-implemented in the plugin.
