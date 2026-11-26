"""Team setup: add, edit and remove agents as often as you like, then save herdr-team.toml.

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
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .board import LIGHT, color, init_colors, palette_index, put, safe_addstr
from .core import (TEMPLATE_ID_RE, TEMPLATE_SKELETON, TEMPLATES, Bot, Team, Trigger, _read, agent_name, apply_template, bot_home, bot_name_error, clip,
                   default_template, display_width, draft_bots, ensure_home, ensure_instructions, find_repos,
                   guess_role, load_team, name_error, project_error, parse_every, render_soul, repo_ref, resolve_repo, save_team,
                   is_user_template, set_soul_purpose, slugify, soul_description, team_problems, template_path,
                   template_preview, template_purpose, templates, user_instructions, user_template_dir)

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
    soul: str | None = None      # SOUL.md text edited (with e) before the bot has a home; written on save


# ---- saving and summaries (no curses; tested in selftest) --------------------------
# A row is a list of (text, style) segments. Styles: "", bold, dim, ok, warn, bad, accent, bot:<name>.

def move_first(items: list, i: int) -> list:
    return [items[i]] + items[:i] + items[i + 1:]


def delete_home(hub: Path, name: str) -> None:
    """Delete a bot's home folder, only if it really is one folder directly under this hub's bots/."""
    base = (hub / ".herdr-team" / "agents").resolve()
    home = bot_home(hub, name).resolve()
    if home.parent != base:
        raise ValueError(f"not an agent home under {base}: {home}")
    if home.is_dir():
        shutil.rmtree(home)


def commit(hub: Path, project: str, drafts: list[Draft], agent_args: list[str], purge: set[str] = frozenset()) -> Team:
    """Apply the edits: delete the homes the user asked to delete (purge), move renamed homes, seed new
    ones, write purposes into SOUL.md, save the spec. Other removed bots keep their homes on disk."""
    bots = [d.bot for d in drafts]
    problems = team_problems(project, bots)
    if problems:
        raise ValueError("; ".join(problems))
    for name in purge:   # first, so a new bot with that name starts from a fresh home
        delete_home(hub, name)
    for d in drafts:
        new = bot_home(hub, d.bot.name)
        if d.origin and d.origin != d.bot.name:
            old = bot_home(hub, d.origin)
            # A case-only rename (api -> Api) is the same folder on macOS: rename it in place.
            if old.is_dir() and (not new.exists() or old.samefile(new)):
                old.rename(new)
        existed = (new / "SOUL.md").exists()
        if d.soul is not None and not has_home(hub, d):
            new.mkdir(parents=True, exist_ok=True)
            (new / "SOUL.md").write_text(d.soul, encoding="utf-8")
            existed = True   # below: only the purpose line follows a purpose changed after editing
        ensure_home(hub, d.bot, d.template)
        if existed:
            before = _read(new / "SOUL.md")
            soul = before.replace("\r\n", "\n")
            if d.reapply:
                soul = apply_template(soul, d.reapply, d.origin or d.bot.name, hub)
            if d.origin and d.origin != d.bot.name and soul.startswith(f"# {d.origin}\n"):
                soul = f"# {d.bot.name}\n" + soul[len(f"# {d.origin}\n"):]
            if soul_description(soul) != d.bot.role.strip():
                soul = set_soul_purpose(soul, d.bot.role)
            if soul != before:
                (new / "SOUL.md").write_text(soul, encoding="utf-8")
    ensure_instructions(hub)
    old = load_team(hub)   # collaborators are edited in herdr-team.toml by hand; keep them
    team = Team(project, bots, agent_args, hub, old.collaborators if old else [])
    save_team(team)
    return team


def where(dirs: list[str]) -> str:
    if not dirs:
        return "no repo"
    first = "./ (this folder)" if dirs[0] == "." else dirs[0]
    return first + (f" +{len(dirs) - 1}" if len(dirs) > 1 else "")


def has_home(hub: Path, d: Draft) -> bool:
    return bool(d.origin) and (bot_home(hub, d.origin) / "SOUL.md").exists()


def soul_text(hub: Path, d: Draft) -> str:
    """What the bot's SOUL.md will be after saving."""
    if has_home(hub, d):
        soul = _read(bot_home(hub, d.origin) / "SOUL.md")
        if d.reapply:
            soul = apply_template(soul, d.reapply, d.bot.name, hub)
        return soul if soul_description(soul) == d.bot.role.strip() else set_soul_purpose(soul, d.bot.role)
    if d.soul is not None:
        return d.soul if soul_description(d.soul) == d.bot.role.strip() else set_soul_purpose(d.soul, d.bot.role)
    return render_soul(d.template or default_template(d.bot.dirs), d.bot.name, d.bot.role, hub)


