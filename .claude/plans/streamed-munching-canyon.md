# Curtailment Manager — Real-Time Throttled Charging

## Context

On high-solar days, the battery fills to 100% while PV still exceeds the 4kW export limit. Once full, excess PV is curtailed (wasted). The existing `best_soc_keep_solar_offset` (standard Predbat) reduces unnecessary overnight charging on sunny days but can't solve the core problem: **managing how the battery charges during solar hours**.

The strategy (validated by simulation on June 16/19 SMA data with 18kWh battery):
1. Keep battery low early in the day, exporting PV at up to 4kW
2. Use look-ahead to reserve battery capacity for future excess-above-4kW periods
3. When PV excess > export limit, absorb only the surplus (letting 4kW export)
4. When PV excess < export limit, charge freely if capacity is available beyond reservation
5. Battery reaches ~100% exactly as solar excess drops below export limit

**Includes pre-solar drain phase**: When battery SOC is above target and PV is low, forced export at up to 4kW to drain battery before solar ramps up. Safe because PV is low — export rate capped at `max(0, 4kW - PV_excess)` so total site export never exceeds 4kW. Uses Mode 7 (Discharge ESS First) with rate limit.

**During high PV: no forced battery export** — only controlled charging via Mode 4. Battery export + PV export could exceed 4.5kW SIG hard shutdown threshold.

## Approach: Execute-Time Controller in execute.py

Not a new file/mixin — add the curtailment manager as methods on the `Execute` class in `execute.py`, called at the top of the per-inverter loop before charge/export window logic. When active, it takes direct control of the inverter and skips normal window logic.

### Why execute.py, not prediction.py/plan.py

- Needs real-time PV/load (`self.pv_power`, `self.load_power`) not just forecasts
- Runs every 5 minutes (Predbat cycle) — much more responsive than 30-min plan windows
- Uses forecast look-ahead for reservation, but controls rates based on actuals
- Normal Predbat plan still handles overnight charging + evening export windows

## Files to Modify

### 1. `apps/predbat/config.py` — Add config items

Add to `CONFIG_ITEMS` (near existing curtailment config ~line 429):

```python
{
    "name": "curtailment_manager_enable",
    "friendly_name": "Curtailment Manager Enable",
    "type": "switch",
    "default": False,
},
{
    "name": "curtailment_manager_threshold_kwh",
    "friendly_name": "Curtailment Manager Activation Threshold (kWh)",
    "type": "input_number",
    "default": 1.0,
    "step": 0.5,
    "min": 0.0,
    "max": 20.0,
    "unit": "kWh",
},
```

### 2. `apps/predbat/fetch.py` — Read config items

Add near line 2092 (where `metric_curtailment_cost` is fetched):

```python
self.curtailment_manager_enable = self.get_arg("curtailment_manager_enable")
self.curtailment_manager_threshold_kwh = self.get_arg("curtailment_manager_threshold_kwh")
```

### 3. `apps/predbat/predbat.py` — Add defaults

Add near line 478 (where `metric_curtailment_cost` default is):

```python
self.curtailment_manager_enable = False
self.curtailment_manager_threshold_kwh = 1.0
```

### 4. `apps/predbat/execute.py` — Core implementation

#### 4a. Hook into execute_plan() (~line 72, after read_only check)

After the read-only check and before calibration check:

```python
# Curtailment Manager — real-time throttled PV charging
if self.curtailment_manager_enable and not self.set_read_only and not inverter.in_calibration:
    curt_active, curt_status = self.execute_curtailment_manager(inverter)
    if curt_active:
        status = curt_status
        continue  # skip normal charge/export window logic
```

#### 4b. Add three methods to Execute class

**`curtailment_should_activate(self)`** — Entry/exit criteria

```
- If not enabled or no export_limit → False
- Scan pv_forecast_minute_step from minutes_now forward
- For each step: excess = pv - load; curtailed = max(excess - export_limit, 0)
- Sum total curtailed kWh (with curtailment_margin applied to PV)
- If total < threshold_kwh → False
- Track first_excess_minute and last_excess_minute
- Activate if minutes_now is between (first_excess - 30 min) and last_excess
- Return False once past last_excess (hand back to normal Predbat)
```

**`curtailment_calculate_reservation(self)`** — Look-ahead

```
- Scan pv_forecast_minute_step from minutes_now forward (future only)
- For each step: excess_above_limit = max(pv*margin - load - export_limit, 0)
- Sum to get total kWh needing absorption
- Apply battery_loss factor
- Clamp to soc_max
- Return reserved_kwh
```

