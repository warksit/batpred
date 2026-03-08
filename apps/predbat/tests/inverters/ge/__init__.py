# -----------------------------------------------------------------------------
# GE inverter test definition
# -----------------------------------------------------------------------------

DEFINITION = {
    "name": "GE",
    "skip": False,
    "mode_expectations": {
        "active_charge": [
            ("discharge_stop", {"device_id": "DID0"}),
            ("charge_start", {"device_id": "DID0", "target_soc": 50}),
        ],
        "demand_charge": [
            ("charge_stop", {"device_id": "DID0"}),
        ],
        "freeze_charge": [
            ("discharge_stop", {"device_id": "DID0"}),
            ("charge_freeze", {"device_id": "DID0", "target_soc": 50}),
        ],
        "hold_charge": [
            ("discharge_stop", {"device_id": "DID0"}),
            ("charge_freeze", {"device_id": "DID0", "target_soc": 30}),
        ],
        "active_export": [
            ("charge_stop", {"device_id": "DID0"}),
            ("discharge_start", {"device_id": "DID0", "target_soc": 50}),
        ],
        "demand_export": [
            ("discharge_stop", {"device_id": "DID0"}),
        ],
        "freeze_export": [
            ("charge_stop", {"device_id": "DID0"}),
            ("discharge_freeze", {"device_id": "DID0", "target_soc": 50}),
        ],
    },
    "transitions": [
        {
            "name": "freeze_charge_to_demand",
            "steps": ["freeze_charge", "demand_charge"],
            "expected": [
                ("charge_stop", {"device_id": "DID0"}),
            ],
        },
        {
            "name": "active_charge_to_demand",
            "steps": ["active_charge", "demand_charge"],
            "expected": [
                ("charge_stop", {"device_id": "DID0"}),
            ],
        },
        {
            "name": "freeze_export_to_demand",
            "steps": ["freeze_export", "demand_export"],
            "expected": [
                ("discharge_stop", {"device_id": "DID0"}),
            ],
        },
        {
            "name": "charge_to_export",
            "steps": ["active_charge", "active_export"],
            "expected": [
                ("charge_stop", {"device_id": "DID0"}),
                ("discharge_start", {"device_id": "DID0", "target_soc": 50}),
            ],
        },
        {
            "name": "export_to_charge",
            "steps": ["active_export", "active_charge"],
            "expected": [
                ("discharge_stop", {"device_id": "DID0"}),
                ("charge_start", {"device_id": "DID0", "target_soc": 50}),
            ],
        },
    ],
}