def is_default_purpose(hub: Path, role: str) -> bool:
    """Empty, or still some template's default sentence: safe to replace when the template changes."""
    return not role.strip() or role.strip() in {template_purpose(t, hub) for t in templates(hub)}


def default_purpose(hub: Path, role: str, template: str) -> str:
    """The purpose to show after picking a template: the user's own words stay, a default follows the template."""
    return template_purpose(template, hub) if is_default_purpose(hub, role) else role


def bot_preview(hub: Path, d: Draft) -> list[list]:
    b = d.bot
    rows = [[(b.name or "new agent", f"bot:{b.name}")],
            [(b.role, "")] if b.role else [("(add a one-sentence purpose)", "dim")], []]
    rows.append([("Folders       ", "dim"), (b.dirs[0] if b.dirs else "agent's home (no repo)", ""),
                 ("  runs here" if b.dirs else "", "dim")])
    rows += [[("        ", ""), (x, "")] for x in b.dirs[1:]]
    if has_home(hub, d):
        kind = [(f"will re-apply the {d.reapply} template (Extra kept)", "warn")] if d.reapply else [("SOUL.md", ""), ("  e open in editor", "dim")]
    else:
        t = d.template or default_template(b.dirs)
        kind = [(f"{t} (edited)", "warn"), ("  e open in editor", "dim")] if d.soul is not None else \
            [(t, ""), (f"  {templates(hub).get(t, '')}", "dim")]
    rows.append([("Instructions  ", "dim"), *kind])
    trig = [f"{t.name} · {t.every}" for t in b.triggers] or ["none"]
    rows.append([("Triggers      ", "dim"), (trig[0], "")])
    rows += [[("        ", ""), (t, "")] for t in trig[1:]]
    rows += [[], [("─ instructions preview ", "dim")]]
    body = soul_text(hub, d)
    start = body.find("\n## ")
    for ln in (body[start + 1:] if start >= 0 else "").splitlines():
        rows.append([(ln, "bold" if ln.startswith("## ") else "dim" if ln.startswith("<!--") else "")])
    return rows


def _changes(old: Bot, d: Draft, touched: set[str]) -> list[str]:
    out = []
    if old.name != d.bot.name:
        out.append(f"name {old.name} → {d.bot.name} (home folder moves)")
    if old.dirs != d.bot.dirs:
        out.append(f"folders {where(old.dirs)} → {where(d.bot.dirs)}")
    if old.role.strip() != d.bot.role.strip():
        out.append("purpose")
    if old.triggers != d.bot.triggers:
        out.append(f"triggers {len(old.triggers)} → {len(d.bot.triggers)}")
    if d.reapply:
        out.append(f"re-apply the {d.reapply} template (Extra kept)")
    if old.name in touched:
        out.append("SOUL.md edited")
    return out


def change_summary(existing: Team | None, project: str, drafts: list[Draft], touched: set[str],
                   instructions_changed: bool, purge: set[str] = frozenset(), master_changed: bool = False) -> list[list]:
    """What saving will do, one row per change."""
    old = {b.name: b for b in existing.bots} if existing else {}
    rows = []
    if existing is None:
        rows.append([("+ ", "ok"), ("new team ", ""), (project, "bold")])
    elif existing.name != project:
        rows.append([("~ ", "warn"), (f"project name {existing.name} → {project}", ""),
                     ("  (start the team again after saving)", "dim")])
    for d in drafts:
        if d.origin is None or d.origin not in old:
            rows.append([("+ ", "ok"), (d.bot.name, f"bot:{d.bot.name}"),
                         (f"  new agent · {where(d.bot.dirs)} · {d.template or default_template(d.bot.dirs)} template"
                          f"{' (edited)' if d.soul is not None else ''}", "")])
        else:
            changes = _changes(old[d.origin], d, touched)
            if changes:
                rows.append([("~ ", "warn"), (d.bot.name, f"bot:{d.bot.name}"), ("  " + " · ".join(changes), "")])
    for name in old:
        if name not in {d.origin for d in drafts}:
            rows.append([("- ", "bad"), (name, "dim"), ("  removed from the team" if name in purge else
                                                         "  removed from the team (SOUL.md and MEMORY.md stay in its home)", "")])
    for name in sorted(purge):
        rows.append([("- ", "bad"), (f"delete home folder of {name}", "bad"), ("  (SOUL.md, MEMORY.md; can't be undone)", "dim")])
    if instructions_changed:
        rows.append([("~ ", "warn"), ("team instructions changed", ""), ("  (the master and all agents)", "dim")])
    if master_changed:
        rows.append([("~ ", "warn"), ("master instructions changed", ""), ("  (master.md)", "dim")])
    return rows or [[("Nothing changed", "dim")]]


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
    safe_addstr(win, 0, 0, "Please enlarge the window", curses.A_BOLD)
    safe_addstr(win, 1, 0, f"need {MIN_W}x{MIN_H}, now {w}x{h}", color("dim"))
    return True


