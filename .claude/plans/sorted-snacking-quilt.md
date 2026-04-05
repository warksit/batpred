# HoldChrg Bug Fix & ML Load Prediction Review

## Context

At 23:25 on March 10, the Predbat plan table showed **"HoldChrg→ 35 (100)"** — displaying a hold at 35% but with charge_limit_best set to 100%. Execution used the 100% value, charging the battery aggressively from 58% to 76%+ instead of holding. User had to enable read-only mode to stop it.

## Root Cause

**Bug in `plan.py:clip_charge_slots()`** (lines ~2275-2280, mum branch).

The clipping function inflates `charge_limit_best` to `soc_max` (100%) while storing the real target in `window["target"]`:

```python
elif soc_max < limit:
    window["target"] = soc_max           # display target (e.g. 35%)
    charge_limit_best[window_n] = self.soc_max  # INFLATED TO 100%!
```

- The plan table uses `window["target"]` for the label → shows "HoldChrg 35"
- The plan table shows `charge_limit_best` in debug brackets → shows "(100)"
- **execute.py only reads `charge_limit_best`** — it never reads `window["target"]`
- So execution sees 100% → charges aggressively instead of holding

Same pattern at lines ~2280-2285 (`soc_max == limit` case).

This is an **upstream Predbat bug** that's harmless for GE inverters (charge windows have time boundaries that prevent over-charging) but critical for SIG inverters where charge_limit is the only control and `soc_limits_block_solar=True`.

### Timeline from logs
```
22:00-22:25  Demand                          — normal
22:31-22:55  Demand (ceiling) target 58%-58% — curtailment ceiling working
23:00        Charging target 58%-100%        — clip_charge_slots inflated limit
23:05-23:25  Charging 61%→76% to 100%       — aggressive charging
23:27        Read-Only                       — user intervention
```

## Fix: Make execute.py use window target instead of inflated charge_limit_best

### Branch: sig-inverter-control (per user request — SIG control changes go there)

### File: `apps/predbat/execute.py` (mum branch base)

**Change**: Where execute.py reads `charge_limit_best[0]` to determine the charge target, check for the window's `target` field and use it instead when available.

Key locations in execute.py that read `charge_limit_best[0]`:

1. **Line ~206 (HoldChrg detection)**:
   ```python
   target_soc = calc_percent_limit(max(self.charge_limit_best[0] if not ... else self.soc_kw, self.reserve), self.soc_max)
   ```
   → Should use `self.charge_window_best[0].get("target", self.charge_limit_best[0])` instead of `self.charge_limit_best[0]`

2. **Line ~549 (SOC target setting)**:
   ```python
   target_soc = calc_percent_limit(max(self.charge_limit_best[0] if not is_freeze else self.soc_kw, self.reserve), self.soc_max)
   ```
   → Same fix

3. **Line ~560 (pre-window target setting)**:
   ```python
   target_soc = calc_percent_limit(max(self.charge_limit_best[0] if not is_freeze else self.soc_kw, self.reserve), self.soc_max)
   ```
   → Same fix

### Implementation approach

Add a helper method to get the effective charge target:

```python
def get_charge_target(self, window_n=0):
    """Get the effective charge target, preferring window target over inflated charge_limit_best."""
    if self.charge_window_best and window_n < len(self.charge_window_best):
        target = self.charge_window_best[window_n].get("target", self.charge_limit_best[window_n])
        return target
    return self.charge_limit_best[window_n]
```

Then replace `self.charge_limit_best[0]` with `self.get_charge_target(0)` in the three locations above.

**Keep `charge_limit_best[0]` unchanged for**: `is_freeze_charge()` checks (those compare against reserve, the actual limit doesn't matter).

### Why not fix in plan.py instead?

`clip_charge_slots` inflates charge_limit_best for a reason — GE inverters use charge_limit as a "stop charging" threshold. Setting it to 100% during a timed charge window means "charge as much as possible within the time window". The time boundary is the real control. Changing this would break GE inverters.

For SIG (no time windows, charge_limit is the only control), we need execute.py to use the real target.

### Testing

1. Run `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
2. Run `pre-commit run --all-files`
3. Deploy to mum's HA (merge sig-inverter-control into mum, deploy all files)
4. Monitor plan table — HoldChrg slots should show matching target and (bracket) values
5. Verify charge_limit entity matches the displayed target, not 100%

## Issue 2: ML Load Prediction

- ML intentionally switched to `load_ml_source: True` (driving battery decisions)
- 13 days training, MAE 0.0026 kWh/slot, learned GSHP/temperature correlation
- Morning heating spike (05:00-07:00) clearly visible
- Some noise at borderline GSHP temps (8-10C) — binary on/off creates slot-level errors
- Total energy tracking reasonable: ML 60.5 kWh vs standard 67.0 kWh

## Current System Status

| Entity | Value |
|--------|-------|
| `switch.predbat_set_read_only` | **on** (since 23:26 UTC) |
| Battery SOC | **76.1%** |
| charge_limit | **100%** (should be lowered when read-only disabled) |
| reserve | **0%** |
| Current plan | HoldChrg 63 (75) — looks correct |

## Action items
1. **Fix the bug** on sig-inverter-control branch, merge into mum, deploy
2. **Re-enable Predbat** (disable read-only) — current plan looks correct
3. **Update MEMORY.md** — record load_ml_source=True is intentional
