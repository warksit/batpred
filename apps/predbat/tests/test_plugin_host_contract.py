# -----------------------------------------------------------------------------
# CM host-dependency contract for the Predbat plugin system
#
# Curtailment Manager (and similar local plugins) rely on:
#   - on_before_plan hook registration with priority ordering
#   - call_before_plan_hooks(context) chaining before calculate_plan
#   - Predbat update path invoking that hook runner
#
# This is intentionally a *consumer* smoke test (lives with CM), not a full
# upstream plugin_system suite. It must FAIL if we point CM at a Predbat
# build that lacks the host API.
#
# Run: cd apps/predbat && python3 tests/test_plugin_host_contract.py
# -----------------------------------------------------------------------------

import inspect
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin_system import PluginSystem, PredBatPlugin


def _mock_base():
    base = MagicMock()
    base.log = MagicMock()
    return base


def test_host_exposes_before_plan_api():
    """CM requires call_before_plan_hooks and priority-aware register_hook."""
    assert hasattr(PluginSystem, "call_before_plan_hooks"), "PluginSystem missing call_before_plan_hooks — CM cannot relax best_soc_keep"
    sig = inspect.signature(PluginSystem.register_hook)
    assert "plugin" in sig.parameters, "register_hook must accept plugin= for priority ordering"
    assert hasattr(PredBatPlugin, "priority"), "PredBatPlugin.priority required for hook ordering"
    print("  test_host_exposes_before_plan_api: PASSED")
    return 0


def test_before_plan_priority_and_chaining():
    """Lower priority runs first; later hooks see earlier mutations of the context."""
    ps = PluginSystem(_mock_base())
    order = []

    class Early(PredBatPlugin):
        priority = 10

        def on_before_plan(self, context):
            order.append("early")
            context = dict(context)
            context["best_soc_keep"] = 1.0
            context["touched_by"] = ["early"]
            return context

    class Late(PredBatPlugin):
        priority = 200

        def on_before_plan(self, context):
            order.append("late")
            context = dict(context)
            context["best_soc_keep"] = context.get("best_soc_keep", 0) + 0.5
            context.setdefault("touched_by", []).append("late")
            return context

    early, late = Early(ps.base), Late(ps.base)
    # Register late first — priority, not registration order, must win
    ps.register_hook("on_before_plan", late.on_before_plan, plugin=late)
    ps.register_hook("on_before_plan", early.on_before_plan, plugin=early)

    out = ps.call_before_plan_hooks({"best_soc_keep": 6.0, "best_soc_keep_weight": 0.5})
    assert order == ["early", "late"], f"priority order wrong: {order}"
    assert out["best_soc_keep"] == 1.5, f"chaining failed: {out}"
    assert out["touched_by"] == ["early", "late"], out
    print("  test_before_plan_priority_and_chaining: PASSED")
    return 0


def test_before_plan_ignores_bad_return_and_isolates_errors():
    """Non-dict returns are ignored; one raising callback must not kill the chain."""
    ps = PluginSystem(_mock_base())

    def boom(context):
        raise RuntimeError("plugin blew up")

    def bad_return(context):
        return "not-a-dict"

    def good(context):
        context = dict(context)
        context["best_soc_keep"] = 2.0
        return context

    ps.register_hook("on_before_plan", boom)
    ps.register_hook("on_before_plan", bad_return)
    ps.register_hook("on_before_plan", good)

    out = ps.call_before_plan_hooks({"best_soc_keep": 9.0})
    assert out["best_soc_keep"] == 2.0, f"good callback should still run: {out}"
    print("  test_before_plan_ignores_bad_return_and_isolates_errors: PASSED")
    return 0


def test_predbat_update_path_calls_before_plan_hooks():
    """Source contract: update path must invoke call_before_plan_hooks before calculate_plan.

    Importing full PredBat.initialize is heavy; we assert the call site exists in
    predbat.py so a rebase that drops the wiring fails this consumer test.
    """
    predbat_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "predbat.py")
    with open(predbat_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "call_before_plan_hooks" in src, "predbat.py must call plugin_system.call_before_plan_hooks"
    # Ordering: hook runner appears before calculate_plan use in the plan block
    hook_idx = src.find("call_before_plan_hooks")
    # First calculate_plan after the hook site should be the plan computation
    calc_after = src.find("calculate_plan", hook_idx)
    assert hook_idx != -1 and calc_after != -1 and calc_after > hook_idx
    print("  test_predbat_update_path_calls_before_plan_hooks: PASSED")
    return 0


def run_plugin_host_contract_tests(_my_predbat=None):
    """Entry compatible with unit_test registry (ignores my_predbat)."""
    failed = 0
    print("*** Plugin host contract (CM dependency) ***")
    for fn in (
        test_host_exposes_before_plan_api,
        test_before_plan_priority_and_chaining,
        test_before_plan_ignores_bad_return_and_isolates_errors,
        test_predbat_update_path_calls_before_plan_hooks,
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
        print("**** Plugin host contract: all PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_plugin_host_contract_tests() else 0)
