"""Seed data for the fake credit union. All people and numbers are fictional."""

from decimal import Decimal

MEMBERS = {
    "12345": {
        "name": "Jordan Ellis",
        "dob": "1984-03-14",
        "ssn_last4": "4821",
        "restricted": False,
        "accounts": [
            {"type": "Share Savings", "number": "12345-S00", "balance": Decimal("2450.75")},
            {"type": "Checking", "number": "12345-C10", "balance": Decimal("812.30")},
            {"type": "Holiday Club", "number": "12345-S05", "balance": Decimal("150.00")},
        ],
    },
    "23456": {
        "name": "Morgan Reyes",
        "dob": "1991-11-02",
        "ssn_last4": "7390",
        "restricted": False,
        "accounts": [
            {"type": "Checking", "number": "23456-C10", "balance": Decimal("3104.18")},
            {"type": "Share Savings", "number": "23456-S00", "balance": Decimal("18920.00")},
        ],
    },
    "10001": {
        "name": "Casey Nguyen",
        "dob": "1979-06-21",
        "ssn_last4": "1156",
        "restricted": True,
        "accounts": [
            {"type": "Share Savings", "number": "10001-S00", "balance": Decimal("50210.44")},
        ],
    },
}