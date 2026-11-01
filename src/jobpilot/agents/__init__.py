"""The four agents: Search, Resume, Apply and the Manager that runs them. See docs/AGENTS.md."""

from jobpilot.agents.manager import run_cycle, select_jobs

AGENTS = ("search", "resume", "apply", "manager")

__all__ = ["AGENTS", "run_cycle", "select_jobs"]
