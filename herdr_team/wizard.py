"""Team editor: add, edit and remove bots as often as you like, then save herdr-team.toml.

Screens: team (project, team instructions, bot list) → bot (name, folders, purpose, instructions, triggers)
→ trigger. A new bot and a new trigger are step forms. Nothing in herdr-team.toml changes until the user
saves; files opened with `e` (SOUL.md, instructions.md) are saved by the editor right away.
Looks and colors come from the board, so both feel like one tool.
"""
import copy
import curses
import locale
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .board import LIGHT, color, init_colors, palette_index, put, safe_addstr
from .core import (TEMPLATES, Bot, Team, Trigger, _read, agent_name, apply_template, bot_home, bot_name_error, clip,
                   default_template, display_width, draft_bots, ensure_home, ensure_instructions, find_repos,
                   guess_role, load_team, name_error, parse_every, render_soul, repo_ref, resolve_repo, save_team,
                   set_soul_purpose, slugify, soul_description, team_problems, template_preview, user_instructions)

ENTER = ("\n", "\r", curses.KEY_ENTER)
BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)
ESC = "\x1b"
TWO_PANE_W = 90          # narrower than this, the preview goes under the list
MIN_W, MIN_H = 40, 12


@dataclass
class Draft:
    bot: Bot
    template: str | None     # SOUL.md template for a bot that has no home yet
    origin: str | None       # the home this bot was loaded from (to carry it along on a rename)
    reapply: str | None = None   # a template to re-apply to an existing SOUL.md (keeps title, purpose, Extra)


# ---- saving and summaries (no curses; tested in selftest) --------------------------
# A row is a list of (text, style) segments. Styles: "", bold, dim, ok, warn, bad, accent, bot:<name>.

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
            before = _read(new / "SOUL.md")
            soul = before.replace("\r\n", "\n")
            if d.reapply:
                soul = apply_template(soul, d.reapply, d.origin or d.bot.name)
            if d.origin and d.origin != d.bot.name and soul.startswith(f"# {d.origin}\n"):
                soul = f"# {d.bot.name}\n" + soul[len(f"# {d.origin}\n"):]
            if soul_description(soul) != d.bot.role.strip():
                soul = set_soul_purpose(soul, d.bot.role)
            if soul != before:
                (new / "SOUL.md").write_text(soul, encoding="utf-8")
    ensure_instructions(hub)
    team = Team(project, bots, agent_args, hub)
    save_team(team)
    return team


def where(dirs: list[str]) -> str:
    if not dirs:
        return "리포 없음"
    first = "./ (이 폴더)" if dirs[0] == "." else dirs[0]
    return first + (f" +{len(dirs) - 1}" if len(dirs) > 1 else "")


def has_home(hub: Path, d: Draft) -> bool:
    return bool(d.origin) and (bot_home(hub, d.origin) / "SOUL.md").exists()


def soul_text(hub: Path, d: Draft) -> str:
    """What the bot's SOUL.md will be after saving."""
    if has_home(hub, d):
        soul = _read(bot_home(hub, d.origin) / "SOUL.md")
        if d.reapply:
            soul = apply_template(soul, d.reapply, d.bot.name)
        return soul if soul_description(soul) == d.bot.role.strip() else set_soul_purpose(soul, d.bot.role)
    return render_soul(d.template or default_template(d.bot.dirs), d.bot.name, d.bot.role)


def bot_preview(hub: Path, d: Draft) -> list[list]:
    b = d.bot
    rows = [[(b.name or "새 봇", f"bot:{b.name}")],
            [(b.role, "")] if b.role else [("(목적을 한 문장으로 적어 주세요)", "dim")], []]
    rows.append([("폴더    ", "dim"), (b.dirs[0] if b.dirs else "봇의 홈 (리포 없음)", ""),
                 ("  실행 위치" if b.dirs else "", "dim")])
    rows += [[("        ", ""), (x, "")] for x in b.dirs[1:]]
    if has_home(hub, d):
        kind = [(f"{d.reapply} 템플릿 다시 적용 예정 (Extra 는 유지)", "warn")] if d.reapply else [("SOUL.md", ""), ("  e 로 편집", "dim")]
    else:
        t = d.template or default_template(b.dirs)
        kind = [(t, ""), (f"  {TEMPLATES.get(t, '')}", "dim")]
    rows.append([("지침    ", "dim"), *kind])
    trig = [f"{t.name} · {t.every}" for t in b.triggers] or ["없음"]
    rows.append([("트리거  ", "dim"), (trig[0], "")])
    rows += [[("        ", ""), (t, "")] for t in trig[1:]]
    rows += [[], [("─ 지침 미리보기 ", "dim")]]
    body = soul_text(hub, d)
    start = body.find("\n## ")
    for ln in (body[start + 1:] if start >= 0 else "").splitlines():
        rows.append([(ln, "bold" if ln.startswith("## ") else "dim" if ln.startswith("<!--") else "")])
    return rows


