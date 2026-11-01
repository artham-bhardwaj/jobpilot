"""Fintech jobs: companies in payments, broking, banking, exchanges and crypto, where capital-markets experience counts.

A job is fintech when its company name contains one of ``FINTECH`` as whole words (case-insensitive), so
"JPMorgan Chase & Co." and "Deutsche Bank AG" match, but "Citizens Advice" does not match "citi".
Used by the Fintech jobs page (/fintech) and ``jobs.search(sector="fintech")``.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, func, literal, or_

FINTECH: tuple[str, ...] = (
    # Indian fintech, payments and broking
    "phonepe", "paytm", "razorpay", "cred", "groww", "zerodha", "upstox", "angel one", "dhan", "smallcase", "cashfree",
    "pine labs", "juspay", "bharatpe", "mobikwik", "zeta", "fampay", "policybazaar", "pinelabs", "m2p", "perfios",
    "navi technologies", "jupiter money", "paysense", "lendingkart", "indmoney", "5paisa", "motilal oswal",
    "icici securities", "hdfc securities", "kotak securities", "nse", "bse", "ncdex", "mcx", "nsdl", "cdsl",
    # Global payments and fintech
    "stripe", "paypal", "visa", "mastercard", "american express", "amex", "adyen", "revolut", "plaid", "brex",
    "klarna", "chime", "robinhood", "fis", "fiserv", "broadridge", "worldline",
    # Banks and investment firms with India tech centres
    "goldman sachs", "morgan stanley", "jpmorgan", "jp morgan", "jpmorgan chase", "citi", "citigroup", "citibank",
    "deutsche bank", "barclays", "hsbc", "ubs", "bny", "bny mellon", "state street", "northern trust", "wells fargo",
    "standard chartered", "bank of america", "blackrock", "fidelity", "natwest", "societe generale", "bnp paribas",
    "credit suisse", "nomura", "macquarie", "lloyds", "td bank", "capital one", "synchrony", "discover financial",
    # Exchanges, market data and trading
    "nasdaq", "lseg", "london stock exchange", "intercontinental exchange", "cme group", "bloomberg",
    "tower research", "graviton", "optiver", "jane street", "citadel", "de shaw", "two sigma", "squarepoint",
    # Crypto exchanges
    "coinbase", "binance", "bybit", "okx", "coindcx", "coinswitch", "wazirx", "kraken",
)

_PUNCT = ",.()&/-'"


def _normalise(name: str) -> str:
    return " " + re.sub(r"\s+", " ", "".join(" " if ch in _PUNCT else ch for ch in name.lower())).strip() + " "


def is_fintech(company: str | None) -> bool:
    """Whether ``company`` is one of the fintech companies (whole-word match)."""
    if not company:
        return False
    norm = _normalise(company)
    return any(f" {key} " in norm for key in FINTECH)


def fintech_clause(column) -> ColumnElement[bool]:
    """SQL equivalent of ``is_fintech`` for a company-name column."""
    expr = func.lower(column)
    for ch in _PUNCT:
        expr = func.replace(expr, ch, " ")
    padded = literal(" ").concat(expr).concat(" ")
    return or_(*(padded.like(f"% {key} %") for key in FINTECH))