def chrome(win, ui: Ui, crumb: str, hints: list[tuple[str, str]], progress=None) -> tuple[int, int]:
    """Title bar, breadcrumb (or step indicator), footer with key hints. Returns the content rows (top, bottom)."""
    win.erase()
    h, w = win.getmaxyx()
    x = put(win, 0, 1, "herdr-team", color("dim"))
    x = put(win, 0, x, "  team setup  ", curses.A_BOLD)
    x = put(win, 0, x, ui.project, color("accent", curses.A_BOLD))
    x = put(win, 0, x, f"  ·  {len(ui.names)} agent{'s' if len(ui.names) != 1 else ''}", color("dim"))
    state, style = ("* unsaved", "warn") if ui.dirty else ("✓ saved", "ok")
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
    for k, (key, label) in enumerate(hints):
        x = put(win, h - 1, x + (3 if k else 0), key, color("accent", curses.A_BOLD))
        x = put(win, h - 1, x, f" {label}", color("dim"))
    return 3, bottom


def _skip(row: list) -> bool:
    """Separators ([]) and hint lines (first segment styled "hint") take no cursor."""
    return not row or row[0][1] == "hint"


def hint_row(indent: int, keys: list[tuple[str, str]]) -> list:
    """A key-hint line under a value: keys in accent, labels dim. The cursor skips it."""
    row = [(" " * indent, "hint")]
    for k, (key, label) in enumerate(keys):
        row += [(("   " if k else "") + key, "accent"), (f" {label}", "dim")]
    return row


def _move(rows: list, cur: int, d: int) -> int:
    i = cur + d
    while 0 <= i < len(rows) and _skip(rows[i]):
        i += d
    return i if 0 <= i < len(rows) else cur


