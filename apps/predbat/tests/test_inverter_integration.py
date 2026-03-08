# -----------------------------------------------------------------------------
# Unified inverter integration tests
# Generic runner — imports ALL_INVERTERS, runs mode table tests for each.
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string

from predbat import PredBat
from tests.test_infra import TestHAInterface
from tests.inverters import ALL_INVERTERS
from tests.inverters._shared import INVERTER_MODES, verify_service_calls, run_single_mode


def run_inverter_integration_tests(my_predbat_dummy):
    """Run mode table tests for all registered inverter types."""
    # Fresh PredBat instance (same setup as test_inverter.py)
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = False
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.minutes_now = 12 * 60
    my_predbat.ha_interface.history_enable = True

    ha = my_predbat.ha_interface

    time_now = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    dummy_items = {
        "number.charge_rate": 1100,
        "number.discharge_rate": 1500,
        "number.charge_rate_percent": 100,
        "number.discharge_rate_percent": 100,
        "number.charge_limit": 100,
        "select.pause_mode": "Disabled",
        "sensor.battery_capacity": 10.0,
        "sensor.battery_soc": 0.0,
        "sensor.soc_max": 10.0,
        "sensor.soc_kw": 1.0,
        "select.inverter_mode": "Eco",
        "sensor.inverter_time": time_now,
        "switch.restart": False,
        "select.idle_start_time": "00:00",
        "select.idle_end_time": "00:00",
        "sensor.battery_power": 5.0,
        "sensor.pv_power": 1.0,
        "sensor.load_power": 2.0,
        "number.reserve": 4.0,
        "switch.scheduled_charge_enable": "off",
        "switch.scheduled_discharge_enable": "off",
        "select.charge_start_time": "01:11:00",
        "select.charge_end_time": "02:22:00",
        "select.discharge_start_time": "03:33:00",
        "select.discharge_end_time": "04:44:00",
        "sensor.predbat_GE_0_scheduled_discharge_enable": "off",
        "number.discharge_target_soc": 4,
        "switch.inverter_button": False,
    }
    my_predbat.ha_interface.dummy_items = dummy_items
    my_predbat.args["auto_restart"] = [{"service": "switch/turn_on", "entity_id": "switch.restart"}]
    my_predbat.args["givtcp_rest"] = None
    my_predbat.args["inverter_type"] = ["GE"]
    my_predbat.args["schedule_write_button"] = "switch.inverter_button"
    for entity_id in dummy_items.keys():
        arg_name = entity_id.split(".")[1]
        my_predbat.args[arg_name] = entity_id

    # Create the inverter with dummy sleep to avoid real sleeps in write_and_poll retries
    from inverter import Inverter

    inv = Inverter(my_predbat, 0)
    inv.sleep = lambda seconds: None

    failed = False
    print("**** Running Inverter Integration Tests ****")

    for definition in ALL_INVERTERS:
        type_name = definition["name"]

        if definition.get("skip"):
            print("SKIP: {} mode table tests ({})".format(type_name, definition.get("skip_reason", "")))
            continue

        expectations = definition["mode_expectations"]
        transitions = definition.get("transitions", [])

        # Per-mode tests
        for mode_name in INVERTER_MODES:
            test_name = "mode_table_{}_{}".format(type_name, mode_name)
            print("**** Running Test: {} ****".format(test_name))

            ha.service_store_enable = True
            ha.service_store = []
            my_predbat.last_service_hash = {}

            run_single_mode(inv, ha, my_predbat, dummy_items, mode_name)

            expected = expectations[mode_name]
            failed |= verify_service_calls(test_name, expected, ha)

        if failed:
            ha.service_store_enable = False
            return failed

        # Transition tests
        for transition in transitions:
            test_name = "mode_table_{}_transition_{}".format(type_name, transition["name"])
            print("**** Running Test: {} ****".format(test_name))

            ha.service_store_enable = True

            for step_name in transition["steps"]:
                ha.service_store = []
                my_predbat.last_service_hash = {}
                run_single_mode(inv, ha, my_predbat, dummy_items, step_name)

            # Verify only the last step's service calls
            failed |= verify_service_calls(test_name, transition["expected"], ha)

        if failed:
            ha.service_store_enable = False
            return failed

    ha.service_store_enable = False
    return failed
