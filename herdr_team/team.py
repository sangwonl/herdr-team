"""Team operations against a live herdr server: up, ask, wait."""
import os
import shlex
import subprocess
import time
from pathlib import Path

from . import board, herdr
from .core import (parse_every, trigger_fire,
                   load_questions, next_question_id, question_blocked, roster_lines, save_questions,
                   transcript_digest, transcript_path,
                   Bot, Team, agent_name, canon_bot, collaborator_links, collaborators_block, linked_teams, bot_cwd, instructions_block, bot_home, ensure_home, render_preamble, append_log, build_prompt, classify, load_tasks, load_team,
                   locked, next_task_id, read_log, resolve_repo, save_tasks, state_dir, update_tasks,
                   wait_done)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
NAME_KEY = "name"  # field of an agent record that carries its herdr agent name


def _edit(path: Path | str) -> str:
    # `Edit(//abs/path)` is Claude Code's absolute-path rule; it covers the Write tool too.
    return f"Edit(/{path})"


def _read_others(team: Team) -> list[str]:
    """Read rules for linked teams' context.md (agents read it before work; no prompt for that)."""
    return [f"Read(/{t.hub / '.herdr-team' / 'context.md'})" for t in linked_teams(team).values()]


def master_prompt(hub: Path) -> str:
    """master-prompt.md plus the team-wide user instructions and linked teams."""
    base = (PLUGIN_ROOT / "master-prompt.md").read_text(encoding="utf-8").rstrip("\n")
    team = load_team(hub)
    return "\n".join([base, "", *instructions_block(hub), *(collaborators_block(team) if team else [])])


def master_args(team: Team) -> list[str]:
    return ["--append-system-prompt-file", str(team.hub / ".herdr-team" / "MASTER.md"),
            "--allowedTools", "Bash(herdr-team *)", _edit(team.hub / ".herdr-team" / "context.md"), *_read_others(team)]


def bot_args(team: Team, bot: Bot) -> list[str]:
    sd = team.hub / ".herdr-team"
    extra = [str(resolve_repo(team.hub, d)) for d in bot.dirs[1:]]
    return ["--append-system-prompt-file", str(bot_home(team.hub, bot.name) / "PREAMBLE.md"),
            "--add-dir", str(sd), *extra,
            # Only the read-and-ask commands: `watch` and `up` run commands taken from a spec file,
            # so a blanket `herdr-team *` would let a bot run anything without a prompt.
            "--allowedTools", "Bash(herdr-team question *)", "Bash(herdr-team roster)", "Bash(herdr-team peek *)",
            _edit(f"{bot_home(team.hub, bot.name)}/**"),
            _edit(f"{sd}/reports/**"), _edit(f"{sd}/answers/**"), *_read_others(team),
            *team.agent_args]


def live_agents() -> dict[str, dict]:
    return {a[NAME_KEY]: a for a in herdr.agents() if a.get(NAME_KEY)}


def _start(name: str, pane: str, args: list[str]) -> str:
    """Start claude in pane. Returns the outcome text for the report line."""
    deadline = time.monotonic() + 15
    while True:
        try:
            herdr.run("agent", "start", name, "--kind", "claude", "--pane", pane,
                      "--timeout", "60000", "--", *args, timeout=90)
            return "started"
        except herdr.HerdrError as e:
            if e.code == "agent_not_ready":
                return "started (waiting for input — check that pane)"
            # A pane created a moment ago has no shell prompt yet; give it a few seconds.
            if "not an available shell" in e.message and time.monotonic() < deadline:
                time.sleep(0.5)
                continue
            raise


def _add_bin_to_path(pane: str) -> None:
    """Prepend the plugin's bin/ to PATH in a pane `up` did not create (so no --env reached its shell)."""
    # ponytail: POSIX shell syntax (sh, bash, zsh); fish users get `herdr-team` only in tabs up creates.
    herdr.run("pane", "run", pane, f"export PATH={shlex.quote(str(PLUGIN_ROOT / 'bin'))}:\"$PATH\"")


