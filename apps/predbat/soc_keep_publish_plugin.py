# -----------------------------------------------------------------------------
# SOC Keep Publish Plugin for Predbat
# Observe-only: publishes the effective best_soc_keep — the value the planner
# actually uses after every on_before_plan plugin has chained the context.
#
# Stock Predbat exposes only input_number.predbat_best_soc_keep (what the user
# configured). When plugins adjust best_soc_keep there is no entity showing the
# resulting value, and neither adjusting plugin can publish it: each sees only
# its own delta, not the end of the chain. This plugin runs last and publishes
# what the planner receives.
#
# Consumed by the /soc-keep-review skill.
# -----------------------------------------------------------------------------

from plugin_system import PredBatPlugin

# Entity published. Distinct from input_number.predbat_best_soc_keep (the user
# setting) — this is the post-plugin effective value.
SENSOR_SUFFIX = "best_soc_keep"

DEFAULT_KEEP_WEIGHT = 0.5  # Predbat's own default, used only if the key is absent


class SocKeepPublishPlugin(PredBatPlugin):
    """
    Publish the effective best_soc_keep after all on_before_plan plugins run.

    Makes no planning decisions and never modifies the context — it returns the
    context unchanged so it is safe at any position in the chain.
    """

    # Run last: must observe the fully-chained value, after curtailment (10)
    # sets best_soc_keep and cold weather (200) additively boosts it. Any future
    # adjusting plugin should sit below this.
    priority = 999

    def register_hooks(self, plugin_system):
        plugin_system.register_hook("on_before_plan", self.on_before_plan, plugin=self)

    def on_before_plan(self, context):
        """Publish the chained keep value. Returns context unmodified."""
        keep = context.get("best_soc_keep")
        if keep is None:
            # Host contract changed — publish nothing rather than a misleading zero
            self.base.log("Warn: soc_keep_publish - no best_soc_keep in context, skipping publish")
            return context

        weight = context.get("best_soc_keep_weight", DEFAULT_KEEP_WEIGHT)
        self.base.dashboard_item(
            "sensor.{}_{}".format(self.base.prefix, SENSOR_SUFFIX),
            round(keep, 2),
            {
                "friendly_name": "Best SoC Keep (effective)",
                "unit_of_measurement": "kWh",
                "icon": "mdi:battery-lock",
                "weight": round(weight, 2),
            },
        )
        return context
