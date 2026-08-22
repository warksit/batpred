#!/usr/bin/env python3
"""Every requirement marked IN FORCE must have code implementing it.

WHY THIS EXISTS
---------------
2026-07-29: the policy flapped Max Export <-> Predbat eight times in 45 minutes.
Cause: R16a (Schmitt deadband, `OUTER_THRESHOLD_KWH = 0.18`) has been a
requirement since v19, but its implementation lived in the 5-second HA
automation that **v30 retired**. `compute_proposed_phase` even carried the
comment "No hysteresis here — that belongs in the HA automation
Schmitt-trigger", pointing at something that no longer existed.

At that moment `OUTER_THRESHOLD_KWH` appeared 3x in REQUIREMENTS.md and 0x in
code. Nothing noticed for months. The user found it by watching the inverter.

This is one instance of a pattern that cost most of 2026-07-28/29:
    R43  marked REPLACED  -> still running
    R45  marked REMOVED   -> load-bearing
    R11  rationale inverted from its mechanism
    R16a required         -> not implemented
The doc and the code drift, and the drift is invisible until it misbehaves.

The Charter requires an `**Implemented in:** file.py:function` field on every
requirement precisely to stop this. That field is documentation; THIS is the
check. A requirement that nothing implements is a failing test, not a surprise.

MAINTAINING IT
--------------
Each entry pairs a requirement with a marker that must appear in PRODUCTION
code (not just tests). The marker should be the thing that would vanish if the
implementation were deleted — a constant, a function name, a distinctive
expression. When you retire a requirement, delete its row here AND move it to
Part 2 (History) in REQUIREMENTS.md. When you add one, add a row.

Run: cd apps/predbat && python3 tests/test_requirements_implemented.py
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (requirement id, what it does, regex that must appear in production code)
REQUIREMENTS = [
    ("R2", "clean deactivate: release + clear read_only", r"_release_to_predbat|_set_read_only"),
    ("R3", "read_only is the CM<->Predbat mutex", r"set_read_only|_read_only_set"),
    ("R4", "defer to Predbat charge window (GSHP-gated)", r"should_defer_to_charge"),
    ("R6", "deactivate / handback at safe_time or sundown", r"reached_safe_time|sundown"),
    ("R9", "overflow integral -> curtailment floor", r"required_headroom_kwh|overflow_floor"),
    ("R9a", "effective_load uses smoothed LoadML", r"smooth_load_forecast"),
    ("R10", "final clamp max(min(floor, keep), reserve)", r"compute_floor_with_source"),
    ("R16a", "Schmitt deadband on Drain entry", r"OUTER_THRESHOLD_KWH"),
    ("R19", "safe_time from solar geometry", r"compute_release_time"),
    ("R21", "safe_time only moves later until peak confirmed", r"safe_scale|peak_confirmed"),
    ("R25", "geometry owns timing", r"compute_solar_overflow|compute_pv_start_time"),
    ("R26", "on_before_plan reduces soc_keep", r"on_before_plan"),
    ("R29", "tomorrow sensor", r"_compute_tomorrow_forecast"),
    ("R42", "p90 scale from Solcast", r"p_scales_from_forecast|p90_scale"),
    ("R43", "floor_scale = max(p90_scale, actual_scale)", r"actual_scale > p90_scale"),
    ("R45", "tapered reserve min(max_reserved, overflow)", r"required_headroom_kwh|effective_max_reserved"),
    ("R47", "state persistence across restarts", r"_save_state|STATE_FILE_NAME"),
    ("R48", "relaxed soc_keep two-phase latch", r"_r48_engaged_today"),
    ("R49", "buffer reduction on confirmed-cloudy afternoons", r"_refresh_effective_max_reserved|BUFFER_REDUCE"),
    ("R50a", "live overflow path is p90, blend dormant", r"_expected_overflow"),
    ("R52", "pre-PV drain", r"_pre_pv_drain_decision"),
    ("R53", "Solcast per-slot overflow integral", r"compute_solcast_overflow"),
    ("R54", "single drain-target rule", r"compute_drain_above"),
    ("R55", "overnight target from the morning gap", r"compute_morning_gap"),
    ("R58", "actual_scale as 30-min calibration only", r"_compute_calibration_ratio|calibration_ratio"),
    ("R59b", "recovery floor nets against P10 generation", r"compute_p10_recovery_floor"),
    ("R60", "effective export cap", r"compute_effective_export_cap"),
    ("R61", "no-surplus drain hold", r"apply_no_surplus_drain_hold"),
    ("R62", "forecast-driven pre-PV target", r"compute_pre_pv_target"),
    ("R63", "drain deadline", r"drain_deadline_breached"),
    ("R64", "rolling median on the overflow estimate", r"smooth_overflow_samples"),
    ("RD4", "low-SOC handover to MSC", r"low_soc"),
    ("RD6", "safe_time drives the Hold override", r"past_safe"),
    ("RD13a", "manual override is the select alone", r"SIG_OVERRIDE_SELECT"),
    ("RD14c", "saving sessions from the Octoplus calendar", r"octoplus_saving_sessions"),
    ("RD20", "keep floor tracks drain intent", r"_set_keep_floor"),
    ("RD34", "dawn reserve floors Predbat's export plan", r"_set_predbat_export_floor"),
    ("RD46", "night need caps Predbat's charge plan", r"_set_predbat_charge_cap|_predbat_charge_cap_kwh"),
]


def _production_sources():
    """Production code only — a requirement satisfied solely by tests is NOT
    implemented, which is exactly the R16a failure mode."""
    out = [os.path.join(ROOT, "curtailment_plugin.py"), os.path.join(ROOT, "curtailment_calc.py")]
    out += sorted(glob.glob(os.path.join(ROOT, "ha", "*.yaml")))
    return out


def test_every_in_force_requirement_has_an_implementation():
    """A requirement with no code behind it is a lie the docs tell."""
    blobs = []
    for path in _production_sources():
        with open(path) as f:
            blobs.append((os.path.basename(path), f.read()))

    missing = []
    for rid, desc, pattern in REQUIREMENTS:
        if not any(re.search(pattern, body) for _, body in blobs):
            missing.append((rid, desc, pattern))

    if missing:
        lines = ["requirements marked IN FORCE with NO production implementation:"]
        for rid, desc, pattern in missing:
            lines.append("  {:<7} {:<50} (looked for /{}/)".format(rid, desc, pattern))
        lines.append("")
        lines.append("Either implement it, or move it to Part 2 (History) in REQUIREMENTS.md")
        lines.append("and delete its row here. Do not leave it claiming to be in force.")
        raise AssertionError("\n".join(lines))
    print("PASS  {} IN FORCE requirements all have production implementations".format(len(REQUIREMENTS)))


def test_the_check_can_actually_fail():
    """Guard the guard: a marker that cannot exist must be reported missing.

    Without this, a bug in the matcher would make the whole suite vacuously
    pass — the same class of silent no-op the audit exists to catch.
    """
    blobs = [(n, b) for n, b in [(os.path.basename(p), open(p).read()) for p in _production_sources()]]
    bogus = r"THIS_MARKER_MUST_NEVER_EXIST_IN_THE_CODEBASE"
    assert not any(re.search(bogus, body) for _, body in blobs), "the negative control must not match"
    print("PASS  negative control: the check reports a genuinely absent marker")


def main():
    """Run the requirement-implementation audit."""
    for t in (test_the_check_can_actually_fail, test_every_in_force_requirement_has_an_implementation):
        t()
    print("test_requirements_implemented: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAIL — {}".format(e))
        sys.exit(1)