def _busy(pane: str) -> str:
    """What holds the pane's foreground instead of its shell: "" when the shell is at its prompt,
    "self" when it is this very command (run from that pane), else the program's name.
    `agent start` and `pane run` need the shell; typed text would land in that program's input."""
    info = herdr.run("pane", "process-info", "--pane", pane).get("process_info", {})
    if info.get("foreground_process_group_id") == info.get("shell_pid"):
        return ""
    if os.environ.get("HERDR_PANE_ID") == pane:
        return "self"
    procs = info.get("foreground_processes") or [{}]
    return procs[0].get("argv0") or procs[0].get("name") or "another program"


def _start_when_idle(hub: Path, name: str, pane: str, args: list[str]) -> None:
    """`up` was run in the pane the agent should take. Fork a child that waits for this command to
    exit and the shell prompt to return, then starts the agent there. The outcome goes to the log."""
    if os.fork():
        return
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    outcome = "failed: the shell did not come back within 2 minutes"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not _busy(pane):
                _add_bin_to_path(pane)
                outcome = _start(name, pane, args)
                break
    except herdr.HerdrError as e:
        outcome = f"failed: {e.message}"
    finally:
        append_log(hub, "up", text=f"master {outcome}")
        os._exit(0)


def up(hub: Path, focus: bool = True) -> int:
    if master_only(hub, "up"):
        return 2
    team = load_team(hub)
    if team is None:
        print("Could not read herdr-team.toml.")
        return 1
    if not team.bots:
        print("The team has no bots. Add some with the team-setup action (suggested key prefix+shift+s).")
        return 1
    state_dir(hub)
    live = live_agents()
    focus_flag = "--focus" if focus else "--no-focus"

    ws = next((w for w in herdr.workspaces() if w.get("label") == team.name), None)
    created = ws is None
    # Agents call `herdr-team`; put this checkout's bin/ on their PATH so no install step is needed.
    # New tabs get it through --env; panes that already exist get it typed into their shell.
    path = f"{PLUGIN_ROOT / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    if created:
        made = herdr.run("workspace", "create", "--cwd", str(hub), "--label", team.name,
                         "--env", f"PATH={path}", focus_flag)
        ws_id, master_pane = made["workspace"]["workspace_id"], made["root_pane"]["pane_id"]
    else:
        ws_id = ws["workspace_id"]
        first_tab = herdr.tabs(ws_id)[0]["tab_id"]
        master_pane = next(p["pane_id"] for p in herdr.panes(ws_id) if p.get("tab_id") == first_tab)

    failed = 0
    master = agent_name(team.name, "master")
    if master in live:
        print("master\talready running")
    else:
        try:
            (hub / ".herdr-team" / "MASTER.md").write_text(master_prompt(hub), encoding="utf-8")
            busy = "" if created else _busy(master_pane)
            if busy == "self":
                _start_when_idle(hub, master, master_pane, master_args(team))
                print("master\tstarts in this pane when this command exits")
            elif busy:
                failed += 1
                print(f"master\tskipped: {master_pane} is running {busy}; quit it, then run team-up again")
            else:
                if not created:
                    _add_bin_to_path(master_pane)
                print(f"master\t{_start(master, master_pane, master_args(team))}")
        except herdr.HerdrError as e:
            failed += 1
            print(f"master\tfailed: {e.message}")
    if created:
        try:
            board.open_pane(master_pane, hub)
        except herdr.HerdrError as e:
            print(f"board\tfailed: {e.message}")

    tabs = {t.get("label"): t["tab_id"] for t in herdr.tabs(ws_id)}
    started = 0
    # ponytail: bots start one after another (a few seconds each); thread this if teams get large.
    for bot in team.bots:
        name = agent_name(team.name, bot.name)
        if name in live:
            print(f"{bot.name}\talready running")
            continue
        missing = [str(resolve_repo(hub, d)) for d in bot.dirs if not resolve_repo(hub, d).is_dir()]
        if missing:
            failed += 1
            print(f"{bot.name}\tfailed: folder not found: {', '.join(missing)}")
            continue
        home = ensure_home(hub, bot)
        (home / "PREAMBLE.md").write_text(render_preamble(team, bot), encoding="utf-8")
        try:
            if bot.name in tabs:
                pane = next(p["pane_id"] for p in herdr.panes(ws_id) if p.get("tab_id") == tabs[bot.name])
                busy = _busy(pane)
                if busy:
                    failed += 1
                    what = "this command" if busy == "self" else busy
                    print(f"{bot.name}\tskipped: {pane} is running {what}; quit it, then run team-up again")
                    continue
                _add_bin_to_path(pane)
            else:
                pane = herdr.run("tab", "create", "--workspace", ws_id, "--cwd", str(bot_cwd(hub, bot)),
                                 "--label", bot.name, "--env", f"PATH={path}",
                                 "--no-focus")["root_pane"]["pane_id"]
            print(f"{bot.name}\t{_start(name, pane, bot_args(team, bot))}")
            started += 1
        except (herdr.HerdrError, StopIteration) as e:
            failed += 1
            print(f"{bot.name}\tfailed: {getattr(e, 'message', 'no pane found in its tab')}")
    if started:
        append_log(hub, "up", text=f"bots {started}/{len(team.bots)}")
    if any(b.triggers for b in team.bots) and "watch" not in tabs:
        try:
            pane = herdr.run("tab", "create", "--workspace", ws_id, "--cwd", str(hub), "--label", "watch",
                             "--env", f"PATH={path}", "--no-focus")["root_pane"]["pane_id"]
            time.sleep(1.5)  # let the new shell reach its prompt
            herdr.run("pane", "run", pane, "herdr-team watch")
            print("watch\tstarted")
        except herdr.HerdrError as e:
            print(f"watch\tfailed: {e.message}")
    return 1 if failed else 0


