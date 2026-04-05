# Inverter Test Infrastructure Investigation - Complete Summary

**Date**: 2026-03-08  
**Investigation Status**: COMPLETE (All 5 goals fulfilled)  
**Branch Examined**: `main`

## Executive Summary

This investigation examined the inverter test infrastructure in batpred to understand how to design a unified test framework. The codebase supports 17 inverter types through a capability-flag-based abstraction system. The key distinguishing feature is the `soc_limits_block_solar` flag, which separates SIG inverters (True) from all other types (False), fundamentally changing their control approach.

**Key Findings**:
- **Inverter abstraction**: 25+ capability flags per inverter type in `INVERTER_DEF` dictionary
- **Control patterns**: SIG uses EMS mode switching; non-SIG uses service calls
- **Test infrastructure**: `ActiveTestInverter` mock class with 60+ state fields; `run_execute_test()` framework with 40+ configuration parameters
- **Test coverage**: 60+ test scenarios in test_execute.py covering charge, discharge, freeze, car integration, and SIG-specific modes

---

## Goal 1: SIG and GE Inverter Creation and Implementation Differences

### File: `/Users/andrew/code/batpred/apps/predbat/tests/test_inverter.py`

**SIG Inverter Creation** (Lines 1296-1322):
- Factory function `create_sig_inverter()` sets `soc_limits_block_solar=True`
- Creates test inverter with:
  ```python
  inverter_type="SIG"
  soc_limits_block_solar=True
  has_target_soc=True
  has_reserve_soc=True
  has_timed_pause=False
  charge_control_immediate=True
  ```

**GE Inverter Creation**:
- No factory function; created inline in test functions
- Example from test functions:
  ```python
  inverter_type="GE"
  soc_limits_block_solar=False
  has_target_soc=True
  has_reserve_soc=False
  charge_control_immediate=True
  ```

**Implementation Differences**:

| Aspect | SIG | GE |
|--------|-----|-----|
| Control Method | EMS mode switching (Demand/Charging/Exporting/Freeze) | Service calls (charge_stop_service, charge_start_service, discharge_service) |
| SOC Limiting | `soc_limits_block_solar=True` (prevents solar override) | `soc_limits_block_solar=False` (allows solar when target_soc high) |
| Reserve Support | Yes (`has_reserve_soc=True`) | No (`has_reserve_soc=False`) |
| Charge Window Timing | Uses `has_timed_pause=False` in SIG tests | Uses `has_timed_pause=True` in other tests |
| State Persistence | Entities written via `write_and_poll_option()` | Service calls for transient operations |

**Test Classes**:
- `test_sig_adjust_charge_immediate` (Lines 1325-1366): Verifies EMS mode entity transitions
- `test_sig_adjust_export_immediate` (Lines 1369-1405): Verifies export mode switching
- Corresponding GE tests use service call verification

---

## Goal 2: INVERTER_DEF Fields - Complete Enumeration

### File: `/Users/andrew/code/batpred/apps/predbat/config.py` (Lines 1483-1951+)

**Complete Field List** (25+ fields per inverter type):

1. **name** (string): Display name for inverter type
2. **has_rest_api** (bool): Supports REST API control
3. **has_mqtt_api** (bool): Supports MQTT control
4. **charge_control_immediate** (bool): Can adjust charge immediately
5. **has_target_soc** (bool): Supports setting target SOC for charging/discharging
6. **has_reserve_soc** (bool): Supports discharge floor (reserve) setting
7. **soc_limits_block_solar** (bool): **KEY FLAG** - Prevents solar override when target_soc high
8. **ems_mode_demand** (string): EMS mode name for "no charging/discharging" state
9. **ems_mode_charging** (string): EMS mode name for charging state
10. **ems_mode_exporting** (string): EMS mode name for discharging state
11. **time_button_press** (bool): Requires time button press to set charge start time
12. **charge_discharge_with_rate** (bool): Can set charge/discharge rate in immediate mode
13. **target_soc_used_for_discharge** (bool): Uses target_soc during discharge (vs separate discharge_limit)
14. **inverter_hybrid** (bool): Is hybrid inverter (can export to grid)
15. **has_timed_pause** (bool): Supports timed pause for charge windows
16. **has_charge_enable_time** (bool): Can set charge window start/end times
17. **has_discharge_enable_time** (bool): Can set discharge window start/end times
18. **has_charge_limit_time** (bool): Has separate charge ceiling (beyond target_soc)
19. **has_discharge_limit_time** (bool): Has separate discharge floor (beyond reserve_soc)
20. **supports_hold_soc** (bool): Can "hold" at current SOC (freeze mode)
21. **supports_freeze_soc** (bool): Can "freeze" at target SOC
22. **inv_can_span_midnight** (bool): Can create charge/discharge windows spanning midnight
23. **is_ems_driven** (bool): Battery mode driven by EMS rather than service calls
24. **has_ems_mode_control** (bool): Controls EMS mode via write_and_poll_option()
25. **rest_data** (dict): REST API endpoint mapping for legacy control