def _changes(old: Bot, d: Draft, touched: set[str]) -> list[str]:
    out = []
    if old.name != d.bot.name:
        out.append(f"이름 {old.name} → {d.bot.name} (홈 폴더 이동)")
    if old.dirs != d.bot.dirs:
        out.append(f"폴더 {where(old.dirs)} → {where(d.bot.dirs)}")
    if old.role.strip() != d.bot.role.strip():
        out.append("목적")
    if old.triggers != d.bot.triggers:
        out.append(f"트리거 {len(old.triggers)} → {len(d.bot.triggers)}개")
    if d.reapply:
        out.append(f"템플릿 {d.reapply} 다시 적용 (Extra 유지)")
    if old.name in touched:
        out.append("SOUL.md 직접 편집")
    return out


def change_summary(existing: Team | None, project: str, drafts: list[Draft], touched: set[str],
                   instructions_changed: bool) -> list[list]:
    """What saving will do, one row per change."""
    old = {b.name: b for b in existing.bots} if existing else {}
    rows = []
    if existing is None:
        rows.append([("+ ", "ok"), ("새 팀 ", ""), (project, "bold")])
    elif existing.name != project:
        rows.append([("~ ", "warn"), (f"프로젝트 이름 {existing.name} → {project}", ""),
                     ("  (에이전트 이름이 바뀌니 팀을 새로 띄워야 합니다)", "dim")])
    for d in drafts:
        if d.origin is None or d.origin not in old:
            rows.append([("+ ", "ok"), (d.bot.name, f"bot:{d.bot.name}"),
                         (f"  봇 추가 · {where(d.bot.dirs)} · {d.template or default_template(d.bot.dirs)} 템플릿", "")])
        else:
            changes = _changes(old[d.origin], d, touched)
            if changes:
                rows.append([("~ ", "warn"), (d.bot.name, f"bot:{d.bot.name}"), ("  " + " · ".join(changes), "")])
    for name in old:
        if name not in {d.origin for d in drafts}:
            rows.append([("- ", "bad"), (name, "dim"), ("  팀에서 뺌 (홈 폴더의 SOUL.md, MEMORY.md 는 남김)", "")])
    if instructions_changed:
        rows.append([("~ ", "warn"), ("팀 공통 지침 수정", ""), ("  (모든 봇과 마스터에 반영)", "dim")])
    return rows or [[("바뀐 것이 없습니다", "dim")]]


def changed_bots(existing: Team | None, drafts: list[Draft], touched: set[str], instructions_changed: bool) -> list[str]:
    """Bots that must be restarted for the saved changes to take effect (new bots included: `up` starts them)."""
    old = {b.name: b for b in existing.bots} if existing else {}
    return [d.bot.name for d in drafts
            if instructions_changed or d.origin not in old or _changes(old[d.origin], d, touched)]


def wrap(s: str, width: int) -> list[str]:
    out, line = [], ""
    for word in s.split(" "):
        cand = f"{line} {word}" if line else word
        if display_width(cand) <= width or not line:
            line = cand
        else:
            out.append(line)
            line = word
    return out + [line] if line else out


# ---- drawing ------------------------------------------------------------------------

@dataclass
class Ui:
    hub: Path
    project: str
    names: list[str] = field(default_factory=list)   # bot names, for stable colors (same as the board)
    dirty: bool = False


def attr(ui: Ui, style: str) -> int:
    if style.startswith("bot:"):
        return color(f"bot{palette_index(style[4:], ui.names) % 6}", curses.A_BOLD)
    return {"bold": curses.A_BOLD, "dim": color("dim"), "ok": color("ok", curses.A_BOLD),
            "warn": color("working", curses.A_BOLD), "bad": color("blocked", curses.A_BOLD),
            "accent": color("accent", curses.A_BOLD)}.get(style, 0)


def draw_row(win, ui: Ui, y: int, x: int, w: int, row: list, selected: bool = False) -> None:
    if selected:   # one solid bar: readable on light and dark themes alike
        safe_addstr(win, y, x - 1, clip(" " + "".join(t for t, _ in row), w + 1), color("accent", curses.A_REVERSE | curses.A_BOLD))
        return
    end = x + w
    for text, style in row:
        if x >= end:
            break
        if display_width(text) > end - x:
            text = clip(text, end - x)
        x = put(win, y, x, text, attr(ui, style))


def box(win, y: int, x: int, h: int, w: int, title: str = "", focus: bool = False) -> None:
    if h < 2 or w < 6:
        return
    tl, tr, bl, br, hz, vt = LIGHT
    b = color("accent" if focus else "dim")
    safe_addstr(win, y, x, tl + hz * (w - 2) + tr, b)
    if title:
        safe_addstr(win, y, x + 2, " " + clip(title, min(display_width(title), w - 6)) + " ", curses.A_BOLD)
    for i in range(1, h - 1):
        safe_addstr(win, y + i, x, vt, b)
        safe_addstr(win, y + i, x + w - 1, vt, b)
    safe_addstr(win, y + h - 1, x, bl + hz * (w - 2) + br, b)


