---
name: Curtailment Manager Plan
description: Real-time throttled PV charging to avoid curtailment on high-solar days
type: project
---

# Curtailment Manager

## The Problem
On high-solar days (>38 kWh), battery fills to 100% while PV still exceeds the 4kW export limit. SMA curtails excess. Once battery is full, energy is wasted.

## The Solution: Discharge Mode + Dynamic Export Limit (validated 2026-03-17)

### Discovery: Discharge ESS First mode is the key
Hardware testing on 2026-03-17 proved that **Command Discharging (ESS First)** + `grid_export_limitation` is the single control lever needed. The SIG automatically:
- Exports up to the export limit
- Charges battery with any PV excess above the limit
- Discharges battery to fill up to the limit if PV excess < limit
- Handles load spikes naturally (battery covers deficit)

### Layer 1: HA Automation (fast, ~5s response) — DEPLOYED
`automation.curtailment_manager_dynamic_export_limit`
- Triggers on PV/load sensor changes (~5s updates)
- Sets `grid_export_limitation = min(PV - load, 4kW)`
- Prevents unnecessary battery discharge to meet export target
- Only fires when mode is "Command Discharging (ESS First)"
- Mode: restart (no queue buildup from rapid PV changes)

### Layer 2: Predbat (planning, 5-min cycle) — TO BUILD
- Calculates target SOC from forecast look-ahead (reservation for future excess)
- Switches into/out of Discharge ESS First mode
- Publishes `predbat_target_export` sensor for HA automation to use as cap (replacing hardcoded 4kW)
- Sets `grid_import_limitation = 0` when curtailment manager is active
- Handles pre-solar drain phase (set export_limit > PV excess to force battery discharge)
- Restores normal MSC operation when curtailment manager deactivates

## Hardware Test Results (2026-03-17)

### Test 1: Mode 4 + charge_rate=0 + grid_import=0
- Battery idle, all PV excess exports ✅
- Export overshot to 4.9kW briefly (SMA hadn't curtailed yet) ⚠️
- SIG did NOT trigger CLS error — CLS only triggers when battery discharge contributes to export

### Test 2: Mode 4 + charge_rate=1kW + grid_import=0
- Battery charged at exactly 0.99kW ✅
- Precise charge rate control confirmed ✅

### Test 3: Discharge ESS + export_limit=2kW
- Export precisely 2.03kW ✅
- Battery absorbed remaining excess (+1.8kW) ✅
- Transition from 2kW to 3kW: smooth, no mode switch needed ✅

### Test 4: Discharge ESS + export_limit=4kW (PV excess < 4kW)
- Battery discharged to top up export to ~4kW ✅
- Confirms SIG actively fills gap between PV excess and export limit

### Test 5: Discharge ESS + export_limit=4kW + discharge_rate=0
- Battery idle, only PV excess exported ✅
- But load spikes cause brief grid import (battery can't respond) ⚠️

### Test 6: Dynamic automation (export_limit = PV - load, cap 4kW)
- Battery near-idle (~0.05kW), export tracks PV excess perfectly ✅
- Adjusts every ~5s as PV/load change ✅
- No SMA curtailment when export < 4kW ✅

### Key Finding: SIG CLS Error
CLS shutdown only triggers when **battery discharge** contributes to excess export. PV passthrough alone does NOT trigger CLS — that's the SMA's problem to curtail. Mode 4 with battery idle is inherently safe.

### Key Finding: grid_export_limitation behaviour by mode
- **Discharge mode**: Acts as a TARGET — SIG exports up to this, battery fills gap or absorbs excess ✅
- **Charge mode (Mode 4)**: Acts as a CEILING only — SIG prioritises battery over export ❌ not useful as control

### Key Finding: SMA curtailment
- SMA Home Manager has independent "Dynamic active power limitation"
- Was set to 3.5kW, now changed to 4kW to match DNO limit
- SIG installer hard limit remains at 4.5kW as safety backstop
- SMA curtails based on grid export power at meter

## Control Model Summary

**Single mode**: Command Discharging (ESS First)
**Single control lever**: `grid_export_limitation`
**Safety**: `grid_import_limitation = 0`

| Desired behaviour | export_limit | Result |
|--|--|--|
| Export all PV excess | PV - load (via automation) | Battery idle, PV excess exports |
| Absorb excess above X | X | Exports X, battery charges remainder |
| Free charge (no export) | 0 | All PV to load + battery |
| Drain battery | > PV excess | Battery discharges to fill gap |

## Implementation Order
1. ~~Test HA automation~~ ✅ DONE (2026-03-17)
2. ~~HA automation deployed~~ ✅ DONE (automation.curtailment_manager_dynamic_export_limit)
3. **Build Predbat integration** — next step
4. End-to-end test on high-solar day

## Current State (2026-03-17)
- HA automation deployed and running on Mum's HA
- Predbat in read-only mode during testing
- SIG in Discharge ESS First mode
- grid_import_limitation = 0
- Automation dynamically adjusting export_limitation based on PV - load, capped at 4kW

## Entity Reference
- PV power: `sensor.sigen_plant_pv_power` (kW)
- Load: `sensor.sigen_plant_consumed_power` (kW)
- Battery power: `sensor.sigen_plant_battery_power` (kW, +charge/-discharge)
- Grid export: `sensor.sigen_plant_grid_export_power` (kW)
- Grid import: `sensor.sigen_plant_grid_import_power` (kW)
- Battery SOC: `sensor.sigen_plant_battery_state_of_charge` (%)
- Battery charging: `binary_sensor.sigen_plant_battery_charging` (~5s updates)
- Export limit control: `number.sigen_plant_grid_export_limitation` (kW)
- Import limit control: `number.sigen_plant_grid_import_limitation` (kW)
- EMS mode: `select.sigen_plant_remote_ems_control_mode`