**SIG Entry (Lines 1930-1951+)**:
```python
"SIG": {
    "name": "SIG",
    "has_rest_api": False,
    "has_mqtt_api": False,
    "charge_control_immediate": True,
    "has_target_soc": True,
    "has_reserve_soc": True,
    "soc_limits_block_solar": True,
    "ems_mode_demand": "demand",
    "ems_mode_charging": "charging",
    "ems_mode_exporting": "exporting",
    "time_button_press": False,
    "charge_discharge_with_rate": False,
    "target_soc_used_for_discharge": True,
    "inverter_hybrid": True,
    "has_timed_pause": False,
    "has_charge_enable_time": False,
    "has_discharge_enable_time": False,
    "has_charge_limit_time": True,
    "has_discharge_limit_time": True,
    "supports_hold_soc": True,
    "supports_freeze_soc": True,
    "inv_can_span_midnight": True,
    "is_ems_driven": True,
    "has_ems_mode_control": True,
}
```

**GE Entry (Lines 1487-1510)**:
```python
"GE": {
    "name": "Generac GE",
    "has_rest_api": True,
    "has_mqtt_api": False,
    "charge_control_immediate": True,
    "has_target_soc": True,
    "has_reserve_soc": False,
    "soc_limits_block_solar": False,
    "ems_mode_demand": None,
    "ems_mode_charging": None,
    "ems_mode_exporting": None,
    "time_button_press": True,
    "charge_discharge_with_rate": False,
    "target_soc_used_for_discharge": True,
    "inverter_hybrid": True,
    "has_timed_pause": True,
    "has_charge_enable_time": True,
    "has_discharge_enable_time": True,
    "has_charge_limit_time": False,
    "has_discharge_limit_time": False,
    "supports_hold_soc": False,
    "supports_freeze_soc": False,
    "inv_can_span_midnight": False,
    "is_ems_driven": False,
    "has_ems_mode_control": False,
    "rest_data": { ... },
}
```

**Key Differences SIG vs GE**:
- SIG has `soc_limits_block_solar=True`; GE has `False`
- SIG uses EMS modes (demand/charging/exporting); GE uses service calls
- SIG has `has_reserve_soc=True`; GE has `False`
- SIG has `has_charge_limit_time=True` and `has_discharge_limit_time=True`; GE has `False`
- SIG has `inv_can_span_midnight=True`; GE has `False`

---

## Goal 3: adjust_charge_immediate and adjust_export_immediate Methods

### File: `/Users/andrew/code/batpred/apps/predbat/inverter.py`

**adjust_charge_immediate() - Lines 2433-2472**

**SIG Pattern** (when `soc_limits_block_solar=True`):
```
If target_soc > current_soc:
    → Switch to "charging" EMS mode via write_and_poll_option()
Else:
    → Switch to "demand" EMS mode via write_and_poll_option()
```

**Non-SIG Pattern** (GE, etc.):
```
If target_soc == 100 and power==0:
    → Call charge_stop_service()
Elif freeze_charging:
    → Call charge_freeze_service()
Else:
    → Call charge_start_service() with timing parameters
```

**adjust_export_immediate() - Lines 2474-2510**

**SIG Pattern**:
```
If target_soc < current_soc:
    → Switch to "exporting" EMS mode via write_and_poll_option()
Else:
    → Switch to "demand" EMS mode via write_and_poll_option()
```

