# SIG Inverter Control + Curtailment Avoidance Plan

## Context

Hardware testing (8 tests on real SIG inverter) revealed that the current SIG control code doesn't properly map Predbat operations to SIG EMS modes. Key issues:
- **Freeze is broken**: Uses MSC + discharge_floor=SOC%, but MSC still allows charging from PV and discharging to load
- **Force export has no rate control**: Sets Mode 7 but doesn't set discharge rate → battery would discharge at 9.6kW max, risking 4.5kW hard shutdown
- **Rate=0 writes are skipped**: Guards at inverter.py:1536,1579 prevent setting rate=0, breaking freeze and charge-during-export suppression
- **adjust_force_export() is a no-op**: SIG has no time windows, so the call does nothing

Curtailment avoidance depends on force export working correctly, so **SIG control must be fixed first**.

## Phase A: SIG Inverter Control

### A1. Expand `adjust_ems_mode()` to coordinate rates
**File**: `apps/predbat/inverter.py:1801`

Add optional `charge_rate` and `discharge_rate` parameters. When provided, write them alongside the mode change to avoid race conditions (mode changes before rate is set).

```python
def adjust_ems_mode(self, ems_mode, discharge_floor, charge_rate=None, discharge_rate=None):
```

Write charge_rate/discharge_rate entities when provided (same entity write logic as adjust_charge_rate/adjust_discharge_rate but without the skip guards).

### A2. Fix `adjust_charge_immediate()` SIG path
**File**: `apps/predbat/inverter.py:2462-2470`

| Scenario | Current | Fixed |
|---|---|---|
| Freeze (freeze=True or target==soc%) | MSC + floor=soc% | Mode 6 (Disch PV First) + disch_rate=0 → battery won't discharge; charge_limit=100% (from adjust_battery_target) allows solar charging. This is "freeze charge" = hold or gain from solar |
| Active charge (target>0) | Mode 4 (PV First) only | Mode 3 (Grid First) + charge_rate. Grid First is correct for charge windows — optimizer already decided to charge. If PV is sufficient, minimal grid import occurs |
| Demand (target=0) | MSC + floor=reserve | MSC + floor=reserve (no change) |

### A3. Fix `adjust_export_immediate()` SIG path
**File**: `apps/predbat/inverter.py:2503-2511`

| Scenario | Current | Fixed |
|---|---|---|
| Freeze export (freeze=True) | MSC + floor=reserve | MSC + charge_rate=0 (block charging during export freeze) |
| Active export (target<100) | Mode 7 but no rate | Mode 7 + safe_discharge_rate (see A5) |
| Demand (target=100) | MSC + floor=reserve | MSC + floor=reserve (no change) |

### A4. Remove rate=0 skip guards
**File**: `apps/predbat/inverter.py:1536-1538, 1579-1581`

Remove the `if new_rate == 0 and self.inv_soc_limits_block_solar: return` guards from both `adjust_charge_rate()` and `adjust_discharge_rate()`. Rate=0 is valid in Command modes (confirmed by tests) and needed for freeze (disch_rate=0) and export (charge_rate=0).

### A5. Safe discharge rate for export
**File**: `apps/predbat/inverter.py` — new helper in `adjust_export_immediate()` SIG path

During force export, cap discharge rate to prevent total site export exceeding export_limit:
```
safe_rate = max(export_limit - current_pv - safety_margin, 0)
discharge_rate = min(requested_rate, safe_rate)
```

If safe_rate is 0 (PV already at export limit), skip discharge entirely (stay MSC).

Current PV available from `self.base.pv_power` (already tracked by Predbat status). Export limit from `self.base.export_limit` config. Safety margin: 500W.

### A6. Fix freeze charge in execute.py
**File**: `apps/predbat/execute.py:202-207`

Currently freeze charge tries `adjust_pause_mode(pause_discharge=True)` (SIG: no-op, no timed pause) then `adjust_discharge_rate(0)` (SIG: skipped by guard). After A4 removes the guard, `adjust_discharge_rate(0)` will work. But it's redundant with A2 since `adjust_charge_immediate(soc%, freeze=True)` now sets Mode 6 + disch_rate=0. Verify no conflict.

### A7. Charging mode: Grid First vs PV First
**Decision needed**: Currently uses Mode 4 (PV First) for all charges. Test results show:
- Mode 4: charges at min(rate, PV_excess) — won't import from grid
- Mode 3: charges at rate, imports from grid if needed

For charge windows (optimizer decided to charge at a specific rate): **Mode 3 is correct**. The optimizer already factored in grid cost. Mode 4 would under-charge during low PV.

For curtailment throttled charging: **Mode 4 is correct**. We want min(rate, PV_excess) to avoid grid import.

Both modes need the charge_rate set. Distinguish by: if export window context → skip (handled by export path); if charge window → Mode 3; if curtailment throttle → Mode 4 (Phase B).

**Simplification**: Use Mode 3 for all charge windows in `adjust_charge_immediate()`. Curtailment throttle is a separate future path.

## Phase B: Curtailment Avoidance (after A is working)

The existing curtailment infrastructure (`generate_curtailment_export_windows()` in plan.py, curtailment cost in prediction.py) should work once Phase A makes force export functional. The pre-solar export windows flow through execute.py's standard export path → `adjust_export_immediate()` → Mode 7 + safe rate.

**Verify only** — no new code expected unless testing reveals issues.

One potential issue: pre-solar windows placed at e.g. 06:00-08:00 would export at safe_rate which depends on real-time PV. At 06:00 PV≈0, so safe_rate≈export_limit (4kW). As PV ramps up toward 08:00, safe_rate decreases. This is correct — the safety cap is dynamic.

## Files to Modify

| File | Changes |
|---|---|
| `apps/predbat/inverter.py` | A1-A5: adjust_ems_mode params, adjust_charge_immediate SIG path, adjust_export_immediate SIG path, remove rate guards, safe rate calc |
| `apps/predbat/execute.py` | A6: verify freeze charge path works with new SIG behavior (may need no changes) |
| `apps/predbat/config.py` | No changes needed — flag stays False, fallback paths handle it |

## Testing

1. `pre-commit run --all-files`
2. `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
3. Deploy to Mum's HA, verify via MCP:
   - Demand → MSC mode
   - Charge window → Mode 3 + charge rate set
   - Freeze charge → Mode 6 + disch_rate=0
   - Force export → Mode 7 + capped discharge rate
   - Export freeze → MSC + charge_rate=0
4. Monitor grid export during force export stays under 4kW

## Resolved Questions

1. **charge_discharge_with_rate flag**: Keep **False** for SIG. Execute.py has fallback paths (lines 398-400, 438-440) that call `adjust_charge_rate(0)` even when the flag is False. Combined with `adjust_ems_mode()` coordinating rates atomically when entering Command modes, all cases are covered. Setting True could cause unwanted 0-rate writes during MSC demand mode.
2. **config.py changes**: None needed — SIG INVERTER_DEF stays as-is.
