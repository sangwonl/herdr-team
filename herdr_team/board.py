"""Status board: an animated curses view of agents. Team mode when a hub is given, all agents otherwise."""
import curses
import locale
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import herdr
from .core import (Team, agent_name, classify, clip, display_width, load_tasks, load_team,
                   read_log, transcript_digest, transcript_path)

# Only glyphs that terminal fonts draw exactly one cell wide: arrows, warning signs and emoji
# vary by font and push borders out of line.
GLYPH = {"working": "*", "blocked": "!", "reported": "✓", "done": "✓",
         "no_report": "x", "idle": "·", "unknown": "?"}
LABEL = {"reported": "보고서 도착", "no_report": "보고서 없음", "unknown": "실행 중이 아님"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CARD_W = 26
CARD_H = 6
FPS = 10


@dataclass
class Card:
    title: str
    sub: str
    status: str
    elapsed: str
    text: str
    target: str
    master: bool = False
    since: float = 0.0     # epoch when the open task was sent; 0 when there is none
    tool: str = ""         # last tool call, e.g. "Edit tile.rs"


# ---- pure --------------------------------------------------------------------

def fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def fmt_clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def spinner(t: float) -> str:
    return SPINNER[int(t * FPS + 1e-6) % len(SPINNER)]


def marquee(width: int, t: float) -> str:
    """A border segment with a bright run that travels left to right."""
    if width <= 0:
        return ""
    if width <= 2:
        return "━" * width
    run = max(1, min(4, width // 3))
    pos = int(t * 12) % (width + run)
    return "".join("━" if pos - run < i <= pos else "─" for i in range(width))


def bus_line(width: int, origin: int, centers: list[int]) -> str:
    """The line that fans out from the master (at column `origin`) to each card column."""
    if width <= 0 or not centers:
        return " " * max(0, width)
    clamp = lambda x: max(0, min(width - 1, x))
    origin, centers = clamp(origin), sorted({clamp(c) for c in centers})
    lo, hi = min(centers + [origin]), max(centers + [origin])
    out = [" "] * width
    for x in range(lo, hi + 1):
        out[x] = "─"
    for c in centers:
        out[c] = "┬"
    # An end that is also the origin needs an arm up to the master's tap.
    out[lo] = ("├" if lo == origin else "┌") if lo in centers else "└"
    out[hi] = ("┤" if hi == origin else "┐") if hi in centers else "┘"
    if lo == hi:
        out[lo] = "│"
    elif origin in centers and lo < origin < hi:
        out[origin] = "┼"
    elif lo < origin < hi:
        out[origin] = "┴"
    return "".join(out)


def tap_col(x: int, w: int, title: str) -> int | None:
    """Column where a connector meets the top border of a card at x, w wide: its middle, but never under
    the title (draw_card writes the ┴ only after it). None when the card is too narrow for one."""
    tw = min(display_width(title), max(0, w - 7))     # the title width draw_card keeps
    col = max(x + w // 2, x + 3 + tw + 2)
    return col if col <= x + w - 2 else None


def card_width(w: int, cols: int) -> int:
    return w if w <= CARD_W else (w - (cols - 1)) // cols


def connector_layout(w: int, titles: list[str]) -> tuple[int, list[int | None]]:
    """(origin, taps): the master's column and, per card in the first grid row, its tap column (or None).
    The origin sits midway between the outer taps, so the trunk meets the bus between or on cards, evenly."""
    cols = grid_columns(w, len(titles))
    cw = card_width(w, cols)
    taps = [tap_col(c * (cw + 1), cw, titles[c]) for c in range(min(cols, len(titles)))]
    real = [t for t in taps if t is not None]
    return ((real[0] + real[-1]) // 2 if real else w // 2), taps


BOLD_BUS = str.maketrans("─┬┴┼┌┐└┘├┤│", "━┳┻╋┏┓┗┛┣┫┃")


def palette_index(name: str, names: list[str]) -> int:
    """A stable color slot per bot: its position in the team, or a hash for a name outside it."""
    return names.index(name) if name in names else sum(name.encode()) % 6


def summary(cards: list["Card"]) -> dict[str, int]:
    counts = {"working": 0, "blocked": 0, "done": 0, "idle": 0}
    for c in cards:
        key = "done" if c.status in ("reported", "done") else c.status if c.status in counts else "idle"
        counts[key] += 1
    return counts


def grid_columns(width: int, n: int, card_w: int = CARD_W) -> int:
    return max(1, min(n, width // (card_w + 1)))


def log_height(n_log: int, grid_h: int, card_rows: int, want: int = 8) -> int:
    """Rows for the log box (incl. borders). Cards win: the log takes only what they leave."""
    if not n_log or want <= 0:
        return 0
    h = min(min(n_log, want) + 2, grid_h - card_rows * CARD_H)
    return h if h >= 3 else 0


def cards_all(agents: list[dict], workspaces: list[dict]) -> list[Card]:
    labels = {w.get("workspace_id"): w.get("label") for w in workspaces}
    cards = []
    for a in agents:
        ws = a.get("workspace_id", "")
        cards.append(Card(
            title=a.get("name") or labels.get(ws) or ws,
            sub=os.path.basename(str(a.get("cwd", "")).rstrip("/")),
            status=a.get("agent_status", "unknown"),
            elapsed="",
            text=a.get("terminal_title_stripped") or "",
            target=a.get("pane_id", ""),
        ))
    rank = {"blocked": 0, "working": 1}
    return sorted(cards, key=lambda c: rank.get(c.status, 2))  # stable: keeps input order within a rank


def cards_team(team: Team, agents: list[dict], tasks: dict[str, dict], now: float,
               report_exists: Callable[[str], bool], tools: dict[str, str] | None = None) -> list[Card]:
    """Master first, then the team's bots in spec order. Agents outside the team are ignored."""
    by_name = {a.get("name"): a for a in agents if a.get("name")}
    tools = tools or {}

    def card(title: str, sub: str, name: str, task: dict | None, master: bool = False) -> Card:
        agent = by_name.get(name)
        if agent is None:
            return Card(title, sub, "unknown", "", "실행 중이 아님", "", master)
        if task:
            status = classify(task, agent, report_exists(task.get("report", "")))
            return Card(title, sub, status, fmt_elapsed(now - task.get("sent_at", now)),
                        task.get("title", ""), agent.get("pane_id", ""), master,
                        since=float(task.get("sent_at", 0) or 0), tool=tools.get(name, ""))
        return Card(title, sub, agent.get("agent_status", "unknown"), "",
                    agent.get("terminal_title_stripped") or "", agent.get("pane_id", ""), master,
                    tool=tools.get(name, ""))

    cards = [card("master", team.name, agent_name(team.name, "master"), None, master=True)]
    for bot in team.bots:
        sub = os.path.basename(bot.dirs[0].rstrip("/")) if bot.dirs else "리포 없음"
        if len(bot.dirs) > 1:
            sub += f" +{len(bot.dirs) - 1}"
        cards.append(card(bot.name, sub, agent_name(team.name, bot.name), tasks.get(bot.name)))
    return cards


LOG_KINDS = {
    # kind: (icon, who it is from, who it is to, trailing note)
    "ask": ("→", "master", "{bot}", ""),
    "reported": ("✓", "{bot}", "master", "보고서 제출"),
    "no_report": ("x", "{bot}", "master", "보고서 없이 종료"),
    "blocked": ("!", "{bot}", "", "사용자 확인 필요"),
    "timeout": ("x", "{bot}", "", "시간 초과"),
    "up": ("+", "", "", "팀 시작"),
    "question": ("?", "{sender}", "{bot}", ""),
    "answer": ("←", "{bot}", "{sender}", "답변"),
    "nudge": ("»", "master", "{bot}", ""),
    "trigger": ("~", "{text}", "{bot}", ""),
    "trigger_done": ("~", "{bot}", "", "{text} 처리 완료"),
}


def log_rows(events: list) -> list[dict]:
    """Events as display rows: ts, icon, src, dst, text, kind. Unknown or malformed events are dropped."""
    rows = []
    for e in events:
        if not isinstance(e, dict) or e.get("kind") not in LOG_KINDS:
            continue
        icon, src, dst, note = LOG_KINDS[e["kind"]]
        fill = {"bot": str(e.get("bot", "")), "sender": str(e.get("from", "master")), "text": str(e.get("text", ""))}
        text = note.format(**fill) if note else fill["text"]
        if e["kind"] == "up":
            text = f"팀 시작 {fill['text']}".strip()
        elif e["kind"] == "trigger":
            text = "트리거"
        rows.append({"ts": str(e.get("ts", "")), "icon": icon, "src": src.format(**fill), "dst": dst.format(**fill),
                     "text": text, "kind": e["kind"]})
    return rows


def log_lines(events: list[dict], width: int) -> list[str]:
    """Plain-text log, one line per event (the colored board renders log_rows instead)."""
    formats = {
        "ask": "{ts} master → {bot}  {text}",
        "reported": "{ts} {bot} → master  보고서 제출",
        "no_report": "{ts} {bot} → master  보고서 없이 종료",
        "blocked": "{ts} {bot}  ⚠ 사용자 확인 필요",
        "timeout": "{ts} {bot}  시간 초과",
        "up": "{ts} 팀 시작 {text}",
        "question": "{ts} {sender} ? {bot}  {text}",
        "answer": "{ts} {bot} → {sender}  답변",
        "nudge": "{ts} master ⇢ {bot}  {text}",
        "trigger": "{ts} ⚡ {text} → {bot}",
        "trigger_done": "{ts} {bot}  ⚡ {text} 처리 완료",
    }
    lines = []
    for e in events:
        fmt = formats.get(e.get("kind"))
        if fmt is None:
            continue
        line = fmt.format(ts=e.get("ts", ""), bot=e.get("bot", ""), text=e.get("text", ""),
                          sender=e.get("from", "master")).rstrip()
        lines.append(clip(line, width) if display_width(line) > width else line)
    return lines


# ---- colors ------------------------------------------------------------------

# name: (256-color index, 8-color fallback). Mid-tones, so they read on light and dark backgrounds alike.
COLORS = {
    "working": (172, curses.COLOR_YELLOW), "blocked": (160, curses.COLOR_RED),
    "ok": (35, curses.COLOR_GREEN), "idle": (244, curses.COLOR_WHITE), "dim": (246, curses.COLOR_WHITE),
    "accent": (33, curses.COLOR_BLUE), "master": (136, curses.COLOR_YELLOW),
    "bot0": (32, curses.COLOR_BLUE), "bot1": (133, curses.COLOR_MAGENTA), "bot2": (71, curses.COLOR_GREEN),
    "bot3": (173, curses.COLOR_YELLOW), "bot4": (98, curses.COLOR_BLUE), "bot5": (37, curses.COLOR_CYAN),
}
STATUS_COLOR = {"working": "working", "blocked": "blocked", "reported": "ok", "done": "ok",
                "no_report": "blocked", "idle": "idle", "unknown": "dim"}
_pairs: dict[str, int] = {}


def init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    rich = curses.COLORS >= 256
    for i, (name, (c256, c8)) in enumerate(COLORS.items(), start=1):
        try:
            curses.init_pair(i, c256 if rich else c8, -1)
            _pairs[name] = i
        except curses.error:
            pass


def color(name: str, extra: int = 0) -> int:
    return (curses.color_pair(_pairs[name]) if name in _pairs else 0) | extra


def safe_addstr(win, y: int, x: int, s: str, attr: int = 0) -> None:
    """Write s at (y, x), cut to the window. Never raises on small or resized windows."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    room = w - x
    if display_width(s) > room:
        s = clip(s, room)
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass  # writing the bottom-right cell raises even though it succeeds


def put(win, y: int, x: int, s: str, attr: int = 0) -> int:
    """safe_addstr that returns the column after the text, for writing colored segments in a row."""
    safe_addstr(win, y, x, s, attr)
    return x + display_width(s)


# ---- drawing -----------------------------------------------------------------

class View:
    """What the loop remembers between frames."""
    def __init__(self) -> None:
        self.sel = -1                           # nothing is selected until an arrow key is pressed
        self.log_mode = 0                       # 0 normal, 1 large, 2 hidden
        self.changed: dict[str, float] = {}     # card title → when its status last changed
        self.last_status: dict[str, str] = {}
        self.log_seen = 0                       # how many log rows existed at the last change
        self.log_new_at = 0.0
        self.names: list[str] = []              # bot names, for stable colors


def name_color(view: View, name: str) -> int:
    if name == "master":
        return color("master", curses.A_BOLD)
    return color(f"bot{palette_index(name, view.names) % 6}", curses.A_BOLD)


LIGHT = "╭╮╰╯─│"
HEAVY = "┏┓┗┛━┃"


def draw_card(win, y: int, x: int, w: int, card: Card, selected: bool, t: float, view: View,
              tap_top: int | None = None, tap_bottom: int | None = None) -> None:
    """One card. tap_top / tap_bottom are screen columns where a connector line joins its border."""
    if w < 6:
        return
    inner = w - 2
    scolor = STATUS_COLOR.get(card.status, "idle")
    fresh = t - view.changed.get(card.title, -99) < 3          # status changed moments ago
    pulse = card.status == "blocked" and int(t * 2) % 2 == 0   # blocked cards blink
    tl, tr, bl, br, hz, vt = HEAVY if selected else LIGHT      # the selected card gets a heavy outline
    border = color("accent" if selected else scolor if card.status in ("working", "blocked") or fresh else "dim",
                   curses.A_BOLD if pulse or fresh or selected else 0)
    # top border with the name
    title = clip(card.title, min(display_width(card.title), max(0, inner - 5)))
    x2 = put(win, y, x, tl + hz + " ", border)
    x2 = put(win, y, x2, title, name_color(view, card.title))
    put(win, y, x2, " " + hz * max(0, x + w - 1 - x2 - 1) + tr, border)
    if tap_top is not None and x2 + 1 < tap_top < x + w - 1:
        safe_addstr(win, y, tap_top, "┷" if selected else "┴", border if selected else color("dim"))
    # body
    glyph = spinner(t) if card.status == "working" else GLYPH.get(card.status, "?")
    state = f"{glyph} {LABEL.get(card.status, card.status)}"
    clock = fmt_clock(time.time() - card.since) if card.since and card.status in ("working", "blocked") else card.elapsed
    rows = [
        (card.sub, color("dim")),
        (None, 0),
        (card.text, 0),
        (("└ " + card.tool) if card.tool else "", color("dim")),
    ]
    for i, (line, attr) in enumerate(rows):
        yy = y + 1 + i
        safe_addstr(win, yy, x, vt, border)
        safe_addstr(win, yy, x + w - 1, vt, border)
        if line is None:   # status line: state on the left, clock on the right
            safe_addstr(win, yy, x + 1, clip(" " + state, inner), color(scolor, curses.A_BOLD))
            if clock and inner - display_width(state) - 2 > len(clock):
                safe_addstr(win, yy, x + w - 2 - len(clock), clock, color("dim"))
        else:
            safe_addstr(win, yy, x + 1, clip(" " + line, inner), attr)
    # bottom border: a bright run travels along it while the agent works
    bottom = marquee(inner, t) if card.status == "working" and not selected else hz * inner
    safe_addstr(win, y + CARD_H - 1, x, bl + bottom + br, border)
    if tap_bottom is not None and x < tap_bottom < x + w - 1:
        safe_addstr(win, y + CARD_H - 1, tap_bottom, "┯" if selected else "┬", border if selected else color("dim"))


def draw_header(win, w: int, title: str, cards: list[Card], t: float) -> None:
    counts = summary(cards)
    x = put(win, 0, 1, "herdr-team", color("dim"))
    x = put(win, 0, x, f" · {title}", curses.A_BOLD) + 3
    for key, cname in (("working", "working"), ("blocked", "blocked"), ("done", "ok"), ("idle", "idle")):
        if counts[key]:
            glyph = spinner(t) if key == "working" else GLYPH[key]
            x = put(win, 0, x, f"{glyph} {counts[key]} {key}", color(cname, curses.A_BOLD)) + 3
    clock = time.strftime("%H:%M:%S")
    if w - len(clock) - 1 > x:   # no room on a narrow pane: the counts matter more than the time
        safe_addstr(win, 0, w - len(clock) - 1, clock, color("dim"))


def draw_log(win, y: int, w: int, h: int, rows: list[dict], t: float, view: View) -> None:
    border = color("dim")
    safe_addstr(win, y, 0, "╭─ ", border)
    x = put(win, y, 3, "activity", color("accent", curses.A_BOLD))
    put(win, y, x, " " + "─" * max(0, w - x - 2) + "╮", border)
    shown = rows[-(h - 2):]
    new_from = len(shown) - (len(rows) - view.log_seen) if t - view.log_new_at < 3 else len(shown)
    for i, r in enumerate(shown):
        yy = y + 1 + i
        fresh = curses.A_BOLD if i >= new_from else 0
        safe_addstr(win, yy, 0, "│", border)
        safe_addstr(win, yy, w - 1, "│", border)
        kcolor = {"reported": "ok", "blocked": "blocked", "no_report": "blocked", "timeout": "blocked",
                  "question": "accent", "answer": "accent", "nudge": "working", "trigger": "working",
                  "trigger_done": "ok", "up": "dim"}.get(r["kind"], "idle")
        x = put(win, yy, 2, r["ts"] + " ", color("dim"))
        x = put(win, yy, x, r["icon"] + " ", color(kcolor, curses.A_BOLD))
        if r["src"]:
            x = put(win, yy, x, r["src"], name_color(view, r["src"]) if r["kind"] != "trigger" else color("dim"))
        if r["dst"]:
            x = put(win, yy, x, " → ", color("dim"))
            x = put(win, yy, x, r["dst"], name_color(view, r["dst"]))
        gap = 2 if r["src"] or r["dst"] else 0
        room = w - 1 - x - gap
        if r["text"] and room > 3:
            plain = r["kind"] in ("ask", "question", "nudge")   # what someone said reads as normal text
            put(win, yy, x + gap, clip(r["text"], room).rstrip(), fresh | (0 if plain else color("dim")))
    safe_addstr(win, y + h - 1, 0, "╰" + "─" * max(0, w - 2) + "╯", border)


def draw(win, title: str, master: Card | None, cards: list[Card], rows: list[dict], error: str,
         t: float, view: View) -> int:
    """Draw one frame. `view.sel` indexes [master] + cards. Returns the grid column count."""
    win.erase()
    h, w = win.getmaxyx()
    everything = ([master] if master else []) + cards
    for c in everything:   # remember when each card's status changed, to flash it
        if view.last_status.get(c.title) not in (None, c.status):
            view.changed[c.title] = t
        view.last_status[c.title] = c.status
    if len(rows) != view.log_seen and t - view.log_new_at >= 3:
        view.log_new_at = t
    draw_header(win, w, title, everything, t)
    y = 1
    offset = 1 if master else 0
    cols = grid_columns(w, len(cards))
    cw = card_width(w, cols)
    card_rows = (len(cards) + cols - 1) // cols
    foot = 1 + (1 if error else 0)
    connect = bool(master and cards and h - y - CARD_H - 2 - foot >= CARD_H)
    origin, taps = connector_layout(w, [c.title for c in cards]) if connect else (w // 2, [])
    centers = [tap for tap in taps if tap is not None]
    if master is not None:
        draw_card(win, y, 0, w, master, view.sel == 0, t, view, tap_bottom=origin if connect else None)
        y += CARD_H
    if connect:
        line = color("dim")
        bus = bus_line(w, origin, centers)
        safe_addstr(win, y, 0, bus, line)
        for card, cx in zip(cards, taps):
            if cx is None:
                continue
            safe_addstr(win, y + 1, cx, "│", line)
            if card.status == "working" and card.since:      # a heavy segment runs from the master to a working bot
                steps = abs(cx - origin) + 1
                pos = int(t * 20) % (steps + 8)
                hot = name_color(view, card.title)
                if pos < steps:
                    px = origin + (pos if cx >= origin else -pos)
                    safe_addstr(win, y, px, bus[px].translate(BOLD_BUS), hot)
                elif pos == steps:
                    safe_addstr(win, y + 1, cx, "┃", hot)
        y += 2
    grid_h = h - y - foot
    want = {0: 8, 1: max(8, h // 2), 2: 0}[view.log_mode]
    log_h = log_height(len(rows), grid_h, card_rows, want)
    visible_rows = max(1, (grid_h - log_h) // CARD_H)
    sel_row = max(0, view.sel - offset) // cols
    first_row = max(0, min(sel_row, card_rows - visible_rows)) if sel_row >= visible_rows else 0
    for i, card in enumerate(cards):
        r, c = divmod(i, cols)
        if first_row <= r < first_row + visible_rows:
            tap = taps[c] if connect and r == 0 and first_row == 0 and c < len(taps) else None
            draw_card(win, y + (r - first_row) * CARD_H, c * (cw + 1), cw, card, view.sel == i + offset, t, view,
                      tap_top=tap)
    if not everything:
        safe_addstr(win, 2, 1, "에이전트가 없습니다", color("dim"))
    if log_h:
        draw_log(win, h - log_h - foot, w, log_h, rows, t, view)
    if t - view.log_new_at >= 3:
        view.log_seen = len(rows)
    if error:
        safe_addstr(win, h - 2, 0, clip(" " + error, w), color("blocked", curses.A_BOLD))
    safe_addstr(win, h - 1, 0, clip(" ←↑↓→ 선택 · ⏎ 창으로 이동 · l 로그 크기 · q 닫기", w), color("dim"))
    win.refresh()
    return cols


# ---- live data ---------------------------------------------------------------

class Poller(threading.Thread):
    """Reads herdr and the hub's state once a second, off the drawing thread, so animation stays smooth."""
    def __init__(self, hub: Path | None) -> None:
        super().__init__(daemon=True)
        self.hub = hub
        self.title = "all agents"
        self.master: Card | None = None
        self.cards: list[Card] = []
        self.rows: list[dict] = []
        self.names: list[str] = []
        self.error = ""
        self._tools: dict[str, str] = {}
        self._tools_at = 0.0

    def _last_tools(self, agents: list[dict]) -> dict[str, str]:
        # Tailing each session record is the costly part; every three seconds is plenty.
        if time.monotonic() - self._tools_at >= 3:
            self._tools_at = time.monotonic()
            self._tools = {}
            for a in agents:
                session = (a.get("agent_session") or {}).get("value")
                if a.get("name") and session and a.get("agent_status") in ("working", "blocked"):
                    path = transcript_path(session, str(a.get("cwd", "")))
                    self._tools[a["name"]] = transcript_digest(path, max_bytes=48 * 1024)["last_tool"]
        return self._tools

    def poll(self) -> None:
        agents = herdr.agents()
        team = load_team(self.hub) if self.hub else None
        if team is None:
            self.title, self.master, self.rows, self.names = "all agents", None, [], []
            self.cards = cards_all(agents, herdr.workspaces())
            return
        cards = cards_team(team, agents, load_tasks(self.hub), time.time(), os.path.exists, self._last_tools(agents))
        self.title, self.names = team.name, [b.name for b in team.bots]
        self.master, self.cards = cards[0], cards[1:]
        self.rows = log_rows(read_log(self.hub, 200))

    def run(self) -> None:
        while True:
            try:
                self.poll()
                self.error = ""
            except herdr.HerdrError as e:
                self.error = e.message
            except Exception as e:   # a bad state file must not freeze the board
                self.error = f"{type(e).__name__}: {e}"
            time.sleep(1.0)


def _loop(win, hub: Path | None) -> int:
    curses.curs_set(0)
    init_colors()
    win.timeout(1000 // FPS)
    win.keypad(True)
    poller = Poller(hub)
    try:
        poller.poll()
    except Exception as e:
        poller.error = getattr(e, "message", str(e))
    poller.start()
    view = View()
    error = ""
    while True:
        master, cards = poller.master, poller.cards
        view.names = poller.names
        total = len(cards) + (1 if master else 0)
        view.sel = max(-1, min(view.sel, total - 1))
        cols = draw(win, poller.title, master, cards, poller.rows, error or poller.error, time.monotonic(), view)
        key = win.getch()
        if key == -1:
            continue
        error = ""
        if key in (ord("q"), 27):
            return 0
        if key in (curses.KEY_RIGHT, ord(">")):
            view.sel += 1
        elif key in (curses.KEY_LEFT, ord("<")):
            view.sel -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            view.sel += cols if not (master and view.sel <= 0) else 1
        elif key in (curses.KEY_UP, ord("k")):
            view.sel = max(0, view.sel - cols)
        elif key == ord("l"):
            view.log_mode = (view.log_mode + 1) % 3
        elif key in (curses.KEY_ENTER, 10, 13) and total and view.sel >= 0:
            target = (([master] if master else []) + cards)[view.sel].target
            if target:
                try:
                    herdr.run("agent", "focus", target)
                except herdr.HerdrError as e:
                    error = e.message


def open_pane(target_pane: str | None, hub: Path | None) -> dict:
    """Open the board as a split to the right of target_pane (or the focused pane)."""
    args = ["plugin", "pane", "open", "--plugin", "herdr-team", "--entrypoint", "board",
            "--placement", "split", "--direction", "right"]
    if target_pane:
        args += ["--target-pane", target_pane]
    if hub:
        args += ["--env", f"HERDR_TEAM_HUB={hub}"]
    return herdr.run(*args)


def run_board(hub: Path | None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        return curses.wrapper(_loop, hub)
    except KeyboardInterrupt:
        return 0