**`execute_curtailment_manager(self, inverter)`** — Main controller

```
- Call curtailment_should_activate() → if False, return (False, "")
- Get real-time: pv_now_kw = self.pv_power/1000, load_now_kw = self.load_power/1000
- Calculate reserved_kwh from look-ahead
- Calculate pv_excess = pv_now_kw - load_now_kw
- Calculate target_soc = soc_max - reserved_kwh (the SOC we should be at right now)

PHASE 1: DRAIN (SOC above target, PV low — only if needed)
  If soc_kw > target_soc AND pv_excess < export_limit_kw * 0.75:
    → Battery is too full for the upcoming solar — drain to make room
    → Skipped entirely if SOC is already at or below target (e.g. after Flux evening export)
    → safe_export_rate = max(0, export_limit_kw - max(0, pv_excess))
    → drain_rate = min(safe_export_rate, (soc_kw - target_soc) / slot_hours)
    → Mode 7 (Discharge ESS First) + discharge_rate = drain_rate
    → Return (True, "CurtMgr draining")

PHASE 2: ABSORB (PV excess > export limit, battery absorbs surplus)
  If pv_excess > export_limit_kw:
    → charge_rate = (pv_excess - export_limit_kw) — absorb only above-limit excess
    → Mode 4 (Charge PV First) + charge_rate in W
    → Return (True, "CurtMgr absorbing")

PHASE 3: FREE CHARGE (PV excess < export limit, spare capacity available)
  If pv_excess > 0 AND pv_excess <= export_limit_kw:
    → available = soc_max - soc_kw - reserved_kwh
    → if available > 0.5:
        charge_rate = pv_excess (charge freely, plenty of room)
        Mode 4 + charge_rate → Return (True, "CurtMgr free-charge")
    → else:
        charge_rate = 0 (hold, save room for future excess)
        MSC + charge_limit < SOC → Return (True, "CurtMgr holding")

PHASE 4: NO EXCESS (load > PV)
  If pv_excess <= 0:
    → If soc_kw > target_soc: stay in drain mode (serve load from battery)
    → else: return (False, "") — let normal Predbat handle

Log: PV, load, SOC, reserved, target_soc, rate, phase
Return (True, "CurtMgr" + phase + details)
```

## SIG Hardware Control — Split Architecture

PV fluctuates too fast (seconds) for Predbat's 5-minute cycle to manage charge rates directly. Split into two layers:

### Predbat (every 5 min) — Planning & Mode Control

Publishes sensors and sets SIG mode:

| Sensor | Purpose |
|--------|---------|
| `sensor.predbat_curtailment_target_export_kw` | Target grid export rate (kW). 4.0 during absorb/hold, 0 during free-charge, negative during drain |
| `sensor.predbat_curtailment_target_soc` | Target SOC % from look-ahead reservation |
| `sensor.predbat_curtailment_phase` | Current phase: `drain`, `absorb`, `hold`, `free_charge`, `off` |

Sets SIG mode via `adjust_ems_mode()`:

| Phase | SIG Mode | Predbat sets | HA automation controls |
|-------|----------|-------------|----------------------|
| **Drain** | Mode 7 (Discharge ESS First) | mode, discharge_floor=target_soc | discharge_rate (via target_export) |
| **Absorb** | Mode 4 (Charge PV First) | mode, charge_limit=100% | charge_rate |
| **Free charge** | Mode 4 (Charge PV First) | mode, charge_limit=100% | charge_rate (= all excess) |
| **Hold** | MSC + charge_limit < SOC | mode, charge_limit=SOC-1% | n/a (MSC ignores rates) |
| **Off** | MSC (normal) | restore charge_limit=100% | n/a |

### HA Automation (triggered by PV sensor change) — Fast Rate Control

Simple automation triggered by state changes on the PV power sensor:

```yaml
trigger:
  - platform: state
    entity_id: sensor.sigen_plant_pv_power  # or equivalent
automation:
  # Read Predbat's target
  target_export = states('sensor.predbat_curtailment_target_export_kw')
  phase = states('sensor.predbat_curtailment_phase')
  pv = states('sensor.sigen_plant_pv_power')   # W
  load = states('sensor.house_load_power')       # W
  pv_excess = pv - load

  if phase == 'absorb':
    charge_rate = max(0, pv_excess - target_export * 1000)
    → write number.sigen_plant_ess_max_charging_limit = charge_rate

  elif phase == 'drain':
    safe_rate = max(0, target_export * 1000 - max(0, pv_excess))  # don't exceed export limit
    → write number.sigen_plant_ess_max_discharging_limit = safe_rate

  elif phase == 'free_charge':
    charge_rate = max(0, pv_excess)
    → write number.sigen_plant_ess_max_charging_limit = charge_rate
```