# ---- ask / wait --------------------------------------------------------------

def caller(hub: Path) -> str:
    """Who is running this command: the team bot whose agent occupies the calling pane, else "master".
    A user's own shell, the master agent and the watch tab all count as the master. Environment
    variables are not used: a shell the user opens in a bot's tab inherits that tab's environment."""
    pane = os.environ.get("HERDR_PANE_ID")
    team = load_team(hub)
    if not pane or team is None:
        return "master"
    try:
        agents = herdr.agents()
    except herdr.HerdrError:
        return "master"   # the pre-allowed tool list is what actually keeps bots off master commands
    # ponytail: a pane moved to another workspace gets a new id while its processes keep the old one.
    name = next((a.get("name", "") for a in agents if a.get("pane_id") == pane), "")
    bots = {agent_name(team.name, b.name): b.name for b in team.bots}
    return bots.get(name, "master")


def master_only(hub: Path, command: str) -> int:
    """0 when the caller may run a master-only command; otherwise prints why and returns 2."""
    if caller(hub) == "master":
        return 0
    print(f"Only the master runs `herdr-team {command}`.")
    return 2


def short_title(text: str, n: int = 60) -> str:
    """The prompt as one short line, for tasks.json, the log and the board."""
    title = " ".join(text.split())
    return title if len(title) <= n else title[:n - 1] + "…"


def send_task(hub: Path, team: Team, bot: Bot, text: str, origin: str = "master", note: str = "") -> tuple[bool, str]:
    """Give bot a task. Returns (sent, task id or the reason it was not sent)."""
    with locked(hub):  # check, id allocation, send and record must not interleave with another ask/wait
        tasks = load_tasks(hub)
        if bot.name in tasks:
            open_task = tasks[bot.name]
            if open_task.get("origin") == "trigger":
                return False, (f"Working on {open_task['id']}, started by trigger '{open_task.get('title', '')}'. "
                               "You'll be notified when it ends; use herdr-team peek to check sooner.")
            return False, f"Task {open_task['id']} is still open. Run herdr-team wait first."
        name = agent_name(team.name, bot.name)
        agent = live_agents().get(name)
        if agent is None:
            return False, "Not running. Start it with the team-up action (suggested key prefix+shift+u)."
        if agent.get("agent_status") in ("working", "blocked"):
            return False, f"Can't send: it is {agent['agent_status']}."
        task_id = next_task_id(hub)
        report = state_dir(hub) / "reports" / f"{task_id}-{bot.name}.md"
        try:
            herdr.run("agent", "prompt", name, build_prompt(hub, task_id, text, report))
        except herdr.HerdrError as e:
            return False, f"Could not send: {e.message}"
        title = short_title(text)
        tasks[bot.name] = {"id": task_id, "title": note or title, "sent_at": time.time(),
                           "seq": agent.get("state_change_seq"), "report": str(report), "origin": origin}
        save_tasks(hub, tasks)
        append_log(hub, "trigger" if origin == "trigger" else "ask", bot.name, task_id, note or title)
    return True, task_id


