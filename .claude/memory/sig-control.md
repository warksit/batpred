# SIG Inverter Control Notes

## Current Architecture (BROKEN)
Predbat writes to intermediate helper entities (`input_number.charge_rate`, `input_select.predbat_requested_mode`).
An HA automation bridges those helpers to actual SIG integration entities/registers.
This automation was a workaround from PR #2224 (Apr 2025) when the Sigenergy integration was raw modbus only.
The newer TypQxQ integration (`github.com/TypQxQ/Sigenergy-Local-Modbus`) exposes proper HA entities, making the automation unnecessary.

## SIG Remote EMS Modes (register 40031)
| Value | Mode | Description |
|---|---|---|
| 0 | PCS Remote Control | Direct remote control |
| 1 | Standby | Stops ALL charge and discharge (no solar either) |
| 2 | Maximum Self Consumption | PV priority, battery covers load, grid covers remainder |
| 3 | Command Charging (Grid First) | Charge from grid first |
| 4 | Command Charging (PV First) | Charge from PV first |
| 5 | Command Discharging (PV First) | Battery discharges, exports to grid |
| 6 | Command Discharging (ESS First) | Battery discharges, exports to grid |

## Key SIG Entities (TypQxQ integration)
- `select.sigen_plant_remote_ems_control_mode` — main mode control
- `switch.sigen_plant_remote_ems_controlled_by_home_assistant` — enables remote EMS control
- `number.sigen_plant_ess_charge_cut_off_state_of_charge` — charge ceiling (blocks ALL charging including solar)
- `number.sigen_plant_ess_discharge_cut_off_state_of_charge` — discharge floor + grid-charge target in MSC mode
- `number.sigen_plant_ess_max_charging_limit` — max charge rate in kW. 0 = BLOCKS charging
- `number.sigen_plant_ess_max_discharging_limit` — max discharge rate in kW. Works in Command Discharge modes (5/6/7), IGNORED in MSC
- `number.sigen_plant_grid_import_limitation` — max grid import in kW. 0 = block grid import (but overrides discharge floor!)
- `number.sigen_plant_grid_export_limitation` — max grid export in kW (currently 4kW)

## Sign Convention
- Battery power: **negative = discharging**, positive = charging
- Grid power: **negative = exporting**, positive = importing

## Verified Behaviours (tested 2026-03-04/05 on Mum's HA via MCP)
- `charge_cut_off_soc = current%` → blocks ALL charging including solar ✅
- `charge_cut_off_soc = current%` with solar producing → solar exports, battery 0 kW ✅
- `discharge_cut_off_soc = current%` (SoC at floor) → battery holds, solar charges, grid ~0 ✅
- `discharge_cut_off_soc > current%` → MSC mode grid-charges battery UP to the floor ⚠️
- `ess_max_charging_limit = 0` → blocks charging ✅
- `ess_max_charging_limit = 9600` → charging works at full rate ✅
- `ess_max_discharging_limit` → works in Command Discharge modes (Mode 5/6/7), IGNORED in MSC. Earlier "unwritable" conclusion was from testing in MSC. Confirmed working: Test #5 Mode 7 + 0.5kW, Test #8 Mode 7 + 2kW ✅
- `grid_import_limitation = 0` → blocks grid import, BUT battery discharges to cover load (overrides discharge floor!) ✅
- MSC mode with grid allowed → grid covers load only, does NOT charge battery ✅
- Command Discharging (PV First) → battery discharges, exports to grid ✅
- Command Discharging (ESS First) → battery discharges, exports to grid ✅

## Critical Behaviour: discharge_cut_off_soc as Grid-Charge Target
In MSC mode, if SoC < discharge_cut_off_soc, the inverter will **grid-charge** to reach the floor.
This means for freeze charge, set discharge_cut_off_soc = current SoC (not above it) to avoid unwanted grid charging.

## Control Mode Table (UPDATED after testing)
| Predbat Mode | remote_ems_mode | discharge_cut_off_soc | charge_cut_off_soc | Notes |
|---|---|---|---|---|
| **Demand** | Maximum Self Consumption | reserve% | 100% | Normal — solar charges, battery covers load |
| **Charging** | Command Charging (PV First) | reserve% | 100% | Mode switch needed — MSC won't grid-charge (unless SoC < discharge floor) |
| **Freeze Charge** | Maximum Self Consumption | current_soc% | 100% | Battery holds, solar charges. Set floor = current SoC exactly, not above |
| **Freeze Discharge** | Maximum Self Consumption | reserve% | current_soc% | Blocks ALL charging incl solar. Battery covers load. |
| **Exporting** | Command Discharging (ESS First) | reserve% | 100% | Forces battery discharge + export. Both discharge modes work |

