"""``jobpilot agents …``: run the agents now, see what they did, switch the apply mode, pause."""

from __future__ import annotations

from jobpilot.cli import CliError


def _print_report(rep: dict | None, limit: int = 15) -> None:
    if not rep:
        print("  (not run yet)")
        return
    print(f"  {rep.get('at', '')} — {rep.get('summary') or ''}")
    for i in (rep.get("items") or [])[:limit]:
        who = f"{i.get('title')} — {i.get('company') or '?'}: " if i.get("title") else ""
        print(f"   · [{i.get('outcome')}] {who}{(i.get('detail') or '')[:200]}")
    for e in (rep.get("errors") or [])[:5]:
        print(f"   ! {e}")


def cmd_agents_run(args) -> int:
    from jobpilot.agents import run_cycle
    from jobpilot.cli import _ctx
    from jobpilot.services.tasks import run_task

    settings, _, factory = _ctx()
    if args.mode == "auto" and not args.yes:
        raise CliError("--mode auto sends and submits real applications. Add --yes to confirm.", code=2)
    res = run_task(settings, factory, "agents", lambda s: run_cycle(s, settings, only=args.only, mode=args.mode),
                   trigger="manual")
    print(f"{res.status}: {res.message}")
    return 0 if res.status in ("success", "skipped") else 1


def cmd_agents_status(args) -> int:
    from jobpilot.agents.base import applied_today, is_paused, last_report, policy
    from jobpilot.cli import _ctx

    _, _, factory = _ctx()
    with factory() as s:
        pol = policy(s)
        print(f"Apply mode: {pol['apply_mode']}{' (PAUSED)' if is_paused(s) else ''} · today {applied_today(s)} of "
              f"{pol['daily_limit']} · policy: score ≥ {pol['min_score']}, {', '.join(pol['allowed_eligibility'])}")
        for agent in ("manager", "search", "resume", "apply"):
            print(f"\n{agent.capitalize()} agent")
            _print_report(last_report(s, agent))
    return 0


def cmd_agents_mode(args) -> int:
    from jobpilot.agents.base import policy
    from jobpilot.cli import _ctx
    from jobpilot.services import settings_store

    _, _, factory = _ctx()
    if args.mode == "auto" and not args.yes:
        raise CliError("auto mode sends and submits real applications. Add --yes to confirm.", code=2)
    with factory() as s:
        pol = {**policy(s), "apply_mode": args.mode}
        settings_store.validate("agents", pol)
        settings_store.put(s, "agents", pol)
        s.commit()
    print(f"Apply mode is now {args.mode}.")
    return 0


def cmd_agents_pause(args) -> int:
    from jobpilot.agents.base import set_paused
    from jobpilot.cli import _ctx

    _, _, factory = _ctx()
    with factory() as s:
        set_paused(s, args.action == "pause", "cli")
        s.commit()
    print("Paused: nothing is sent or submitted (search and resumes continue)." if args.action == "pause"
          else "Resumed.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("agents", help="The Search, Resume, Apply and Manager agents")
    a = p.add_subparsers(dest="agents_cmd", required=True)
    s = a.add_parser("run", help="Run one cycle now (search → tailored resumes → apply)")
    s.add_argument("--only", choices=["search", "resume", "apply"], help="Run only this agent (and what it needs)")
    s.add_argument("--mode", choices=["dry_run", "auto"], help="Override the apply mode for this run")
    s.add_argument("--yes", action="store_true", help="Confirm --mode auto")
    s.set_defaults(func=cmd_agents_run)
    a.add_parser("status", help="What each agent did last").set_defaults(func=cmd_agents_status)
    s = a.add_parser("mode", help="Set the apply mode: dry_run (nothing sent) or auto")
    s.add_argument("mode", choices=["dry_run", "auto"])
    s.add_argument("--yes", action="store_true", help="Confirm auto")
    s.set_defaults(func=cmd_agents_mode)
    for action in ("pause", "resume"):
        a.add_parser(action, help=f"{action.capitalize()} sending and submitting (same switch as Telegram /{action})"
                     ).set_defaults(func=cmd_agents_pause, action=action)
