# herdr-team

https://github.com/user-attachments/assets/334d8fb5-1fd5-40d7-a9f9-793bd1237e1f

A [herdr](https://herdr.dev) plugin that runs a team of [Claude Code](https://claude.com/claude-code) agents across your repos. You talk to one **master** agent; it hands work to **bots**, each in its own tab, and collects their reports. A status board shows what everyone is doing. A bot can own one repo, look at several, or work with no repo at all.

**Requirements:** herdr 0.9.5+ (macOS or Linux), Python 3.11+ as `python3`, Claude Code. The editor, board and CLI are in English; agent prompts and bot templates are still in Korean.

## Install

```sh
herdr plugin install sangwonl/herdr-team
```

Then bind keys in `~/.config/herdr/config.toml` and run `herdr server reload-config`. Change the keys if they clash with your config.

```toml
[[keys.command]]
key = "prefix+t"
type = "plugin_action"
command = "herdr-team.team-board"
description = "team board"

[[keys.command]]
key = "prefix+shift+e"
type = "plugin_action"
command = "herdr-team.team-setup"
description = "edit team"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "herdr-team.team-up"
description = "start team"
```

The master and bots get `herdr-team` on their `PATH` automatically. Tabs that `up` creates get it too.

## Quick start

1. In herdr, open a pane in your hub: a folder that holds your repos (or is one).
2. `prefix+shift+e`: pick bots, then `s` to save. Each repo starts with one bot.
3. `prefix+shift+u`: opens the master, the board and one tab per bot.
4. Tell the master what you want, e.g. "ask api to add a /health endpoint".

The editor shows its own key hints. After saving, it offers to restart the bots that changed. To check a bot, press `Enter` on its card in the board (`prefix+t`) or use herdr's `prefix+g`.
Board keys: `↑` `↓` (or `j` `k`) move · `Enter` jump to that agent's pane · `l` log size · `q` close.

## Working with the master

- The master gives each bot one task at a time (`ask`), waits for the reports (`wait`), and can look in (`peek`), redirect (`nudge`) or just ask a question (`question`).
- Bots write results to `.herdr-team/reports/`, read each other's reports, and ask each other questions. Only the master hands out work.
- `.herdr-team/context.md` holds goals, contracts between repos and decisions. The master keeps it current, and every bot reads it before a task.
- Permissions granted at start: the master may run any `herdr-team` command. Bots may run only `question`, `roster` and `peek`, and may edit only their own home, `reports/` and `answers/`. Everything else goes through Claude Code's normal approval prompts.

## Instructions

Each bot's instructions come in three layers:

1. **Template**: the defaults written into a new bot's `.herdr-team/bots/<bot>/SOUL.md`. Templates: `developer`, `frontend`, `backend`, `devops`, `qa`, `security`, `data`, `repo-owner`, `reviewer`, `writer`, `researcher`, `blank`.
2. **Team-wide**: `.herdr-team/instructions.md` goes to every bot and the master (`e` on the editor's main screen). A file with only comments is ignored.
3. **Per bot**: the `## Extra` section of `SOUL.md`. It is kept when you re-apply a template.

Agents read these at start. Restart bots after a change. For the master, `/exit` it and start the team again.

## Commands

| Command | What it does |
|---|---|
| `herdr-team edit` | Team editor (same as `team-setup`) |
| `herdr-team up` | Start the team; skips what is already running (same as `team-up`) |
| `herdr-team board` | Status board (same as `team-board`) |
| `herdr-team restart [bot...]` | Restart bots (all if none given) to apply changes. Busy bots are skipped |
| `herdr-team ask <bot> "<task>"` | Give a bot a task (master only) |
| `herdr-team wait [bot...]` | Wait until open tasks finish or a bot gets blocked |
| `herdr-team peek <bot>` | A bot's state, task, last tool call and recent messages |
| `herdr-team nudge <bot> "<message>"` | Redirect a working bot (master only) |
| `herdr-team question <bot> "<question>"` | Ask and wait for the answer |
| `herdr-team roster` | Team members and their states |

Commands find the hub by looking up from the current folder, or use `HERDR_TEAM_HUB`. Exit codes: `0` ok · `1` herdr error · `2` refused, unknown or not-running bot, or a bot is blocked · `3` timed out.

## Triggers

Add triggers in the editor (bot → triggers). Each one runs its `run` command on a schedule; when the output changes, the bot gets a task.

## Security

- Triggers (`[[bots.triggers]]` in `herdr-team.toml`) run their `run` command with `sh -c` as you. Anyone who can write that file can run commands on your machine.
- Trigger output goes into the bot's prompt verbatim, so outside text such as PR titles can act as instructions.
- Only run teams from hubs you set up yourself.

## Development

```sh
python3 -m herdr_team selftest
```

## License

MIT. See [LICENSE](LICENSE).