def ask(hub: Path, bot_name: str, text: str) -> int:
    team = load_team(hub)
    if team and _other_team(team, bot_name):
        t = _other_team(team, bot_name)
        print(f'Tasks go through {t}\'s master: herdr-team question {t}/master "..."')
        return 2
    bot_name = canon_bot(team, bot_name.split("/", 1)[-1])
    bot = next((b for b in team.bots if b.name == bot_name), None) if team else None
    if caller(hub) != "master":
        print('Only the master hands out tasks. If another bot needs to do something, put it in your report. '
              'To find something out, use `herdr-team question <bot> "<question>"`.')
        return 2
    if bot is None:
        names = ", ".join(b.name for b in team.bots) if team else ""
        print(f"{bot_name}: no such bot in the team. Bots: {names}")
        return 2
    sent, info = send_task(hub, team, bot, text)
    print(f"{bot_name} {info} sent" if sent else f"{bot_name}: {info}")
    return 0 if sent else 2


def task_states(team: Team, tasks: dict[str, dict], agents: dict[str, dict]) -> dict[str, str]:
    return {bot: classify(t, agents.get(agent_name(team.name, bot)), os.path.exists(t.get("report", "")))
            for bot, t in tasks.items()}


def wait(hub: Path, bots: list[str], timeout: float = 1800.0, poll: float = 2.0) -> int:
    team = load_team(hub)
    if team is None:
        print("Could not read herdr-team.toml.")
        return 1
    # Tasks started by a trigger are collected by `watch`, not by the master.
    watched = {b: t for b, t in load_tasks(hub).items()
               if (not bots or b in bots) and t.get("origin") != "trigger"}
    if not watched:
        print("No open tasks")
        return 0
    deadline = time.monotonic() + timeout
    announced: set[str] | None = None
    errors, timed_out = 0, False
    while True:
        try:
            states = task_states(team, watched, live_agents())
            errors = 0
        except herdr.HerdrError as e:
            errors += 1
            if errors >= 5:
                print(f"herdr error: {e.message}")
                return 3
            time.sleep(poll)
            continue
        blocked = {b for b, s in states.items() if s == "blocked"}
        if announced is None:
            announced = set(blocked)  # already blocked when we started: report once, keep waiting for the rest
            if blocked and any(s == "working" for s in states.values()):
                print("Already blocked: " + ", ".join(sorted(blocked)) + " (waiting for the rest)", flush=True)
            if not any(s == "working" for s in states.values()):
                break
        else:
            announced &= blocked  # a bot that left blocked counts as new if it blocks again
            if wait_done(states, announced):
                break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(poll)

    log = read_log(hub, 200)
    closed = []
    for bot, state in states.items():
        task = watched[bot]
        report = task.get("report", "")
        print(f"{bot}\t{state}\t{report if os.path.exists(report) else '-'}")
        if state in ("reported", "no_report"):
            closed.append((bot, task["id"]))
            append_log(hub, state, bot, task["id"])
        elif state == "blocked":
            last = next((e for e in reversed(log) if e.get("task") == task["id"]), {})
            if last.get("kind") != "blocked":
                append_log(hub, "blocked", bot, task["id"])
        elif timed_out:
            append_log(hub, "timeout", bot, task["id"])

    def close(tasks: dict[str, dict]) -> None:
        for bot, task_id in closed:
            if tasks.get(bot, {}).get("id") == task_id:  # leave a newer task for the same bot alone
                del tasks[bot]
    update_tasks(hub, close)
    if timed_out:
        return 3
    return 2 if any(s == "blocked" for s in states.values()) else 0


# ---- restart -------------------------------------------------------------------

