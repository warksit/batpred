# SIG Inverter Control + Curtailment Avoidance Plan

## Context

Hardware testing (8 tests on real SIG inverter) revealed the current SIG control code doesn't properly map Predbat operations to SIG EMS modes. Curtailment avoidance depends on force export working, so SIG control must be fixed first.

## Safety Constraints

- SIG hard shutdown at **4.5kW** total site export (needs manual restart)
- SMA curtails at **3.5kW** export (soft, ~250W overshoot)
- DNO limit: **4kW**. SIG `grid_export_limitation` = 4kW but does NOT actively throttle discharge
- SIG max discharge: **9.6kW** — must cap dynamically based on PV
- **Rate limits IGNORED in MSC mode** — only work in Command modes

## Hardware Test Results

| # | Config | Battery | Grid | Finding |
|---|---|---|---|---|
| 1 | Mode 6 + disch_limit=0 | +0.8kW | -4kW | NOT a freeze — still charges |
| 2 | Mode 6 + disch_limit=0 + charge_cut_off<SOC | 0kW | -1.1kW | TRUE FREEZE — completely idle |
| 3 | MSC + charge_cut_off<SOC (PV>load) | 0kW | -1.4kW | Charge freeze only |
| 3r | MSC + charge_cut_off<SOC (PV<load) | -2.4kW | ~0kW | NOT full freeze — still discharges |
| 4 | Mode 4 (Charge PV First) + 0.5kW | +0.5kW | -2.6kW | Throttled PV charging works |
| 5 | Mode 7 (Disch ESS First) + 0.5kW | -0.5kW | +1.5kW | Controlled discharge works |
| 6 | Mode 3 (Charge Grid First) + 8kW | +8.0kW | +2.3kW | Grid First imports from grid |
| 7 | Mode 4 (Charge PV First) + 8kW | +8.0kW | +2.5kW | Also imports (PV < limit) |
| 8 | Mode 7 (Disch ESS First) + 2kW | -2.0kW | -3.65kW | Forced export at set rate |

### Pending Evening Tests (no PV)
- **Test 6 redo**: Mode 3 + charge_limit, no PV — confirm grid charging at night
- **Test 7 redo**: Mode 4 + charge_limit, no PV — does it charge from grid or refuse?

## Branches (3 total)

1. **Update main** to upstream/main
2. **Rebase `unified-inverter-tests`** onto main — test framework + updated SIG expectations (Phase A)
3. **Create `sig-control-v2`** off `unified-inverter-tests` — SIG control implementation (Phase B)
4. **Create `curtailment-v2`** off `sig-control-v2` — curtailment avoidance (Phase C)
5. **Merge into `mum`** for deployment

## Phase A: `unified-inverter-tests` — Update Test Expectations

Rebase onto main, then update SIG expectations so tests define the correct behavior (tests will FAIL until Phase B implements the control code).

### Step 1: Update `tests/inverters/sig/__init__.py`

Add `charge_rate` and `discharge_rate` to `extra_entities` / `extra_args`. Rewrite `mode_expectations`:

| Predbat Mode | EMS Mode | disch_rate | charge_rate | discharge_floor |
|---|---|---|---|---|
| demand_charge | MSC | max | max | reserve |
| active_charge | Command Charging (Grid First) | — | optimizer rate | reserve |
| freeze_charge | Command Discharging (PV First) | 0 | — | reserve |
| active_export | Command Discharging (ESS First) | safe_rate | 0 | reserve |
| demand_export | MSC | max | max | reserve |
| freeze_export | MSC | — | 0 | reserve |

## Phase B: `sig-control-v2` — SIG Inverter Control (make tests pass)

Reference: old `sig-inverter-control` branch. Useful files via `git show sig-inverter-control:apps/predbat/<file>`:
- `config.py` — SIG INVERTER_DEF (entity mapping, flags — correct, reuse)
- `inverter.py` — `adjust_ems_mode()` structure (expand with rate params)
- `templates/sigenergy_sigenstor.yaml` — SIG template YAML
- `execute.py` — HoldChrg bug fix (commit `6c5688c`, not SIG-specific, worth including)
- `tests/test_execute.py` — optimizer charge window tests (commits `8dff551`, `d70ae74`, not SIG-specific)

