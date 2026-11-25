"""Team editor: add, edit and remove bots as often as you like, then save herdr-team.toml.

Screens: team (project name + bot list) → bot (name, folders, purpose, template, triggers) → trigger.
Nothing is written until the user saves on the team screen.
"""
import copy
import curses
import locale
import os
from dataclasses import dataclass
from pathlib import Path

from .board import safe_addstr
from .core import (TEMPLATES, Bot, Team, Trigger, _read, agent_name, apply_template, bot_home, bot_name_error, clip,
                   display_width, draft_bots, ensure_home, find_repos, guess_role, load_team, name_error,
                   parse_every, repo_ref, resolve_repo, save_team, set_soul_purpose, slugify, soul_description,
                   team_problems)

ENTER = ("\n", "\r", curses.KEY_ENTER)
BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)
ESC = "\x1b"


@dataclass
class Draft:
    bot: Bot
    template: str | None     # SOUL.md template for a bot that has no home yet
    origin: str | None       # the home this bot was loaded from (to carry it along on a rename)
    reapply: str | None = None   # a template to re-apply to an existing SOUL.md (keeps title and purpose)


# ---- saving (no curses; tested in selftest) ------------------------------------

def move_first(items: list, i: int) -> list:
    return [items[i]] + items[:i] + items[i + 1:]


def commit(hub: Path, project: str, drafts: list[Draft], agent_args: list[str]) -> Team:
    """Apply the edits: move renamed homes, seed new ones, write purposes into SOUL.md, save the spec.
    Homes of bots removed from the team are left on disk (their memory may be wanted later)."""
    bots = [d.bot for d in drafts]
    problems = team_problems(project, bots)
    if problems:
        raise ValueError("; ".join(problems))
    for d in drafts:
        new = bot_home(hub, d.bot.name)
        if d.origin and d.origin != d.bot.name:
            old = bot_home(hub, d.origin)
            if old.is_dir() and not new.exists():
                old.rename(new)
        existed = (new / "SOUL.md").exists()
        ensure_home(hub, d.bot, d.template)
        if existed:
            soul = before = _read(new / "SOUL.md")
            if d.reapply:
                soul = apply_template(soul, d.reapply, d.origin or d.bot.name)
            if d.origin and d.origin != d.bot.name and soul.startswith(f"# {d.origin}\n"):
                soul = f"# {d.bot.name}\n" + soul[len(f"# {d.origin}\n"):]
            if soul_description(soul) != d.bot.role.strip():
                soul = set_soul_purpose(soul, d.bot.role)
            if soul != before:
                (new / "SOUL.md").write_text(soul, encoding="utf-8")
    team = Team(project, bots, agent_args, hub)
    save_team(team)
    return team


# ---- widgets -------------------------------------------------------------------

def _header(win, hub: Path, step: str) -> None:
    win.erase()
    safe_addstr(win, 0, 0, f" 팀 편집 · {hub}", curses.A_BOLD)
    safe_addstr(win, 1, 0, " " + step, curses.A_DIM)


def input_line(win, hub: Path, step: str, label: str, value: str, validate=None, note: str = "") -> str | None:
    """One-line editor. Enter confirms (only if validate passes), Esc returns None."""
    curses.curs_set(1)
    error = ""
    while True:
        _header(win, hub, step)
        safe_addstr(win, 3, 1, label)
        safe_addstr(win, 4, 1, "> " + value)
        safe_addstr(win, 6, 1, error, curses.A_BOLD)
        if note:
            safe_addstr(win, 7, 1, note, curses.A_DIM)
        safe_addstr(win, 9, 1, "Enter 확인 · Ctrl+U 지우기 · Esc 취소", curses.A_DIM)
        h, w = win.getmaxyx()
        try:
            win.move(4, min(w - 1, 3 + display_width(value)))
        except curses.error:
            pass
        win.refresh()
        key = win.get_wch()
        if key == ESC:
            curses.curs_set(0)
            return None
        if key in ENTER:
            error = (validate(value.strip()) if validate else None) or ""
            if not error:
                curses.curs_set(0)
                return value.strip()
        elif key in BACKSPACE:
            value = value[:-1]
        elif key == "\x15":
            value = ""
        elif isinstance(key, str) and key.isprintable():
            value += key
            error = ""