**Why this works**: The SIG in Mode 4 charges up to the rate limit. The HA automation continuously adjusts that limit to match actual PV excess. When PV spikes, the automation immediately raises the charge rate to absorb the surplus. When PV dips, it lowers the rate so more PV exports.

**Hardware test confirmation**:
- Test #4: Mode 4 + charge_limit=0.5kW → exact 0.5kW charging
- Test #5: Mode 7 + disch_limit=0.5kW → exact 0.5kW discharge
- Test #8: Mode 7 + disch_limit=2kW → exact 2kW discharge
- Test #3: MSC + charge_cut_off < SOC → battery idle (hold phase)
- Rate limits only work in Command modes, IGNORED in MSC

### Entities used

| Entity | HA entity | Written by |
|--------|-----------|------------|
| `inverter_mode` | `select.sigen_plant_remote_ems_control_mode` | Predbat |
| `charge_rate` | `number.sigen_plant_ess_max_charging_limit` | HA automation |
| `discharge_rate` | `number.sigen_plant_ess_max_discharging_limit` | HA automation |
| `charge_limit` | `number.sigen_plant_ess_charge_cut_off_state_of_charge` | Predbat |
| `sig_discharge_cut_off_soc` | `number.sigen_plant_ess_discharge_cut_off_state_of_charge` | Predbat |

### Safety

- **Drain phase**: Only enters when `pv_excess < export_limit * 0.75`. HA automation caps discharge_rate to `4kW - PV_excess` to prevent total export exceeding 4kW.
- **Absorb phase**: Charge rate = `PV_excess - 4kW`. If PV drops below 4kW, charge_rate → 0 (no charging, PV exports freely). Mode 4 may import small amounts from grid on brief dips — acceptable.
- **4.5kW hard shutdown**: Never force battery export (Mode 7) when PV excess is high. Drain phase guard ensures this.

## Unit Conversions (critical)

- `self.export_limit` is stored as `W / MINUTE_WATT` (i.e. kW/60). To get kW: `self.export_limit * 60`
- `pv_forecast_minute_step[m]` is kWh per PREDICT_STEP (5 min). To get kW: `value * 60 / step`
- `self.pv_power` / `self.load_power` are in Watts
- `adjust_ems_mode` charge_rate parameter is in Watts
- `self.soc_kw` / `self.soc_max` are in kWh

## Status Display

The status string returned from execute_curtailment_manager should show:
- `"CurtMgr charging 2.1kW rsv=8.5kWh"` — actively absorbing excess
- `"CurtMgr holding rsv=12.0kWh"` — zero charge rate, saving capacity
- `"CurtMgr free-charge 3.2kW"` — charging freely (spare capacity beyond reservation)

## What This Replaces — Remove Existing Curtailment Code

The curtailment manager replaces the curtailment-v2 branch's approach. Starting from `main`, these items from the curtailment-v2 branch are NOT carried over:

- `generate_curtailment_export_windows()` in plan.py — forced export windows (failed, dangerous on SIG)
- `metric_curtailment_cost` penalty in prediction.py — cost penalty in optimizer (insufficient lever)

### What we KEEP (already in main or standard Predbat):
- `best_soc_keep` + `best_soc_keep_solar_offset` — does the heavy lifting for reducing overnight charging on sunny days. Standard Predbat feature, not our code.
- `curtailment_margin` config — reused by the curtailment manager for forecast safety margin
- `clipped_today` / `curtailed_today` tracking in prediction.py — useful for monitoring

## Edge Cases

1. **Battery already full at activation**: reserved > 0, available < 0, charge_rate = 0. PV exports at limit, excess above limit curtails. Unavoidable — the curtailment manager prevents this from happening next time.

2. **Cloudy day / bad forecast**: If actual PV is lower than forecast, less excess than predicted. Reservation is too conservative → battery charges more slowly (good, not harmful). If no excess materializes, `pv_excess <= 0` → falls back to normal Predbat.

3. **Evening handoff**: Once past `last_excess_minute`, `curtailment_should_activate()` returns False. Normal Predbat export window logic takes over. Battery should be near-full by then.

