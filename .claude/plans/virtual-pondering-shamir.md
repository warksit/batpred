# Fix: SIG charge_limit blocks solar → CLS fault on over-export

## Context

CLS fault on SIG at 13:05 on 2026-03-23. The charge ceiling (`ess_charge_cut_off_state_of_charge`) was set below 100%, which on SIG (`soc_limits_block_solar=True`) blocks ALL charging including solar. Excess solar that couldn't enter the battery was forced to export, exceeding the 4.5kW hard limit → inverter fault.

**Root cause:** On SIG, the charge ceiling blocks solar. It should ALWAYS be 100%. Charge control (stop charging at target) is handled by EMS mode switching (Grid First → MSC), not by the charge ceiling. The discharge floor (`sig_discharge_cut_off_soc`) handles preventing discharge below target.

## Current SIG special case (execute.py:645)

```python
if soc_limits_block_solar and (isFreezeCharge or soc == 0):
    soc = 100.0  # Force charge_limit=100% to allow solar
```

This covers freeze charge and demand (soc=0), but **NOT hold charge or active charge** — those set charge_limit to target%, blocking solar.

## Fix

### Change: Always force charge_limit=100% on SIG

**File: `apps/predbat/execute.py` line 645**

```python
# Current:
if getattr(inverter, "inv_soc_limits_block_solar", False) and (isFreezeCharge or soc == 0):
    soc = 100.0

# Fixed: ALWAYS set charge_limit=100% on SIG — charge ceiling blocks solar
if getattr(inverter, "inv_soc_limits_block_solar", False):
    soc = 100.0
```

**Why this is safe:** On SIG, charging is stopped by switching EMS mode (Grid First → MSC), not by the charge ceiling. The worst case is overshooting the target by ~4% between Predbat cycles during overnight grid charging (8.8kW × 5min = 0.73kWh on 18kWh battery) — harmless extra stored energy, far better than the CLS fault risk from blocking solar.

The discharge floor (`sig_discharge_cut_off_soc`) already correctly prevents discharge below target, set by `adjust_ems_mode` in `adjust_charge_immediate`:
- Hold above target → MSC, discharge_floor=target_soc (line 2480-2482)
- Freeze → MSC, discharge_floor=reserve (line 2483-2485)
- Demand → MSC, discharge_floor=reserve (line 2483-2485)

### Update docstring

Update `adjust_battery_target_multi` docstring to explain that SIG always uses 100% charge ceiling.

## Files to modify

- `apps/predbat/execute.py` — simplify condition in `adjust_battery_target_multi` (line 645)

## Verification

1. `pre-commit run --all-files`
2. `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
3. Deploy to Mum's HA and verify charge_limit stays at 100% in all modes
