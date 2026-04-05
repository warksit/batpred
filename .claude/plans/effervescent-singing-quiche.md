# SIG Inverter Control Strategy for Predbat

## Context

The current SIG control (branches `sig-inverter-control` + `curtailment-avoidance`) uses only 3 of 7 EMS modes, ignores rate control registers, and has broken freeze implementations. Starting fresh from `main` with a strategy that uses the SIG's full capability set.

Key insight: the SIG has 7 EMS modes with independent rate controls. Predbat's GivEnergy-centric "freeze" concept may be unnecessary when you can directly express "charge at X rate" or "discharge at X rate."

## Branch Strategy

Two branches from `main`:

1. **`sig-control-v2`** — Generic SIG inverter control: entity mapping, full EMS mode support, rate control, freeze (TBD after hardware tests)
2. **`curtailment`** — Inverter-agnostic curtailment cost model in prediction/optimizer + SIG-specific throttled charging execution

Deploy: merge both into `mum-no-curtailment` for Mum's HA.
Upstream: `sig-control-v2` PR first, `curtailment` PR second.

## Phase 1: Hardware Testing (before any code)

**Ask user for go-ahead before starting any tests.** User needs to:
1. Confirm Predbat is in read-only mode (via `set_read_only` toggle)
2. Confirm timing is acceptable (midday for zero-cost freeze tests, cheap rate for Mode 3)
3. Confirm current SIG state is stable and ready for testing

All tests at midday = near-zero cost (PV covers load). ~5 min observation each via MCP entity writes + sensor readings.

| # | Mode | Registers | Question |
|---|---|---|---|
| 1 | Mode 5 (Discharge PV First) | discharge_limit=0 | PV serves load? Battery idle? |
| 2 | Mode 5 | discharge_limit=0, charge_cut_off=current | Battery frozen? PV serves load? Excess exports? |
| 3 | Mode 2 (MSC) | charge_cut_off=current, discharge_cut_off=reserve | Battery serves load? Solar exports? |
| 4 | Mode 2 (MSC) | grid_export_limit=0 | Battery serves load? PV charges? No export? |
| 5 | Mode 4 (Charge PV First) | charging_limit=2000 | PV charges at 2kW? Excess exports? |
| 6 | Mode 6 (Discharge ESS First) | discharging_limit=2000 | Battery exports at 2kW? |
| 7 | Mode 3 (Charge Grid First) | charging_limit=2000 | Grid charges at 2kW? (save for 14p window) |

**After testing:** Restore all entities to demand defaults, disable read-only.

## Phase 2: Decisions (based on test results)

- **Freeze:** Drop for SIG (`support_charge_freeze=False`, `support_discharge_freeze=False`), map to working mode, or use Mode 5+limit=0?
- **Rate control:** Confirm ess_max_charging/discharging_limit work in correct modes
- **Grid export limit:** Usable as dynamic control or too risky?

## Phase 3: `sig-control-v2` (from `main`)

### 3a. Entity Mapping & INVERTER_DEF
- `soc_limits_block_solar: True` flag (from existing work, clean rewrite)
- Standard entities: `charge_limit`, `reserve`, `inverter_mode`
- New entities: `sig_discharge_cut_off_soc`, `sig_charge_rate_limit`, `sig_discharge_rate_limit`
- Optional: `sig_grid_export_limit`, `sig_grid_import_limit`

### 3b. Full EMS Mode Support

Expand `adjust_ems_mode()` to write all relevant registers per mode:

| Predbat Mode | SIG EMS Mode | discharge_cut_off | charge_cut_off | ess_charge_limit | ess_discharge_limit |
|---|---|---|---|---|---|
| **Demand** | Mode 2 (MSC) | reserve | 100% | — | — |
| **Charge (grid)** | Mode 3 (Grid First) | reserve | target_soc | charge_rate | — |
| **Charge (PV)** | Mode 4 (PV First) | reserve | target_soc | charge_rate | — |
| **Export** | Mode 6 (Discharge ESS First) | target_soc | 100% | — | discharge_rate |
| **Freeze** | TBD after Phase 1 tests | | | | |

### 3c. Rate Control
- `ess_max_charging_limit` written in Mode 3/4
- `ess_max_discharging_limit` written in Mode 5/6
- Remove rate=0 guard for `soc_limits_block_solar` inverters

## Phase 4: `curtailment` (from `main`, depends on `sig-control-v2`)

### 4a. Inverter-agnostic: Curtailment Cost Model
- Prediction model forecasts curtailment: when PV > load + export_limit + available_charge_rate
- `metric_curtailment_cost` penalizes curtailed energy in optimizer metric
- All inverter types benefit — optimizer avoids plans that cause curtailment

### 4b. SIG-specific: Throttled Charging Execution
- During peak solar: Mode 4 + reduced `ess_max_charging_limit`
- `charging_limit = max(0, forecast_pv - forecast_load - export_limit)`
- Battery charges slowly, excess PV exports up to grid cap (4kW)
- Grid Export Limitation acts as hardware safety net
- Benefits: no wasted cycles (vs pre-drain), battery stays available for evening

## Files to Modify

**`sig-control-v2`:**
- `apps/predbat/inverter.py` — `adjust_ems_mode()`, `adjust_charge_immediate()`, `adjust_export_immediate()`
- `apps/predbat/config.py` — INVERTER_DEF["SIG"], new entity definitions
- `apps/predbat/execute.py` — Mode selection paths for SIG

**`curtailment`:**
- `apps/predbat/prediction.py` — Curtailment forecasting in simulation loop
- `apps/predbat/plan.py` — Curtailment cost in optimizer metric, charge rate optimization

## Verification
1. `pre-commit run --all-files`
2. `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
3. Deploy to Mum's HA, observe via MCP
4. Compare plan output before/after for regression
