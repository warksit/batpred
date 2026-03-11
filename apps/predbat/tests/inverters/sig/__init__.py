# -----------------------------------------------------------------------------
# SIG inverter test definition
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string

# SIG EMS mode constants
MSC = "Maximum Self Consumption"
CHARGE_GRID_FIRST = "Command Charging (Grid First)"
DISCHARGE_ESS_FIRST = "Command Discharging (ESS First)"
STANDBY = "Standby"

# Default test values
MAX_CHARGE_RATE = 2600  # battery_rate_max_charge * MINUTE_WATT (from battery_rate_max_raw default)
MAX_DISCHARGE_RATE = 2600  # battery_rate_max_discharge * MINUTE_WATT
RESERVE = 4  # matches inv.reserve_percent default

# Extra entities SIG needs beyond the shared set
EXTRA_ENTITIES = {
    "number.sig_discharge_cut_off_soc": 0,
}

# Extra args SIG needs
EXTRA_ARGS = {
    "sig_discharge_cut_off_soc": "number.sig_discharge_cut_off_soc",
}


DEFINITION = {
    "name": "SIG",
    "skip": False,
    "inverter_type": "SIG",
    "extra_entities": EXTRA_ENTITIES,
    "extra_args": EXTRA_ARGS,
    "mode_expectations": {
        # demand_charge: MSC, rates=max, floor=reserve
        "demand_charge": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": MSC}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
            ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
        ],
        # active_charge: Mode 3 (Grid First), charge_rate=optimizer, floor=reserve
        # Setup sets charge_limit=50 (target), so charge_rate should be current rate
        "active_charge": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": CHARGE_GRID_FIRST}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
            ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
        ],
        # freeze_charge: MSC, rates=max, floor=reserve (same as demand on SIG)
        "freeze_charge": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": MSC}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
            ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
        ],
        # hold_charge: MSC, rates=max, floor=reserve
        # (charge_limit=soc% set by adjust_battery_target in setup, not by immediate call)
        "hold_charge": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": MSC}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
            ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
        ],
        # active_export: Mode 7 (ESS First), discharge_rate=safe, charge_rate=0, floor=reserve
        # Safe rate calculation depends on PV power and export limit
        "active_export": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": DISCHARGE_ESS_FIRST}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ("number/set_value", {"entity_id": "number.charge_rate", "value": 0}),
            ("number/set_value", {"entity_id": "number.discharge_rate"}),  # safe rate — value checked separately
        ],
        # demand_export: MSC, rates=max, floor=reserve (same as demand_charge)
        "demand_export": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": MSC}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
            ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
        ],
        # freeze_export: Standby (battery completely idle)
        "freeze_export": [
            ("select/select_option", {"entity_id": "select.inverter_mode", "option": STANDBY}),
            ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
        ],
    },
    "transitions": [
        {
            "name": "freeze_charge_to_demand",
            "steps": ["freeze_charge", "demand_charge"],
            "expected": [
                ("select/select_option", {"entity_id": "select.inverter_mode", "option": MSC}),
                ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
                ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
                ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
            ],
        },
        {
            "name": "active_charge_to_demand",
            "steps": ["active_charge", "demand_charge"],
            "expected": [
                ("select/select_option", {"entity_id": "select.inverter_mode", "option": MSC}),
                ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
                ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
                ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
            ],
        },
        {
            "name": "charge_to_export",
            "steps": ["active_charge", "active_export"],
            "expected": [
                ("select/select_option", {"entity_id": "select.inverter_mode", "option": DISCHARGE_ESS_FIRST}),
                ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
                ("number/set_value", {"entity_id": "number.charge_rate", "value": 0}),
                ("number/set_value", {"entity_id": "number.discharge_rate"}),
            ],
        },
        {
            "name": "export_to_charge",
            "steps": ["active_export", "active_charge"],
            "expected": [
                ("select/select_option", {"entity_id": "select.inverter_mode", "option": CHARGE_GRID_FIRST}),
                ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
                ("number/set_value", {"entity_id": "number.charge_rate", "value": MAX_CHARGE_RATE}),
                ("number/set_value", {"entity_id": "number.discharge_rate", "value": MAX_DISCHARGE_RATE}),
            ],
        },
        {
            "name": "export_to_standby",
            "steps": ["active_export", "freeze_export"],
            "expected": [
                ("select/select_option", {"entity_id": "select.inverter_mode", "option": STANDBY}),
                ("number/set_value", {"entity_id": "number.sig_discharge_cut_off_soc", "value": RESERVE}),
            ],
        },
    ],
}
