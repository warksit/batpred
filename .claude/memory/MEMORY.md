# Batpred Project Memory

## Pre-Push CI Checks (MANDATORY)
- `pre-commit run --all-files`
- `cd coverage && python3 ../apps/predbat/unit_test.py --quick`

## Current State (2026-04-05)
- **Deployed branch**: `curtailment-manager` (based on upstream v8.34.4 + sig-control-v2)
- **Deployed version**: v10 (totals-based energy balance) — deployed Apr 5
- **Rollback branch**: `mum-no-curtailment` (old, don't redeploy without checking)
- **Active work**: monitoring v10 live performance, first clean test day = Apr 6
- **Baby due imminently** — solutions must be autonomous/simple

## Branch Map
| Branch | Status | Purpose |
|--------|--------|---------|
| `curtailment-manager` | **DEPLOYED** | sig-control-v2 + curtailment plugin |
| `sig-control-v2` | Keep | SIG inverter control — PR after unified-inverter-tests |
| `unified-inverter-tests` | Keep | Test framework — PR first |
| `main` | Updated to upstream v8.34.4 | Base |
| `mum-no-curtailment` | Superseded | Old deploy branch, keep as rollback |

## Deploy to Mum's HA
- SSH: `ssh hassio@100.110.70.80` (Tailscale)
- Path: `/addon_configs/6adb4f0d_predbat/`
- Deploy ALL .py files together (never just one — see [feedback_deploy_all_files.md](feedback_deploy_all_files.md))
- **Deploy method**: see [reference_deploy_ssh_method.md](reference_deploy_ssh_method.md) — files are root-owned, must use `sudo cp /dev/stdin`
- Predbat auto-restarts on watched file change (plugin files need a touch — see deploy reference)
- **TODO**: improve deployment — rebase on upstream, deploy whole branch via rsync

## Solcast PV Forecast (RESOLVED 2026-03-19)
- **Issue [#3597](https://github.com/springfall2008/batpred/issues/3597)**: was `pv_today` sensor misconfiguration, not upstream code bug
- **Root cause**: `pv_today` pointed to `sensor.sigen_plant_daily_pv_energy` (SIG's DC PV input, always 0 for AC-coupled SMA)
- **Fix**: changed to `sensor.sigen_plant_daily_third_party_inverter_energy` in apps.yaml
- **Why 5x too low**: upstream `pv_calibration` uses `pv_today` history; zero history → every slot clamped to `PV_CALIBRATION_LOWEST` (0.2)
- **Old code hid it**: pre-refactor used `predbat.pv_power` for calibration, never read `pv_today`
- Now running upstream solcast.py — calibration will learn ~0.8x Solcast overestimate over a few days
- Comment drafted for issue explaining the resolution

## SIG Inverter Control
- See [sig-control.md](sig-control.md) for entity mapping, test results, mode table
- See [sig-freeze-issues.md](sig-freeze-issues.md) for freeze mode limitations
- Key bug fixed 2026-03-17: `charge_limit=0` in demand mode blocked solar (execute.py)
- `soc_limits_block_solar` means charge_limit MUST be 100% unless actively limiting solar
- **SIG hard export limit**: 4.5kW — exceeding causes inverter fault/shutdown
- **SMA export limit**: 4.25kW (set 2026-04-02 to prevent cascade: SIG overvoltage trip → SMA uncontrolled → CLS fault on recovery)

## Cold Weather SOC Keep
- See [cold-weather-gshp-analysis.md](cold-weather-gshp-analysis.md) — data analysis, model, validation
- See [cold-soc-keep-automation.md](cold-soc-keep-automation.md) — existing HA automations (being replaced by plugin)
- See [project_cold_weather_alert_keep.md](project_cold_weather_alert_keep.md) — v2 alert_keep mechanism (deployed 2026-03-26)
- See [project_predheat_future.md](project_predheat_future.md) — PredHeat as long-term replacement
- **Active plan**: `.claude/plans/toasty-dazzling-hanrahan.md` — plugin architecture + cold weather + curtailment

## Pending Tasks
1. **SIG execute tests** — need tests for all modes × soc vs target
2. ~~Report upstream Solcast bug~~ — filed as #3597, resolved (config issue)
3. **Rebase curtailment-manager on upstream main** — drop reverted solcast.py, sync with v8.34.5
4. ~~Move solar offset to plugin~~ — DONE (2026-03-23): removed from plan.py/fetch.py/config.py/predbat.py. Plugin's morning gap calculation replaces it.
5. **PR plan**: submit `unified-inverter-tests` first, then rebase `sig-control-v2` on top
6. **Fix IOG test** — `test_multi_car_iog_load_slots_regression` missing `args["car_charging_limit"]`
7. **Plugin directory structure** — before PR'ing plugin system changes, refactor to `plugins/curtailment_manager/` and `plugins/cold_weather/` subdirectories with tests alongside. Update plugin discovery to scan subdirs, fix import paths, update deploy script.

## Curtailment Manager Status (2026-04-05)
- **v10 DEPLOYED** — totals-based energy balance, shared `_compute_curtailment`
- **Architecture**: plugin (5-min) + HA automation (5-sec real-time)
  - `curtailment_plugin.py` — energy balance from Solcast totals, publishes Active/Off + floor
  - `curtailment_calc.py` — pure functions (solar_elevation, compute_release_time, morning_gap, etc.)
  - HA automation `curtailment_manager_dynamic_export_limit` — three-state: Charge/Drain/Hold
  - `tests/test_curtailment.py` — integration tests (actual plugin.calculate() + independent physics)
  - `REQUIREMENTS.md` — R1-R37, definitive requirements, TDD enforced
- **Algorithm**: activation = total excess > headroom (R5). Floor = soc_cap - absorption × 1.10 (R9).
  - PV from Solcast remaining sensor × forecast shape fraction × energy_ratio
  - Load from LoadML sum × load_ratio. All to overflow window (first-to-last overflow slot).
  - One shared `_compute_curtailment()` — live and tomorrow use identical code (R31)
- **HA automation**: Charge (SOC < floor-0.5, export=0), Drain (SOC > floor+0.5, export=DNO), Hold (export=min(excess,DNO))
- **Tomorrow sensor**: simple excess vs headroom (PV start to release time). No floor — live handles that.
- **Solar geometry**: day's peak PV → scale → safe_time for 95% cap removal
- **SIG charge curve**: rated 8.8kW. At 95%: 7kW. At 97%+: 2.8kW (cliff)
- **Run tests**: `cd apps/predbat && python3 tests/test_curtailment.py`
- **Review skills**: `/curtailment-review`, `/soc-keep-review`

## Cold Weather & Load Config (2026-03-31)
- `days_previous: [1,2,3,4,5,6,7]` — averages all 7 days, modal filter strips cooking outliers
- Cold weather boost: `max(0, prediction - rolling_avg)` — only boosts for EXTRA above what LoadML knows
- Cold weather boost window: 22:00-07:00 only (don't influence daytime planning)
- `best_soc_keep`: 4.0 kWh base (buffer for forecast error, not load itself)
- Predbat accounts for load in charge targets. best_soc_keep is just the safety margin.

## Reference
- [curtailment-mar18-test.md](curtailment-mar18-test.md) — live test analysis plan
- [curtailment-redesign.md](curtailment-redesign.md) — redesign plan, modelling data
- [sig-control.md](sig-control.md) — SIG entity mapping, hardware test results
- [cold-soc-keep-automation.md](cold-soc-keep-automation.md) — cold weather automation
- [feedback_deploy_all_files.md](feedback_deploy_all_files.md) — deploy safety rules
- [feedback_clipped_vs_curtailed.md](feedback_clipped_vs_curtailed.md) — terminology
- [feedback_predbat_plan_units.md](feedback_predbat_plan_units.md) — plan values are kWh/slot, not kW
- [feedback_test_before_deploy.md](feedback_test_before_deploy.md) — test API calls locally before deploying to live
- [feedback_use_loadml_for_floor.md](feedback_use_loadml_for_floor.md) — use LoadML not base load for floor calc
- [feedback_discuss_before_coding.md](feedback_discuss_before_coding.md) — discuss and agree before writing code
- [feedback_check_before_removing.md](feedback_check_before_removing.md) — check commit history before removing/simplifying code
- [reference_gshp_sensor.md](reference_gshp_sensor.md) — GSHP power/energy sensor entity IDs
- [project_grid_overvoltage.md](project_grid_overvoltage.md) — SIG overvoltage trips, SMA limits raised, Ricky to fix SIG
- Predbat MCP: Tailscale `100.110.70.80:8199`, config in `.claude.json` local scope
- Plan file: `.claude/plans/curtailment-v5-mar18-test.md` — detailed overflow table
