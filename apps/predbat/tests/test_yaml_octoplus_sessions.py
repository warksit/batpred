#!/usr/bin/env python3
# Octoplus session discrimination — the single point of truth.
#
# ha/octoplus_session_helpers.yaml decides ONCE whether a running Octoplus event
# is a Power Up (free import -> grid charge) or a Power Down (paid -> export at
# the cap). Octopus publish both on one feed with no type field, so getting this
# wrong is expensive in both directions.
#
# Three guards here:
#   1. the sensors classify real observed events correctly, and are exact
#      complements — an event can never be both or neither
#   2. no consumer re-implements the test, so when upstream #1590 lands there is
#      exactly one body to rewrite and no stale copy left behind
#   3. the deliberate decisions stay made: no time-of-day gate, and the handback
#      restores whoever was driving
#
# Run: cd apps/predbat && python3 tests/test_yaml_octoplus_sessions.py
import datetime
import os
import sys

import jinja2
import yaml

HERE = os.path.dirname(__file__)
HELPERS_PATH = os.path.join(HERE, "..", "ha", "octoplus_session_helpers.yaml")
INTENT_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_intent_helpers.yaml")
AUTOMATION_PATH = os.path.join(HERE, "..", "ha", "octoplus_power_up_grid_charge.yaml")

EVENTS_ENTITY = "event.octopus_energy_a_4ba7c915_octoplus_power_down_events"
LEGACY_CALENDAR = "calendar.octopus_energy_a_4ba7c915_octoplus_saving_sessions"
UP_SENSOR = "binary_sensor.octoplus_power_up_active"
DOWN_SENSOR = "binary_sensor.octoplus_power_down_active"

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 16, 13, 30, tzinfo=UTC)


def _load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _at(hour, minute=0):
    """A tz-aware datetime on the observation day."""
    return datetime.datetime(2026, 8, 16, hour, minute, tzinfo=UTC)


def _event(start_h, end_h, points, drop_points=False):
    """One joined_events entry, shaped as the integration publishes it."""
    ev = {"id": 5143, "start": _at(start_h), "end": _at(end_h)}
    if not drop_points:
        ev["octopoints_per_kwh"] = points
    return ev


def render(joined, now=NOW):
    """Render both discrimination sensors against a joined_events list."""
    doc = _load(HELPERS_PATH)
    by_id = {s["unique_id"]: s for s in doc["binary_sensors"]}
    env = jinja2.Environment()

    def as_datetime(value):
        if isinstance(value, datetime.datetime):
            return value
        try:
            return datetime.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    ctx = {
        "state_attr": lambda e, a: joined if (e == EVENTS_ENTITY and a == "joined_events") else None,
        "as_datetime": as_datetime,
        "now": lambda: now,
    }

    def one(uid):
        return env.from_string(by_id[uid]["state"]).render(**ctx).strip().lower() == "true"

    return one("octoplus_power_up_active"), one("octoplus_power_down_active")


# The observed corpus. Power Downs are this account's 18 joined events; the Power
# Up is 2026-08-16, which another account (upstream #1820) saw as four
# consecutive 0-point slots joined individually.
POWER_DOWN_POINTS = (16, 93, 96, 102, 109, 415)
POWER_UP_SLOTS = ((11, 12), (12, 13), (13, 14), (14, 15))


def test_power_down_points_classify_as_power_down():
    """Every reward this account has ever been paid means Power Down."""
    for pts in POWER_DOWN_POINTS:
        up, down = render([_event(13, 14, pts)])
        assert not up, "{} pts must NOT be a Power Up — grid-charging through a paid session imports 6.6 kW in the hour we are paid to export".format(pts)
        assert down, "{} pts must be a Power Down".format(pts)
    print("PASS  {} observed reward values classify as Power Down".format(len(POWER_DOWN_POINTS)))