def menu(win, hub: Path, step: str, rows: list[str], hint: str, keys: str = "", cur: int = 0,
         checked: list[bool] | None = None, message: str = "") -> tuple[str, int]:
    """A list to move through. Returns (key, cursor): key is "enter", "esc", or one of `keys`.
    With `checked`, Space toggles the row under the cursor."""
    curses.curs_set(0)
    top = 0
    while True:
        _header(win, hub, step)
        h, w = win.getmaxyx()
        visible = max(1, h - 7)
        cur = max(0, min(cur, len(rows) - 1))
        top = max(0, min(top, cur), cur - visible + 1)
        for i, row in enumerate(rows[top:top + visible], start=top):
            mark = "" if checked is None else f"[{'x' if checked[i] else ' '}] "
            attr = curses.A_REVERSE if i == cur else 0
            safe_addstr(win, 3 + i - top, 1, clip(f" {mark}{row}", w - 2), attr)
        if message:
            safe_addstr(win, h - 2, 1, clip(message, w - 2), curses.A_BOLD)
        safe_addstr(win, h - 1, 1, clip(hint, w - 2), curses.A_DIM)
        win.refresh()
        key = win.get_wch()
        message = ""
        if key == ESC:
            return "esc", cur
        if key in ENTER:
            return "enter", cur
        if key in (curses.KEY_UP, "k"):
            cur -= 1
        elif key in (curses.KEY_DOWN, "j"):
            cur += 1
        elif key == " " and checked is not None and rows:
            checked[cur] = not checked[cur]
        elif isinstance(key, str) and key and key in keys:
            return key, cur


def confirm_in(win, hub: Path, step: str, question: str) -> bool:
    key, _ = menu(win, hub, step, [question], "y 예 · n 아니오 · Esc 아니오", keys="yn")
    return key == "y"


# ---- screens --------------------------------------------------------------------

def _where(hub: Path, dirs: list[str]) -> str:
    if not dirs:
        return "(리포 없음)"
    first = "./ (이 폴더)" if dirs[0] == "." else dirs[0]
    return first + (f" +{len(dirs) - 1}" if len(dirs) > 1 else "")


def edit_folders(win, hub: Path, name: str, dirs: list[str]) -> list[str] | None:
    """Pick the folders a bot can see. The first one is where it runs. None when cancelled."""
    known = [repo_ref(hub, r) for r in find_repos(hub)]
    items = list(dirs) + [k for k in known if k not in dirs]
    on = [d in dirs for d in items]
    cur = 0
    while True:
        chosen = [d for d, c in zip(items, on) if c]
        where = f"실행 위치: {chosen[0]}" if chosen else "실행 위치: 봇의 홈 폴더 (리포 없음)"
        rows = [("./ (이 폴더)" if d == "." else d) for d in items] or ["(찾은 리포가 없습니다. a 로 경로를 추가하세요)"]
        key, cur = menu(win, hub, f"{name} · 볼 폴더 — {where}", rows,
                        "Space 선택 · f 맨 위로(실행 위치) · a 경로 추가 · Enter 완료 · Esc 취소",
                        keys="fa", cur=cur, checked=on if items else None)
        if key == "esc":
            return None
        if key == "enter":
            return [d for d, c in zip(items, on) if c]
        if key == "f" and items:
            items, on = move_first(items, cur), move_first(on, cur)
            on[0], cur = True, 0
        elif key == "a":
            path = input_line(win, hub, f"{name} · 폴더 추가", "폴더 경로 (허브 기준 상대 경로 또는 절대 경로)", "",
                              lambda v: None if v and (hub / Path(v).expanduser()).is_dir() else "존재하는 폴더를 입력하세요")
            if path:
                full = (hub / Path(path).expanduser()).resolve()
                ref = repo_ref(hub, full) if full == hub or full.parent == hub else str(full)
                if ref not in items:
                    items.append(ref)
                    on.append(True)
                cur = items.index(ref)


def _slug_ok(v: str) -> bool:
    return bool(v) and v == v.lower() and v.replace("-", "").replace("_", "").isalnum() and v.isascii()


def edit_trigger(win, hub: Path, bot: str, trig: Trigger, others: list[str]) -> Trigger | None:
    step = f"{bot} · 트리거"
    name = input_line(win, hub, step, "트리거 이름 (소문자·숫자·-·_)", trig.name,
                      lambda v: None if _slug_ok(v) and v not in others else "소문자·숫자·-·_ 로, 다른 트리거와 겹치지 않게")
    if name is None:
        return None
    every = input_line(win, hub, step, "얼마나 자주 확인할까 (30s, 10m, 2h, 1d)", trig.every or "10m",
                       lambda v: None if parse_every(v) else "30s, 10m, 2h, 1d 형식입니다")
    if every is None:
        return None
    run = input_line(win, hub, step, "확인할 명령 (허브 폴더에서 실행. 출력이 바뀌면 봇에게 일이 간다)", trig.run,
                     lambda v: None if v else "명령이 필요합니다",
                     note="예: gh pr list --search 'review-requested:@me' --json number,title")
    if run is None:
        return None
    prompt = input_line(win, hub, step, "출력이 바뀌었을 때 봇에게 줄 지시", trig.prompt,
                        lambda v: None if v else "지시가 필요합니다")
    if prompt is None:
        return None
    return Trigger(name, every, run, prompt)


