#!/usr/bin/env python3
"""Guards for the mirrored Predbat addon config, `ha/mum-apps.yaml`.

WHY THIS EXISTS. On 2026-08-17 the 18:00 saving session was invisible to the
whole stack. `octopus_saving_session` pointed at
`binary_sensor.octopus_energy_*_octoplus_saving_sessions`, which the Octopus
integration removed in v19.0.0 (ADR 0004 renamed Saving Sessions -> Power Down).
Predbat's `auto_config` could not resolve the `re:` argument, logged
"unable to match ..., now will disable", and DELETED the argument. `fetch_octopus_sessions`
is gated on `if "octopus_saving_session" in self.args`, so the entire feature went
quiet: no auto-join, no saving rate in the plan. The failure was a warning in a log
nobody greps, in a file that was not in git.

So the guard that matters is not "does the YAML parse" — it is "does this pointer
still resolve to an entity that exists". These tests resolve it the way
`userinterface.py:resolve_arg_re` does (anchored `^...$`, group(1) wins) against the
real entity names, so a rename breaks the test rather than the battery.

Run: cd apps/predbat && python3 tests/test_yaml_mum_apps.py
"""

import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
APPS_YAML = os.path.join(HERE, "..", "ha", "mum-apps.yaml")

# Entity names as they exist on the box, verified live 2026-08-17. The account id is
# part of the name, which is exactly why the config uses a regex rather than a literal.
LIVE_POWER_DOWN_EVENTS = "event.octopus_energy_a_4ba7c915_octoplus_power_down_events"
LIVE_POWER_UP_EVENTS = "event.octopus_energy_a_4ba7c915_octoplus_power_up_events"

# Removed by the integration in v19.0.0. Anything pointing here is pointing at nothing.
RETIRED_BINARY_SENSOR = "binary_sensor.octopus_energy_a_4ba7c915_octoplus_saving_sessions"
RETIRED_CALENDAR = "calendar.octopus_energy_a_4ba7c915_octoplus_saving_sessions"


def _load():
    """Parse the mirrored config with PyYAML, which is what Predbat itself uses."""
    with open(APPS_YAML) as handle:
        return yaml.safe_load(handle)["pred_bat"]


def _resolve(arg_value, entity_names):
    """Resolve a `re:` argument exactly as userinterface.py:resolve_arg_re does.

    Anchored `^...$`, first matching key wins, group(1) if the pattern has groups.
    Returns the resolved entity name, or None if nothing matched (which is the
    case that silently disabled the whole feature).
    """
    if not isinstance(arg_value, str) or not arg_value.startswith("re:"):
        return arg_value
    pattern = "^" + arg_value[3:] + "$"
    for name in entity_names:
        found = re.search(pattern, name)
        if found:
            return found.group(1) if found.groups() else found.group(0)
    return None


def test_config_parses_and_is_the_site_config():
    """The mirror is loadable and is Mum's SIG config, not the upstream template."""
    cfg = _load()
    assert cfg["inverter_type"] == "SIG", "expected inverter_type SIG, got {}".format(cfg.get("inverter_type"))
    assert cfg["prefix"] == "predbat", "expected prefix predbat, got {}".format(cfg.get("prefix"))


def test_saving_session_resolves_to_the_live_power_down_entity():
    """The pointer must resolve — an unresolved `re:` deletes the arg and kills the feature."""
    cfg = _load()
    resolved = _resolve(cfg["octopus_saving_session"], [LIVE_POWER_DOWN_EVENTS, LIVE_POWER_UP_EVENTS])
    assert resolved == LIVE_POWER_DOWN_EVENTS, "octopus_saving_session resolved to {!r}, expected {!r}".format(resolved, LIVE_POWER_DOWN_EVENTS)


def test_saving_session_does_not_match_power_up_events():
    """Power Up rides its own entity; matching it here would point sessions at the dead scrape."""
    cfg = _load()
    resolved = _resolve(cfg["octopus_saving_session"], [LIVE_POWER_UP_EVENTS])
    assert resolved is None, "octopus_saving_session must not match the Power Up entity, matched {!r}".format(resolved)


