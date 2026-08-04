# -----------------------------------------------------------------------------
# GSHP DHW cycle meter — HA automation YAML test harness
#
# Loads ha/gshp_dhw_cycle_meter.yaml and renders the delta template against
# known meter readings, plus asserts the trigger/guard structure that stops a
# spurious cycle overwriting the day's real figure.
#
# Why the guards are worth a test: over 25 Jul - 4 Aug the heat pump showed two
# excursions above standby that were NOT reheats (Sat 25 Jul 12:00 and Fri
# 31 Jul 18:00, both 46.7 W) — 3.3 W under the 50 W threshold, and the 18:00 one
# is after the 13:00 window opens. Without the sustain and once-per-day guards a
# slightly larger blip would record ~0 kWh and mark the day done.
#
# Run: cd apps/predbat && python3 tests/test_yaml_dhw_meter.py
# -----------------------------------------------------------------------------

import os
import sys

import jinja2
import yaml

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ha",
    "gshp_dhw_cycle_meter.yaml",
)

POWER = "sensor.heat_pump_energy_meter_power"
LIFETIME = "sensor.heat_pump_energy_meter_energy"
START_KWH = "input_number.gshp_dhw_cycle_start_kwh"


def load_doc():
    """Parse the automation YAML."""
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def render(template, states):
    """Render a HA-style Jinja template with a states() shim."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["states"] = lambda e: str(states.get(e, "unknown"))
    return env.from_string(template).render().strip()


def branch(doc, marker):
    """Return the choose branch whose sequence mentions `marker`."""
    for opt in doc["actions"][0]["choose"]:
        if marker in yaml.safe_dump(opt):
            return opt
    raise AssertionError("no branch containing {}".format(marker))


def test_delta_template():
    """The recorded value is end-meter minus start-meter, clamped at zero."""
    doc = load_doc()
    end = branch(doc, "gshp_dhw_today_kwh")
    tmpl = None
    for step in end["sequence"]:
        if step.get("action") == "input_number.set_value":
            tmpl = step["data"]["value"]
    assert tmpl, "end branch must write input_number.gshp_dhw_today_kwh"

    # 4 Aug real numbers: cycle 13:10:50 -> 13:45:19, ~1.5 kWh of reheat.
    out = render(tmpl, {LIFETIME: "1929.96727", START_KWH: "1928.46727"})
    assert abs(float(out) - 1.5) < 0.001, out

    # A stale start reading (HA restarted mid-cycle) must not write negative kWh.
    out = render(tmpl, {LIFETIME: "1929.0", START_KWH: "1999.0"})
    assert float(out) == 0, "negative delta must clamp to 0, got {}".format(out)

    # Unavailable sensors must not raise or produce nonsense.
    out = render(tmpl, {})
    assert float(out) == 0, out
    print("  test_delta_template: PASSED")


def test_start_is_sustained_and_once_per_day():
    """Guards against the 46.7 W blips that are not reheats."""
    doc = load_doc()
    trig = {t["id"]: t for t in doc["triggers"]}

    assert trig["cycle_start"]["above"] == 50
    assert trig["cycle_start"].get("for") == "00:01:00", "an instantaneous blip must not arm the meter"

    start = branch(doc, "gshp_dhw_cycle_start_kwh")
    guards = yaml.safe_dump(start["conditions"])
    assert "gshp_dhw_done_today" in guards, "only the FIRST cycle after 13:00 may be recorded"
    assert "13:00:00" in guards, "start must be gated to at/after 13:00"
    # A numeric_state trigger only fires on crossing, so a reheat already running
    # at 13:00 needs the time trigger to arm it.
    assert "window_open" in yaml.safe_dump(start["conditions"]), "13:00 trigger must also be able to start the cycle"
    print("  test_start_is_sustained_and_once_per_day: PASSED")


def test_every_close_path_records_and_rearms():
    """Both the normal end and the failsafe must write the value AND set the
    once-per-day flag — otherwise a later blip re-opens and overwrites it."""
    doc = load_doc()
    for marker, name in (("cycle_end", "normal end"), ("failsafe_close", "failsafe")):
        opt = None
        for o in doc["actions"][0]["choose"]:
            c = yaml.safe_dump(o["conditions"])
            if marker in c and "gshp_dhw_running" in c and "day_reset" not in c:
                opt = o
        assert opt, "no {} branch".format(name)
        seq = yaml.safe_dump(opt["sequence"])
        assert "gshp_dhw_today_kwh" in seq, "{} must record the value".format(name)
        assert "turn_off" in seq and "gshp_dhw_running" in seq, "{} must clear the latch".format(name)
        assert "gshp_dhw_done_today" in seq, "{} must set done_today".format(name)

    # Midnight must re-arm everything, or the meter records once and never again.
    reset = None
    for o in doc["actions"][0]["choose"]:
        if "day_reset" in yaml.safe_dump(o["conditions"]):
            reset = o
    assert reset, "no midnight reset branch"
    seq = yaml.safe_dump(reset["sequence"])
    assert "gshp_dhw_done_today" in seq and "gshp_dhw_running" in seq, "midnight must clear both latches"
    assert "value: 0" in seq, "a day with no reheat must read 0.0, not yesterday's figure"
    print("  test_every_close_path_records_and_rearms: PASSED")


def run_dhw_meter_yaml_tests():
    """Run the DHW cycle meter YAML harness."""
    print("**** Running GSHP DHW cycle meter YAML tests ****")
    failed = False
    for fn in (test_delta_template, test_start_is_sustained_and_once_per_day, test_every_close_path_records_and_rearms):
        try:
            fn()
        except AssertionError as e:
            print("  {}: FAILED — {}".format(fn.__name__, e))
            failed = True
    if not failed:
        print("**** GSHP DHW cycle meter YAML: all PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_dhw_meter_yaml_tests() else 0)