def test_zero_points_in_window_is_a_power_up():
    """The 2026-08-16 event: 0 pts, free import, four hourly slots."""
    for start_h, end_h in POWER_UP_SLOTS:
        joined = [_event(start_h, end_h, 0)]
        up, down = render(joined, now=_at(start_h, 30))
        assert up, "0-pt slot {}:00-{}:00 must be a Power Up".format(start_h, end_h)
        assert not down, "0-pt slot {}:00-{}:00 must not also force Max Export".format(start_h, end_h)
    print("PASS  all {} observed 0-pt slots classify as Power Up".format(len(POWER_UP_SLOTS)))


def test_sensors_are_exact_complements_in_window():
    """Never both, never neither — inside a joined window exactly one is on.

    This is the guard that makes two separately-written scans safe: they share a
    window test written twice, and this asserts the copies agree over the matrix
    rather than trusting them to.
    """
    bad = []
    n = 0
    for pts in (0, 1, 16, 415, -5):
        for start_h, end_h in POWER_UP_SLOTS:
            n += 1
            up, down = render([_event(start_h, end_h, pts)], now=_at(start_h, 30))
            if up == down:
                bad.append("pts={} {}:00-{}:00 -> up={} down={}".format(pts, start_h, end_h, up, down))
    assert not bad, "{} of {} in-window cases are not complementary:\n  {}".format(len(bad), n, "\n  ".join(bad[:8]))
    print("PASS  sensors are exact complements over {} in-window cases".format(n))


def test_outside_the_window_both_are_off():
    """A joined event that is not running now must drive nothing."""
    for now in (_at(10, 30), _at(15, 30)):
        up, down = render([_event(11, 12, 0), _event(13, 14, 96)], now=now)
        assert not up and not down, "at {} no event is running; got up={} down={}".format(now, up, down)
    # Boundaries: start is inclusive, end is exclusive.
    up, _ = render([_event(11, 12, 0)], now=_at(11, 0))
    assert up, "window start must be inclusive"
    up, _ = render([_event(11, 12, 0)], now=_at(12, 0))
    assert not up, "window end must be exclusive, or two adjacent slots both claim the boundary"
    print("PASS  outside the window both sensors are off; boundaries are [start, end)")


def test_unreadable_reward_fails_towards_power_down():
    """Absent points must NOT read as free electricity.

    0 is the signal, so a missing field cannot be allowed to look like one.
    Losing a free hour costs the import/export spread; importing 6.6 kW through
    a real saving session costs the premium and the import.
    """
    up, down = render([_event(13, 14, None, drop_points=True)])
    assert not up, "a missing reward must never authorise grid charging"
    assert down, "a missing reward must still count as a paid session"
    print("PASS  unreadable reward fails towards Power Down")


def test_no_joined_events_drives_nothing():
    """Empty list, and the attribute missing entirely (integration reloading)."""
    for joined in ([], None):
        up, down = render(joined)
        assert not up and not down, "joined={!r} must drive nothing; got up={} down={}".format(joined, up, down)
    print("PASS  absent/empty joined_events drives nothing")


def test_available_but_unjoined_events_are_ignored():
    """Slots are offers until joined. Acting on an offer we did not take would
    import at the full rate for an hour."""
    doc = _load(HELPERS_PATH)
    for sensor in doc["binary_sensors"]:
        assert "available_events" not in sensor["state"], "{} must read joined_events only — an offer is not participation".format(sensor["unique_id"])
    print("PASS  sensors read joined_events only, never available_events")


def _consumer_blobs():
    """Every file that acts on a session, as text."""
    return {
        "octoplus_power_up_grid_charge.yaml": yaml.safe_dump(_load(AUTOMATION_PATH)),
        "sig_dispatch_intent_helpers.yaml": yaml.safe_dump(_load(INTENT_PATH)),
    }


def test_no_consumer_reimplements_the_discriminator():
    """The single point of truth must stay single.

    The discriminator is a PROXY that upstream is expected to replace (#1590).
    A second copy in a consumer means the swap leaves a stale one behind, still
    exporting through free hours. Four copies lived in the automation alone
    before 2026-08-16.
    """
    for name, blob in _consumer_blobs().items():
        for token in ("octopoints_per_kwh", "joined_events"):
            assert token not in blob, "{} contains {!r} — the discriminator belongs ONLY in octoplus_session_helpers.yaml".format(name, token)
    print("PASS  no consumer re-implements the discriminator")


