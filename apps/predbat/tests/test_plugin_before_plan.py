# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Host contract tests for on_before_plan plugin hooks.

Plugins may adjust planning parameters (e.g. best_soc_keep) before
calculate_plan() runs. These tests lock the public PluginSystem API and
the call site in predbat.py so the feature does not regress silently.
"""

import inspect
import os
from unittest.mock import MagicMock

from plugin_system import PluginSystem, PredBatPlugin


def _mock_base():
    base = MagicMock()
    base.log = MagicMock()
    return base


def test_plugin_before_plan(my_predbat):
    """Run on_before_plan host contract suite. Returns 0 on success, 1 on failure."""
    print("*** Running test: Plugin on_before_plan host contract")
    failed = 0

    # --- API surface ---
    if not hasattr(PluginSystem, "call_before_plan_hooks"):
        print("ERROR: PluginSystem missing call_before_plan_hooks")
        return 1
    sig = inspect.signature(PluginSystem.register_hook)
    if "plugin" not in sig.parameters:
        print("ERROR: register_hook must accept plugin= for priority")
        return 1
    if not hasattr(PredBatPlugin, "priority"):
        print("ERROR: PredBatPlugin.priority missing")
        return 1
    print("OK: PluginSystem before-plan API present")

    # --- Priority ordering + context chaining ---
    ps = PluginSystem(_mock_base())
    order = []

    class Early(PredBatPlugin):
        priority = 10

        def on_before_plan(self, context):
            order.append("early")
            context = dict(context)
            context["best_soc_keep"] = 1.0
            return context

    class Late(PredBatPlugin):
        priority = 200

        def on_before_plan(self, context):
            order.append("late")
            context = dict(context)
            context["best_soc_keep"] = context.get("best_soc_keep", 0) + 0.5
            return context

    early = Early(ps.base)
    late = Late(ps.base)
    ps.register_hook("on_before_plan", late.on_before_plan, plugin=late)
    ps.register_hook("on_before_plan", early.on_before_plan, plugin=early)
    out = ps.call_before_plan_hooks({"best_soc_keep": 6.0, "best_soc_keep_weight": 0.5})
    if order != ["early", "late"]:
        print("ERROR: priority order wrong: {}".format(order))
        failed = 1
    elif out.get("best_soc_keep") != 1.5:
        print("ERROR: context chaining failed: {}".format(out))
        failed = 1
    else:
        print("OK: priority ordering and context chaining")

    # --- Error isolation + non-dict return ignored ---
    ps2 = PluginSystem(_mock_base())

    def boom(context):
        raise RuntimeError("plugin error")

    def bad_return(context):
        return "not-a-dict"

    def good(context):
        context = dict(context)
        context["best_soc_keep"] = 2.0
        return context

    ps2.register_hook("on_before_plan", boom)
    ps2.register_hook("on_before_plan", bad_return)
    ps2.register_hook("on_before_plan", good)
    out2 = ps2.call_before_plan_hooks({"best_soc_keep": 9.0})
    if out2.get("best_soc_keep") != 2.0:
        print("ERROR: error isolation / bad return handling failed: {}".format(out2))
        failed = 1
    else:
        print("OK: error isolation and non-dict returns ignored")

    # --- Call site still present in predbat.py (before calculate_plan) ---
    predbat_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "predbat.py")
    with open(predbat_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    hook_idx = src.find("call_before_plan_hooks")
    calc_idx = src.find("calculate_plan", hook_idx) if hook_idx != -1 else -1
    if hook_idx == -1 or calc_idx == -1 or calc_idx <= hook_idx:
        print("ERROR: predbat.py must invoke call_before_plan_hooks before calculate_plan")
        failed = 1
    else:
        print("OK: predbat.py call site present before calculate_plan")

    if not failed:
        print("*** Plugin on_before_plan host contract: PASSED")
    return failed