def select(win, ui: Ui, crumb: str, title: str, rows: list[list], hints: list[tuple[str, str]], keys: str = "",
           cur: int = 0, preview=None, checked: list[bool] | None = None, message: str = "", progress=None,
           note: str = "") -> tuple[str, int]:
    """A list in a panel, with an optional preview panel (right, or below on narrow screens).
    rows: [] is a separator the cursor skips. preview(cur) -> (title, rows).
    Returns (key, cursor): key is "enter", "esc" or one of `keys`. With `checked`, Space toggles."""
    curses.curs_set(0)
    # A new screen: repaint everything once. curses' line diff can leave stale cells next to
    # clipped wide (Korean) text on some terminals.
    win.clear()
    top = 0
    if rows and _skip(rows[cur]):
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
            elif note:   # stays on screen; an error message takes its place for one keypress
                safe_addstr(win, h - 3, 2, clip(note, w - 4), attr(ui, "accent"))
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
    hints = [("Enter", "next" if progress else "ok"), ("Ctrl+U", "clear"), ("Esc", "cancel" if first else "back")]
    while True:
        error = (validate(value.strip()) if validate and (value.strip() or tried) else None) or ""
        if not too_small(win):
            h, w = win.getmaxyx()
            t, _ = chrome(win, ui, crumb, hints, progress)
            bw = min(w, 80)
            box(win, t, 0, 8, bw, progress[1][progress[0]] if progress else "Input", True)
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
            t, b = chrome(win, ui, crumb, [("y", "yes"), ("n", "no"), ("Esc", "no")])
            bw = min(w - 4, 64)
            lines = wrap(question, bw - 4)
            bh = len(lines) + 6   # border, gap, question, gap, answer keys, gap, border
            y, x = max(t, t + (b - t - bh) // 2), (w - bw) // 2
            box(win, y, x, bh, bw, "Confirm", True)
            for i, line in enumerate(lines):
                safe_addstr(win, y + 2 + i, x + 2, line)
            keys = [("y", "accent"), (" Yes   ", ""), ("n", "accent"), (" No", "")]
            kw = min(sum(display_width(t) for t, _ in keys), bw - 4)
            draw_row(win, ui, y + 3 + len(lines), x + bw - 2 - kw, kw, keys)   # right-aligned, like dialog buttons
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
        error = f"Could not open the editor ({editor}): {e}"
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
        rows = [[("Runs in    ", "dim"), (chosen[0] if chosen else "the agent's home folder (no repo)", "bold")]]
        rows += [[("Also sees  " if i == 0 else "           ", "dim"), (d, "")] for i, d in enumerate(chosen[1:])]
        rows += [[], [("Pick none for an agent with no repo that works in its home.", "dim")],
                 [("Several agents may share a repo.", "dim")]]
        return "Selected folders", rows

    while True:
        chosen = [d for d, c in zip(items, on) if c]
        rows = [[("./ (this folder)" if d == "." else d, ""), ("  runs here" if chosen and d == chosen[0] else "", "ok")]
                for d in items] or [[("No repos found. Press a to add a path", "dim")]]
        last = progress and progress[0] == len(progress[1]) - 1
        key, cur = select(win, ui, crumb, f"Folders for {name}", rows,
                          [("Space", "select"), ("f", "run here"), ("a", "add path"),
                           ("Enter", "done" if not progress or last else "next"),
                           ("Esc", "cancel" if not progress or progress[0] == 0 else "back")],
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
            path = input_line(win, ui, crumb + " / add folder", "Folder path (relative to the hub, or absolute)", "",
                              lambda v: None if v and (ui.hub / Path(v).expanduser()).is_dir() else "Enter a folder that exists")
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
    names = ["Name", "Every", "Command", "Prompt"]

    def field_(label, default, validate, note=""):
        return lambda vals, i: input_line(win, ui, crumb, label, vals[i] if vals[i] is not None else default,
                                          validate, note, (i, names, vals))
    got = run_steps([
        field_("Trigger name (a-z, 0-9, -, _)", trig.name,
               lambda v: None if _slug_ok(v) and v not in others else "Use a-z, 0-9, - and _, unique among triggers"),
        field_("How often to check (30s, 10m, 2h, 1d)", trig.every or "10m",
               lambda v: None if parse_every(v) else "Use 30s, 10m, 2h or 1d"),
        field_("Command to check (runs in the hub; new output gives the agent a task)", trig.run,
               lambda v: None if v else "A command is required", "e.g. gh pr list --search 'review-requested:@me' --json number,title"),
        field_("Prompt for the agent when the output changes", trig.prompt, lambda v: None if v else "A prompt is required"),
    ])
    return Trigger(*got) if got else None


def edit_triggers(win, ui: Ui, crumb: str, bot: Bot) -> None:
    cur = 0

    def preview(i):
        if i >= len(bot.triggers):
            return "Triggers", [[("Runs a command on a schedule. When its output", "dim")], [("changes, the agent gets the prompt and output as a task.", "dim")]]
        t = bot.triggers[i]
        return t.name, [[("Every    ", "dim"), (t.every, "")], [("Command  ", "dim"), (t.run, "")], [],
                        [("Prompt", "dim")], *[[(ln, "")] for ln in wrap(t.prompt, 60)]]
    while True:
        rows = [[(clip(t.name, 18) + " ", "bold"), (clip(t.every, 5) + " ", "dim"), (t.run, "")] for t in bot.triggers]
        rows += [[("+ add trigger", "accent")]]
        key, cur = select(win, ui, crumb + " / triggers", "Triggers — new command output gives the agent a task", rows,
                          [("Enter", "edit"), ("n", "add"), ("d", "delete"), ("Esc", "back")],
                          keys="nd", cur=cur, preview=preview)
        if key == "esc":
            return
        if key == "d" and cur < len(bot.triggers):
            del bot.triggers[cur]
        elif key == "n" or (key == "enter" and cur == len(bot.triggers)):
            t = edit_trigger(win, ui, crumb + " / new trigger", Trigger("", "10m", "", ""), [t.name for t in bot.triggers])
            if t:
                bot.triggers.append(t)
                cur = len(bot.triggers) - 1
        elif key == "enter":
            others = [t.name for i, t in enumerate(bot.triggers) if i != cur]
            t = edit_trigger(win, ui, crumb + " / trigger", bot.triggers[cur], others)
            if t:
                bot.triggers[cur] = t


def pick_template(win, ui: Ui, crumb: str, name: str, current: str, progress=None) -> str | None:
    """Choose a template. n / c / e / d add, duplicate, edit and delete the hub's own templates
    (.herdr-team/templates); those files are written right away, apart from saving the team."""
    first = not progress or progress[0] == 0
    message = ""
    while True:
        labels = templates(ui.hub)
        ids = list(labels)
        rows = [[(clip(t, 12) + " ", "bold"),
                 (labels[t] if labels[t] != t else "", "accent" if is_user_template(ui.hub, t) else "dim")] for t in ids]

        def preview(i):
            purpose = template_purpose(ids[i], ui.hub)
            lines = [[(purpose, "")] if purpose else [("(no default purpose)", "dim")], []]
            for ln in template_preview(ids[i], 14, ui.hub):
                lines += [[]] if ln.startswith("## ") and len(lines) > 2 else []
                lines.append([(ln, "bold" if ln.startswith("## ") else "")])
            note = ("Your file, saved now; kept if you quit setup." if is_user_template(ui.hub, ids[i])
                    else "Built in, read-only. c makes your copy.")
            return f"{ids[i]} template", lines + [[], [(note, "dim")]]
        key, cur = select(win, ui, crumb, f"Template for {name}", rows,
                          [("Enter", "select"), ("Esc", "cancel" if first else "back"),
                           ("n", "new"), ("c", "copy"), ("e", "edit"), ("d", "delete")],
                          keys="ncde", cur=ids.index(current) if current in ids else 0, preview=preview,
                          progress=progress, message=message)
        message, current = "", ids[cur]
        if key == "esc":
            return None
        if key == "enter":
            return current
        if key in ("n", "c"):
            new = template_id_input(win, ui, crumb, f"{current}-copy" if key == "c" else "")
            if new:
                path = user_template_dir(ui.hub) / f"{new}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(_read(template_path(current, ui.hub)) if key == "c" else TEMPLATE_SKELETON, encoding="utf-8")
                message, current = open_editor(win, path), new
        elif not is_user_template(ui.hub, current):
            message = "Built-in templates are read-only — c duplicates it"
        elif key == "e":
            message = open_editor(win, user_template_dir(ui.hub) / f"{current}.md")
        elif confirm_in(win, ui, crumb, f"Delete your template {current}? Saved agents keep their SOUL.md."
                        + (" The built-in one comes back." if current in TEMPLATES else "")):
            (user_template_dir(ui.hub) / f"{current}.md").unlink()


def template_id_input(win, ui: Ui, crumb: str, value: str) -> str | None:
    """Ask for a new template id. A built-in id overrides that template in this hub (after a confirm)."""
    def check(v):
        if not TEMPLATE_ID_RE.match(v):
            return "Use a-z, 0-9, - and _; start with a letter or digit"
        if is_user_template(ui.hub, v):
            return f"You already have a template named {v}"
        return None
    got = input_line(win, ui, crumb + " / new template", "Template id (its file name in .herdr-team/templates)", value,
                     check, note="A built-in id (e.g. developer) overrides that template in this hub.")
    if got and got in TEMPLATES and not confirm_in(win, ui, crumb, f"{got} is a built-in template. Override it in this hub?"):
        return None
    return got


def edit_new_soul(win, ui: Ui, d: Draft) -> str:
    """Open a not-yet-saved bot's SOUL.md in the editor: the edited text if any, else the template.
    The text lives in the draft until the team is saved; the temp file is removed right away."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="SOUL-", delete=False, encoding="utf-8") as f:
        f.write(soul_text(ui.hub, d))
    path = Path(f.name)
    try:
        error = open_editor(win, path)
        text = _read(path)
    finally:
        path.unlink(missing_ok=True)
    if not error and text != soul_text(ui.hub, d):
        d.soul = text
        d.bot.role = soul_description(text)   # a purpose typed in the editor wins over the Purpose row
        ui.dirty = True
    return error


REMOVE = "remove"   # edit_bot's answer when the user picks "remove bot"


def edit_bot(win, ui: Ui, project: str, draft: Draft, others: list[str], touched: set[str]) -> Draft | str | None:
    """Edit a copy of the bot; returns it on 'done', REMOVE on 'remove bot', None when cancelled."""
    d = copy.deepcopy(draft)
    bot = d.bot
    cur, message = 0, ""
    while True:
        home = has_home(ui.hub, d)
        if home and d.reapply:
            soul = [(f"will re-apply the {d.reapply} template", "warn")]
        elif home:
            soul = [("SOUL.md", "")]
        else:
            t = d.template or default_template(bot.dirs)
            soul = [(f"{t} (edited)" if d.soul is not None else t, "")]
        # (kind, row): Enter acts on the kind, so hint lines can sit between rows without shifting anything.
        items = [
            ("name", [("Name          ", "dim"), (bot.name, f"bot:{bot.name}")]),
            ("folders", [("Folders       ", "dim"), (where(bot.dirs), "")]),
            ("purpose", [("Purpose       ", "dim"), (bot.role, "")] if bot.role else [("Purpose       ", "dim"), ("(add one sentence)", "warn")]),
            ("soul", [("Instructions  ", "dim"), *soul]),
            # Short enough for 50 columns: 32 cells right of the indent.
            ("", hint_row(14, [("e", "edit"), ("Enter", "re-apply template" if home else "change template")])),
            ("triggers", [("Triggers      ", "dim"), (f"{len(bot.triggers)}", ""),
                          (f"  {', '.join(t.name for t in bot.triggers)}" if bot.triggers else "", "dim")]),
            ("", []),
            ("done", [("✓ done", "ok")]),
            ("remove", [("✕ remove agent", "warn")]),
        ]
        kinds, rows = [k for k, _ in items], [r for _, r in items]
        crumb = f"team / {bot.name or 'new agent'}"
        key, cur = select(win, ui, crumb, "Edit agent", rows,
                          [("Enter", "edit"), ("e", "open in editor"), ("s", "done"), ("Esc", "cancel")],
                          keys="se", cur=cur, preview=lambda _: ("Preview", bot_preview(ui.hub, d)), message=message)
        message = ""
        if key == "esc":
            return None
        kind = kinds[cur]
        if key == "enter" and kind == "remove":
            return REMOVE
        if key == "s" or (key == "enter" and kind == "done"):
            err = bot_name_error(project, bot.name, others)
            if err:
                cur, message = kinds.index("name"), err
                continue
            return d
        if key == "e":
            if not home:
                message = edit_new_soul(win, ui, d)
            else:
                message = open_editor(win, bot_home(ui.hub, d.origin) / "SOUL.md")
                touched.add(d.origin)
                ui.dirty = True
            continue
        if key != "enter":
            continue
        if kind == "name":
            got = input_line(win, ui, crumb + " / name", "Agent name (e.g. api, AcmeWeb, reviewer)", bot.name,
                             lambda v: bot_name_error(project, v, others))
            if got is not None:
                bot.name = got
        elif kind == "folders":
            got = edit_folders(win, ui, crumb + " / folders", bot.name, bot.dirs)
            if got is not None:
                bot.dirs = got
        elif kind == "purpose":
            got = input_line(win, ui, crumb + " / purpose", "What is this agent for? (one sentence; it opens SOUL.md)", bot.role)
            if got is not None:
                bot.role = got
        elif kind == "soul" and not home:
            got = pick_template(win, ui, crumb + " / template", bot.name, d.template or default_template(bot.dirs))
            if got is not None and (d.soul is None or confirm_in(
                    win, ui, crumb, f"Discard your edits to SOUL.md and start over from the {got} template?")):
                d.template, d.soul = got, None
                bot.role = default_purpose(ui.hub, bot.role, got)
        elif kind == "soul":
            got = pick_template(win, ui, crumb + " / re-apply template", bot.name, d.reapply or default_template(bot.dirs))
            if got is not None and confirm_in(win, ui, crumb, f"Replace the template part of SOUL.md (How I work, Never) with the {got} "
                                                              "template? The title, purpose and ## Extra stay."):
                d.reapply = got
        elif kind == "triggers":
            edit_triggers(win, ui, crumb, bot)


def new_bot(win, ui: Ui, project: str, others: list[str]) -> Draft | None:
    """Step form: name → folders → template → purpose. Esc goes back a step."""
    names = ["Name", "Folders", "Template", "Purpose"]
    crumb = "team / new agent"

    def name_step(vals, i):
        return input_line(win, ui, crumb, "Agent name (e.g. api, AcmeWeb, reviewer)", vals[0] or "",
                          lambda v: bot_name_error(project, v, others), progress=(i, names, vals))

    def dirs_step(vals, i):
        return edit_folders(win, ui, crumb, vals[0], vals[1] or [], progress=(i, names, [vals[0], None, None, None]))

    def template_step(vals, i):
        shown = [vals[0], where(vals[1]), None, None]
        return pick_template(win, ui, crumb, vals[0], vals[2] or default_template(vals[1]), progress=(i, names, shown))

    def role_step(vals, i):
        guess = guess_role(resolve_repo(ui.hub, vals[1][0])) if vals[1] else ""
        shown = [vals[0], where(vals[1]), vals[2], None]
        value = vals[3] if vals[3] and not is_default_purpose(ui.hub, vals[3]) else guess or template_purpose(vals[2], ui.hub)
        return input_line(win, ui, crumb, "What is this agent for? (one sentence; shown in the roster and on the board)", value,
                          note="Leave it empty to fill in later.", progress=(i, names, shown))
    got = run_steps([name_step, dirs_step, template_step, role_step])
    return Draft(Bot(got[0], got[1], got[3]), got[2], None) if got else None


def review(win, ui: Ui, rows: list[list]) -> bool:
    key, _ = select(win, ui, "team / save", "Review before saving", rows, [("s", "save"), ("Enter", "save"), ("Esc", "back")],
                    keys="s")
    return key in ("s", "enter")


@dataclass
class Saved:
    team: Team
    changed: list[str]          # bots to restart for the changes to take effect
    renamed_project: bool
    instructions_changed: bool
    gone: list[str]             # old names no longer in the team (removed or renamed); their agents may still run
    master_changed: bool = False   # master.md


def team_screen(win, hub: Path) -> Saved | None:
    win.keypad(True)
    init_colors()
    existing = load_team(hub)
    project = existing.name if existing else slugify(hub.name)
    if existing:
        drafts = [Draft(b, None, b.name) for b in existing.bots]
    else:   # first run: one bot per repo found, ready to edit or remove
        drafts = [Draft(b, None, None) for b, on in draft_bots(hub, None) if on]
        for d in drafts:   # no package description: the template's default purpose, editable
            d.bot.role = d.bot.role or template_purpose(default_template(d.bot.dirs), hub)
    suggested = existing is None   # first run: the bots are only suggestions until saved
    agent_args = existing.agent_args if existing else []
    ui = Ui(hub, project, dirty=existing is None)
    instructions_path = hub / ".herdr-team" / "instructions.md"
    instructions_before = user_instructions(hub)
    master_before = user_instructions(hub, "master.md")
    touched: set[str] = set()
    purge: set[str] = set()   # homes to delete when the team is saved

    def remove(i: int) -> None:
        """d on the list or "remove bot" in the bot editor. Deleting the home is asked separately (default no)."""
        d = drafts[i]
        if not confirm_in(win, ui, "team / remove", f"Remove {d.bot.name} from the team?"):
            return
        if has_home(hub, d) and confirm_in(win, ui, "team / remove",
                                           f"Also delete its home folder (SOUL.md, MEMORY.md)? This cannot be undone."):
            purge.add(d.origin)
        del drafts[i]
        ui.dirty = True
    first = 4   # rows: project, team instructions, master, separator, bots..., add, separator, save
    cur, message = first, ""
    while True:
        ui.project, ui.names = project, [d.bot.name for d in drafts]
        instr, master = user_instructions(hub), user_instructions(hub, "master.md")
        rows = [[("Project       ", "dim"), (project, "accent")],
                [("Instructions  ", "dim"), (f"{len(instr.splitlines())} line{'s' if len(instr.splitlines()) != 1 else ''}" if instr else "empty", "" if instr else "dim"),
                 ("  for the master and all agents", "dim")],
                [("Master        ", "dim"), (f"{len(master.splitlines())} line{'s' if len(master.splitlines()) != 1 else ''}" if master else "empty",
                                            "" if master else "dim"),
                 ("  for the master only", "dim")],
                []]
        rows += [[(clip(d.bot.name, 14) + " ", f"bot:{d.bot.name}"), (clip(where(d.bot.dirs), 16) + " ", "dim"),
                  (d.bot.role or "(no purpose)", "" if d.bot.role else "warn"),
                  (f"  triggers {len(d.bot.triggers)}" if d.bot.triggers else "", "dim")] for d in drafts]
        add_row = first + len(drafts)
        save_row = add_row + 2
        rows += [[("+ add agent", "accent")], [], [("✓ save and quit", "ok")]]

        def preview(i):
            if first <= i < add_row:
                body = bot_preview(hub, drafts[i - first])   # name, purpose, then details: the hint goes high
                return "Preview", body[:2] + [[("Enter edit (name, folders, purpose) · d remove", "dim")]] + body[2:]
            if i == 0:
                return "Project", [[("Used to find this team's workspace in herdr.", "dim")], [],
                                    [("Hub            ", "dim"), (str(hub), "")],
                                    [("agent_args     ", "dim"), (" ".join(agent_args) or "none", "")]]
            if i == 2:
                return "Master", master_preview(hub, project)
            if i == 1:
                body = [[(ln, "")] for ln in instr.splitlines()] or [[("Empty for now.", "dim")]]
                return "Team instructions", body + [[], [("Sent to the master and all agents as user instructions.", "dim")],
                                               [("Enter or e opens it in your editor. e.g. run tests before reporting.", "dim")]]
            return "Team", [[(f"{len(drafts)} agent{'s' if len(drafts) != 1 else ''}", "bold")], [],
                          [("Per-agent instructions: template + ## Extra in SOUL.md.", "dim")],
                          [("Instructions for everyone: Instructions row above.", "dim")]]
        key, cur = select(win, ui, "team", "Team", rows,
                          # Most-needed keys first: a narrow footer cuts from the right.
                          [("Enter", "edit"), ("d", "remove"), ("n", "add"), ("s", "save"), ("Esc", "quit"),
                           ("e", "instructions")],
                          keys="ndse", cur=cur, preview=preview, message=message,
                          note="Suggested agents — d remove · Enter edit · n add" if suggested and drafts
                          else "" if drafts else "No agents yet. Press n to add one (an agent needs no repo).")
        message = ""
        names = [d.bot.name for d in drafts]
        if key == "esc":
            if not ui.dirty or confirm_in(win, ui, "team / quit", "Quit and discard unsaved team changes (agents, folders, names)? "
                                                                 "Files you edited with e are already saved."):
                return None
        elif key == "s" or (key == "enter" and cur == save_row):
            problems = team_problems(project, [d.bot for d in drafts])
            if problems:
                message = "Can't save: " + problems[0]
                continue
            instructions_changed = user_instructions(hub) != instructions_before
            if not review(win, ui, change_summary(existing, project, drafts, touched, instructions_changed, purge,
                                                  user_instructions(hub, "master.md") != master_before)):
                continue
            team = commit(hub, project, drafts, agent_args, purge)
            return Saved(team, changed_bots(existing, drafts, touched, instructions_changed),
                         bool(existing) and existing.name != project, instructions_changed,
                         [b.name for b in existing.bots if b.name not in {d.bot.name for d in drafts}] if existing else [],
                         bool(existing) and user_instructions(hub, "master.md") != master_before)
        elif cur == 2 and key in ("enter", "e"):   # Master row: master.md, like the Instructions row
            message = open_editor(win, ensure_instructions(hub, "master.md"))
            if user_instructions(hub, "master.md") != master_before:
                ui.dirty = True
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
            remove(cur - first)
        elif key == "enter" and cur == 0:
            got = input_line(win, ui, "team / project", "Project name (lowercase; names this team's workspace in herdr)", project, project_error)
            if got and got != project:
                project, ui.dirty = got, True
        elif key == "enter" and first <= cur < add_row:
            i = cur - first
            got = edit_bot(win, ui, project, drafts[i], names[:i] + names[i + 1:], touched)
            if got == REMOVE:
                remove(i)
            elif got:
                drafts[i], ui.dirty = got, True


def master_preview(hub: Path, project: str) -> list[list]:
    text = user_instructions(hub, "master.md")
    rows = [[("Instructions (master only)", "bold")]]
    rows += [[(ln, "")] for ln in text.splitlines()[:12]] or [[("Empty for now.", "dim")]]
    return rows + [[], [("Enter or e opens master.md in your editor.", "dim")],
                   [("Applies after /exit and team-up.", "dim")]]


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
        print("Quit without saving.")
        return 1
    team = saved.team
    print(f"Saved: {hub / 'herdr-team.toml'} ({len(team.bots)} agent{'s' if len(team.bots) != 1 else ''})")
    print(f"Team instructions: {hub / '.herdr-team' / 'instructions.md'} · agent instructions: {hub / '.herdr-team' / 'agents'}/<agent>/SOUL.md")
    try:
        live = team_mod.live_agents()
    except herdr.HerdrError:
        live = {}
    running = [n for n in [agent_name(team.name, "master"), *(agent_name(team.name, b.name) for b in team.bots)] if n in live]
    try:
        if not running:
            if saved.renamed_project:
                print("The project name changed. If a workspace with the old name is open, close it.")
            return team_mod.up(hub) if confirm("Start the team now?") else 0
        for name in saved.gone:
            if agent_name(team.name, name) in live:
                print(f"{name}: renamed or removed but still running. Type /exit in its pane.")
        if saved.instructions_changed or saved.master_changed:
            print("To give the master the changes, type /exit in its pane, then run the team-up action (suggested key prefix+shift+u).")
        if not saved.changed:
            return 0
        if confirm(f"Restart the changed agents? ({', '.join(saved.changed)})"):
            return team_mod.restart(hub, saved.changed)
        print(f"To apply later: herdr-team restart {' '.join(saved.changed)}")
        return 0
    except herdr.HerdrError as e:
        print(f"herdr error: {e.message}")
        return 1
