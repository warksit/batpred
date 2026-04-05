# Dynamic buffer + respect minimum SOC

## Context

The curtailment plugin's buffer (currently fixed at 1.0 kWh) reserves extra battery headroom for forecast error. A fixed buffer is a compromise — too small risks curtailment, too large prevents the battery reaching 100% before sunset. Buffer should scale with risk (remaining overflow) and decline naturally as overflow is consumed.

The plugin must also respect `best_soc_keep` (after solar offset) as a SOC floor. The existing `best_soc_keep_solar_offset` already reduces `best_soc_keep` on big solar days — the plugin sees the post-offset value via `self.base.best_soc_keep`.

## Changes

### 1. HA helpers

**Existing** — `input_number.curtailment_manager_buffer`: rename friendly name to "Curtailment Min Buffer", keep at 1.0 kWh. This is the floor.

**New** — `input_number.curtailment_manager_buffer_percent`: "Curtailment Buffer %", value 30, min 0, max 100, step 5, unit %. Proportional rate.

**Existing** — `input_number.predbat_best_soc_keep_solar_offset`: change from 1.5 to 2.5 kWh. Gives 0.5 kWh soc_keep floor on big days → 17.58 kWh headroom.

### 2. `apps/predbat/curtailment_plugin.py`

**Constants:**
```python
HA_BUFFER_MIN = "input_number.curtailment_manager_buffer"           # kWh floor (rename: "Min Buffer")
HA_BUFFER_PCT = "input_number.curtailment_manager_buffer_percent"   # % of overflow
```

**`get_config()`** — read both helpers:
```python
return enabled, dno_limit, buffer_min_kwh, buffer_pct
```

**`calculate(buffer_min_kwh=1.0, buffer_pct=30)`** — dynamic buffer + SOC floor:
```python
dynamic_buffer = max(buffer_min_kwh, remaining_overflow * buffer_pct / 100.0)
target_soc_kwh = compute_target_soc(remaining_overflow, soc_max, dynamic_buffer)

# Respect Predbat's minimum SOC (already reduced by solar offset on big days)
soc_keep = getattr(self.base, "best_soc_keep", 0)
reserve = getattr(self.base, "reserve", 0)
target_soc_kwh = max(target_soc_kwh, soc_keep, reserve)
```

Return `dynamic_buffer` as 5th element from `calculate()`.

**`on_update()`** — pass `dynamic_buffer` through to `publish()`.

**`publish()`** — add `buffer_kwh` attribute to phase sensor.

### 3. `apps/predbat/tests/test_curtailment.py`

Update MockBase: add `best_soc_keep=0`, `reserve=0`, buffer helpers return %.

New tests:
- 10 kWh overflow at 30% → buffer = 3.0 kWh
- 2 kWh overflow at 30% → buffer = 1.0 kWh (floor)
- Target SOC clamped above `best_soc_keep`

### 4. No changes to `curtailment_calc.py`

## Dynamic buffer examples (30%, 1 kWh floor, 18.08 kWh battery, soc_keep=0.5 after offset)

| Overflow | Buffer | Target | Headroom | Shortfall |
|---|---|---|---|---|
| 15 kWh | 4.5 kWh | 0.5 (clamped) | 17.58 | 0 |
| 10 kWh | 3.0 kWh | 5.08 | 13.0 | 0 |
| 5 kWh | 1.5 kWh | 11.58 | 6.5 | 0 |
| 3 kWh | 1.0 (floor) | 14.08 | 4.0 | 0 |
| 1 kWh | 1.0 (floor) | 16.08 | 2.0 | 0 |

## Verification
1. `cd apps/predbat && python3 tests/test_curtailment.py`
2. Create HA helper `curtailment_manager_buffer_percent` at 30%
3. Rename existing buffer helper friendly name to "Curtailment Min Buffer"
4. Set `best_soc_keep_solar_offset` to 2.5
5. Deploy to Mum's HA
6. Watch `buffer_kwh` attribute decline through afternoon