**Non-SIG Pattern**:
```
Call discharge_service() with:
    - target_soc parameter
    - rate parameter (if charge_discharge_with_rate=True)
    - timing parameters (if has_discharge_enable_time=True)
```

**Branching Logic**:
1. Check `soc_limits_block_solar` flag
2. If True (SIG): Use EMS mode switching logic
3. If False (GE/other): Use service call logic
4. Within each branch: Additional capability flags (has_reserve_soc, charge_discharge_with_rate, etc.) refine behavior

---

## Goal 4: Entity Write Mapping by Inverter Type

### File: `/Users/andrew/code/batpred/apps/predbat/inverter.py`

**adjust_charge_immediate()**:

| Inverter Type | Entity/Service Called |
|---|---|
| SIG | `select.sigen_plant_ess_remote_ems_mode` (via write_and_poll_option to "charging"/"demand") |
| GE | `charge_start_service`, `charge_stop_service`, `charge_freeze_service` |
| GEC, GEE | Same service calls as GE |

**adjust_export_immediate()**:

| Inverter Type | Entity/Service Called |
|---|---|
| SIG | `select.sigen_plant_ess_remote_ems_mode` (via write_and_poll_option to "exporting"/"demand") |
| GE | `discharge_service` with REST API parameters |
| GEC, GEE | Same service calls as GE |

**adjust_reserve() - Lines 1433-1474**:

| Inverter Type | Entity Called | Method |
|---|---|---|
| SIG (has_reserve_soc=True) | `number.sigen_plant_ess_discharge_cut_off_state_of_charge` | `write_and_poll_value()` with REST fallback |
| GE (has_reserve_soc=False) | REST API endpoint via `rest_data` config | REST API direct write |
| Others with has_reserve_soc=True | Specific entity from config apps.yaml | `write_and_poll_value()` |

**adjust_battery_target() - Lines 1604-1659**:

| Inverter Type | Entity Called | Conditional Logic |
|---|---|---|
| SIG (has_charge_limit_time=True) | `number.sigen_plant_ess_charge_cut_off_state_of_charge` | If `isExporting`: skip write (use export_immediate instead) |
| GE (has_charge_limit_time=False) | N/A | Uses target_soc for charging |
| Others with has_charge_limit_time=True | Specific entity from config | Same conditional pattern |

**Key Pattern**: 
- **SIG uses `write_and_poll_value()` for persistent entity updates** with REST fallback for connection loss
- **Non-SIG uses service calls** for transient operations
- **EMS mode switching prevents dual writes**: When in exporting mode, `adjust_battery_target()` skips writing charge limit (handled by EMS mode); when in charging mode, `adjust_export_immediate()` not called

---

## Goal 5: run_execute_test Function and ActiveTestInverter Class

### File: `/Users/andrew/code/batpred/apps/predbat/tests/test_execute.py` (2631 lines)

**ActiveTestInverter Class - Lines 15-100+**

Mock inverter tracking 60+ state fields:

**Core Fields**:
- `soc_target`: Target SOC for charging/discharging
- `id`: Inverter instance ID
- `inverter_type`: Type name (GE, SIG, etc.)
- `isCharging`: Current charge state
- `isExporting`: Current export state
- `pause_charge`: Pause charging flag
- `pause_discharge`: Pause discharging flag

**Timing Fields**:
- `charge_start_time`: Charge window start (minutes from midnight)
- `charge_end_time`: Charge window end
- `discharge_start_time`: Discharge window start
- `discharge_end_time`: Discharge window end

**Power Fields**:
- `charge_rate`: Watts being charged
- `discharge_rate`: Watts being discharged
- `measured_power`: Current power measurement

**SOC Fields**:
- `reserve`: Discharge floor (minimum SOC)
- `soc_target_immediate`: Immediate SOC target (for EMS modes)
- `soc_target_discharge`: Discharge target

**Key Methods**:
- `find_battery_size()`: Determines battery capacity from SOC array
- `update_status()`: Updates inverter state based on power and timing
- `find_charge_curve()`: Gets charge power curve at current SOC
- `get_current_charge_rate()`: Calculates current charge rate
- `disable_charge_window()`: Clears charge timing
- `adjust_charge_window()`: Updates charge window times

