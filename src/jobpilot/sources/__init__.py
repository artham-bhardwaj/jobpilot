"""Job-source adapters. Each adapter covers one well-defined, permitted source."""

from jobpilot.sources.ats import AshbyAdapter, GreenhouseAdapter, LeverAdapter
from jobpilot.sources.pages import JsonLdAdapter

ADAPTERS = {a.kind: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), JsonLdAdapter())}

__all__ = ["ADAPTERS"]
