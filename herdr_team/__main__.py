"""herdr-team: master/sub agent teams on herdr. See README.md."""
import argparse
import os
import sys
from pathlib import Path

from . import core


def hub_from(args) -> Path | None:
    """--hub, then HERDR_TEAM_HUB, then the nearest herdr-team.toml above cwd."""
    explicit = getattr(args, "hub", None) or os.environ.get("HERDR_TEAM_HUB")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return core.find_hub(Path.cwd())


def need_hub(args) -> Path:
    hub = hub_from(args)
    if hub is not None and core.spec_error(hub):
        sys.exit(core.spec_error(hub))
    if hub is None or core.load_team(hub) is None:
        sys.exit("herdr-team.toml 을 찾지 못했습니다. 허브 폴더에서 `herdr-team edit` 을 먼저 실행하세요.")
    return hub


def run_herdr(fn) -> int:
    """Run fn, turning a herdr failure into one line instead of a traceback."""
    from . import herdr
    try:
        return fn()
    except herdr.HerdrError as e:
        print(f"herdr 오류: {e.message}", file=sys.stderr)
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
                print("herdr-team.toml 을 찾지 못했습니다. team-setup 을 먼저 실행하세요.", file=sys.stderr)
                return 1
            from . import team
            return team.up(hub)
        if name == "setup":
            if not cwd:
                print("현재 창의 작업 폴더를 알 수 없습니다.", file=sys.stderr)
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
    p.add_argument("--hub", help="프로젝트 허브 폴더 (기본: 현재 폴더에서 위로 탐색)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="자체 점검")
    sub.add_parser("board", help="현황판")
    sub.add_parser("edit", help="팀 편집 화면: 봇 추가·수정·삭제, 트리거 (처음 만들 때도 이것)")
    sub.add_parser("init", help="edit 와 같다")
    at = sub.add_parser("attach", help="봇이나 마스터의 세션으로 들어가기 (봇을 안 적으면 마스터)")
    at.add_argument("bot", nargs="?", default="")
    u = sub.add_parser("up", help="팀 띄우기 (이미 떠 있는 것은 건너뛴다)")
    u.add_argument("--no-focus", action="store_true", help="새 워크스페이스로 포커스를 옮기지 않는다")
    k = sub.add_parser("ask", help="봇에게 일 주기")
    k.add_argument("bot")
    k.add_argument("text", nargs="+")
    w = sub.add_parser("wait", help="열린 작업이 정리될 때까지 기다리기 (종료 코드 0 전부 종료 / 2 막힌 봇 있음 / 3 시간 초과)")
    w.add_argument("bots", nargs="*")
    w.add_argument("--timeout", type=float, default=1800.0, help="초 (기본 1800)")
    sub.add_parser("roster", help="팀 명단과 각 봇의 상태")
    pk = sub.add_parser("peek", help="봇이 지금 무엇을 하는지 요약해서 보기")
    pk.add_argument("bot")
    n = sub.add_parser("nudge", help="진행 중인 봇에게 방향 수정 보내기 (작업을 새로 만들지 않는다)")
    n.add_argument("bot")
    n.add_argument("text", nargs="+")
    q = sub.add_parser("question", help="다른 봇(또는 master)에게 묻고 답을 기다리기 (종료 코드 3 = 아직 답 없음)")
    q.add_argument("bot")
    q.add_argument("text", nargs="+")
    q.add_argument("--timeout", type=float, default=100.0, help="초 (기본 100)")
    sub.add_parser("watch", help="트리거를 지켜보다가 봇에게 일 주기 (계속 실행된다)")
    rs = sub.add_parser("restart", help="봇을 다시 띄워 바뀐 설정을 반영 (봇을 안 적으면 전부)")
    rs.add_argument("bots", nargs="*")
    rs.add_argument("--force", action="store_true", help="일하는 중인 봇도 다시 띄운다")
    a = sub.add_parser("action", help="herdr 플러그인 액션 (herdr 가 호출)")
    a.add_argument("name", choices=["setup", "up", "board"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "selftest":
        from . import selftest
        return selftest.run()
    if args.cmd == "board":
        from . import board
        hub = hub_from(args)
        return board.run_board(hub if hub and core.load_team(hub) else None)
    if args.cmd == "attach":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.attach(hub, args.bot))
    if args.cmd in ("init", "edit"):
        from . import team, wizard
        hub = hub_from(args) or Path.cwd().resolve()
        if core.spec_error(hub):
            # Starting from blank defaults here would overwrite roles and agent_args.
            sys.exit(core.spec_error(hub) + "\n파일을 고치거나 지운 뒤 다시 실행하세요.")
        code = wizard.run_wizard(hub)
        if code == 0 and wizard.confirm("지금 팀을 띄울까요?"):
            return run_herdr(lambda: team.up(hub))
        return code
    if args.cmd == "up":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.up(hub, focus=not args.no_focus))
    if args.cmd == "ask":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.ask(hub, args.bot, " ".join(args.text)))
    if args.cmd == "wait":
        from . import team
        hub = need_hub(args)
        return run_herdr(lambda: team.wait(hub, args.bots, args.timeout))
    if args.cmd in ("roster", "peek", "nudge", "question", "watch", "restart"):
        from . import team
        hub = need_hub(args)
        calls = {
            "roster": lambda: team.roster(hub),
            "peek": lambda: team.peek(hub, args.bot),
            "nudge": lambda: team.nudge(hub, args.bot, " ".join(args.text)),
            "question": lambda: team.question(hub, args.bot, " ".join(args.text), args.timeout),
            "watch": lambda: team.watch(hub),
            "restart": lambda: team.restart(hub, args.bots, args.force),
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
