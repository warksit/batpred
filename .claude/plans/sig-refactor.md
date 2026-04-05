# SIG Inverter Refactor: Use standard entity mapping

## Status: COMPLETE
All code changes done and tests passing on `sig-inverter-control` branch.
Mum's apps.yaml already configured with standard entities (done in prior session).

## Problem with current design

SIG uses `has_target_soc: False` and `has_reserve_soc: False` in INVERTER_DEF, with all SIG-specific logic isolated in `adjust_sig_mode()`. This works but:

1. **SIG is a special case everywhere** — `adjust_charge_immediate` and `adjust_export_immediate` both have `if self.inv_has_ems_mode_control: ... return` early-exit blocks
2. **Separate entity namespace** — SIG uses `sig_remote_ems_mode`, `sig_discharge_cut_off_soc`, `sig_charge_cut_off_soc` args instead of standard ones
3. **`has_target_soc: False` is a lie** — SIG has a target SOC concept (charge_cut_off_soc)
4. **Workarounds due to wrong reserve mapping** — `reserve` was mapped to `discharge_cut_off_soc` which is dual-purpose (reserve + freeze). This caused:
   - `adjust_reserve` had to be a no-op for SIG to prevent feedback loop (freeze writes high value → read back as reserve_percent)
   - `adjust_sig_mode` had to use `reserve_min` instead of `reserve_percent` to avoid stale values from previous freeze cycles

## Key insight: SIG has separate reserve and discharge floor entities

- `number.sigen_plant_ess_backup_state_of_charge` — **true reserve** (minimum SOC the battery won't go below)
- `number.sigen_plant_ess_discharge_cut_off_state_of_charge` — **discharge floor** (used in freeze/hold charge to prevent discharge, NOT the reserve)
- `number.sigen_plant_ess_charge_cut_off_state_of_charge` — **charge ceiling** (target SOC / used in freeze export to block charging)

With the correct mapping, the workarounds go away:
- `adjust_reserve` works normally — writes to `backup_state_of_charge`, no feedback loop
- `reserve_percent` is read from the real reserve entity, always correct
- `discharge_cut_off_soc` is only written during freeze/hold modes

## Proposed refactor

### INVERTER_DEF changes

```python
"SIG": {
    "has_target_soc": True,
    "has_reserve_soc": True,
    "soc_limits_block_solar": True,   # NEW — charge_limit/reserve affect solar too
    "has_ems_mode_control": True,     # keep for EMS mode switching
    ...
}
```

When `soc_limits_block_solar` is True:
- `charge_limit` (charge_cut_off_soc) blocks ALL charging including solar, not just grid
- "Stop charging" must set charge_limit=100 (not 0)
- "Stop discharging" must set discharge_cut_off_soc=reserve_min (not 100)

### Standard entity mapping in apps.yaml

Replace `sig_*` args with standard ones:
- `charge_limit:` → `number.sigen_plant_ess_charge_cut_off_state_of_charge`
- `reserve:` → `number.sigen_plant_ess_backup_state_of_charge` (true reserve)
- `inverter_mode:` → `select.sigen_plant_ess_remote_ems_mode`

New entity needed for freeze/hold (not a standard predbat arg):
- `sig_discharge_cut_off_soc:` → `number.sigen_plant_ess_discharge_cut_off_state_of_charge` (only written during freeze/hold charge)

### Code changes

1. **`config.py`** — Add `soc_limits_block_solar` flag to SIG INVERTER_DEF, set `has_target_soc: True`, `has_reserve_soc: True`

2. **`inverter.py`**:
   - `adjust_reserve` — Remove the `if inv_has_ems_mode_control: return` no-op. Standard flow works now that reserve maps to `backup_state_of_charge`
   - `adjust_charge_immediate` — Replace `if has_ems_mode_control: adjust_sig_mode(); return` with `soc_limits_block_solar`-aware logic:
     - "demand" (target_soc=0): set charge_limit=100, EMS=MSC
     - "freeze/hold" (target==soc or freeze): write discharge_cut_off_soc=current_soc, EMS=MSC
     - "charging" (target>0): set charge_limit=target, EMS=Command Charging
   - `adjust_export_immediate` — Same pattern:
     - "demand" (target_soc=100): reset discharge_cut_off_soc=reserve_min, EMS=MSC
     - "freeze" (target==soc or freeze): set charge_limit=current_soc, EMS=MSC
     - "exporting" (target<100): EMS=Command Discharging
   - `adjust_sig_mode` — Remove entirely, logic absorbed into standard flow

3. **`execute.py`** — Audit `target_soc = 0` usage. With `has_target_soc: True`, code paths that use target=0 to mean "not charging" may break.

4. **`tests/inverters/sig/__init__.py`** — Update entity names in expectations to match new mapping. Expected values should NOT change (that's the whole point of the tests).

### Why it was rejected before (PR #3124)

Setting `has_target_soc=True` broke freeze charge because execute.py set target to current_soc%, which for SIG means charge_cut_off_soc=current% → blocks solar. The `soc_limits_block_solar` flag handles this by inverting the "stop" semantics.

### Risk areas

- Every place execute.py uses `target_soc = 0` to mean "no charge target" needs checking
- SIG `discharge_cut_off_soc` in MSC mode: if SoC < floor, inverter grid-charges. Must only be set high during freeze/hold, reset to reserve_min otherwise
- Verify that `adjust_battery_target` works correctly with `has_target_soc: True` + `soc_limits_block_solar`

### Test strategy

The SIG mode table tests lock down the observable entity state. The refactor changes internals but the test expectations (entity values) stay the same — just with updated entity names. Run tests after each change to catch regressions.

### Files to change

- `config.py` — SIG INVERTER_DEF flags + `soc_limits_block_solar`
- `inverter.py` — `adjust_charge_immediate`, `adjust_export_immediate`, `adjust_reserve`; remove `adjust_sig_mode`
- `execute.py` — Audit target_soc=0 usage
- `tests/inverters/sig/__init__.py` — Update entity names in expectations
- Mum's `apps.yaml` — Switch from `sig_*` entities to standard `charge_limit`, `reserve`, `inverter_mode`
