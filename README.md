# herdr-team

Run a team of Claude Code agents across your repos: one **master** agent talks with you and hands work to **bots**, each working in its own terminal, and collects their reports. A status board shows what every agent is doing.

- [herdr](https://herdr.dev) is a terminal workspace manager for coding agents. herdr-team is a herdr plugin plus a small CLI built on it.
- [Claude Code](https://claude.com/claude-code) is the agent that runs in every master and bot pane.

A bot can own one repo, look at several, or work with no repo at all (research, writing, review).

## Requirements

- herdr 0.9.5+ (macOS or Linux)
- Claude Code (`claude` on `PATH`)
- Python 3.11+ as `python3` on `PATH`. Standard library only, nothing to `pip install`. The macOS system `python3` (3.9) is too old.

Note: the CLI messages, team editor and agent prompts are currently in Korean.

## Install

```sh
git clone https://github.com/<owner>/herdr-team.git
cd herdr-team
herdr plugin link "$PWD"
ln -s "$PWD/bin/herdr-team" ~/.local/bin/herdr-team   # ~/.local/bin must be on your PATH
herdr-team selftest
```

Optional key binding in `~/.config/herdr/config.toml`. The plugin has three actions: `team-setup`, `team-up`, `team-board`.

```toml
[[keys.command]]
key = "prefix+b"
type = "plugin_action"
command = "herdr-team.team-board"
description = "team board"
```

## Quick start

```sh
cd ~/src/acme            # the "hub": a folder that holds your repos (or is one)
herdr-team edit          # pick bots, then press s to save
herdr-team up            # opens the master, the board and one tab per bot
# now talk to the master in its pane: "ask api to add a /health endpoint"
```

## Set up and edit a team

Run `herdr-team edit` in the hub, or the `team-setup` action inside herdr (opens a popup). The same editor creates a team and changes it later. `init` is another name for `edit`. Nothing changes until you save with `s`.

- On first run, the hub itself (if it is a repo) and each repo directly under it get a bot. Remove the ones you don't need with `d`.
- **Add a bot** (`n`): name → folders → template → one-sentence purpose. Pick zero or more folders. With none, the bot works in its own home folder. Several bots may share a repo.
- **Edit a bot** (`Enter`): name, folders (`f` sets the working folder, `a` adds another path), purpose, triggers.
  Renaming moves the bot's home folder and updates the `SOUL.md` title. The purpose is the first paragraph of `SOUL.md`.
- **Delete a bot** (`d`): removes it from the team only. Its `SOUL.md` and `MEMORY.md` stay on disk.
- After saving, the editor offers to start the team. Bots that are already running keep the old settings until you run `herdr-team restart [bot...]`. Start newly added bots with `herdr-team up`.

### Team spec: `herdr-team.toml`

```toml
name = "acme"
agent_args = []                  # extra Claude Code arguments for every bot

[[bots]]
name = "api"
dirs = ["./api"]                 # first entry is the working folder; the rest are extra folders it can access

[[bots]]
name = "scout"
dirs = []                        # no repo: runs in its own home folder

[[bots.triggers]]                # optional, see "Triggers"
name = "review-requests"
every = "10m"
run = "gh pr list --repo acme/api --search 'review-requested:@me' --json number,title"
prompt = "There are new review requests. Review any PR you haven't seen yet."
```

### Bot home: `.herdr-team/bots/<bot>/`

| File | Written by | Contents |
|---|---|---|
| `SOUL.md` | you | Who the bot is. First paragraph after the title is its purpose (shown in the roster and board), then `## How I work` and `## Never` |
| `MEMORY.md` | the bot | Lasting facts useful in any conversation. Survives restarts |
| `PREAMBLE.md` | herdr-team | Rebuilt on every `up`: SOUL, roster, where shared context lives, working rules. Don't edit |

Templates for a new `SOUL.md`: `developer`, `repo-owner`, `reviewer`, `writer`, `researcher`, `blank`. New bots with folders default to `developer`, bots without folders to `researcher`. A template is only a draft: edit `SOUL.md` yourself. To apply a different template to an existing bot, choose "re-apply template" under the bot's instructions in the editor (title and purpose are kept, the other sections are replaced).

## Start the team

Run `herdr-team up` in the hub, or the `team-up` action. It creates a herdr workspace named after the project, with the master (`<project>-master`) and the board in the first tab and one tab per bot (`<project>-<bot>`). If any bot has triggers, it also opens a `watch` tab. Running it again is safe: it only starts what is missing.

- On a hub Claude Code hasn't seen before, it may ask whether to trust the folder. That agent shows as blocked. Answer once in its pane.
- If bots keep stopping for approvals, add `agent_args = ["--permission-mode", "acceptEdits"]`.

### Permissions granted at start

| Agent | Runs without asking | Edits without asking |
|---|---|---|
| master | any `herdr-team` command | `.herdr-team/context.md` |
| bot | `herdr-team question`, `roster`, `peek` | its own home, `.herdr-team/reports/`, `.herdr-team/answers/` |

Bots can also read `.herdr-team/` and the extra folders in `dirs`. Everything else goes through Claude Code's normal approval prompts.

## Working with the master

Talk to the master in its pane as usual. It drives the bots with the commands below. You can run them yourself from the hub too.

```sh
herdr-team ask api "add a /health endpoint"   # hand out work; returns immediately
herdr-team wait                               # block until open tasks finish
herdr-team peek api                           # what is api doing right now?
herdr-team nudge api "keep the response JSON" # correct course without a new task
herdr-team question web "which port does the dev server use?"
```

- One open task per bot. A bot writes its result to a report file, and `wait` prints `<bot> <state> <report path>` per line, with state `reported`, `no_report`, `blocked` or `working`.
- Bots don't hand each other work. If a bot needs something done in another bot's area, it writes that in its report and the master assigns it.

### Keeping bots in the loop

- **Shared context** `.herdr-team/context.md`: goals, contracts between repos, decisions. The master maintains it and every bot reads it before starting a task.
- **Roster**: every bot knows the other bots' names and purposes.
- **Reports**: bots can read each other's reports in `.herdr-team/reports/`.
- **Questions**: bots ask each other directly with `question`, even while the other bot is busy.

### Change a bot and check it: `edit` → `restart` → `attach`

Use `edit` to change a bot's folders, purpose, triggers or template, and `attach` to see for yourself that it came up right.

```sh
herdr-team edit            # Enter on the web bot → change its folders → s to save
herdr-team restart web     # apply to the running bot (for a newly added bot: herdr-team up)
herdr-team attach web      # jump to web's pane and check its first screen
herdr-team attach          # back to the master
```

- Saving alone doesn't change a running bot. `restart` applies it, and the bot starts a fresh conversation. Busy bots are skipped unless you pass `--force`.
- Inside herdr, `attach` just moves the view to that pane (`herdr agent focus`, same as `Enter` on the board). From a terminal outside herdr, it hands that terminal over to the agent (`herdr agent attach`), so the command doesn't return right away.

## Commands

Run from the hub (or any folder below it), or pass `--hub <folder>` before the command.

| Command | What it does |
|---|---|
| `herdr-team edit` (or `init`) | Team editor: add, change and remove bots and triggers |
| `herdr-team up [--no-focus]` | Start the team; skips what's already running. `--no-focus` keeps focus where it is. Master only |
| `herdr-team board` | Open the status board |
| `herdr-team roster` | Team members and each one's state |
| `herdr-team attach [bot]` | Go to a bot's session (the master if omitted) |
| `herdr-team restart [bot...] [--force]` | Restart bots (all if none given) to pick up changed `SOUL.md`, folders and settings. Starts a new conversation. `--force` also restarts busy bots. Master only |
| `herdr-team ask <bot> "<task>"` | Give a bot a task. Returns immediately. One open task per bot. Master only |
| `herdr-team wait [bot...] [--timeout SEC]` | Return when all open tasks finish, a bot newly blocks, or the timeout passes (default 1800) |
| `herdr-team peek <bot>` | Current state, open task, last tool call, last 3 messages |
| `herdr-team nudge <bot> "<message>"` | Send a course correction to a working bot. No new task. Master only |
| `herdr-team question <bot> "<question>" [--timeout SEC]` | Ask and wait for the answer (default 100 s). Bots use it too |
| `herdr-team watch` | Run triggers (keeps running; `up` starts it in a `watch` tab). Master only |
| `herdr-team selftest` | Run the built-in tests |

"Master only" means a bot calling it is refused. Your own shell counts as the master.

Exit codes: `0` success · `1` herdr error · `2` refused, unknown or not-running bot, or a bot is blocked · `3` timed out (`question`: no answer yet) · `130` interrupted with Ctrl-C.
Exceptions: `up` returns `1` if any agent fails to start, and `wait` returns `3` after repeated herdr errors.

## Triggers

Add `[[bots.triggers]]` to a bot and `herdr-team watch` runs the `run` command in the hub every `every`. When the command succeeds and its output is non-empty and different from last time, the bot gets `prompt` plus that output as a task.

- The first run only records a baseline; it doesn't send anything.
- If the bot is busy, the trigger is skipped and retried next interval.
- `watch` collects finished trigger tasks and tells the master the report path when the master is idle.
- `every` takes `30s`, `10m`, `2h`, `1d`.

## Security and trust boundary

- **`herdr-team.toml` is code.** Trigger `run` commands run through `sh -c` with your user's permissions, and `watch` re-reads the file every cycle. Anyone who can write that file can run shell commands on your machine. Only put commands you trust there, and don't run a team from a hub you didn't set up.
- **Trigger output becomes bot instructions.** Up to 4000 characters of `run` output go into the bot's prompt verbatim. If that output contains text from other people (PR titles, issue bodies, commit messages), they can steer the bot. Keep such bots' permissions narrow and review what they do.
- The pre-granted permissions above are the only ones herdr-team adds. `agent_args` such as `--permission-mode` widen them for every bot.

## Status board

Run `herdr-team board` or the `team-board` action.

- **Team mode** (opened in a hub): a summary line (working, blocked, done counts), the master, the team's bots, and a log at the bottom. Each bot card shows its folders, state, time since it got the task, what it's doing and its last tool call.
- Each bot has its own color, used in the log too.
- Working cards spin and show a moving highlight, with a dot traveling from the master to the bot. Blocked cards blink red. Cards whose state just changed and new log lines are highlighted for 3 seconds.
- **All-agents mode** (opened where there's no team spec): every agent on the herdr server, with its workspace name.

| Key | Action |
|---|---|
| Arrow keys (also `j` `k`) | Select a card |
| `Enter` | Go to that agent's pane |
| `l` | Log size (normal → large → hidden) |
| `q` / `Esc` | Close |

State symbols: `◐` working · `⚠` blocked · `✓` done / report arrived · `∅` no report · `·` idle · `?` unknown

Log notation: `master → bot` task · `bot → master` report · `A ? B` question · `B → A` answer · `master ⇢ bot` nudge · `⚡ name → bot` trigger

## Development

```sh
python3 -m herdr_team selftest
```

## License

MIT. See [LICENSE](LICENSE).
