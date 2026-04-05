---
name: Curtailment Next Session Prompt
description: Comprehensive prompt to resume curtailment manager work after /clear
type: project
---

# Curtailment Manager — Resume Prompt

Paste this after /clear to resume:

---

## Context

We're building a curtailment manager for a SIG (Sigenergy) battery + SMA solar system. The goal: on high-solar days, manage battery charging so it reaches 100% exactly when PV excess drops below the 4kW DNO export limit, maximising export and avoiding SMA curtailment.

Read these memory files for full context:
- `memory/curtailment-plan.md` — hardware test results, control model, HA automation details
- `memory/sig-control.md` — SIG entity mapping, verified behaviours
- `.claude/plans/curtailment-manager-v3.md` — original implementation spec (partially superseded by new findings)

## What's Done

1. **Hardware testing (2026-03-17)**: Proved that **Command Discharging (ESS First)** + `grid_export_limitation` is the single control lever. The SIG automatically exports up to the limit and charges battery with the remainder. No need for dynamic charge_rate calculation or HA automation for fast rate control.

2. **HA automation deployed**: `automation.curtailment_manager_dynamic_export_limit` on Mum's HA. Triggers on PV/load sensor changes (~5s), sets `grid_export_limitation = min(PV - load, 4kW)`. Keeps battery idle when we don't want it draining. Only fires when SIG is in Discharge ESS First mode.

3. **SMA limit changed**: SMA Home Manager export limit raised from 3.5kW to 4kW to match DNO limit.

## Current Live State (check before changing anything!)

- Predbat is in **READ-ONLY MODE** (`switch.predbat_set_read_only` = on) — turn this off when resuming normal operation
- SIG is in **Command Discharging (ESS First)** mode
- `grid_import_limitation = 0` (safety — no grid import)
- HA automation is dynamically adjusting `grid_export_limitation`
- These need to be reset if we're done testing: mode → MSC, grid_import_limitation → 100, grid_export_limitation → 4

## What's Next: Predbat Planning Layer

Build the planning logic in `execute.py` that decides WHEN to activate the curtailment manager and WHAT export limit to target. The HA automation handles fast response; Predbat handles the strategy.

### Core Algorithm

Each Predbat cycle (5 min), using the PV/load forecast:

1. **Should we activate?** Scan forecast — if total predicted curtailment (PV excess > 4kW) exceeds threshold (e.g. 1kWh), activate.

2. **Calculate reservation**: Sum future unavoidable excess (PV - load - 4kW) for remaining solar hours. This is how much battery capacity we MUST keep free to absorb later.

3. **Calculate target SOC**: `target_soc = soc_max - reserved_kwh`. This is what SOC should be RIGHT NOW.

4. **Decide phase**:
   - `SOC > target_soc` → **drain phase**: set export_limit high (> PV excess) so battery discharges to make room. Only safe when PV is low.
   - `SOC ≈ target_soc` → **hold phase**: set export_limit = PV - load (HA automation does this). Battery idle, excess exports.
   - `SOC < target_soc` → **free charge phase**: set export_limit = 0 or switch to MSC. Battery charges freely from solar.
   - PV excess > 4kW → **absorb phase**: set export_limit = 4kW. Battery absorbs everything above 4kW automatically.

5. **Publish sensors** for HA automation:
   - `sensor.predbat_curtailment_phase` (drain/hold/absorb/free_charge/off)
   - `sensor.predbat_curtailment_target_export_kw` (max export for HA automation cap)
   - `sensor.predbat_curtailment_target_soc` (for monitoring)

6. **Set mode**: Switch to Discharge ESS First when active, back to MSC when inactive. Set `grid_import_limitation = 0` when active, restore when inactive.

### Key Implementation Points

- Hook into `execute_plan()` in `execute.py` after read-only check, before charge/export window logic
- When active, skip normal Predbat window logic (`continue` in the inverter loop)
- Use `self.pv_forecast_minute_step` for forecast data (kWh per 5-min step, multiply by 12 for kW)
- Use `self.soc_kw` / `self.soc_max` for battery state
- Export limit entity is `number.sigen_plant_grid_export_limitation` — write via `adjust_ems_mode()` or direct entity write
- `self.export_limit` is stored as W/MINUTE_WATT (kW/60). To get kW: multiply by 60.
- Predbat values in plan are kWh per 30-min slot (multiply by 2 for kW)
- Config items needed: `curtailment_manager_enable` (switch), `curtailment_manager_threshold_kwh` (input_number)

### Files to Modify

1. `apps/predbat/config.py` — add config items
2. `apps/predbat/fetch.py` — read config items
3. `apps/predbat/predbat.py` — add defaults
4. `apps/predbat/execute.py` — core implementation (3 methods on Execute class)

### Update HA Automation

The HA automation currently has a hardcoded 4kW cap. Update it to read from `sensor.predbat_curtailment_target_export_kw` instead, so Predbat controls the max export target.

### Safety

- Never enter drain phase (battery discharge) when PV excess is high — total export could exceed SIG's 4.5kW limit
- Drain phase guard: only when PV excess < export_limit * 0.75
- SIG CLS shutdown only triggers when battery discharge contributes to export; PV passthrough alone is safe
- Always set grid_import_limitation = 0 when in discharge mode to prevent grid import
- Restore all entities (mode, grid_import_limitation, grid_export_limitation) when deactivating