def restart(hub: Path, names: list[str], wait: float = 20.0) -> int:
    """Stop the named bots (or every bot when names is empty) and start them again with `up`,
    so edited SOUL.md, folders and triggers take effect. A working or blocked bot is skipped."""
    if master_only(hub, "restart"):
        return 2
    team = load_team(hub)
    wanted = names or [b.name for b in team.bots]
    unknown = [n for n in wanted if n not in [b.name for b in team.bots]]
    if unknown:
        print(f"No such bot in the team: {', '.join(unknown)}")
        return 2
    live = live_agents()
    stopping = []
    for name in wanted:
        agent = live.get(agent_name(team.name, name))
        if agent is None:
            continue   # not running: `up` starts it
        if agent.get("agent_status") in ("working", "blocked"):
            print(f"{name}: skipped, it is {agent['agent_status']}. Try again when it is done.")
            continue
        herdr.run("agent", "prompt", agent_name(team.name, name), "/exit")
        stopping.append(agent_name(team.name, name))
    skipped = [n for n in wanted if agent_name(team.name, n) in live and agent_name(team.name, n) not in stopping]
    if skipped and len(skipped) == len(wanted):
        return 2   # every bot was running and skipped; nothing for `up` to start
    deadline = time.monotonic() + wait
    while stopping and time.monotonic() < deadline:
        time.sleep(0.5)
        gone = set(stopping) - set(live_agents())
        stopping = [n for n in stopping if n not in gone]
    if stopping:
        print(f"Did not exit: {', '.join(stopping)} — check those panes.")
    time.sleep(1.0)   # let the shells come back to their prompts
    return up(hub, focus=False) or (2 if skipped else 0)   # `up` also starts bots that were not running


# ---- roster / peek / nudge / question -----------------------------------------

def _agent_for(team: Team, who: str) -> tuple[str, dict | None]:
    name = agent_name(team.name, who)
    return name, live_agents().get(name)


def _known(team: Team, who: str) -> bool:
    return who == "master" or any(b.name == who for b in team.bots)


def _target(team: Team, target: str) -> tuple["Team | None", str, str]:
    """`bot`, `master` or `<team>/<bot|master>` -> (that team, the name in it, error or "")."""
    if "/" not in target:
        return team, target, ""
    tname, who = target.split("/", 1)
    if tname == team.name:
        return team, canon_bot(team, who), ""
    other = linked_teams(team).get(tname)
    if other is None:
        why = next((r for c, _, r in collaborator_links(team) if c.name == tname), "not one of this team's collaborators")
        return None, who, f"{tname}: {why}"
    return other, canon_bot(other, who), ""


def _other_team(team: Team, target: str) -> str:
    """For ask and nudge: the other team's name when target points outside this team, else ""."""
    tname = target.split("/", 1)[0] if "/" in target else ""
    return tname if tname and tname != team.name else ""


def roster(hub: Path) -> int:
    team = load_team(hub)
    try:
        live = live_agents()
    except herdr.HerdrError:
        live = {}

    def status(who: str) -> str:
        return live.get(agent_name(team.name, who), {}).get("agent_status", "not running")
    print(f"- master: keeps the big picture with the user and hands out tasks. [{status('master')}]")
    for bot, line in zip(team.bots, roster_lines(team, english=True)):
        print(f"{line} [{status(bot.name)}]")
    for c, other, why in collaborator_links(team):
        if other is None:
            print(f"\n{c.name}: {why}")
            continue
        def ostatus(who: str) -> str:
            return live.get(agent_name(other.name, who), {}).get("agent_status", "not running")
        print(f"\nTeam {other.name} ({other.hub}) · ask with `herdr-team question {other.name}/<bot|master> ...`")
        print(f"- {other.name}/master [{ostatus('master')}]")
        for bot, line in zip(other.bots, roster_lines(other, english=True)):
            print(f"- {other.name}/{line[2:]} [{ostatus(bot.name)}]")
    return 0


def peek(hub: Path, target: str) -> int:
    team, who, err = _target(load_team(hub), target)
    if err:
        print(err)
        return 2
    if not _known(team, who):
        print(f"{target}: no such bot in {team.name}.")
        return 2
    hub = team.hub
    name, agent = _agent_for(team, who)
    if agent is None:
        print(f"{who}: not running.")
        return 2
    print(f"{who}: {agent.get('agent_status')} · {agent.get('terminal_title_stripped') or ''}")
    task = load_tasks(hub).get(who)
    if task:
        print(f"Open task {task['id']} ({int((time.time() - task.get('sent_at', time.time())) // 60)} min ago): {task.get('title', '')}")
    session = (agent.get("agent_session") or {}).get("value", "")
    digest = transcript_digest(transcript_path(session, agent.get("cwd", ""))) if session else {"last_tool": "", "texts": []}
    if digest["last_tool"]:
        print(f"Last tool: {digest['last_tool']}")
    for text in digest["texts"]:
        print("· " + " ".join(text.split()))
    if not digest["texts"] and not digest["last_tool"]:  # no readable session record: show the bottom of the screen
        screen = [l for l in herdr.text("agent", "read", name, "--source", "visible").splitlines() if l.strip()]
        print("\n".join(screen[-15:]))
    return 0


