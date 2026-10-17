"""Route registration."""

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    from jobpilot.web.routes import applications, calibration, core, hub, insight, jobs, outreach, profile, resumes

    for module in (core, profile, hub, resumes, jobs, outreach, applications, insight, calibration):
        app.include_router(module.router)
