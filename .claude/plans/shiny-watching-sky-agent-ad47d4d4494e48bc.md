# GivEnergy (GE) Inverter Control Deep Dive - Complete Summary

## Overview
This document summarizes a comprehensive exploration of how GivEnergy inverter control works in the batpred codebase. The investigation traced the complete control flow from capability definitions through individual control methods to the orchestration logic in execute.py.

## 1. INVERTER_DEF Configuration for "GE"

Located in `/Users/andrew/code/batpred/apps/predbat/config.py` (lines 1483-1510):

```python
INVERTER_DEF["GE"] = {
    "has_rest_api": True,
    "charge_control_immediate": False,
    "has_charge_enable_time": True,
    "has_target_soc": True,
    "has_reserve_soc": True,
    "has_ge_inverter_mode": True,
    "inv_default_target_soc": 0,
    "inv_has_discharge_enable_time": False,
    "inv_has_timed_pause": False,
    "inv_has_ems_mode_control": False,
    "battery_rate_max_charge": 3300,
    "battery_rate_max_discharge": 3300,
    ... [other flags]
}
```

### Key Capability Flags and Their Meaning:

| Flag | Value | Meaning |
|------|-------|---------|
| `has_rest_api` | True | GE supports REST API calls for control (rest_setReserve, rest_setChargeTarget, rest_setBatteryMode, rest_enableChargeSchedule) |
| `charge_control_immediate` | False | Must use charge windows or service templates; cannot directly command immediate charge/discharge via simple entity writes |
| `has_charge_enable_time` | True | Supports scheduled charge windows with start/end times (charge_start_time, charge_end_time, scheduled_charge_enable entities) |
| `has_target_soc` | True | Supports charge_limit entity to set target SoC for charging |
| `has_reserve_soc` | True | Supports reserve entity to set minimum SoC (won't discharge below this) |
| `has_ge_inverter_mode` | True | GE-specific mode control (Eco vs Timed Export modes) |
| `inv_has_ems_mode_control` | False | Does NOT have EMS mode; uses service templates instead for charge/discharge commands |
| `battery_rate_max_charge` | 3300 | Maximum charge rate in watts |
| `battery_rate_max_discharge` | 3300 | Maximum discharge rate in watts |

---

## 2. Charge Control for GE: adjust_charge_immediate()

**File:** `/Users/andrew/code/batpred/apps/predbat/inverter.py` (lines 2490-2527)

**Purpose:** Immediately command the inverter to start/stop charging or enter freeze mode (when charge_control_immediate=False for non-EMS inverters like GE).

**Key Insight:** Since GE has `charge_control_immediate: False`, this method:
1. Does NOT write directly to an entity
2. Instead calls Home Assistant service templates defined in configuration
3. Uses templates like `charge_start_service`, `charge_stop_service`, `discharge_stop_service`, `charge_freeze_service`

**Service Templates Called:**
- `charge_freeze_service` - Pause charging at current SoC (freeze mode)
- `charge_start_service` - Begin charging toward target_soc
- `charge_stop_service` - Stop charging
- `discharge_stop_service` - Stop discharging (grid hold mode)

**Service Data Passed:**
```
call_service_template(
    charge_start_service,
    {
        "device_id": device_id,
        "target_soc": target_soc,  # Percentage
        "power": power  # In watts
    }
)
```

**Example Flow:**
- To charge: `adjust_charge_immediate(target_soc=80)` → calls charge_start_service
- To freeze: `adjust_charge_immediate(target_soc=50, freeze=True)` → calls charge_freeze_service
- To hold: `adjust_charge_immediate()` → calls discharge_stop_service then charge_stop_service

---

## 3. Reserve Control: adjust_reserve()

**File:** `/Users/andrew/code/batpred/apps/predbat/inverter.py` (lines 1430-1477)

**Purpose:** Set the minimum battery SoC that the inverter will not discharge below.

**What It Does:**
1. Takes a `reserve_soc` percentage (e.g., 15%)
2. Clamps it between `reserve_percent` (config minimum) and `reserve_max` (usually 100%)
3. Writes to the `reserve` entity via:
   - REST API: `rest_setReserve(reserve_soc)` if available
   - Direct entity write: `write_and_poll_value("reserve", reserve_soc)` with polling confirmation

**Inverter Behavior:**
- The inverter will automatically stop discharging when battery reaches the reserve percentage
- If grid is charging and SoC drops to reserve, inverter grid charging stops
- Reserve acts as a floor - the inverter won't go below this in normal operation

**Example:**
```python
adjust_reserve(15)  # Don't discharge below 15% SoC
```

---

## 4. Battery Target SoC: adjust_battery_target()

**File:** `/Users/andrew/code/batpred/apps/predbat/inverter.py` (lines 1607-1664)

**Purpose:** Set the target SoC percentage that the inverter will charge to when charging is enabled.

**What It Does:**
1. Takes `target_soc` (e.g., 80%)
2. Clamps to minimum (reserve_percent)
3. Writes to `charge_limit` entity via:
   - REST API: `rest_setChargeTarget(target_soc)` if available
   - Direct entity write: `write_and_poll_value("charge_limit", target_soc)` with polling

**For GE Inverters:**
- `target_soc` = the percentage the inverter will charge to
- Once enabled (via charge window), inverter charges to this target then stops
- Works in conjunction with charge windows (adjust_charge_window)

**Example:**
```python
adjust_battery_target(80)  # Charge to 80% SoC
```

**Typical Sequence:**
1. Set reserve: `adjust_reserve(15)` → won't discharge below 15%
2. Set target: `adjust_battery_target(80)` → will charge to 80%
3. Set window: `adjust_charge_window(start_time, end_time)` → charge during this window
4. Enable window: (implicit when adjust_charge_window is called)

---

## 5. Charge Window Control: adjust_charge_window() and disable_charge_window()

**File:** `/Users/andrew/code/batpred/apps/predbat/inverter.py` (lines 2567-2659, 2277-2341)

### adjust_charge_window()

**Purpose:** Configure when the inverter should automatically charge via a scheduled window.

**What It Does:**
1. Reads old charge start/end times from `charge_start_time` and `charge_end_time` entities (or REST data)
2. Calculates new start/end times in minutes, handling midnight-spanning windows via `compute_window_minutes()`
3. Applies clock skew adjustments:
   - `inverter_clock_skew_start`: offset applied to charge_start_time
   - `inverter_clock_skew_end`: offset applied to charge_end_time
4. Converts times to "HH:MM:SS" format
5. If not currently in the new window and times changed: calls `disable_charge_window()` first (prevents charge blip)
6. Writes new start time via `write_and_poll_option("charge_start_time", "HH:MM:SS")`
7. For H:M format: also writes to `charge_start_hour` and `charge_start_minute` entities
8. Writes new end time via `write_and_poll_option("charge_end_time", "HH:MM:SS")`
9. Calls REST API if available: `rest_setChargeSchedule(new_times)`
10. Presses time button if `inv_time_button_press` flag is True
11. Sends notification if `set_inverter_notify` is enabled

**Clock Skew Example:**
```python
# If inverter clock is 5 minutes fast:
inverter_clock_skew_start = 5  # Subtract 5 minutes from desired start
adjust_charge_window(start_min=540, end_min=600)  # 9:00-10:00
# Actually writes: 8:55-9:55 to inverter (accounting for 5 min fast clock)
```

**Example:**
```python
adjust_charge_window(start_min=540, end_min=600)  # 9:00 AM to 10:00 AM
# Inverter will: charge during 9:00-10:00 window, up to target_soc set earlier
```

### disable_charge_window()

**Purpose:** Turn off automatic scheduled charging.

**What It Does:**
1. Reads old charge schedule enabled state from `scheduled_charge_enable` entity
2. Calls `adjust_idle_time()` with midnight times (00:00:00) to set nominal inactive window
3. If charging was enabled: writes False to `scheduled_charge_enable` entity via `write_and_poll_switch()`
4. Calls `rest_enableChargeSchedule(False)` if REST data available
5. For inverters without `has_charge_enable_time`: calls `adjust_charge_window()` with midnight times as fallback
6. Presses time button if `inv_time_button_press` is True
7. Updates cached status if `notify=True`
8. Sends notification if enabled

**Example:**
```python
disable_charge_window()  # Stops all scheduled charging
```

### Key Discovery: No Separate enable_charge_window()

**Important:** There is NO separate `enable_charge_window()` method in the codebase. 
- **Charging is enabled implicitly** when you call `adjust_charge_window()` with valid times
- **Charging is disabled explicitly** by calling `disable_charge_window()`
- This is a two-method pattern, not three

---

## 6. Inverter Mode Control: adjust_inverter_mode()

**File:** `/Users/andrew/code/batpred/apps/predbat/inverter.py` (lines 1955-2009)

**Purpose:** Control whether the inverter is in passive Eco mode or actively exporting during scheduled windows.

**For GE Inverters:**
- Sets `inverter_mode` entity to either:
  - `"Eco"` - Passive mode, inverter follows grid (uses battery to minimize grid draw when grid prices high)
  - `"Timed Export"` - Active export mode, inverter actively exports battery to grid during scheduled windows

**What It Does:**
1. If `force_export=True`: writes "Timed Export" to `inverter_mode` entity
2. If `force_export=False`: writes "Eco" to `inverter_mode` entity
3. Uses `write_and_poll_option()` with retry logic for confirmation
4. Alternative: calls `rest_setBatteryMode()` if REST API available
5. Validates the mode was actually set via polling

**Example:**
```python
adjust_inverter_mode(force_export=True)   # Enable "Timed Export" for active exporting
adjust_inverter_mode(force_export=False)  # Switch to "Eco" mode (passive)
```

**Typical Scenarios:**
- During high export periods: `adjust_inverter_mode(force_export=True)` → enables Timed Export
- During low prices: `adjust_inverter_mode(force_export=False)` → switches to Eco mode
- Combined with charge windows for complete scheduling

---

## 7. Complete Control Flow: From Predbat to GE Inverter

### High-Level Sequence Diagram

```
Batpred Calculation Loop
    ↓
Decision: Charge/Hold/Export/Demand
    ↓
├─ CHARGE MODE:
│  ├─ adjust_reserve(min_soc)           → Write to "reserve" entity
│  ├─ adjust_battery_target(charge_soc) → Write to "charge_limit" entity
│  ├─ adjust_charge_window(start, end)  → Write to "charge_start_time", "charge_end_time"
│  └─ adjust_inverter_mode(force_export=False) → Write "Eco" to "inverter_mode"
│
├─ HOLD MODE (Freeze):
│  ├─ adjust_charge_immediate(soc, freeze=True) → Call charge_freeze_service
│  └─ adjust_inverter_mode(force_export=False)  → Stay in Eco
│
├─ EXPORT MODE:
│  ├─ adjust_reserve(high_soc)              → Don't discharge below this
│  ├─ disable_charge_window()               → Stop scheduled charging
│  ├─ adjust_inverter_mode(force_export=True) → Switch to "Timed Export"
│  └─ adjust_charge_immediate() (discharge) → Call discharge_stop_service
│
└─ DEMAND MODE (Grid Hold):
   ├─ disable_charge_window()        → Stop charging
   ├─ adjust_charge_immediate()      → Call discharge_stop_service
   └─ adjust_inverter_mode(False)    → Stay in Eco
```

### Entities Written By Predbat

For a **complete control sequence**, predbat writes to these Home Assistant entities:

| Entity | Purpose | Example Value | Set By Method |
|--------|---------|---------------|---------------|
| `reserve` | Minimum SoC (won't discharge below) | 15 (percentage) | `adjust_reserve()` |
| `charge_limit` | Target SoC for charging | 80 (percentage) | `adjust_battery_target()` |
| `charge_start_time` | Charge window start time | "09:00:00" | `adjust_charge_window()` |
| `charge_end_time` | Charge window end time | "10:00:00" | `adjust_charge_window()` |
| `charge_start_hour` | Hour component (if H:M format) | 9 | `adjust_charge_window()` |
| `charge_start_minute` | Minute component (if H:M format) | 0 | `adjust_charge_window()` |
| `scheduled_charge_enable` | Enable/disable scheduled charging | on/off | `adjust_charge_window()` / `disable_charge_window()` |
| `inverter_mode` | Eco vs Timed Export | "Eco" or "Timed Export" | `adjust_inverter_mode()` |
| Service templates | Immediate charge/freeze/discharge | via service calls | `adjust_charge_immediate()` |
| REST API | Alternative control path (if enabled) | Various payloads | Various methods |

### Execution Flow in execute.py

**File:** `/Users/andrew/code/batpred/apps/predbat/execute.py`

The orchestration logic:

1. **Lines 74, 185, 217, 452, 475, 617, 698:** Calls `adjust_reserve()` based on:
   - Current battery state
   - Price signals (charge when cheap, discharge when expensive)
   - User configuration

2. **Lines 127, 499-546, 557-590:** Calls `adjust_battery_target()` via `adjust_battery_target_multi()`:
   - Sets different target SoCs for different forecast periods
   - Updates multiple times throughout the day as forecast changes

3. **Lines 188, 220, 230, 234, 277:** Calls `adjust_charge_window()`:
   - When enabling a charge period
   - With calculated start/end times from the prediction algorithm
   - Clock skew automatically applied

4. **Lines 182, 213, 258, 293, 300, 303:** Calls `disable_charge_window()`:
   - When switching out of charge mode
   - Before updating to a new charge window (prevents overlap)
   - When exporting or holding battery

5. **Lines 531, 548, 550, 595, 606, 608:** Calls `adjust_charge_immediate()`:
   - To freeze battery at current SoC
   - To immediately stop charging or discharging
   - To command immediate mode changes via service templates

### Example: "Charge to 80% from 9:00-10:00 AM"

```python
# Step 1: Decide not to export, set charging target
adjust_reserve(15)                    # Don't discharge below 15%
adjust_battery_target(80)             # Charge to 80%

# Step 2: Set charge window
adjust_charge_window(540, 600)        # 9:00 AM to 10:00 AM

# Step 3: Switch to passive mode
adjust_inverter_mode(False)           # Use "Eco" mode

# Result on GE Inverter:
# - During 9:00-10:00: If grid available, charge toward 80%
# - After 10:00: Stop charging (window closed)
# - Never discharge below 15% SoC
# - In Eco mode: use battery to offset grid draw intelligently
```

### Example: "Hold Battery at 50% and Prepare to Export"

```python
# Step 1: Prepare for export
adjust_reserve(60)                         # Don't discharge below 60%
adjust_battery_target(60)                  # If charging, stop at 60%
disable_charge_window()                    # Stop any scheduled charging

# Step 2: Immediate freeze to exact SoC
adjust_charge_immediate(50, freeze=True)   # Pause at 50% exactly

# Step 3: Switch to export mode
adjust_inverter_mode(True)                 # Switch to "Timed Export"

# Result:
# - Battery frozen at 50%
# - Ready to export immediately if opportunity arises
# - Won't charge or discharge below 60% (reserve)
```

---

## 8. Control Architecture Summary

### Three-Layer Architecture:

**Layer 1: Capability Definition (config.py)**
- INVERTER_DEF["GE"] defines what GE can do
- Determines which control methods are available
- Enables/disables features like REST API, charge windows, etc.

**Layer 2: Individual Control Methods (inverter.py)**
- `adjust_reserve()` - Set discharge floor
- `adjust_battery_target()` - Set charge ceiling
- `adjust_charge_window()` - Configure scheduled charge window
- `disable_charge_window()` - Turn off scheduled charging
- `adjust_charge_immediate()` - Immediate charge/freeze/discharge via service templates
- `adjust_inverter_mode()` - Switch between Eco and Timed Export

**Layer 3: Orchestration (execute.py)**
- Calls the control methods in sequence based on:
  - Predicted solar generation
  - Electricity prices
  - Battery state
  - User configuration
  - Day/time of day

### Control Hierarchy:

```
GE Inverter Capabilities (INVERTER_DEF)
    ↓
Individual Control Methods (inverter.py)
    ├─ Backup to each other (REST API vs entity writes)
    ├─ Polling confirmation (ensure changes took effect)
    └─ Retry logic (if write fails)
    ↓
Orchestration Logic (execute.py)
    └─ Decides WHAT to command, WHEN to command it
```

---

## 9. Key Technical Details

### write_and_poll Pattern

Most entity writes use this pattern:
```python
def write_and_poll_value(entity, value):
    write_to_entity(entity, value)          # Write value
    for attempt in range(max_retries):
        actual = read_entity(entity)        # Read back
        if actual == value:
            return True                      # Success
        sleep(poll_delay)
    return False                             # Failed after retries
```

This ensures the inverter actually accepted the command.

### Service Templates

For non-EMS inverters like GE, service templates are defined in Home Assistant configuration:
```yaml
service: "switch.turn_on"
data:
  entity_id: "switch.{{ device_id }}_charge"
  target_soc: "{{ target_soc }}"
```

Predbat calls these templates, which are interpreted by Home Assistant/GivTCP integration.

### Clock Skew Compensation

If the inverter's internal clock is out of sync with Home Assistant:
```python
inverter_clock_skew_start = 5   # Inverter clock is 5 minutes fast
# When setting 9:00 AM start time:
actual_start = 9:00 - 5 min = 8:55  # Write 8:55 to compensate
```

This ensures the inverter charges at the correct time despite clock differences.

---

## 10. Summary Table: GE Control Capabilities

| Capability | Method | Entity/Service | Purpose |
|-----------|--------|-----------------|---------|
| Set discharge floor | `adjust_reserve()` | reserve | Min SoC inverter won't discharge below |
| Set charge target | `adjust_battery_target()` | charge_limit | SoC% inverter charges to when enabled |
| Schedule charging | `adjust_charge_window()` | charge_start_time, charge_end_time | When to automatically charge |
| Stop scheduled charging | `disable_charge_window()` | scheduled_charge_enable | Turn off automatic charging |
| Immediate charge/freeze | `adjust_charge_immediate()` | charge_freeze_service | Freeze at SoC or start charging immediately |
| Mode control | `adjust_inverter_mode()` | inverter_mode | Switch Eco ↔ Timed Export |
| Backup: REST API | Various | REST endpoints | Alternative control path if entities fail |

---

## Conclusion

GE inverter control in batpred is a well-architected three-layer system:

1. **Capabilities** are declared upfront (INVERTER_DEF), enabling code to know what's possible
2. **Control methods** are carefully designed with polling and retry logic to ensure reliability
3. **Orchestration** in execute.py uses these methods to implement complex battery management strategies

The result is a flexible, robust system where predbat can command the GE inverter to charge, hold, export, or enter demand mode by writing to a small set of Home Assistant entities and calling service templates, with automatic fallback to REST API if entity writes fail.