def test_no_consumer_keys_on_the_legacy_calendar():
    """The legacy calendar carries BOTH categories, and is removed January 2027.

    Keying on it forces Max Export through a free-import hour today, and goes
    permanently false in January 2027 with no error and no log.
    """
    for name, blob in _consumer_blobs().items():
        assert LEGACY_CALENDAR not in blob, "{} still keys on the legacy saving-sessions calendar; use {}".format(name, DOWN_SENSOR)
    intent = yaml.safe_dump(_load(INTENT_PATH))
    assert DOWN_SENSOR in intent, "the Max Export forcing must read {}".format(DOWN_SENSOR)
    automation = yaml.safe_dump(_load(AUTOMATION_PATH))
    assert UP_SENSOR in automation, "the grid charge must read {}".format(UP_SENSOR)
    print("PASS  consumers read the discrimination sensors, not the legacy calendar")


def test_automation_has_no_time_of_day_gate():
    """Andrew, 2026-08-16: no hour gate.

    Octopus publish 11:00-16:00 for Power Ups and every observed Power Down
    starts 18:00 or later, but the hour is correlation and must not become a
    second, weaker copy of the test. If it ever becomes logic it belongs in the
    discrimination sensor with the rest.
    """
    blob = yaml.safe_dump(_load(AUTOMATION_PATH))
    for token in (".hour", "st.hour", "now().hour"):
        assert token not in blob, "octoplus_power_up_grid_charge.yaml contains {!r} — the hour gate was removed deliberately".format(token)
    print("PASS  no time-of-day gate in the power-up automation")


def test_handback_restores_whoever_was_driving():
    """Same owner both sides: CM -> session -> CM, or Predbat -> session -> Predbat.

    The previous version cleared `sig_override`, stood CM down and disabled the
    three Predbat mappers, then restored only CM — leaving the override cleared
    and the mappers off.
    """
    doc = _load(AUTOMATION_PATH)
    blob = yaml.safe_dump(doc)
    for var in ("prev_override", "prev_cm_on", "prev_mappers_on"):
        assert var in blob, "the automation must capture {} before it takes the wheel, or handback is a guess".format(var)

    # The capture must happen before anything is switched off, or it records our
    # own changes instead of the pre-window owner.
    actions = doc["actions"]
    capture_idx = next((i for i, a in enumerate(actions) if "variables" in a), None)
    assert capture_idx is not None, "no variables block captures the pre-window owner"
    for i, action in enumerate(actions):
        if action.get("action") in ("input_boolean.turn_off", "automation.turn_off", "input_select.select_option"):
            assert i > capture_idx, "action #{} ({}) runs BEFORE the owner is captured".format(i, action.get("action"))

    # Restoring the mappers unconditionally would hand Predbat the registers in
    # the seconds before CM's mutex disabled them again.
    turn_on_mappers = [a for a in actions if a.get("action") == "automation.turn_on"]
    assert not turn_on_mappers, "mapper restore must be guarded by prev_mappers_on, not an unconditional turn_on"
    assert "automation.turn_on" in blob, "the mappers must be restored when they were on before the window"
    print("PASS  handback captures and restores the pre-window owner")


def run():
    """Run the Octoplus session discrimination harness."""
    print("**** Octoplus session discrimination (single point of truth) ****")
    failed = False
    for fn in (
        test_power_down_points_classify_as_power_down,
        test_zero_points_in_window_is_a_power_up,
        test_sensors_are_exact_complements_in_window,
        test_outside_the_window_both_are_off,
        test_unreadable_reward_fails_towards_power_down,
        test_no_joined_events_drives_nothing,
        test_available_but_unjoined_events_are_ignored,
        test_no_consumer_reimplements_the_discriminator,
        test_no_consumer_keys_on_the_legacy_calendar,
        test_automation_has_no_time_of_day_gate,
        test_handback_restores_whoever_was_driving,
    ):
        try:
            fn()
        except AssertionError as e:
            print("FAIL — {}".format(e))
            failed = True
    print("test_yaml_octoplus_sessions: {}".format("FAILURES" if failed else "ALL PASSED"))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
