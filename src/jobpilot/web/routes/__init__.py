"""Route registration."""

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    from jobpilot.web.routes import (agents, applications, calibration, core, desk, hub, insight, jobs, outreach, profile,
                                     resumes, tracker)

    for module in (core, agents, profile, hub, resumes, jobs, desk, outreach, applications, insight, calibration, tracker):
        app.include_router(module.router)