def test_free_electricity_points_at_the_live_power_up_feed():
    """Predbat must be TOLD about free sessions, and told the right entity.

    2026-10-10: a Free Electricity session was booked for the next day 13:00-15:00
    and Predbat planned the whole afternoon at 11.83p ("Import rates: min 11.83p"),
    because no `octopus_free_*` key had ever been set. `fetch_octopus_sessions` is
    gated on the key being present, the same shape as the 2026-08-17 defect.

    `octopus_free_electricity`, NOT `octopus_free_session`: the latter skips any
    event without a `code`, and the 2026 campaign events carry `code: null`.

    A LITERAL, not a `re:` — an unmatched `re:` is deleted by auto_config and the
    feature goes quiet again; a literal that goes stale fails this test instead.
    """
    cfg = _load()
    assert "octopus_free_session" not in cfg, "octopus_free_session drops events whose code is null — use octopus_free_electricity"
    value = cfg.get("octopus_free_electricity")
    assert value == LIVE_POWER_UP_EVENTS, "octopus_free_electricity must be the live Power Up events entity {!r}, found {!r}".format(LIVE_POWER_UP_EVENTS, value)


def test_charge_rate_limit_is_the_rated_charging_power():
    """Predbat must model the battery's CHARGE limit, not its discharge limit.

    `battery_rate_max` points at the rated DISCHARGING power (9.6 kW) and Predbat
    uses it for both directions unless told otherwise. The pack's charge limit is
    8.8 kW. Live 2026-10-11: "charge rate 9.6kW" in the log, and a plan that
    promised 0% -> 100% of 18.08 kWh inside a two-hour free session, which 8.8 kW
    cannot deliver (17.6 kWh).
    """
    cfg = _load()
    assert cfg["battery_rate_max"] == ["sensor.sigen_plant_ess_rated_discharging_power"], "precondition: battery_rate_max is still the discharge rating, got {!r}".format(cfg["battery_rate_max"])
    # A literal in watts, NOT the sensor. The key validates as an integer; pointing it at
    # sensor.sigen_plant_ess_rated_charging_power (8.8, in kW) modelled the right rate but
    # logged "is not a valid type integer ... found 1 configuration errors" on every start
    # (live 2026-10-11 15:03).
    assert cfg.get("inverter_limit_charge") == [8800], "inverter_limit_charge must be the pack's 8.8 kW charge limit as integer watts, got {!r}".format(cfg.get("inverter_limit_charge"))


def test_no_pointer_at_a_retired_octopus_entity():
    """The exact 2026-08-17 defect: a config value naming an entity that no longer exists."""
    with open(APPS_YAML) as handle:
        body = "\n".join(line for line in handle.read().split("\n") if not line.strip().startswith("#"))
    for retired in (RETIRED_BINARY_SENSOR, "_octoplus_saving_session", RETIRED_CALENDAR):
        assert retired not in body, "config still references retired Octopus entity {!r} (removed in integration v19.0.0)".format(retired)


def test_mcp_secret_is_redacted_in_the_repo_copy():
    """This fork is public. The mirror must never carry the live MCP secret."""
    cfg = _load()
    secret = cfg["mcp_secret"]
    assert secret == "REDACTED-SEE-BOX", "mcp_secret in the repo mirror must be 'REDACTED-SEE-BOX', found {!r} — do not commit the live value".format(secret)


def run():
    """Run the mirrored addon-config harness."""
    print("**** mum-apps.yaml (mirrored Predbat addon config) ****")
    failed = False
    for fn in (
        test_config_parses_and_is_the_site_config,
        test_saving_session_resolves_to_the_live_power_down_entity,
        test_saving_session_does_not_match_power_up_events,
        test_free_electricity_points_at_the_live_power_up_feed,
        test_charge_rate_limit_is_the_rated_charging_power,
        test_no_pointer_at_a_retired_octopus_entity,
        test_mcp_secret_is_redacted_in_the_repo_copy,
    ):
        try:
            fn()
        except AssertionError as e:
            print("FAIL — {}".format(e))
            failed = True
    print("test_yaml_mum_apps: {}".format("FAILURES" if failed else "ALL PASSED"))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