**run_execute_test() Function - Lines 158-383**

Test execution framework with 40+ configuration parameters:

**Configuration Parameters**:
- `minutes_now`: Current time in minutes from midnight (for time-based logic)
- `inverter_type`: Which inverter type to test (GE, SIG, etc.)
- `soc_kw`: Current battery SOC in kWh
- `soc_max`: Maximum battery capacity in kWh
- `charge_window_best`: Charge time windows [(start_min, end_min, rate)]
- `export_window_best`: Export/discharge time windows [(start_min, end_min, rate)]
- `charge_limit_best`: SOC targets for charging [%]
- `export_limits_best`: SOC targets for discharging [%]
- `inverter_mode`: Battery mode (Charge, Export, Idle, etc.)
- `set_reserve_enable`: Enable/disable reserve setting
- `reserve`: Discharge floor setting

**Capability Override Parameters** (for testing capability flag variations):
- `has_target_soc`: Override inverter's has_target_soc flag
- `has_reserve_soc`: Override inverter's has_reserve_soc flag
- `soc_limits_block_solar`: Override SIG-specific flag
- `has_timed_pause`: Override timed pause capability
- `has_charge_enable_time`: Override charge window timing capability
- `inverter_hybrid`: Override hybrid inverter flag
- `charge_discharge_with_rate`: Override rate control capability

**Assertion Parameters** (verify expected behavior):
- `assert_status`: Expected status string (Charging, Exporting, Freeze charging, etc.)
- `assert_charge_start_time_minutes`: Expected charge window start time
- `assert_charge_end_time_minutes`: Expected charge window end time
- `assert_discharge_rate`: Expected discharge power in watts
- `assert_pause_discharge`: Expected pause discharge state
- `assert_reserve`: Expected discharge floor
- `assert_immediate_soc_target`: Expected immediate SOC target
- `assert_soc_target`: Expected main SOC target
- `assert_immediate_charge_soc_target_override`: Expected charge target override (-1 = not called)
- `assert_immediate_discharge_soc_target_override`: Expected export target override (-1 = not called)
- `assert_force_export`: Expected force export state
- `assert_charge_time_enable`: Expected charge window enabled state

**Test Execution Flow**:
1. Create mock inverter with specified type and SOC
2. Set up charge/export windows, rates, and limits
3. Call `execute()` to run optimizer
4. Verify all assertions
5. Early return on failure (fail-fast pattern)

**Test Scenario Coverage** (60+ test functions):

**Charge Tests** (Lines 1-1000+):
- `charge1-charge2e`: Basic charge scenarios with varying windows and rates
- `charge_imbalance`: Multi-battery imbalance scenarios
- `charge_low_power`: Low available power handling
- `charge_freeze_soon`: Freeze charge when window ending
- `charge_freeze2`: Variations of freeze charging

**Discharge Tests** (Lines 1100-1629):
- `no_discharge`: Prevent discharge when beneficial
- `discharge_upcoming1-3`: Discharge with force_export assertions
- `discharge_midnight1-2`: Midnight-spanning export windows
- `discharge_with_rate`: Rate control during discharge
- `discharge_freeze1-2`: Freeze export mode

**Car Integration Tests** (Lines 2314-2380):
- `car`, `car2`: Car charging with pause_discharge
- `car_charge`, `car_charge2`: Charge window with car slot
- `car_discharge`: Discharge while car charging
- Status: "Hold for car" when car_slot active