**Key insights (standard Predbat modes)**:
- `grid_import_limitation` NOT used for standard modes — overrides discharge floor, dangerous
- `ess_max_charging_limit` writable but not needed for standard modes — cut-off SOC registers handle freeze
- `ess_max_discharging_limit` works in Command Discharge modes (5/6/7), IGNORED in MSC
- All standard control via: mode switching + charge_cut_off_soc + discharge_cut_off_soc

**Key insights (curtailment manager — validated 2026-03-17)**:
- Curtailment uses Command Discharging (ESS First) + `grid_export_limitation` as single control lever
- `grid_export_limitation` in discharge mode = TARGET (SIG fills up to it). In charge mode = CEILING only (not useful)
- `grid_import_limitation = 0` used as safety net during curtailment (prevents grid import in charge mode)
- HA automation adjusts `grid_export_limitation` = min(PV - load, 4kW) every ~5s
- SIG CLS shutdown only triggers when battery DISCHARGE contributes to export; PV passthrough alone is safe
- SMA Home Manager has separate curtailment at 4kW (was 3.5kW, changed 2026-03-17)
- SIG installer hard export limit: 4.5kW (safety backstop)

## Git History of SIG Support
- PR #2224 (Adam Zebrowski, Apr 2025) — Original SIG support with raw modbus automation
- PR #3007 (iangregory, Nov 2025) — Updated SIG automations, still automation-based
- PR #3124 (Dec 2025) — Reverted `has_target_soc=True` because it broke freeze charge (set SoC to 0%)
- PR #3118 (Dec 2025) — Minor doc fix to automation

## Sigenergy Integration Source
Cloned to `/tmp/sigenergy-modbus` (from `github.com/TypQxQ/Sigenergy-Local-Modbus`)
Register definitions in `modbusregisterdefinitions.py`, entity configs in `number.py`, `select.py`, `sensor.py`
- `ess_max_charging_limit` = register 40032 (HOLDING, U32, gain=1000)
- `ess_max_discharging_limit` = register 40034 (HOLDING, U32, gain=1000) — works in Command Discharge modes, ignored in MSC

## HA Access
- Mum's HA MCP: `mcp__claude_ai_Middlemuir_Homeassistant_Mums__` prefix
- Predbat add-on config path: `/addon_configs/6adb4f0d_predbat/`

## Branch Status
- `sig-inverter-control` branch — refactored to standard entity mapping (commit 49267de)
- Merged into `mum` branch, deployed to Mum's HA (2026-03-08)
- Not yet pushed to origin or PR'd upstream

## Bug: Oscillation (FIXED in 3e40d4d)
`adjust_battery_target` called `adjust_sig_mode("charging")` → then `adjust_charge_immediate(freeze=True)` called `adjust_sig_mode("freeze_charge")`. Two conflicting writes per Predbat cycle (~5min). Fix: removed adjust_sig_mode from adjust_battery_target entirely. SIG mode control now exclusively via adjust_charge_immediate / adjust_export_immediate.

## Bug: battery_min_soc forcing set_reserve_min (FIXED via config)
Mum's apps.yaml had `battery_min_soc: 10`. Predbat enforces `set_reserve_min >= battery_min_soc`. Changed to 0.

## has_target_soc History
Previously rejected (PR #3124) because target=0 blocked solar and freeze set target=current_soc blocking solar.
Now RESOLVED via `soc_limits_block_solar` flag — `has_target_soc: True` with inverted "stop" semantics (100=stop, not 0).

## Mum's apps.yaml Key Mappings (UPDATED after refactor)
- `reserve:` → `number.sigen_plant_ess_backup_state_of_charge` (true reserve)
- `charge_limit:` → `number.sigen_plant_ess_charge_cut_off_state_of_charge` (charge ceiling)
- `inverter_mode:` → `select.sigen_plant_ess_remote_ems_mode` (EMS mode)
- `sig_discharge_cut_off_soc:` → `number.sigen_plant_ess_discharge_cut_off_state_of_charge` (freeze/hold only)
- `charge_rate:` / `discharge_rate:` → real hardware entities (rate=0 guard prevents writing 0)
- Local reference: `.claude/mum-apps.yaml`

## Mum's Tariff Notes
- Deemed export (export rate effectively £0)
- Cosy Octopus-style rates (cheap windows ~14p, expensive ~43p)
- 18 kWh battery (SigenStor EC 10.0 SP)
