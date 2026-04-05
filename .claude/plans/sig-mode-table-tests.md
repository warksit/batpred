# SIG Mode Table Tests

## Goal
Get SIG mode table tests passing in `tests/inverters/sig/__init__.py` on the `sig-inverter-control` branch.

## Current state
- Branch: `sig-inverter-control`, clean working tree
- GE mode table tests: passing (7 modes + 5 transitions)
- SIG definition: placeholder with `skip: True`
- SIG is commented out in `tests/inverters/__init__.py` registry

## What SIG does differently from GE
- GE verifies **service calls** (charge_start, discharge_stop, etc.)
- SIG verifies **entity state** — it writes directly to 3 entities via `adjust_sig_mode()`:
  - `select.sig_remote_ems_mode` — EMS mode string
  - `number.sig_discharge_cut_off_soc` — discharge floor (%)
  - `number.sig_charge_cut_off_soc` — charge ceiling (%)

## Changes needed

### 1. `tests/inverters/_shared.py`
- Add `verify_entity_state(test_name, expected_entities, dummy_items)` function
- Add `definition` parameter to `run_single_mode()` so it can call per-inverter reset logic
- Keep default reset logic (GE service args) but allow definition to override via `reset_mode` callable

### 2. `tests/test_inverter_integration.py`
- Move inverter creation into per-type factory functions (`create_ge_inverter`, `create_sig_inverter`)
- Create fresh PredBat per inverter type to avoid cross-contamination
- Support `"verify": "entity_state"` in definition to use `verify_entity_state` instead of `verify_service_calls`
- SIG factory sets up: `inverter_type=["SIG"]`, `sig_*` entity args, SIG dummy entities

### 3. `tests/inverters/sig/__init__.py`
- Set `skip: False`
- Set `"verify": "entity_state"`
- Provide `reset_mode` function that resets SIG entities to neutral before each test
- Fill `mode_expectations` — expected entity values after each of the 7 modes:
  - `active_charge`: EMS=Command Charging (PV First), floor=4(reserve), ceiling=50(target)
  - `demand_charge`: EMS=MSC, floor=4, ceiling=100
  - `freeze_charge`: EMS=MSC, floor=50(soc), ceiling=100
  - `hold_charge`: EMS=MSC, floor=30(soc), ceiling=100 (target==soc triggers freeze)
  - `active_export`: EMS=Command Discharging (ESS First), floor=4, ceiling=100
  - `demand_export`: EMS=MSC, floor=4, ceiling=100
  - `freeze_export`: EMS=MSC, floor=4, ceiling=80(soc)
- Fill `transitions` — same 5 as GE but with entity expectations

### 4. `tests/inverters/__init__.py`
- Uncomment SIG import and registry entry

## Verification
1. `cd coverage && python3 ../apps/predbat/unit_test.py --test inverter_integration` — all GE + SIG tests pass
2. `--test inverter` — existing tests still pass
3. `--quick` — full suite passes
4. `pre-commit run --all-files` — passes
