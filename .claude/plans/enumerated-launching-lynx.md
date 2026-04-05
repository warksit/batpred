# Curtailment v8.1: Calculated export limit (not binary)

## Problem

Currently: Charge phase export=0, Export phase export=DNO. Binary. At 49% SOC with 6.4kW excess, all 6.4kW dumps to battery. Battery fills fast, hits floor, overflow takes over, no control.

## Fix: Calculate export limit to reach floor at the right speed

```
target_charge_rate = energy_to_floor / time_remaining
export_limit = clamp(excess - target_charge_rate, 0, DNO)
```

Battery charges at exactly the rate needed to reach the floor when the danger period ends. Export limit tracks PV dynamically. No binary phases.

## Calculation

Each cycle in `calculate()`:

```python
# How much SOC needs to increase to reach floor
energy_to_floor = max(0, floor - soc_kw)  # kWh

# How long until PV excess drops below DNO for good (from forecast)
# Use last_danger_slot (minutes) from trajectory
time_remaining_hours = max(last_danger / 60.0, 0.5)  # minimum 30 min

# Target charge rate: how fast to charge to reach floor in time
target_charge_rate = energy_to_floor / time_remaining_hours  # kW

# If already at/above floor: target = 0 (no charging, export everything up to DNO)
# If below floor: target = controlled rate
```

Publish `target_charge_rate` for the HA automation to use.

## HA Automation

The automation computes the actual export limit every second:

```yaml
export_limit = min(max(excess - target_charge_rate, 0), dno)
```

Examples with 6.4kW excess:
- target_rate 0.5kW → export 5.9kW (slow charge, lots of export)
- target_rate 2.0kW → export 4.4kW (moderate charge)
- target_rate 6.4kW → export 0 (charge fast, no export — old Charge behaviour)
- target_rate 0 (at floor) → export min(6.4, 4.0) = 4.0 (DNO — old Export behaviour)

When excess < target_rate: export = 0, battery charges from all PV. Natural.
When excess > DNO + target_rate: export = DNO, overflow + target_rate charges battery. Overflow is physics.

## What this replaces

No more binary Charge/Export/Hold phases for export limit. The export limit is a CALCULATED value. The "phase" concept simplifies to:
- Below floor: export = excess - target_rate (controlled charge)
- At/above floor: export = min(excess, DNO) (overflow management)
- Released: export = 0 (charge to 100%)

The HA automation already sets the export limit dynamically. It just needs the target_charge_rate instead of a phase name.

## Floor calculation

Unchanged from v8: `floor = soc_cap - net_charge_managed`. The floor determines WHERE we're heading. The charge rate determines HOW FAST.

But the floor ALSO needs the base-load adjustment (discussed earlier) to account for spike overflow. Otherwise floor is too high (89%) and there's no headroom.

Two options for floor:
1. Use base load (0.5kW) for overflow estimate → lower floor, more headroom
2. Use LoadML load but apply a spike factor → moderate floor

Option 1 ignores LoadML for floor (user said don't ignore LoadML). Option 2 uses LoadML with adjustment.

**Proposed: use LoadML load but compute the floor from the UNMANAGED trajectory peak, not managed net_charge.**

```python
# Unmanaged peak tells us total energy the battery WOULD absorb
# The export capacity is what we CAN export at DNO
# The overflow is what the battery MUST absorb (can't be exported)

unmanaged_excess = peak_unmanaged - current_soc  # total kWh battery absorbs in MSC
export_capacity = dno_limit * pv_hours  # max kWh exportable
spike_overflow = max(0, unmanaged_excess * SPIKE_FACTOR - export_capacity)
floor = soc_cap - spike_overflow
```

Where SPIKE_FACTOR accounts for PV being peaky (validated: 1.5-2x from Mar 28 data).

Actually, simpler: the managed trajectory WITH the target_charge_rate naturally handles this. The battery charges at the controlled rate, absorbs overflow above DNO, and the floor + charge_rate ensure SOC arrives at floor exactly when overflow ends. No spike factor needed — the charge rate adapts.

**Final floor approach: keep v8 floor formula but the charge rate smooths the approach.** The floor doesn't need to be lower because we're not dumping all PV to battery. We're charging at a controlled rate. Overflow above DNO is the only uncontrolled charge — and the headroom (10%) handles that.

## Plugin changes

In `calculate()`:
- Compute `target_charge_rate` from `energy_to_floor / time_remaining`
- Store as `self._target_charge_rate`
- Publish in phase sensor attributes
- Return to `on_update()` which passes to `apply()`

In `publish()`:
- Add `target_charge_rate` attribute

In `apply()`:
- Set export limit to `max(0, min(actual_excess - target_charge_rate, dno_limit))`
- Or: publish target_charge_rate and let HA automation compute export limit

## HA Automation changes

Replace phase-based export limit with rate-based:

```yaml
variables:
  target_rate: "{{ state_attr('sensor.predbat_curtailment_phase', 'target_charge_rate') | float(0) }}"
  new_limit: "{{ [excess - target_rate, 0] | max | min(dno) | round(1) }}"
```

The phase name (Charge/Export/Hold) becomes informational only — for logging. The export limit is always calculated from `excess - target_rate`.

## Files

- `curtailment_plugin.py`: compute and publish target_charge_rate
- HA automation: use target_charge_rate for export limit
- `tests/`: test charge rate calculation, test export limit follows rate

## Tests (write first)

- `test_charge_rate_below_floor`: SOC 30%, floor 60%, 4h remaining → rate = 1.4kW
- `test_charge_rate_at_floor`: SOC 60%, floor 60% → rate = 0
- `test_charge_rate_above_floor`: SOC 70%, floor 60% → rate = 0 (no charging)
- `test_export_limit_follows_rate`: rate 1kW, excess 5kW → export 4kW (= DNO)
- `test_export_limit_low_excess`: rate 1kW, excess 0.5kW → export 0, battery charges 0.5kW
