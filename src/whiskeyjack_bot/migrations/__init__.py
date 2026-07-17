"""Ledger schema migrations, applied by :mod:`whiskeyjack_bot.ledger`.

Migrations live inside the package (not at the repository root shown in the
handoff's proposed tree) so they ship in the wheel and load via
``importlib.resources`` regardless of install layout.
"""
