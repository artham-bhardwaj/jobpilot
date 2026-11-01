"""Job-source adapters. Each adapter covers one well-defined, permitted source."""

from jobpilot.sources.ats import AshbyAdapter, GreenhouseAdapter, LeverAdapter
from jobpilot.sources.feeds import RemoteOKAdapter, WeWorkRemotelyAdapter, WorkingNomadsAdapter
from jobpilot.sources.careers import (AmazonJobsAdapter, AppleJobsAdapter, AtlassianCareersAdapter, GoogleCareersAdapter,
                                     MicrosoftCareersAdapter)
from jobpilot.sources.pages import JsonLdAdapter, SitemapAdapter
from jobpilot.sources.remote import HimalayasAdapter, JobicyAdapter
from jobpilot.sources.workday import WorkdayAdapter
from jobpilot.sources.yc import YCJobsAdapter

ADAPTERS = {a.kind: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), JsonLdAdapter(), SitemapAdapter(),
                                    AmazonJobsAdapter(), MicrosoftCareersAdapter(), GoogleCareersAdapter(),
                                    AtlassianCareersAdapter(), WorkdayAdapter(), AppleJobsAdapter(),
                                    HimalayasAdapter(), JobicyAdapter(), RemoteOKAdapter(),
                                    WorkingNomadsAdapter(), WeWorkRemotelyAdapter(), YCJobsAdapter())}

__all__ = ["ADAPTERS"]