### Step 2: SIG INVERTER_DEF (`config.py`)

- `soc_limits_block_solar: True`, `has_target_soc: True`, `has_reserve_soc: True`
- `has_time_window: False`, `has_discharge_enable_time: False`, `has_timed_pause: False`
- `charge_discharge_with_rate: False` (rates coordinated inside adjust_ems_mode instead)
- Entities: charge_limit, reserve, inverter_mode, charge_rate, discharge_rate, sig_discharge_cut_off_soc

### Step 3: `adjust_ems_mode()` (`inverter.py`)

New function — central SIG control. Coordinates mode + rates + discharge floor atomically:
```python
def adjust_ems_mode(self, ems_mode, discharge_floor, charge_rate=None, discharge_rate=None):
```
Writes EMS mode, discharge floor, and optionally charge/discharge rate entities.

### Step 4: SIG paths in `adjust_charge_immediate()` and `adjust_export_immediate()` (`inverter.py`)

Add `inv_soc_limits_block_solar` branches using adjust_ems_mode:
- **Freeze charge** → Mode 6 + disch_rate=0 (battery idle, solar allowed via charge_limit=100%)
- **Active charge** → Mode 3 + charge_rate (grid import OK — optimizer decided to charge)
- **Active export** → Mode 7 + safe_discharge_rate + charge_rate=0
- **Freeze export** → MSC + charge_rate=0
- **Demand** → MSC + floor=reserve

### Step 5: Remove rate=0 skip guards (`inverter.py`)

Remove `if new_rate == 0 and self.inv_soc_limits_block_solar: return` from `adjust_charge_rate()` and `adjust_discharge_rate()`. Enables freeze (disch_rate=0), export (charge_rate=0), and rate reset on demand exit.

### Step 6: Safe discharge rate cap (`inverter.py`)

In `adjust_export_immediate()` SIG path:
```
safe_rate = max(export_limit - current_pv - 500, 0)
discharge_rate = min(requested_rate, safe_rate)
```
If safe_rate ≤ 0, stay MSC (don't export). Uses `self.base.pv_power` and `self.base.export_limit`.

### Step 7: Template and docs

Update `templates/sigenergy_sigenstor.yaml` and `docs/inverter-setup.md` for standard entity mapping.

## Phase C: `curtailment-v2` — Curtailment Avoidance

Reference: old `curtailment-avoidance` branch (commit `d2b9546` is the final state). Key files to review via `git show curtailment-avoidance:apps/predbat/<file>`: prediction.py (curtailment cost), plan.py (window generation), config.py (config entries), output.py (display), tests/test_curtailment.py.

Also see old `sig-inverter-control` branch for how SIG control integrated with execute.py export paths.

Write from scratch on top of working SIG control:
- **prediction.py**: curtailment cost in simulation loop (penalise export > export_limit)
- **plan.py**: `generate_curtailment_export_windows()` — pre-solar export windows
- **config.py**: export_limit, metric_curtailment_cost, curtailment_margin entries
- **output.py**: curtailment display in plan table
- **tests**: test_curtailment.py

Pre-solar windows flow through execute.py → `adjust_export_immediate()` → Mode 7 + safe rate (from Phase A).

## Verification

1. `pre-commit run --all-files`
2. `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
3. Deploy to Mum's HA via MCP, verify:
   - Demand → MSC
   - Charge window → Mode 3 + charge rate
   - Freeze charge → Mode 6 + disch_rate=0
   - Force export → Mode 7 + capped rate
   - Export freeze → MSC + charge_rate=0
4. Monitor grid export stays under 4kW during force export

## Resolved Questions

1. **charge_discharge_with_rate**: Keep False. Execute.py fallbacks handle charge_rate=0 during export/freeze. Rates coordinated atomically inside adjust_ems_mode().
2. **Grid First vs PV First for charging**: Mode 3 (Grid First) for charge windows. Mode 4 (PV First) reserved for future curtailment throttled charging.
