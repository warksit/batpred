# Curtailment Avoidance Feature

## Branch: `curtailment-avoidance`

## What it does
Prevents PV curtailment by computing a lookahead MaxSOC ceiling from the PV/load forecast. Battery never charges above the level that would cause future curtailment. Pre-solar export drains battery when SOC exceeds ceiling.

## Key algorithm
```
future_curtailable[minute] = sum of max(pv-load-export_limit, 0) for all future slots
max_soc_ceiling[minute] = soc_max - future_curtailable[minute]
```
In ECO mode: cap charging at ceiling, force discharge if SOC > ceiling.

## Validated with simulation
Real June 19 2025 data: 13kWh/day curtailed → 0kWh with lookahead. £2.80/day saving, identical end-of-day SOC.
System: 18kWh battery, 10kW inverter, 4kW export limit.

## Implementation status (as of context clear)

### DONE:
1. prediction.py — lookahead ceiling pre-computation (after line 559 area)
2. prediction.py — ECO mode throttle + force discharge (after battery_draw calc in ECO)
3. prediction.py — `curtailed_today` counter, `predict_curtailed_best` dict, separate tracking at export limit clip
4. prediction.py — `final_curtailed` in return tuple and cache
5. prediction.py — `thread_run_prediction_single` updated to unpack new field

### TODO:
6. prediction.py — Update ALL other callers that unpack run_prediction results:
   - `thread_run_prediction_charge` (~line 225)
   - `thread_run_prediction_charge_min_max` (~line 50 area)
   - wrapped_run_prediction_single, wrapped_run_prediction_charge, wrapped_run_prediction_charge_min_max
7. plan.py — Update all places that call run_prediction and unpack results (search for "final_carbon_g" to find them)
8. plan.py — Add to `compute_metric`: `metric += final_curtailed * self.metric_curtailment_cost`
9. plan.py — Wire `predict_curtailed_best` at ~line 3362
10. config.py — Add `metric_curtailment_cost` as expert-mode input_number, default 0
11. fetch.py — Load `metric_curtailment_cost` (follow `metric_battery_cycle` pattern)
12. fetch.py — Generate curtailment-avoidance export windows when export_limit > 0 and metric_curtailment_cost > 0
13. output.py — "Curt kWh" column header + row data when export_limit > 0
14. web_helper.py — "Curt kWh" column in responsive table
15. unit_test.py — Tests for curtailment logic

### CRITICAL: Tests will FAIL until step 6+7 done
Return tuple size changed but not all callers updated yet.

## Key design decisions
- Lookahead activates when `export_limit > 0` (no config needed for core behaviour)
- `metric_curtailment_cost` (p/kWh) lets optimizer factor curtailment into decisions
- Curtailment-avoidance export windows needed because deemed export users have no natural export windows
- "Curtailment" = avoidable (export limit), "Clipping" = unavoidable (inverter limit)
- User is on FIT with deemed export, SIG MODBUS inverter, waiting for SEG

## User's system
- Mum's HA instance, installed as HACS app
- Fork: warksit/batpred
- 18kWh battery, 10kW inverter, 4kW export limit
- FIT at 21.5p/kWh generation

## Plan file
`.claude/plans/floofy-zooming-thompson.md`

## Pre-existing test failure
`debug_cases/predbat_debug_agile1.yaml` fails on main — not our issue.