4. **Overnight charging**: Normal Predbat optimizer handles this. On sunny forecast days, the optimizer naturally charges less overnight (solar will fill the battery). The curtailment manager handles daytime rate control.

5. **Mode 4 grid import risk**: Hardware tests confirmed Mode 4 imports from grid when PV < charge_rate. Mitigation: charge_rate is calculated from actual PV excess, so should closely match available PV. Brief dips may cause small imports — acceptable.

6. **charge_rate = 0 in Mode 4**: Not hardware-tested. Use MSC + charge_limit < SOC instead (confirmed by test 3: battery completely idle).

## Branch

Create `curtailment-manager` from `sig-control-v2` (after testing/fixing v2).

**Pre-requisite**: `sig-control-v2` may need the `adjust_reserve` fix from v1 (`3285eea` — 5-line guard in `adjust_reserve()` to skip GE-style backup SOC writes on SIG). Cherry-pick if investigation confirms it's needed on v2.

**SIG branch lineage**:
- `sig-inverter-control` (v1) — original SIG refactor, has all fixes including adjust_reserve
- `sig-control-v2` — rewrite, has HoldChrg fixes but NOT adjust_reserve fix
- `curtailment-v2` — abandoned (failed export window approach, debrief in `memory/feedback_curtailment_session_debrief.md`)

## Implementation Order — HA Automation First

The HA automation is the critical dependency. If Mode 4 + dynamic charge_rate doesn't achieve the target export in practice, the whole plan is pointless. Test this BEFORE building the Predbat integration.

### Step 1: Prove the HA automation works (manual test on Mum's HA)

1. Put Predbat in read_only mode (`switch.predbat_set_read_only`)
2. Manually set SIG to Mode 4 (Charge PV First) via HA
3. Create and deploy the HA automation that adjusts charge_rate based on PV excess
4. Set a fixed target export of 4kW (hardcoded, no Predbat sensors yet)
5. Monitor during a sunny period: does grid export stay near 4kW as PV fluctuates?
6. Check: how fast does the automation react? Does the SIG accept rapid rate changes? Any lag?

**Success criteria**: Grid export stays within 3.5-4.5kW during sustained PV excess > 4kW, with automation reacting to PV changes within ~10 seconds.

**If this fails**: The approach won't work and we need an alternative (e.g. accept imperfect 5-min targeting, or use charge_limit ceiling approach with MSC).

### Step 2: Build Predbat curtailment manager

Only after Step 1 succeeds. Predbat publishes target sensors, sets modes, manages look-ahead.

### Step 3: Integration test

Deploy both together on Mum's HA. Predbat drives the planning, HA automation drives the rates.

## Verification

### 1. Simulation with real SMA data

Test with three June 2025 days from `/Users/andrew/code/mum-energy/data/sma/daily_15min/`:

| Day | File | Total PV | Excess>4kW | Scenario |
|-----|------|----------|------------|----------|
| Peak | `Energy_Balance_2025_06_19.csv` | 66.5 kWh | 13.0 kWh | Sustained high PV, worst curtailment risk |
| Average | `Energy_Balance_2025_06_22.csv` | 45.4 kWh | 1.3 kWh | Moderate PV, borderline activation |
| Low | `Energy_Balance_2025_06_09.csv` | 38.6 kWh | 0.8 kWh | Lowest excess in June — tests threshold boundary |

| No-activate | `Energy_Balance_2025_04_18.csv` | 19.7 kWh | 0.0 kWh | April cloudy day, peak 4.2kW — should NOT activate |

Write a standalone simulation script that:
- Reads SMA CSV data (handles both kW and W formats)
- Runs the same algorithm as the curtailment manager (look-ahead + charge rate)
- Outputs per-slot table: time, PV, load, excess, charge_rate, export, curtailed, SOC%, reserved
- Reports totals: exported, curtailed, battery absorbed, final SOC
- Compares against "no management" baseline (battery charges freely in MSC)

### 2. Unit tests
Test the three methods with mock forecast data:
- Activation criteria (threshold, time window, should-not-activate on poor days)
- Reservation calculation accuracy
- Charge rate for: excess > limit, excess < limit with/without available capacity

### 3. Pre-commit + existing tests
`pre-commit run --all-files` and `cd coverage && python3 ../apps/predbat/unit_test.py --quick`

### 4. Deploy to Mum's HA
Set `curtailment_manager_enable: True`, `export_limit: 4000` in apps.yaml. Monitor via logs and Predbat plan display for "CurtMgr" status.
