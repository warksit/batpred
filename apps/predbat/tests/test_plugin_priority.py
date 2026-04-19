# -----------------------------------------------------------------------------
# Predbat Home Battery System - Plugin priority test
# Copyright Trefor Southwell 2026 - All Rights Reserved
# -----------------------------------------------------------------------------
# Tests that discover_plugins registers hooks in ascending PLUGIN_PRIORITY order.
# Plugins without PLUGIN_PRIORITY default to priority 100.
#
# Run: cd apps/predbat && python3 tests/test_plugin_priority.py

import os
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin_system import PluginSystem


class MockBase:
    def __init__(self):
        self.log_lines = []

    def log(self, msg):
        self.log_lines.append(msg)


PLUGIN_TEMPLATE = textwrap.dedent(
    """
    class {class_name}:
        {priority_attr}
        def __init__(self, base):
            self.base = base
        def register_hooks(self, plugin_system):
            plugin_system.register_hook('on_before_plan', self.hook)
        def hook(self, context):
            context.setdefault('order', []).append('{class_name}')
            return context
    """
)


def _write_plugin(dirpath, filename, class_name, priority=None):
    priority_attr = "PLUGIN_PRIORITY = {}".format(priority) if priority is not None else ""
    content = PLUGIN_TEMPLATE.format(class_name=class_name, priority_attr=priority_attr)
    with open(os.path.join(dirpath, filename), "w") as f:
        f.write(content)


def test_hooks_called_in_priority_order():
    print("*** Running test: hooks called in PLUGIN_PRIORITY order")
    with tempfile.TemporaryDirectory() as tmp:
        # Alphabetical order would be: a_plugin, b_plugin, c_plugin (all end _plugin.py).
        # Priorities chosen so alphabetical order and priority order differ —
        # proves the sort happens, not a filesystem ordering coincidence.
        _write_plugin(tmp, "a_plugin.py", "AEarlyPlugin", priority=10)
        _write_plugin(tmp, "b_plugin.py", "BLatePlugin", priority=200)
        _write_plugin(tmp, "c_plugin.py", "CMiddlePlugin", priority=None)  # defaults to 100

        base = MockBase()
        ps = PluginSystem(base)
        ps.discover_plugins(plugin_dirs=[tmp])

        context = {}
        ps.call_hooks("on_before_plan", context)

        expected = ["AEarlyPlugin", "CMiddlePlugin", "BLatePlugin"]
        assert context["order"] == expected, "Expected {}, got {}".format(expected, context["order"])
    print("  OK: hooks called in priority order (early=10, default=100, late=200)")
    return 0


def test_additive_after_setter_preserves_boost():
    """Simulates curtailment (sets) + cold_weather (additive boost) interaction.

    Curtailment must run first (lower priority) so cold weather's additive
    boost preserves the overnight GSHP floor on overflow + cold days.
    """
    print("*** Running test: additive plugin preserves setter's value")
    with tempfile.TemporaryDirectory() as tmp:
        # Setter plugin: sets best_soc_keep = 1.5 (like curtailment reduction)
        setter = textwrap.dedent(
            """
            class SetterPlugin:
                PLUGIN_PRIORITY = 10
                def __init__(self, base):
                    self.base = base
                def register_hooks(self, ps):
                    ps.register_hook('on_before_plan', self.hook)
                def hook(self, ctx):
                    ctx['best_soc_keep'] = 1.5
                    return ctx
            """
        )
        # Adder plugin: adds 1.29 to best_soc_keep (like cold weather boost)
        adder = textwrap.dedent(
            """
            class AdderPlugin:
                PLUGIN_PRIORITY = 200
                def __init__(self, base):
                    self.base = base
                def register_hooks(self, ps):
                    ps.register_hook('on_before_plan', self.hook)
                def hook(self, ctx):
                    ctx['best_soc_keep'] = ctx.get('best_soc_keep', 0) + 1.29
                    return ctx
            """
        )
        with open(os.path.join(tmp, "setter_plugin.py"), "w") as f:
            f.write(setter)
        with open(os.path.join(tmp, "adder_plugin.py"), "w") as f:
            f.write(adder)

        base = MockBase()
        ps = PluginSystem(base)
        ps.discover_plugins(plugin_dirs=[tmp])

        context = {"best_soc_keep": 3.0}  # initial value (e.g. Predbat base)
        ps.call_hooks("on_before_plan", context)

        # Expected: setter forces 1.5, adder boosts +1.29 → 2.79 (cold weather preserved)
        assert abs(context["best_soc_keep"] - 2.79) < 0.01, "Expected 2.79, got {}".format(context["best_soc_keep"])
    print("  OK: setter (1.5) + adder (+1.29) = 2.79 — cold weather floor preserved")
    return 0


if __name__ == "__main__":
    failed = 0
    failed += test_hooks_called_in_priority_order()
    failed += test_additive_after_setter_preserves_boost()
    if failed == 0:
        print("**** All plugin priority tests PASSED ****")
    else:
        print("**** {} plugin priority tests FAILED ****".format(failed))
        sys.exit(1)