def too_small(win) -> bool:
    h, w = win.getmaxyx()
    if h >= MIN_H and w >= MIN_W:
        return False
    win.erase()
    safe_addstr(win, 0, 0, "창을 조금 키워 주세요", curses.A_BOLD)
    safe_addstr(win, 1, 0, f"최소 {MIN_W}x{MIN_H}, 지금 {w}x{h}", color("dim"))
    return True


def chrome(win, ui: Ui, crumb: str, hints: list[tuple[str, str]], progress=None) -> tuple[int, int]:
    """Title bar, breadcrumb (or step indicator), footer with key hints. Returns the content rows (top, bottom)."""
    win.erase()
    h, w = win.getmaxyx()
    x = put(win, 0, 1, "herdr-team", color("dim"))
    x = put(win, 0, x, "  팀 편집  ", curses.A_BOLD)
    x = put(win, 0, x, ui.project, color("accent", curses.A_BOLD))
    x = put(win, 0, x, f"  ·  봇 {len(ui.names)}개", color("dim"))
    state, style = ("* 저장 안 됨", "warn") if ui.dirty else ("✓ 저장됨", "ok")
    if w - display_width(state) - 2 > x:
        safe_addstr(win, 0, w - display_width(state) - 1, state, attr(ui, style))
    safe_addstr(win, 1, 0, LIGHT[4] * w, color("dim"))
    x = put(win, 2, 1, crumb, color("dim"))
    bottom = h - 4
    if progress:
        step, names, values = progress
        x += 3
        for k, name in enumerate(names):
            if k:
                x = put(win, 2, x, " ── ", color("dim"))
            if k < step:
                x = put(win, 2, x, f"✓ {name}", attr(ui, "ok"))
            elif k == step:
                x = put(win, 2, x, f" {k + 1}/{len(names)} {name} ", color("accent", curses.A_REVERSE | curses.A_BOLD))
            else:
                x = put(win, 2, x, f"{k + 1} {name}", color("dim"))
        x = 2
        for k in range(step):
            if values[k] is not None:
                x = put(win, h - 3, x, names[k] + " ", color("dim"))
                x = put(win, h - 3, x, clip(str(values[k]), min(24, display_width(str(values[k])))), curses.A_BOLD) + 3
        bottom = h - 5
    safe_addstr(win, h - 2, 0, LIGHT[4] * w, color("dim"))
    x = 1
    for key, label in hints:
        x = put(win, h - 1, x, key, color("accent", curses.A_BOLD))
        x = put(win, h - 1, x, f" {label}   ", color("dim"))
    return 3, bottom


def _move(rows: list, cur: int, d: int) -> int:
    i = cur + d
    while 0 <= i < len(rows) and not rows[i]:
        i += d
    return i if 0 <= i < len(rows) else cur