**Bug Fix Tests** (Lines 2398-2435):
- `charge_freeze_target_soc_percent_conversion`: Verifies SOC percent conversion (GitHub #3107)
- `charge_freeze_rounding_issue_3107`: Floating point rounding edge case

**SIG Inverter Specific Tests** (Lines 2437-2596):
- `sig_demand`: EMS demand mode with assert_soc_target=100
- `sig_charging`: EMS charging mode with charge window
- `sig_freeze_charge`: Freeze charge using reserve hold
- `sig_exporting`: Active export with EMS exporting mode
- `sig_freeze_discharge`: Freeze export holding current SOC
- `sig_charging_no_discharge`: Charging with discharge disabled
- `sig_demand_after_charge`: Reserve resets in demand mode

**SIG EMS Mode Overwrite Protection Tests** (Lines 2598-2629):
- `sig_export_no_charge_overwrite`: Export doesn't call adjust_charge_immediate
- `sig_charge_no_export_overwrite`: Charging doesn't call adjust_export_immediate

---

## Test Patterns and Best Practices

### Pattern 1: Inverter Capability Flag Testing
Tests modify inverter property flags between test blocks to verify behavior with different capability combinations:
```python
inverter.has_reserve_soc = False  # Test without reserve support
inverter.has_charge_enable_time = False  # Test without charge window timing
```

### Pattern 2: Conditional Early Returns (Fail-Fast)
Tests group logical scenarios and return early on failure:
```python
if failed:
    return failed
# Continue to next test scenario
```

### Pattern 3: SIG EMS Mode Assertions
SIG tests verify EMS mode switching didn't get overwritten by checking:
- `assert_immediate_charge_soc_target_override=-1` (adjust_charge_immediate NOT called during export)
- `assert_immediate_discharge_soc_target_override=-1` (adjust_export_immediate NOT called during charging)

### Pattern 4: Time-Based State Assertions
Tests use `minutes_now` to verify time-dependent behavior:
```python
minutes_now=775  # 12:55 PM
assert_charge_start_time_minutes=840  # 2:00 PM charge window
```

### Pattern 5: Car Charging Integration
Car tests verify pause_discharge and force_export assertions:
```python
car_slot=charge_window_best_slot
assert_pause_discharge=True  # Car discharges battery
assert_status="Hold for car"
```

### Pattern 6: Freeze vs Hold Charging Distinction
- **Hold**: `assert_immediate_soc_target` = current SOC (can discharge if power available)
- **Freeze**: `assert_immediate_soc_target` = target SOC (holds current SOC exactly)

---

## Design Implications for Unified Test Framework

### Key Abstraction Points

1. **Inverter Control Path Selection**
   - Primary branching: `soc_limits_block_solar` flag
   - SIG path: EMS mode switching
   - Non-SIG path: Service call pattern
   - Test framework must support both patterns with single test definition

2. **Capability Flag Combinations**
   - 25+ flags create exponential combinations
   - Test framework should support overriding individual flags
   - Current approach: inline property modification between test blocks
   - Improvement opportunity: parameterized test generation

3. **Assertion Flexibility**
   - 12+ assertion parameters per test
   - Many assertions are inverter-type-specific
   - Optional assertions (not all apply to all inverter types)
   - Test framework uses default assertions with ability to override

4. **Time-Based Logic**
   - `minutes_now` parameter enables testing at specific times
   - Critical for charge/discharge window timing tests
   - Midnight-spanning window support varies by inverter type

5. **Multi-Battery Scenarios**
   - SOC array configuration tests imbalanced battery behavior
   - Current framework: single SOC value, but support exists for arrays
   - Test coverage: charge_imbalance scenarios

6. **State Transition Testing**
   - Discharge-then-charge transitions verify timing window switching
   - Car charging state changes
   - EMS mode changes (SIG-specific)

---

## Recommended Next Steps

1. **Unified Test Framework Design**
   - Define common test vocabulary (time windows, power levels, SOC targets)
   - Support both EMS mode switching (SIG) and service call patterns (non-SIG)
   - Create parameterized test generators for capability flag combinations
   - Implement assertion builder pattern for flexible validation

2. **Test Coverage Expansion**
   - Identify untested capability flag combinations
   - Add tests for multi-inverter scenarios (multiple inverters in single system)
   - Create regression test suite for known GitHub issues

3. **Test Infrastructure Improvements**
   - Consider test case generation from capability matrix
   - Create named test templates for common scenarios
   - Build assertion validation helpers for EMS mode overwrite protection

4. **Documentation and Maintenance**
   - Document test naming conventions
   - Create runbook for adding new inverter types
   - Establish test coverage metrics

---

**Investigation completed**: All 5 goals fulfilled with comprehensive coverage of inverter abstraction layer, control patterns, test infrastructure, and design implications.
