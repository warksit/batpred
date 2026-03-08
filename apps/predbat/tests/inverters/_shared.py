# -----------------------------------------------------------------------------
# Shared mode table test infrastructure
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string

# Shared mode definitions — universal for all inverter types.
# Each mode describes:
#   soc_percent  - the inverter's current SoC before the action
#   setup        - calls execute.py makes before the immediate call
#   action       - the adjust_*_immediate call
INVERTER_MODES = {
    "active_charge": {
        "soc_percent": 30,
        "setup": [
            ("adjust_battery_target", {"soc": 50, "isCharging": True}),
        ],
        "action": ("adjust_charge_immediate", {"target_soc": 50, "freeze": False}),
    },
    "demand_charge": {
        "soc_percent": 30,
        "setup": [
            ("adjust_battery_target", {"soc": 100, "isCharging": False}),
        ],
        "action": ("adjust_charge_immediate", {"target_soc": 0, "freeze": False}),
    },
    "freeze_charge": {
        "soc_percent": 50,
        "setup": [
            ("adjust_battery_target", {"soc": 100, "isCharging": True}),
            ("adjust_reserve", {"reserve": 50}),
        ],
        "action": ("adjust_charge_immediate", {"target_soc": 50, "freeze": True}),
    },
    "hold_charge": {
        "soc_percent": 30,
        "setup": [
            ("adjust_battery_target", {"soc": 100, "isCharging": True}),
            ("adjust_reserve", {"reserve": 30}),
        ],
        "action": ("adjust_charge_immediate", {"target_soc": 30, "freeze": False}),
    },
    "active_export": {
        "soc_percent": 80,
        "setup": [
            ("adjust_battery_target", {"soc": 100, "isExporting": True}),
        ],
        "action": ("adjust_export_immediate", {"target_soc": 50, "freeze": False}),
    },
    "demand_export": {
        "soc_percent": 80,
        "setup": [
            ("adjust_battery_target", {"soc": 100, "isExporting": False}),
        ],
        "action": ("adjust_export_immediate", {"target_soc": 100, "freeze": False}),
    },
    "freeze_export": {
        "soc_percent": 80,
        "setup": [
            ("adjust_battery_target", {"soc": 80, "isExporting": True}),
        ],
        "action": ("adjust_export_immediate", {"target_soc": 50, "freeze": True}),
    },
}


def verify_service_calls(test_name, expected_calls, ha):
    """Verify that service calls match expected sequence (name + key params)."""
    failed = False
    actual_calls = ha.get_service_store()

    if len(actual_calls) != len(expected_calls):
        print("ERROR: {} expected {} service calls, got {} - expected {} got {}".format(test_name, len(expected_calls), len(actual_calls), expected_calls, actual_calls))
        return True

    for i, (exp_service, exp_params) in enumerate(expected_calls):
        actual_service, actual_params = actual_calls[i]
        if actual_service != exp_service:
            print("ERROR: {} call {} service should be '{}' got '{}'".format(test_name, i, exp_service, actual_service))
            failed = True
        for key, val in exp_params.items():
            if key not in actual_params or actual_params[key] != val:
                print("ERROR: {} call {} param '{}' should be {} got {}".format(test_name, i, key, val, actual_params.get(key, "<missing>")))
                failed = True

    return failed


def run_single_mode(inv, ha, my_predbat, dummy_items, mode_name, reset_entities=True):
    """Execute a single mode on the inverter: setup calls then immediate action."""
    mode_def = INVERTER_MODES[mode_name]

    # Ensure non-REST mode (previous tests may have set rest_data)
    inv.rest_data = None
    inv.rest_api = None

    # Ensure service args are set (previous tests may have cleared them)
    my_predbat.args["charge_start_service"] = "charge_start"
    my_predbat.args["charge_stop_service"] = "charge_stop"
    my_predbat.args["charge_freeze_service"] = "charge_freeze"
    my_predbat.args["discharge_start_service"] = "discharge_start"
    my_predbat.args["discharge_stop_service"] = "discharge_stop"
    my_predbat.args["discharge_freeze_service"] = "discharge_freeze"
    my_predbat.args["device_id"] = "DID0"

    # Set inverter SoC for this mode
    inv.soc_percent = mode_def["soc_percent"]
    inv.reserve_percent = 4
    inv.reserve_max = 100

    if reset_entities:
        # Reset entities to avoid no-change skip in adjust_battery_target/adjust_reserve
        dummy_items["number.charge_limit"] = 0
        dummy_items["number.reserve"] = 0

    # Run setup calls (what execute.py does before the immediate call)
    for method_name, kwargs in mode_def["setup"]:
        getattr(inv, method_name)(**kwargs)

    # Clear service store — we only verify the immediate call's services
    ha.service_store = []
    my_predbat.last_service_hash = {}

    # Run the immediate action
    action_method, action_kwargs = mode_def["action"]
    getattr(inv, action_method)(**action_kwargs)
