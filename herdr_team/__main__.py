"""herdr-team: master/sub agent teams on herdr. See README.md."""
import argparse
import os
import sys
from pathlib import Path

from . import core


def hub_from(args) -> Path | None:
    """HERDR_TEAM_HUB (set by the plugin's panes), then the nearest herdr-team.toml above cwd."""
    explicit = os.environ.get("HERDR_TEAM_HUB")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return core.find_hub(Path.cwd())


def need_hub(args) -> Path:
    hub = hub_from(args)
    if hub is not None and core.spec_error(hub):
        sys.exit(core.spec_error(hub))
    if hub is None or core.load_team(hub) is None:
        sys.exit("No herdr-team.toml found. Create a team first with the team-setup action (suggested key prefix+shift+s) in the hub folder.")
    # Agent names are matched in any case: `ask acmeweb` reaches AcmeWeb.
    team = core.load_team(hub)
    if getattr(args, "bot", None):
        args.bot = core.canon_bot(team, args.bot)
    if getattr(args, "bots", None):
        args.bots = [core.canon_bot(team, b) for b in args.bots]
    return hub


def run_herdr(fn) -> int:
    """Run fn, turning a herdr failure into one line instead of a traceback."""
    from . import herdr
    try:
        return fn()
    except herdr.HerdrError as e:
        print(f"herdr error: {e.message}", file=sys.stderr)
        return 1


def action(name: str) -> int:
    """Entry point for manifest actions. herdr runs these with the plugin dir as cwd."""
    import json
    from . import board, herdr
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    cwd = ctx.get("focused_pane_cwd") or ctx.get("workspace_cwd")
    pane = ctx.get("focused_pane_id")
    hub = core.find_hub(Path(cwd)) if cwd else None
    try:
        if name == "board":
            board.open_pane(pane, hub)
            return 0
        if name == "up":
            if hub is None:
                print("No herdr-team.toml found. Create a team first with the team-setup action (suggested key prefix+shift+s).", file=sys.stderr)
                return 1
            from . import team
            return team.up(hub)
        if name == "setup":
            if not cwd:
                print("Can't tell the current pane's working folder.", file=sys.stderr)
                return 1
            herdr.run("plugin", "pane", "open", "--plugin", "herdr-team", "--entrypoint", "setup",
                      "--env", f"HERDR_TEAM_HUB={hub or cwd}")
            return 0
    except herdr.HerdrError as e:
        print(e.message, file=sys.stderr)
        return 1
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="herdr-team")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")
    sub.add_parser("setup", help="Team setup: create the team, add, change and remove agents and triggers")
    sub.add_parser("up", help="Start the team (skips what is already running)")
    sub.add_parser("board", help="Status board")
    rs = sub.add_parser("restart", help="Restart agents to apply changed settings (all if none given; busy agents are skipped)")
    rs.add_argument("bots", nargs="*", metavar="agent")
    k = sub.add_parser("ask", help="Give an agent a task")
    k.add_argument("bot", metavar="agent")
    k.add_argument("text", nargs="+")
    w = sub.add_parser("wait", help="Wait up to 30 min for open tasks (exit 0 all done / 2 an agent is blocked / 3 timed out)")
    w.add_argument("bots", nargs="*", metavar="agent")
    sub.add_parser("roster", help="Team members and each one's state")
    pk = sub.add_parser("peek", help="Summary of what an agent is doing now")
    pk.add_argument("bot", metavar="agent")
    n = sub.add_parser("nudge", help="Send a course correction to a working agent (no new task)")
    n.add_argument("bot", metavar="agent")
    n.add_argument("text", nargs="+")
    q = sub.add_parser("question", help="Ask another agent (or the master) and wait up to 100 s for the answer (exit 3 = no answer yet)")
    q.add_argument("bot", metavar="agent")
    q.add_argument("text", nargs="+")
    # Hidden: run by `up` (watch), by the plugin manifest (action), and by developers (selftest).
    sub.add_parser("watch")
    sub.add_parser("selftest")
    a = sub.add_parser("action")
    a.add_argument("name", choices=["setup", "up", "board"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Any command moves the old .herdr-team/bots to agents/ once, before a screen opens, so the
    # notice is seen. Agents started before the move still point at the old folder.
    hub = hub_from(args) if args.cmd != "selftest" else None
    if hub and core.migrate_homes(hub):
        print("Moved .herdr-team/bots to .herdr-team/agents. Running agents still use the old paths: restart them "
              "to pick up the new paths (herdr-team restart; for the master, /exit and then team-up).", file=sys.stderr)

    if args.cmd == "selftest":
        from . import selftest
        return selftest.run()
    if args.cmd == "board":
        from . import board
        hub = hub_from(args)
        return board.run_board(hub if hub and core.load_team(hub) else None)
    if args.cmd == "setup":
        from . import wizard
        hub = hub_from(args) or Path.cwd().resolve()
        if core.spec_error(hub):
            # Starting from blank defaults here would overwrite roles and agent_args.
            sys.exit(core.spec_error(hub) + "\nFix or delete the file, then run this again.")
        return wizard.run_wizard(hub)
    if args.cmd == "up":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.up(hub))
    if args.cmd == "ask":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.ask(hub, args.bot, " ".join(args.text)))
    if args.cmd == "wait":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.wait(hub, args.bots))
    if args.cmd in ("roster", "peek", "nudge", "question", "watch", "restart"):
        from . import team
        hub = need_hub(args)
        calls = {
            "roster": lambda: team.roster(hub),
            "peek": lambda: team.peek(hub, args.bot),
            "nudge": lambda: team.nudge(hub, args.bot, " ".join(args.text)),
            "question": lambda: team.question(hub, args.bot, " ".join(args.text)),
            "watch": lambda: team.watch(hub),
            "restart": lambda: team.restart(hub, args.bots),
        }
        try:
            return run_herdr(calls[args.cmd])
        except KeyboardInterrupt:
            return 130
    if args.cmd == "action":
        return action(args.name)
    return 2


if __name__ == "__main__":
    sys.exit(main())
