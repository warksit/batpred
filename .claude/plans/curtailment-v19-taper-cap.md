# Curtailment v19: Tapered Cap

## Problem

Current floor formula caps `max_target_soc = 90% × soc_max` (R45) — reserves a
flat 1.8 kWh always-empty buffer for CLS safety during overflow. This buffer is
binding throughout the day, even after overflow has wound down.

On days with a thin post-release PV tail (e.g. 2026-04-23 forecast: 2.8 kWh PV
after safe_time, ~1.5 kWh evening load → 1.3 kWh net recovery), the 1.8 kWh
reserved room cannot be refilled by MSC post-release. Sunset SOC lands at 92-97%
instead of 100%, even though total daily PV forecast is 68 kWh.

User's observation: 22 April, battery sat at 100% from 17:30 to 20:00. That is
the expected behavior on a big-PV day.

## Design

The 90% cap defends against **mid-overflow CLS risk**: if LoadML over-predicts
load, real overflow exceeds forecast → battery fills faster than expected → if
it hits 100% mid-overflow, excess exports to grid → SIG CLS fault.

This risk only exists while `remaining_overflow > 0`. After safe_time, PV is by
definition below DNO+load — no CLS risk.

**Taper**: scale the reserved buffer with `remaining_overflow`, capped at the
current 10% (1.8 kWh).

```python
MAX_RESERVED_KWH = 1.8  # 10% of soc_max — same as today's R45 cap
buffer = min(MAX_RESERVED_KWH, remaining_overflow)
max_target_soc = soc_max - buffer
overflow_floor = max_target_soc - remaining_overflow * OVERFLOW_SAFETY_FACTOR
```

Physical meaning: reserve headroom equal to the overflow still expected to
arrive, capped at 10%. The 1.2× `OVERFLOW_SAFETY_FACTOR` separately handles
"overflow arrives bigger than forecast".

**Trace for tomorrow (forecast ~12 kWh overflow, safe_time 17:38):**

| Time | remaining | buffer | max_target | floor |
|---|---|---|---|---|
| 13:00 | 10 | 1.8 (clamped) | 90% | clamped to keep |
| 16:00 | 3 | 1.8 (clamped) | 90% | 76% |
| 17:00 | 0.5 | 0.5 | 97.2% | 94% |
| 17:30 | 0.1 | 0.1 | 99.4% | 98.8% |
| 17:38 (safe) | 0 | 0 | 100% | 100% |

Drain profile during peak unchanged (when `remaining > 1.8`, buffer clamps at
1.8, max_target stays 90%). Only the **tail** behaves differently — cap rises
toward 100% as overflow winds down.

Post-release MSC fills remaining gap (if any). On thin-tail days the gap is
tiny; on big-tail days MSC fills whatever's left and any surplus exports.

## Safety analysis

**Tail LoadML error scenario**: remaining=0.1 kWh, actual error adds 0.5 kWh
extra overflow. Buffer = 0.1 kWh. Short by 0.4 kWh → potential SIG CLS.

**Defense**: at the tail, PV is physically close to DNO+load (that's why
remaining is small). Physical overflow rate is `max(0, PV−load−DNO)`, which is
tiny regardless of LoadML error. 0.1 kWh remaining already implies PV ~ 4 kW;
overflow rate ~ 0.2 kW; 30 minutes worth is 0.1 kWh. LoadML can't materially
inflate this because PV itself is dropping.

**R43 bypass** still triggers if actual_scale exceeds p90_scale mid-day —
forecast overflow recomputes larger, buffer recomputes larger, floor drops.
Ratchet bypass lets floor drop. Existing mechanism unchanged.

**During peak overflow** (`remaining > 1.8`), behavior is identical to today.
Full 1.8 kWh CLS buffer plus 1.2× safety factor. No regression in drain depth.

## Interactions

- **R11 ratchet** — floor only ever rises with the taper (both `buffer` and
  `remaining × 1.2` move in the same direction). Ratchet trivially satisfied.
- **R43 bypass** — unchanged. Scale rise still triggers bypass to allow drop.
- **R48 relaxed soc_keep** — uses `max_target_soc - soc_keep` for
  `room_with_base_keep`. Dynamic cap means this grows near safe_time, which
  correctly suppresses R48 at the tail (no need to relax keep post-drain).
- **R47 state persistence** — `_floor_ratchet` just stores a number. No change.
- **Activation R5** — no interaction.

## Files to Modify

1. `apps/predbat/curtailment_plugin.py` — `calculate()` active branch
2. `apps/predbat/curtailment_plugin.py` — `_compute_tomorrow_forecast()` for
   sensor consistency
3. `apps/predbat/REQUIREMENTS.md` — R9, R45 (rewrite as taper)
4. `apps/predbat/tests/test_curtailment.py` — new tests (TDD)

## TDD — failing tests first

Add to `test_curtailment.py`:

### `test_cap_taper_at_peak_overflow`

```text
remaining_overflow = 10 kWh → buffer = 1.8 (clamped) → max_target = 90%
Drain depth matches today's behavior.
```

### `test_cap_taper_near_safe_time`

```text
remaining_overflow = 0.5 kWh → buffer = 0.5 → max_target = soc_max - 0.5
Floor rises to ~97% (not stuck at 90%).
```

### `test_cap_at_safe_time_hits_100`

```text
remaining_overflow = 0 → buffer = 0 → max_target = soc_max
Floor = 100%, battery tracks to 100%.
```

### `test_cap_taper_ratchet_noise_immune`

```text
remaining sequence [1.0, 1.5, 1.0] → floor for cycle N+1 ≥ cycle N value.
Ratchet holds against oscillation.
```

### Integration: sunset SOC on tomorrow-equivalent scenario

Update/add an integration test matching tomorrow's profile (68 kWh total,
~12 kWh overflow, thin 2.8 kWh tail). Assert sunset SOC ≥ 98%.

### Regression test — current 90% cap behavior during peak

Existing tests with `remaining > 1.8` should STILL PASS (drain depth unchanged).

## Deploy Order

1. Write the 5 failing tests above. Verify they fail against current code.
2. Implement taper in `calculate()`.
3. Verify new tests pass; existing tests still pass.
4. Implement taper in `_compute_tomorrow_forecast()`.
5. Update REQUIREMENTS.md — R9, R45.
6. Pre-commit + full test suite.
7. Commit.
8. **Deploy overnight** (plugin Off). Same rules as v18 — never mid-day.
9. Watch tomorrow's behavior via `sensor.predbat_curtailment_phase` attributes
   and sunset SOC. Success signal: sunset SOC ≥ 98% on 23 April.

## Risks

- **LoadML tail error**: covered above — physical PV limit bounds the risk.
- **Tomorrow sensor divergence**: if I update live but forget tomorrow sensor,
  sensor shows old 90% target. Non-safety, just cosmetic. Cover in step 4.
- **R48 `room_with_base_keep` now dynamic**: if `needs_room` flickers on
  activation day, the relaxed-keep latch could toggle on/off. `_keep_recovered`
  latch is one-way — once SOC reaches keep, stays there. So bounded risk.

## Success criteria

- All existing `test_curtailment.py` passes (no regression).
- New 5 tests pass.
- Deploy overnight 22 April.
- Tomorrow (23 April, forecast 68 kWh): sunset SOC ≥ 98% (was ≥ 90% under
  current R45).
- No CLS faults mid-day.

## Constants

```python
MAX_RESERVED_KWH = 1.8       # 10% of soc_max (18.08 × 0.10)
OVERFLOW_SAFETY_FACTOR = 1.2  # unchanged
```

Nothing else changes.