def edit_triggers(win, hub: Path, bot: Bot) -> None:
    cur = 0
    while True:
        rows = [f"{clip(t.name, 18)} {clip(t.every, 5)} {t.run}" for t in bot.triggers] + ["+ 트리거 추가"]
        key, cur = menu(win, hub, f"{bot.name} · 트리거 — 명령의 출력이 바뀌면 봇에게 일이 간다", rows,
                        "Enter 편집 · n 추가 · d 삭제 · Esc 돌아가기", keys="nd", cur=cur)
        if key == "esc":
            return
        if key == "d" and cur < len(bot.triggers):
            del bot.triggers[cur]
        elif key == "n" or (key == "enter" and cur == len(bot.triggers)):
            t = edit_trigger(win, hub, bot.name, Trigger("", "10m", "", ""), [t.name for t in bot.triggers])
            if t:
                bot.triggers.append(t)
                cur = len(bot.triggers) - 1
        elif key == "enter":
            others = [t.name for i, t in enumerate(bot.triggers) if i != cur]
            t = edit_trigger(win, hub, bot.name, bot.triggers[cur], others)
            if t:
                bot.triggers[cur] = t


def pick_template(win, hub: Path, name: str, current: str) -> str | None:
    ids = list(TEMPLATES)
    key, cur = menu(win, hub, f"{name} · 지침(SOUL.md) 템플릿",
                    [f"{clip(t, 12)} {TEMPLATES[t]}" for t in ids], "Enter 선택 · Esc 취소",
                    cur=ids.index(current) if current in ids else 0)
    return None if key == "esc" else ids[cur]


def edit_bot(win, hub: Path, project: str, draft: Draft, others: list[str]) -> Draft | None:
    """Edit a copy of the bot; returns it on 'done', None when cancelled."""
    d = copy.deepcopy(draft)
    bot = d.bot
    cur = 0
    while True:
        has_home = bool(d.origin) and (bot_home(hub, d.origin) / "SOUL.md").exists()
        if has_home and d.reapply:
            soul_row = f"{d.reapply} 템플릿으로 다시 적용 (저장할 때. 제목과 목적은 유지)"
        elif has_home:
            soul_row = f"SOUL.md — Enter 로 템플릿 다시 적용, 직접 편집: {bot_home(hub, d.origin)}/SOUL.md"
        else:
            soul_row = f"{d.template or '(선택)'} — {TEMPLATES.get(d.template or '', '')}"
        rows = [
            f"이름       {bot.name}",
            f"볼 폴더    {_where(hub, bot.dirs)}" + (f"  ({', '.join(bot.dirs)})" if len(bot.dirs) > 1 else ""),
            f"목적       {bot.role or '(한 문장으로 적어 주세요)'}",
            f"지침       {soul_row}",
            f"트리거     {len(bot.triggers)}개" + (f"  ({', '.join(t.name for t in bot.triggers)})" if bot.triggers else ""),
            "",
            "✓ 완료",
        ]
        key, cur = menu(win, hub, f"봇 편집 · {bot.name or '새 봇'}", rows,
                        "Enter 편집 · s 완료 · Esc 취소 (이 봇의 변경을 버림)", keys="s", cur=cur)
        if key == "esc":
            return None
        if key == "s" or (key == "enter" and cur == 6):
            if not bot.name or bot_name_error(project, bot.name, others):
                cur = 0
                continue
            return d
        if key != "enter":
            continue
        if cur == 0:
            got = input_line(win, hub, "봇 이름", "봇 이름 (에이전트 이름은 <프로젝트>-<봇>)", bot.name,
                             lambda v: bot_name_error(project, v, others))
            if got is not None:
                bot.name = got
        elif cur == 1:
            got = edit_folders(win, hub, bot.name, bot.dirs)
            if got is not None:
                bot.dirs = got
        elif cur == 2:
            got = input_line(win, hub, f"{bot.name} · 목적", "무엇을 위한 봇인가 (한 문장. SOUL.md 첫 문단이 된다)",
                             bot.role)
            if got is not None:
                bot.role = got
        elif cur == 3 and not has_home:
            got = pick_template(win, hub, bot.name, d.template or ("developer" if bot.dirs else "researcher"))
            if got is not None:
                d.template = got
        elif cur == 3:
            got = pick_template(win, hub, bot.name, d.reapply or ("developer" if bot.dirs else "researcher"))
            if got is not None and confirm_in(win, hub, f"{bot.name} · 템플릿 다시 적용",
                                              f"SOUL.md 의 How I work / Never 절을 {got} 템플릿으로 바꿀까요? "
                                              "(제목과 목적은 유지. 직접 고친 절은 사라집니다)"):
                d.reapply = got
        elif cur == 4:
            edit_triggers(win, hub, bot)