def select(win, ui: Ui, crumb: str, title: str, rows: list[list], hints: list[tuple[str, str]], keys: str = "",
           cur: int = 0, preview=None, checked: list[bool] | None = None, message: str = "", progress=None) -> tuple[str, int]:
    """A list in a panel, with an optional preview panel (right, or below on narrow screens).
    rows: [] is a separator the cursor skips. preview(cur) -> (title, rows).
    Returns (key, cursor): key is "enter", "esc" or one of `keys`. With `checked`, Space toggles."""
    curses.curs_set(0)
    top = 0
    if rows and not rows[cur]:
        cur = _move(rows, cur, 1)
    while True:
        if not too_small(win):
            h, w = win.getmaxyx()
            t, b = chrome(win, ui, crumb, hints, progress)
            area = b - t + 1
            pv = preview(cur) if preview else None
            if pv and w >= TWO_PANE_W:
                lw = max(44, min(64, w // 2))
                lh, lx, pw = area, 0, (t, lw + 1, area, w - lw - 1)
            else:
                lw = w
                lh = len(rows) + 2 if pv and area - len(rows) - 2 >= 6 else area
                pw = (t + lh, 0, area - lh, w) if pv and area - lh >= 6 else None
            box(win, t, 0, lh, lw, title, True)
            inner = lh - 2
            top = max(0, min(top, cur), cur - inner + 1)
            for i, row in enumerate(rows[top:top + inner], start=top):
                yy = t + 1 + i - top
                if not row:
                    safe_addstr(win, yy, 2, LIGHT[4] * (lw - 4), color("dim"))
                    continue
                if checked is not None:
                    row = [("[x] " if checked[i] else "[ ] ", "ok" if checked[i] else "dim"), *row]
                draw_row(win, ui, yy, 2, lw - 4, row, i == cur)
            if top + inner < len(rows):
                safe_addstr(win, t + lh - 1, lw - 4, "↓", color("dim"))
            if pv and pw:
                py, px, ph, pwid = pw
                box(win, py, px, ph, pwid, pv[0])
                for i, row in enumerate(pv[1][:ph - 2]):
                    draw_row(win, ui, py + 1 + i, px + 2, pwid - 4, row)
            if message:
                safe_addstr(win, h - 3, 2, clip("✗ " + message, w - 4), attr(ui, "bad"))
        win.refresh()
        key = win.get_wch()
        message = ""
        if key == ESC:
            return "esc", cur
        if key in ENTER:
            return "enter", cur
        if key in (curses.KEY_UP, "k"):
            cur = _move(rows, cur, -1)
        elif key in (curses.KEY_DOWN, "j"):
            cur = _move(rows, cur, 1)
        elif key == " " and checked is not None and rows:
            checked[cur] = not checked[cur]
        elif isinstance(key, str) and key and key in keys:
            return key, cur


def input_line(win, ui: Ui, crumb: str, label: str, value: str, validate=None, note: str = "",
               progress=None) -> str | None:
    """One-line field in a panel. Errors show under the field as you type; Enter only passes a valid value."""
    tried = False
    first = not progress or progress[0] == 0
    hints = [("Enter", "다음" if progress else "확인"), ("Ctrl+U", "지우기"), ("Esc", "취소" if first else "이전 단계")]
    while True:
        error = (validate(value.strip()) if validate and (value.strip() or tried) else None) or ""
        if not too_small(win):
            h, w = win.getmaxyx()
            t, _ = chrome(win, ui, crumb, hints, progress)
            bw = min(w, 80)
            box(win, t, 0, 8, bw, progress[1][progress[0]] if progress else "입력", True)
            safe_addstr(win, t + 1, 2, clip(label, min(display_width(label), bw - 4)), curses.A_BOLD)
            shown = value
            while display_width(shown) > bw - 8:
                shown = shown[1:]
            put(win, t + 3, 2, "> ", color("accent", curses.A_BOLD))
            vx = put(win, t + 3, 4, shown)
            if error:
                safe_addstr(win, t + 4, 2, clip("✗ " + error, min(display_width(error) + 2, bw - 4)), attr(ui, "bad"))
            elif value.strip() and validate:
                safe_addstr(win, t + 4, 2, "✓", attr(ui, "ok"))
            if note:
                safe_addstr(win, t + 6, 2, clip(note, min(display_width(note), bw - 4)), color("dim"))
            curses.curs_set(1)
            try:
                win.move(t + 3, min(w - 1, vx))
            except curses.error:
                pass
        win.refresh()
        key = win.get_wch()
        if key == ESC:
            curses.curs_set(0)
            return None
        if key in ENTER:
            tried = True
            if not (validate(value.strip()) if validate else None):
                curses.curs_set(0)
                return value.strip()
        elif key in BACKSPACE:
            value = value[:-1]
        elif key == "\x15":
            value = ""
        elif isinstance(key, str) and key.isprintable():
            value += key


def confirm_in(win, ui: Ui, crumb: str, question: str) -> bool:
    while True:
        if not too_small(win):
            h, w = win.getmaxyx()
            t, b = chrome(win, ui, crumb, [("y", "예"), ("n", "아니오"), ("Esc", "아니오")])
            bw = min(w - 4, 64)
            lines = wrap(question, bw - 4)
            bh = len(lines) + 4
            y, x = max(t, t + (b - t - bh) // 2), (w - bw) // 2
            box(win, y, x, bh, bw, "확인", True)
            for i, line in enumerate(lines):
                safe_addstr(win, y + 2 + i, x + 2, line)
        win.refresh()
        key = win.get_wch()
        if key in ("y", "Y"):
            return True
        if key in ("n", "N", ESC):
            return False


def open_editor(win, path: Path) -> str:
    """Hand the terminal to $VISUAL / $EDITOR (vi by default), then take it back. Returns an error or ""."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    curses.def_prog_mode()
    curses.endwin()
    error = ""
    try:
        subprocess.call([*shlex.split(editor), str(path)])
    except (OSError, ValueError) as e:
        error = f"편집기를 열지 못했습니다 ({editor}): {e}"
    finally:
        curses.reset_prog_mode()
        curses.curs_set(0)
        win.clear()
        win.refresh()
    return error


def run_steps(fns: list) -> list | None:
    """A step form: each fn(values, step) returns a value, or None to go back a step. None if left from step 1."""
    values = [None] * len(fns)
    i = 0
    while 0 <= i < len(fns):
        got = fns[i](values, i)
        if got is None:
            i -= 1
            continue
        values[i] = got
        i += 1
    return values if i == len(fns) else None


# ---- screens --------------------------------------------------------------------------

def edit_folders(win, ui: Ui, crumb: str, name: str, dirs: list[str], progress=None) -> list[str] | None:
    """Pick the folders a bot can see. The first one is where it runs. None when cancelled."""
    known = [repo_ref(ui.hub, r) for r in find_repos(ui.hub)]
    items = list(dirs) + [k for k in known if k not in dirs]
    on = [d in dirs for d in items]
    cur, message = 0, ""

    def preview(_):
        chosen = [d for d, c in zip(items, on) if c]
        rows = [[("실행 위치  ", "dim"), (chosen[0] if chosen else "봇의 홈 폴더 (리포 없음)", "bold")]]
        rows += [[("추가 폴더  " if i == 0 else "           ", "dim"), (d, "")] for i, d in enumerate(chosen[1:])]
        rows += [[], [("0개를 고르면 리포 없이 자기 홈에서 일하는 봇이 된다.", "dim")],
                 [("같은 리포를 여러 봇이 봐도 된다.", "dim")]]
        return "고른 폴더", rows

    while True:
        chosen = [d for d, c in zip(items, on) if c]
        rows = [[("./ (이 폴더)" if d == "." else d, ""), ("  실행 위치" if chosen and d == chosen[0] else "", "ok")]
                for d in items] or [[("찾은 리포가 없습니다. a 로 경로를 추가하세요", "dim")]]
        last = progress and progress[0] == len(progress[1]) - 1
        key, cur = select(win, ui, crumb, f"{name} 가 볼 폴더", rows,
                          [("Space", "선택"), ("f", "실행 위치로"), ("a", "경로 추가"),
                           ("Enter", "완료" if not progress or last else "다음"),
                           ("Esc", "취소" if not progress or progress[0] == 0 else "이전 단계")],
                          keys="fa", cur=cur, preview=preview, checked=on if items else None,
                          message=message, progress=progress)
        message = ""
        if key == "esc":
            return None
        if key == "enter":
            return [d for d, c in zip(items, on) if c]
        if key == "f" and items:
            items, on = move_first(items, cur), move_first(on, cur)
            on[0], cur = True, 0
        elif key == "a":
            path = input_line(win, ui, crumb + " / 폴더 추가", "폴더 경로 (허브 기준 상대 경로 또는 절대 경로)", "",
                              lambda v: None if v and (ui.hub / Path(v).expanduser()).is_dir() else "존재하는 폴더를 입력하세요")
            if path:
                full = (ui.hub / Path(path).expanduser()).resolve()
                ref = repo_ref(ui.hub, full) if full == ui.hub or full.parent == ui.hub else str(full)
                if ref not in items:
                    items.append(ref)
                    on.append(True)
                cur = items.index(ref)


def _slug_ok(v: str) -> bool:
    return bool(v) and v == v.lower() and v.replace("-", "").replace("_", "").isalnum() and v.isascii()


def edit_trigger(win, ui: Ui, crumb: str, trig: Trigger, others: list[str]) -> Trigger | None:
    names = ["이름", "간격", "명령", "지시"]

    def field_(label, default, validate, note=""):
        return lambda vals, i: input_line(win, ui, crumb, label, vals[i] if vals[i] is not None else default,
                                          validate, note, (i, names, vals))
    got = run_steps([
        field_("트리거 이름 (소문자·숫자·-·_)", trig.name,
               lambda v: None if _slug_ok(v) and v not in others else "소문자·숫자·-·_ 로, 다른 트리거와 겹치지 않게"),
        field_("얼마나 자주 확인할까 (30s, 10m, 2h, 1d)", trig.every or "10m",
               lambda v: None if parse_every(v) else "30s, 10m, 2h, 1d 형식입니다"),
        field_("확인할 명령 (허브 폴더에서 실행. 출력이 바뀌면 봇에게 일이 간다)", trig.run,
               lambda v: None if v else "명령이 필요합니다", "예: gh pr list --search 'review-requested:@me' --json number,title"),
        field_("출력이 바뀌었을 때 봇에게 줄 지시", trig.prompt, lambda v: None if v else "지시가 필요합니다"),
    ])
    return Trigger(*got) if got else None


def edit_triggers(win, ui: Ui, crumb: str, bot: Bot) -> None:
    cur = 0

    def preview(i):
        if i >= len(bot.triggers):
            return "트리거", [[("명령을 주기적으로 실행하고, 출력이 바뀌면", "dim")], [("봇에게 지시와 출력을 작업으로 보낸다.", "dim")]]
        t = bot.triggers[i]
        return t.name, [[("간격  ", "dim"), (t.every, "")], [("명령  ", "dim"), (t.run, "")], [],
                        [("지시", "dim")], *[[(ln, "")] for ln in wrap(t.prompt, 60)]]
    while True:
        rows = [[(clip(t.name, 18) + " ", "bold"), (clip(t.every, 5) + " ", "dim"), (t.run, "")] for t in bot.triggers]
        rows += [[("+ 트리거 추가", "accent")]]
        key, cur = select(win, ui, crumb + " / 트리거", "트리거 — 명령의 출력이 바뀌면 봇에게 일이 간다", rows,
                          [("Enter", "편집"), ("n", "추가"), ("d", "삭제"), ("Esc", "돌아가기")],
                          keys="nd", cur=cur, preview=preview)
        if key == "esc":
            return
        if key == "d" and cur < len(bot.triggers):
            del bot.triggers[cur]
        elif key == "n" or (key == "enter" and cur == len(bot.triggers)):
            t = edit_trigger(win, ui, crumb + " / 새 트리거", Trigger("", "10m", "", ""), [t.name for t in bot.triggers])
            if t:
                bot.triggers.append(t)
                cur = len(bot.triggers) - 1
        elif key == "enter":
            others = [t.name for i, t in enumerate(bot.triggers) if i != cur]
            t = edit_trigger(win, ui, crumb + " / 트리거", bot.triggers[cur], others)
            if t:
                bot.triggers[cur] = t


def pick_template(win, ui: Ui, crumb: str, name: str, current: str, progress=None) -> str | None:
    ids = list(TEMPLATES)
    rows = [[(clip(t, 12) + " ", "bold"), (TEMPLATES[t] if TEMPLATES[t] != t else "", "dim")] for t in ids]

    def preview(i):
        lines = []
        for ln in template_preview(ids[i], 14):
            lines += [[]] if ln.startswith("## ") and lines else []
            lines.append([(ln, "bold" if ln.startswith("## ") else "")])
        return f"{ids[i]} 템플릿", lines + [[], [("## Extra — 이 봇만의 지침. 저장 뒤 e 로 적는다.", "dim")]]
    first = not progress or progress[0] == 0
    key, cur = select(win, ui, crumb, f"{name} 의 지침 템플릿", rows,
                      [("Enter", "선택"), ("Esc", "취소" if first else "이전 단계")],
                      cur=ids.index(current) if current in ids else 0, preview=preview, progress=progress)
    return None if key == "esc" else ids[cur]


def edit_bot(win, ui: Ui, project: str, draft: Draft, others: list[str], touched: set[str]) -> Draft | None:
    """Edit a copy of the bot; returns it on 'done', None when cancelled."""
    d = copy.deepcopy(draft)
    bot = d.bot
    cur, message = 0, ""
    while True:
        home = has_home(ui.hub, d)
        if home and d.reapply:
            soul = [(f"{d.reapply} 템플릿 다시 적용 예정", "warn")]
        elif home:
            soul = [("SOUL.md", ""), ("  e 직접 편집 · Enter 템플릿 다시 적용", "dim")]
        else:
            t = d.template or default_template(bot.dirs)
            soul = [(t, ""), (f"  {TEMPLATES.get(t, '')}", "dim")]
        rows = [
            [("이름      ", "dim"), (bot.name, f"bot:{bot.name}")],
            [("볼 폴더   ", "dim"), (where(bot.dirs), "")],
            [("목적      ", "dim"), (bot.role, "")] if bot.role else [("목적      ", "dim"), ("(한 문장으로 적어 주세요)", "warn")],
            [("지침      ", "dim"), *soul],
            [("트리거    ", "dim"), (f"{len(bot.triggers)}개", ""),
             (f"  {', '.join(t.name for t in bot.triggers)}" if bot.triggers else "", "dim")],
            [],
            [("✓ 완료", "ok")],
        ]
        crumb = f"팀 / {bot.name or '새 봇'}"
        key, cur = select(win, ui, crumb, "봇 편집", rows,
                          [("Enter", "편집"), ("e", "SOUL.md 편집기로"), ("s", "완료"), ("Esc", "취소")],
                          keys="se", cur=cur, preview=lambda _: ("미리보기", bot_preview(ui.hub, d)), message=message)
        message = ""
        if key == "esc":
            return None
        if key == "s" or (key == "enter" and cur == 6):
            err = bot_name_error(project, bot.name, others)
            if err:
                cur, message = 0, err
                continue
            return d
        if key == "e":
            if not home:
                message = "새 봇의 SOUL.md 는 저장한 뒤에 생긴다. 지금은 오른쪽 미리보기로 확인하세요."
            else:
                message = open_editor(win, bot_home(ui.hub, d.origin) / "SOUL.md")
                touched.add(d.origin)
                ui.dirty = True
            continue
        if key != "enter":
            continue
        if cur == 0:
            got = input_line(win, ui, crumb + " / 이름", "봇 이름 (에이전트 이름은 <프로젝트>-<봇>)", bot.name,
                             lambda v: bot_name_error(project, v, others))
            if got is not None:
                bot.name = got
        elif cur == 1:
            got = edit_folders(win, ui, crumb + " / 볼 폴더", bot.name, bot.dirs)
            if got is not None:
                bot.dirs = got
        elif cur == 2:
            got = input_line(win, ui, crumb + " / 목적", "무엇을 위한 봇인가 (한 문장. SOUL.md 첫 문단이 된다)", bot.role)
            if got is not None:
                bot.role = got
        elif cur == 3 and not home:
            got = pick_template(win, ui, crumb + " / 템플릿", bot.name, d.template or default_template(bot.dirs))
            if got is not None:
                d.template = got
        elif cur == 3:
            got = pick_template(win, ui, crumb + " / 템플릿 다시 적용", bot.name, d.reapply or default_template(bot.dirs))
            if got is not None and confirm_in(win, ui, crumb, f"SOUL.md 의 템플릿 부분(How I work, Never)을 {got} 템플릿으로 "
                                                              "바꿀까요? 제목, 목적, ## Extra 절은 그대로 남습니다."):
                d.reapply = got
        elif cur == 4:
            edit_triggers(win, ui, crumb, bot)


def new_bot(win, ui: Ui, project: str, others: list[str]) -> Draft | None:
    """Step form: name → folders → template → purpose. Esc goes back a step."""
    names = ["이름", "폴더", "템플릿", "목적"]
    crumb = "팀 / 새 봇"

    def name_step(vals, i):
        return input_line(win, ui, crumb, "봇 이름 (예: api, reviewer, scout)", vals[0] or "",
                          lambda v: bot_name_error(project, v, others), progress=(i, names, vals))

    def dirs_step(vals, i):
        return edit_folders(win, ui, crumb, vals[0], vals[1] or [], progress=(i, names, [vals[0], None, None, None]))

    def template_step(vals, i):
        shown = [vals[0], where(vals[1]), None, None]
        return pick_template(win, ui, crumb, vals[0], vals[2] or default_template(vals[1]), progress=(i, names, shown))

    def role_step(vals, i):
        guess = guess_role(resolve_repo(ui.hub, vals[1][0])) if vals[1] else ""
        shown = [vals[0], where(vals[1]), vals[2], None]
        return input_line(win, ui, crumb, "무엇을 위한 봇인가 (한 문장. 명단과 현황판에 나온다)", vals[3] or guess,
                          note="비워 두면 나중에 봇 편집에서 적을 수 있다.", progress=(i, names, shown))
    got = run_steps([name_step, dirs_step, template_step, role_step])
    return Draft(Bot(got[0], got[1], got[3]), got[2], None) if got else None


def review(win, ui: Ui, rows: list[list]) -> bool:
    key, _ = select(win, ui, "팀 / 저장", "저장하기 전에 확인", rows, [("s", "저장"), ("Enter", "저장"), ("Esc", "돌아가기")],
                    keys="s")
    return key in ("s", "enter")


@dataclass
class Saved:
    team: Team
    changed: list[str]          # bots to restart for the changes to take effect
    renamed_project: bool
    instructions_changed: bool
    gone: list[str]             # old names no longer in the team (removed or renamed); their agents may still run


def team_screen(win, hub: Path) -> Saved | None:
    win.keypad(True)
    init_colors()
    existing = load_team(hub)
    project = existing.name if existing else slugify(hub.name)
    if existing:
        drafts = [Draft(b, None, b.name) for b in existing.bots]
    else:   # first run: one bot per repo found, ready to edit or remove
        drafts = [Draft(b, None, None) for b, on in draft_bots(hub, None) if on]
    agent_args = existing.agent_args if existing else []
    ui = Ui(hub, project, dirty=existing is None)
    instructions_path = hub / ".herdr-team" / "instructions.md"
    instructions_before = user_instructions(hub)
    touched: set[str] = set()
    cur, message = 3, ""
    if not drafts:
        message = "리포를 찾지 못했습니다. n 으로 봇을 추가하세요 (리포 없이 조사만 하는 봇도 됩니다)."
    first = 3   # rows: project, team instructions, separator, bots..., add, separator, save
    while True:
        ui.project, ui.names = project, [d.bot.name for d in drafts]
        instr = user_instructions(hub)
        rows = [[("프로젝트      ", "dim"), (project, "accent")],
                [("팀 공통 지침  ", "dim"), (f"{len(instr.splitlines())}줄" if instr else "비어 있음", "" if instr else "dim"),
                 ("  모든 봇과 마스터에게", "dim")],
                []]
        rows += [[(clip(d.bot.name, 14) + " ", f"bot:{d.bot.name}"), (clip(where(d.bot.dirs), 16) + " ", "dim"),
                  (d.bot.role or "(목적 없음)", "" if d.bot.role else "warn"),
                  (f"  트리거 {len(d.bot.triggers)}" if d.bot.triggers else "", "dim")] for d in drafts]
        add_row = first + len(drafts)
        save_row = add_row + 2
        rows += [[("+ 봇 추가", "accent")], [], [("✓ 저장하고 끝내기", "ok")]]

        def preview(i):
            if first <= i < add_row:
                return "미리보기", bot_preview(hub, drafts[i - first])
            if i == 0:
                return "프로젝트", [[("에이전트 이름  ", "dim"), (agent_name(project, "master"), "")],
                                    [("               ", ""), (agent_name(project, "<봇>"), "")],
                                    [("허브           ", "dim"), (str(hub), "")],
                                    [("agent_args     ", "dim"), (" ".join(agent_args) or "없음", "")]]
            if i == 1:
                body = [[(ln, "")] for ln in instr.splitlines()] or [[("아직 비어 있습니다.", "dim")]]
                return "팀 공통 지침", body + [[], [("모든 봇의 지침과 마스터 프롬프트에 사용자 지침으로 들어간다.", "dim")],
                                               [("Enter 또는 e 로 편집기에서 연다. 예: 보고하기 전에 테스트를 돌린다.", "dim")]]
            return "팀", [[(f"봇 {len(drafts)}개", "bold")], [],
                          [("봇마다 지침은 템플릿 + SOUL.md 의 ## Extra 절,", "dim")],
                          [("팀 전체 지침은 팀 공통 지침에 적는다.", "dim")]]
        key, cur = select(win, ui, "팀", "팀", rows,
                          [("Enter", "편집"), ("n", "봇 추가"), ("d", "빼기"), ("e", "공통 지침 편집"),
                           ("s", "저장"), ("Esc", "나가기")],
                          keys="ndse", cur=cur, preview=preview, message=message)
        message = ""
        names = [d.bot.name for d in drafts]
        if key == "esc":
            if not ui.dirty or confirm_in(win, ui, "팀 / 나가기", "저장하지 않은 팀 변경(봇, 폴더, 이름 등)을 버리고 나갈까요? "
                                                                 "e 로 고친 파일은 이미 저장돼 있습니다."):
                return None
        elif key == "s" or (key == "enter" and cur == save_row):
            problems = team_problems(project, [d.bot for d in drafts])
            if problems:
                message = "저장할 수 없습니다: " + problems[0]
                continue
            instructions_changed = user_instructions(hub) != instructions_before
            if not review(win, ui, change_summary(existing, project, drafts, touched, instructions_changed)):
                continue
            team = commit(hub, project, drafts, agent_args)
            return Saved(team, changed_bots(existing, drafts, touched, instructions_changed),
                         bool(existing) and existing.name != project, instructions_changed,
                         [b.name for b in existing.bots if b.name not in {d.bot.name for d in drafts}] if existing else [])
        elif key == "e" or (key == "enter" and cur == 1):
            message = open_editor(win, ensure_instructions(hub))
            if user_instructions(hub) != instructions_before:
                ui.dirty = True
        elif key == "n" or (key == "enter" and cur == add_row):
            d = new_bot(win, ui, project, names)
            if d:
                drafts.append(d)
                ui.dirty, cur = True, first + len(drafts) - 1
        elif key == "d" and first <= cur < add_row:
            name = drafts[cur - first].bot.name
            if confirm_in(win, ui, "팀 / 빼기", f"{name} 을 팀에서 뺄까요? 홈 폴더의 SOUL.md, MEMORY.md 는 남습니다."):
                del drafts[cur - first]
                ui.dirty = True
        elif key == "enter" and cur == 0:
            got = input_line(win, ui, "팀 / 프로젝트", "프로젝트 이름 (에이전트 이름 앞에 붙는다)", project,
                             lambda v: name_error(agent_name(v, "master")) if v else "이름이 비어 있습니다")
            if got and got != project:
                project, ui.dirty = got, True
        elif key == "enter" and first <= cur < add_row:
            i = cur - first
            got = edit_bot(win, ui, project, drafts[i], names[:i] + names[i + 1:], touched)
            if got:
                drafts[i], ui.dirty = got, True


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run_wizard(hub: Path) -> int:
    """Edit, save, then bring the saved changes to life: restart changed bots of a running team, or start it."""
    from . import herdr, team as team_mod
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        saved = curses.wrapper(team_screen, hub)
    except KeyboardInterrupt:
        saved = None
    if saved is None:
        print("저장하지 않고 나왔습니다.")
        return 1
    team = saved.team
    print(f"저장: {hub / 'herdr-team.toml'} (봇 {len(team.bots)}개)")
    print(f"팀 공통 지침: {hub / '.herdr-team' / 'instructions.md'} · 봇 지침: {hub / '.herdr-team' / 'bots'}/<봇>/SOUL.md")
    try:
        live = team_mod.live_agents()
    except herdr.HerdrError:
        live = {}
    running = [n for n in [agent_name(team.name, "master"), *(agent_name(team.name, b.name) for b in team.bots)] if n in live]
    try:
        if not running:
            if saved.renamed_project:
                print("프로젝트 이름이 바뀌었습니다. 예전 이름의 워크스페이스가 떠 있다면 닫아 주세요.")
            return team_mod.up(hub) if confirm("지금 팀을 띄울까요?") else 0
        for name in saved.gone:
            if agent_name(team.name, name) in live:
                print(f"{agent_name(team.name, name)}: 이름을 바꿨거나 팀에서 뺀 봇이 아직 떠 있습니다. 그 창에서 /exit 하세요.")
        if saved.instructions_changed:
            print("마스터에 팀 공통 지침을 반영하려면 마스터 창에서 /exit 한 뒤 team-up 액션(권장 키 prefix+shift+u)을 실행하세요.")
        if not saved.changed:
            return 0
        if confirm(f"바뀐 봇을 다시 띄울까요? ({', '.join(saved.changed)})"):
            return team_mod.restart(hub, saved.changed)
        print(f"나중에 반영하려면: herdr-team restart {' '.join(saved.changed)}")
        return 0
    except herdr.HerdrError as e:
        print(f"herdr 오류: {e.message}")
        return 1