def nudge(hub: Path, who: str, text: str) -> int:
    if master_only(hub, "nudge"):   # a nudge reads as the master's direction; bots ask questions instead
        return 2
    team = load_team(hub)
    if _other_team(team, who):
        t = _other_team(team, who)
        print(f'Course corrections go through {t}\'s master: herdr-team question {t}/master "..."')
        return 2
    who = who.split("/", 1)[-1]
    if not _known(team, who) or who == "master":
        print(f"{who}: no such bot in the team.")
        return 2
    name, agent = _agent_for(team, who)
    if agent is None:
        print(f"{who}: not running.")
        return 2
    if agent.get("agent_status") == "blocked":
        print(f"{who}: waiting for the user, so it can't take this. Tell the user.")
        return 2
    herdr.run("agent", "prompt", name, f"[herdr-team 전달] {text}")
    append_log(hub, "nudge", who, text=text.strip().splitlines()[0] if text.strip() else "")
    print(f"{who} nudged")
    return 0


def question(hub: Path, target: str, text: str, timeout: float = 100.0, poll: float = 1.0) -> int:
    """Ask a bot or master of this team, or of a linked team as `<team>/<bot|master>`.
    The question record and the answer file live in the answering team's hub: its bots may write
    only their own hub's answers/, and its board then shows the question too."""
    team = load_team(hub)
    asker = caller(hub)
    tteam, who, err = _target(team, target)
    if err:
        print(err)
        return 2
    if not _known(tteam, who):
        print(f"{target}: no such bot in {'the team' if tteam is team else tteam.name}.")
        return 2
    other = tteam is not team
    if not other and who == asker:
        print("You can't ask yourself.")
        return 2
    sender = f"{team.name}/{asker}" if other else asker      # as recorded in the answering hub
    label = f"{tteam.name}/{who}" if other else who           # as recorded in this hub
    with locked(tteam.hub):
        # A question the target already sent us lives in our hub; asking back would deadlock both.
        if question_blocked(load_questions(hub), asker, label):
            print(f"{target} has an open question for you. Answer it first.")
            return 2
        name, agent = _agent_for(tteam, who)
        if agent is None:
            print(f"{target}: not running.")
            return 2
        if agent.get("agent_status") == "blocked":
            print(f"{target}: waiting for the user, so it can't answer.")
            return 2
        qid = next_question_id(tteam.hub)
        answer = state_dir(tteam.hub) / "answers" / f"{qid}-{who}.md"
        origin = f"{team.name} 팀 {asker}" if other else asker
        herdr.run("agent", "prompt", name,
                  f"[herdr-team 질문 {qid}] {origin} 의 질문이다:\n\n{text}\n\n"
                  f"답을 아래 파일에 5~15줄로 써라. 파일만 쓰고, 하던 일이 있으면 그대로 이어가라.\n{answer}")
        save_questions(tteam.hub, load_questions(tteam.hub) + [
            {"id": qid, "from": sender, "to": who, "answer": str(answer), "sent_at": time.time()}])
        append_log(tteam.hub, "question", who, qid, text.strip().splitlines()[0] if text.strip() else "", sender=sender)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if answer.exists() and answer.stat().st_size > 0:
            time.sleep(0.3)  # let the writer finish
            append_log(tteam.hub, "answer", who, qid, sender=sender)
            print(answer.read_text(encoding="utf-8", errors="replace").strip())
            return 0
        time.sleep(poll)
    print(f"{target} has not answered yet. The answer will be written to: {answer}")
    return 3


# ---- watch: polling triggers ---------------------------------------------------

class WatchState:
    def __init__(self) -> None:
        self.next_at: dict[str, float] = {}   # trigger key → when to run next
        self.every: dict[str, str] = {}       # trigger key → the interval that deadline was computed from
        self.told_blocked: set[str] = set()   # task ids the master was already told are blocked
        self.warned: set[str] = set()
        # ponytail: notices for the master live in memory; they are lost if `watch` restarts.
        self.pending: list[str] = []