def new_bot(win, hub: Path, project: str, others: list[str]) -> Draft | None:
    """Ask the essentials for a new bot in order: name, folders, template, purpose."""
    name = input_line(win, hub, "새 봇", "봇 이름 (예: api, reviewer, scout)", "",
                      lambda v: bot_name_error(project, v, others))
    if not name:
        return None
    dirs = edit_folders(win, hub, name, [])
    if dirs is None:
        return None
    template = pick_template(win, hub, name, "developer" if dirs else "researcher")
    if template is None:
        return None
    role = guess_role(resolve_repo(hub, dirs[0])) if dirs else ""
    role = input_line(win, hub, f"{name} · 목적", "무엇을 위한 봇인가 (한 문장)", role)
    if role is None:
        return None
    return Draft(Bot(name, dirs, role), template, None)


def team_screen(win, hub: Path) -> Team | None:
    win.keypad(True)
    existing = load_team(hub)
    project = existing.name if existing else slugify(hub.name)
    if existing:
        drafts = [Draft(b, None, b.name) for b in existing.bots]
    else:   # first run: one bot per repo found, ready to edit or remove
        drafts = [Draft(b, None, None) for b, on in draft_bots(hub, None) if on]
    agent_args = existing.agent_args if existing else []
    dirty = existing is None
    cur, message = 2, ""
    if not drafts:
        message = "리포를 찾지 못했습니다. n 으로 봇을 추가하세요 (리포 없이 조사만 하는 봇도 됩니다)."
    while True:
        rows = [f"프로젝트   {project}", ""]
        rows += [f"{clip(d.bot.name, 14)} {clip(_where(hub, d.bot.dirs), 22)} {clip(d.bot.role, 34)}"
                 + (f"  트리거 {len(d.bot.triggers)}" if d.bot.triggers else "") for d in drafts]
        rows += ["+ 봇 추가", "", "✓ 저장하고 끝내기"]
        add_row, save_row = 2 + len(drafts), 4 + len(drafts)
        key, cur = menu(win, hub, f"팀 · 봇 {len(drafts)}개" + (" · 저장 안 됨" if dirty else ""), rows,
                        "Enter 편집 · n 봇 추가 · d 삭제 · s 저장 · Esc 나가기", keys="nds", cur=cur, message=message)
        message = ""
        names = [d.bot.name for d in drafts]
        if key == "esc":
            if not dirty or confirm_in(win, hub, "나가기", "저장하지 않은 변경을 버리고 나갈까요?"):
                return None
        elif key == "s" or (key == "enter" and cur == save_row):
            problems = team_problems(project, [d.bot for d in drafts])
            if problems:
                message = "저장할 수 없습니다: " + problems[0]
                continue
            return commit(hub, project, drafts, agent_args)
        elif key == "n" or (key == "enter" and cur == add_row):
            d = new_bot(win, hub, project, names)
            if d:
                drafts.append(d)
                dirty, cur = True, 2 + len(drafts) - 1
        elif key == "d" and 2 <= cur < add_row:
            name = drafts[cur - 2].bot.name
            if confirm_in(win, hub, "봇 삭제", f"{name} 을 팀에서 뺄까요? (홈 폴더의 SOUL.md, MEMORY.md 는 남습니다)"):
                del drafts[cur - 2]
                dirty = True
        elif key == "enter" and cur == 0:
            got = input_line(win, hub, "프로젝트", "프로젝트 이름 (에이전트 이름 앞에 붙는다)", project,
                             lambda v: name_error(agent_name(v, "master")) if v else "이름이 비어 있습니다")
            if got and got != project:
                project, dirty = got, True
        elif key == "enter" and 2 <= cur < add_row:
            i = cur - 2
            got = edit_bot(win, hub, project, drafts[i], names[:i] + names[i + 1:])
            if got:
                drafts[i], dirty = got, True


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run_wizard(hub: Path) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        team = curses.wrapper(team_screen, hub)
    except KeyboardInterrupt:
        team = None
    if team is None:
        print("저장하지 않고 나왔습니다.")
        return 1
    print(f"저장: {hub / 'herdr-team.toml'} (봇 {len(team.bots)}개)")
    print(f"봇 지침: {hub / '.herdr-team' / 'bots'}/<봇>/SOUL.md — 직접 고쳐서 다듬을 수 있습니다.")
    print("떠 있는 봇에 반영하려면 herdr-team restart [봇...], 새 봇을 띄우려면 herdr-team up.")
    return 0
