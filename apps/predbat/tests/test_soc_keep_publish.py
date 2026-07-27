# -----------------------------------------------------------------------------
# Tests for soc_keep_publish_plugin — the effective best_soc_keep sensor
#
# This sensor was previously published by a local patch to predbat.py. It now
# comes from a plugin so we can run stock Predbat. These tests lock:
#   - it publishes the FULLY CHAINED value (not any single plugin's delta)
#   - it never modifies the context (observe-only)
#   - the entity ID stays sensor.<prefix>_best_soc_keep, which /soc-keep-review
#     reads and which HA recorder history is continuous on
#
# Run: cd apps/predbat && python3 tests/test_soc_keep_publish.py
# -----------------------------------------------------------------------------

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin_system import PluginSystem, PredBatPlugin
from soc_keep_publish_plugin import SocKeepPublishPlugin


def _mock_base():
    base = MagicMock()
    base.log = MagicMock()
    base.prefix = "predbat"
    base.dashboard_item = MagicMock()
    return base


def test_publishes_chained_value_not_intermediate():
    """Must publish the end of the chain, after every adjusting plugin has run."""
    base = _mock_base()
    ps = PluginSystem(base)

    class FakeCurtailment(PredBatPlugin):
        priority = 10

        def on_before_plan(self, context):
            context = dict(context)
            context["best_soc_keep"] = 2.0  # sunny day: relax the keep
            return context

    class FakeColdWeather(PredBatPlugin):
        priority = 200

        def on_before_plan(self, context):
            context = dict(context)
            context["best_soc_keep"] = context["best_soc_keep"] + 1.5  # additive boost
            context["best_soc_keep_weight"] = 1.0
            return context

    publisher = SocKeepPublishPlugin(base)
    # Register out of order — priority, not registration order, must decide
    publisher.register_hooks(ps)
    cold = FakeColdWeather(base)
    curt = FakeCurtailment(base)
    ps.register_hook("on_before_plan", cold.on_before_plan, plugin=cold)
    ps.register_hook("on_before_plan", curt.on_before_plan, plugin=curt)

    out = ps.call_before_plan_hooks({"best_soc_keep": 6.0, "best_soc_keep_weight": 0.5})

    assert out["best_soc_keep"] == 3.5, "chain wrong: {}".format(out)
    base.dashboard_item.assert_called_once()
    entity, state, attrs = base.dashboard_item.call_args[0]
    assert entity == "sensor.predbat_best_soc_keep", "entity ID changed: {}".format(entity)
    assert state == 3.5, "published {} — not the chained value (2.0 or 6.0 = wrong position)".format(state)
    assert attrs["weight"] == 1.0, "weight not taken from chained context: {}".format(attrs)
    assert attrs["unit_of_measurement"] == "kWh"
    print("  test_publishes_chained_value_not_intermediate: PASSED")
    return 0


def test_runs_last():
    """Priority must exceed the adjusting plugins so it observes, never pre-empts."""
    assert SocKeepPublishPlugin.priority > 200, "must run after cold_weather (200)"
    print("  test_runs_last: PASSED")
    return 0


def test_does_not_modify_context():
    """Observe-only: the returned context must be unchanged."""
    base = _mock_base()
    publisher = SocKeepPublishPlugin(base)
    ctx = {"best_soc_keep": 4.0, "best_soc_keep_weight": 0.5}
    out = publisher.on_before_plan(ctx)
    assert out == {"best_soc_keep": 4.0, "best_soc_keep_weight": 0.5}, "context mutated: {}".format(out)
    print("  test_does_not_modify_context: PASSED")
    return 0


def test_missing_key_skips_publish():
    """If the host contract changes, publish nothing rather than a misleading 0."""
    base = _mock_base()
    publisher = SocKeepPublishPlugin(base)
    out = publisher.on_before_plan({"best_soc_keep_weight": 0.5})
    base.dashboard_item.assert_not_called()
    assert out == {"best_soc_keep_weight": 0.5}
    print("  test_missing_key_skips_publish: PASSED")
    return 0


def test_weight_defaults_when_absent():
    """Absent weight falls back to Predbat's default rather than raising."""
    base = _mock_base()
    publisher = SocKeepPublishPlugin(base)
    publisher.on_before_plan({"best_soc_keep": 4.0})
    _, _, attrs = base.dashboard_item.call_args[0]
    assert attrs["weight"] == 0.5, "expected default weight, got {}".format(attrs["weight"])
    print("  test_weight_defaults_when_absent: PASSED")
    return 0


def run_soc_keep_publish_tests(_my_predbat=None):
    """Entry compatible with unit_test registry (ignores my_predbat)."""
    failed = 0
    print("*** SOC keep publish plugin ***")
    for fn in (
        test_publishes_chained_value_not_intermediate,
        test_runs_last,
        test_does_not_modify_context,
        test_missing_key_skips_publish,
        test_weight_defaults_when_absent,
    ):
        try:
            failed |= fn() or 0
        except AssertionError as e:
            print(f"  {fn.__name__}: FAILED — {e}")
            failed = 1
        except Exception as e:
            print(f"  {fn.__name__}: ERROR — {e}")
            failed = 1
    if not failed:
        print("**** SOC keep publish plugin: all PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_soc_keep_publish_tests() else 0)