def _run_trigger(hub: Path, command: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["sh", "-c", command], cwd=hub, capture_output=True, timeout=120)
        # Output is arbitrary bytes from outside; make it safe to store and to pass as an argument.
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace").replace("\0", "")
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def watch_tick(hub: Path, state: WatchState, now: float) -> None:
    """One pass: run due triggers, collect finished trigger tasks, tell the master."""
    team = load_team(hub)   # re-read every pass so spec edits apply without a restart
    if team is None:
        return
    for bot in team.bots:
        for trig in bot.triggers:
            key = f"{bot.name}.{trig.name}"   # '.' cannot appear in either name, so keys never collide
            every = parse_every(trig.every)
            if every is None:
                if key not in state.warned:
                    state.warned.add(key)
                    print(f"{key}: can't read every = {trig.every!r} (e.g. 30s, 10m, 2h). Skipping.", flush=True)
                continue
            if state.every.get(key) != trig.every:   # new or edited interval: run now
                state.every[key] = trig.every
                state.next_at.pop(key, None)
            if now < state.next_at.get(key, 0.0):
                continue
            state.next_at[key] = now + every
            last = state_dir(hub) / "triggers" / f"{key}.last"
            prev = last.read_text(encoding="utf-8", errors="replace") if last.exists() else None
            code, out = _run_trigger(hub, trig.run)
            if trigger_fire(prev, out, code):
                sent, info = send_task(hub, team, bot, f"{trig.prompt}\n\n트리거 `{trig.name}` 의 명령 출력:\n{out[:4000]}",
                                       origin="trigger", note=trig.name)
                print(f"{key}: {'sent ' + info if sent else 'skipped — ' + info}", flush=True)
                if not sent:
                    continue   # keep the old baseline so the next pass tries again
            if code == 0:
                last.write_text(out, encoding="utf-8")

    tasks = {b: t for b, t in load_tasks(hub).items() if t.get("origin") == "trigger"}
    if tasks or state.pending:
        agents = live_agents()
        done = []
        for bot, state_name in task_states(team, tasks, agents).items():
            if state_name == "blocked" and tasks[bot]["id"] not in state.told_blocked:
                state.told_blocked.add(tasks[bot]["id"])
                append_log(hub, "blocked", bot, tasks[bot]["id"])
                state.pending.append(f"[herdr-team] {bot} 이 트리거 '{tasks[bot].get('title', '')}' 작업 중 사용자 확인을 "
                                     "기다리고 있다. 사용자에게 알려라.")
            if state_name in ("reported", "no_report"):
                task = tasks[bot]
                done.append((bot, task["id"]))
                append_log(hub, "trigger_done", bot, task["id"], task.get("title", ""))
                report = task["report"] if os.path.exists(task.get("report", "")) else "보고서 없음"
                state.pending.append(f"[herdr-team] {bot} 이 트리거 '{task.get('title', '')}' 작업을 마쳤다. 보고서: {report}")
                print(f"{bot}: trigger task {task['id']} {state_name}", flush=True)
        if done:
            update_tasks(hub, lambda t: [t.pop(b) for b, tid in done if t.get(b, {}).get("id") == tid])
        master = agents.get(agent_name(team.name, "master"))
        del state.pending[:-20]   # the master may be away for a long time; keep only the latest notices
        if state.pending and master and master.get("agent_status") in ("idle", "done"):
            herdr.run("agent", "prompt", agent_name(team.name, "master"), "\n".join(state.pending))
            state.pending.clear()


def watch(hub: Path, tick: float = 5.0) -> int:
    if master_only(hub, "watch"):
        return 2
    state = WatchState()
    print(f"herdr-team watch: watching triggers in {hub}. Ctrl+C to stop.", flush=True)
    while True:
        try:
            watch_tick(hub, state, time.monotonic())
        except herdr.HerdrError as e:
            print(f"herdr error (retrying): {e.message}", flush=True)
        except Exception as e:   # one bad trigger must not stop the others; `up` does not restart watch
            print(f"error (retrying): {type(e).__name__}: {e}", flush=True)
        time.sleep(tick)
