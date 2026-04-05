# Unified Inverter Test Framework — Restructured

## Context

The current `test_inverter.py` and `test_execute.py` are effectively GE-specific tests. We've added mode table tests and universal execute scenarios inline in those files, but adding a new inverter type means scattering expectations across multiple shared files.

The goal is a clean structure where each inverter type is self-contained in its own folder. Adding a new inverter = create files in `tests/inverters/<name>/`, add one line to the registry. The generic test runner doesn't change.

This also lays groundwork for inverter folders eventually containing more than just test expectations (inverter definition, dummy apps.yaml, etc.).

## Scope (initial PR)

- Layer 1 only: service call verification (mode table tests)
- GE active, SIG definition file exists but commented out in registry
- Leave existing `test_inverter.py` and `test_execute.py` completely untouched
- Remove the mode table code we added to `test_inverter.py` and universal execute code from `test_execute.py` (revert our commit on the branch)

## File structure

```
apps/predbat/tests/
    inverters/
        __init__.py              # Registry: ALL_INVERTERS list
        _shared.py               # INVERTER_MODES dict, verify_service_calls(), run_single_mode()
        ge/
            __init__.py           # GE_DEFINITION: mode_expectations + transitions
        sig/
            __init__.py           # SIG_DEFINITION: skip=True placeholder
    test_inverter_integration.py  # Generic runner — imports ALL_INVERTERS, runs mode table tests
```

## File contents

### `tests/inverters/__init__.py` (registry)
```python
from tests.inverters.ge import DEFINITION as GE_DEFINITION
# from tests.inverters.sig import DEFINITION as SIG_DEFINITION  # pending SIG control PR

ALL_INVERTERS = [
    GE_DEFINITION,
    # SIG_DEFINITION,
]
```

### `tests/inverters/_shared.py`
Contains the shared infrastructure (moved from test_inverter.py):
- `INVERTER_MODES` dict — mode definitions (soc_percent, setup calls, action)
- `verify_service_calls(test_name, expected_calls, ha)` — assertion helper
- `run_single_mode(inv, ha, my_predbat, dummy_items, mode_name)` — executes one mode

### `tests/inverters/ge/__init__.py`
```python
DEFINITION = {
    "name": "GE",
    "skip": False,
    "mode_expectations": {
        "active_charge": [...],
        "demand_charge": [...],
        # ... all 7 modes
    },
    "transitions": [
        {"name": "freeze_charge_to_demand", "steps": [...], "expected": [...]},
        # ... all 5 transitions
    ],
}
```

### `tests/inverters/sig/__init__.py`
```python
DEFINITION = {
    "name": "SIG",
    "skip": True,
    "skip_reason": "Pending SIG control PR",
    "mode_expectations": {},
    "transitions": [],
}
```

### `tests/test_inverter_integration.py`
Generic runner that:
1. Imports `ALL_INVERTERS` from `tests.inverters`
2. Imports `INVERTER_MODES`, `verify_service_calls`, `run_single_mode` from `tests.inverters._shared`
3. Sets up inverter (same as existing test_inverter.py setup)
4. Iterates ALL_INVERTERS, skipping those with `skip=True`
5. For each: runs all 7 modes, then transitions
6. Exports `run_inverter_integration_tests(my_predbat)`

### `unit_test.py`
Add one entry to test registry:
```python
("inverter_integration", run_inverter_integration_tests),
```

## Changes to existing files

- `test_inverter.py` — revert our mode table additions (delete INVERTER_MODES, GE_EXPECTATIONS, SIG_EXPECTATIONS, GE_TRANSITIONS, verify_service_calls, run_single_mode, run_mode_table_tests, and the call to run_mode_table_tests)
- `test_execute.py` — revert our universal execute additions (delete run_universal_execute_tests and its call)
- `unit_test.py` — add `("inverter_integration", run_inverter_integration_tests)` to test registry

## Verification

1. `python3 unit_test.py --test inverter_integration` — 7 mode tests + 5 transitions pass, SIG skipped
2. `python3 unit_test.py --test inverter` — existing tests still pass unchanged
3. `python3 unit_test.py --quick` — full suite passes
4. `pre-commit run --all-files` — passes
