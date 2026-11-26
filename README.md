# herdr-team

https://github.com/user-attachments/assets/334d8fb5-1fd5-40d7-a9f9-793bd1237e1f

A [herdr](https://herdr.dev) plugin that runs a team of [Claude Code](https://claude.com/claude-code) agents across your repos. You talk to one **master** agent; it hands work to its **agents**, each in its own tab, and collects their reports. A status board shows what everyone is doing. An agent can own one repo, look at several, or work with no repo at all.

**Requirements:** herdr 0.9.5+ (macOS or Linux), Python 3.11+ as `python3`, Claude Code. Setup, the board and the CLI are in English; agent prompts and templates are still in Korean.

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
key = "prefix+shift+s"
type = "plugin_action"
command = "herdr-team.team-setup"
description = "team setup"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "herdr-team.team-up"
description = "start team"
```

The master and its agents get `herdr-team` on their `PATH` automatically. Tabs that `up` creates get it too.

## Quick start

1. In herdr, open a pane in your hub: a folder that holds your repos (or is one).
2. `prefix+shift+s`: pick agents, then `s` to save. Each repo starts with one agent.
3. `prefix+shift+u`: opens the master, the board and one tab per agent.
4. Tell the master what you want, e.g. "ask api to add a /health endpoint".

Setup shows its own key hints. After saving, it offers to restart the agents that changed. To check an agent, press `Enter` on its card in the board (`prefix+t`) or use herdr's `prefix+g`.
In setup, `Tab` completes folder names when you type a folder path. To remove an agent, press `d` on it or pick `remove agent` in its editor. Setup then asks whether to delete its home folder (`SOUL.md`, `MEMORY.md`) too. The default is no, and the folder is deleted only when you save.
`up` finds the team's workspace by its label, the project name. If you rename the workspace in herdr, the next `up` creates a new one. To rename the team, change the project name in setup.
Board keys: `↑` `↓` (or `j` `k`) move · `Enter` jump to that agent's pane · `l` log size · `q` close.

## Working with the master

- The master gives each agent one task at a time (`ask`), waits for the reports (`wait`), and can look in (`peek`), redirect (`nudge`) or just ask a question (`question`).
- Agents write results to `.herdr-team/reports/`, read each other's reports, and ask each other questions. Only the master hands out work.
- `.herdr-team/context.md` holds goals, contracts between repos and decisions. The master keeps it current, and every agent reads it before a task.
- Permissions granted at start: the master may run any `herdr-team` command. Agents may run only `question`, `roster` and `peek`, and may edit only their own home, `reports/` and `answers/`. Everything else goes through Claude Code's normal approval prompts.

## Instructions

Each agent's instructions come in three layers:

1. **Template**: the defaults written into a new agent's `.herdr-team/agents/<agent>/SOUL.md`. Templates: `developer`, `frontend`, `backend`, `devops`, `qa`, `security`, `data`, `repo-owner`, `reviewer`, `writer`, `researcher`, `blank`.
   Each template has a default purpose, used when you leave Purpose empty.
   Your own templates go in `.herdr-team/templates/<id>.md` (`n` new, `c` copy, `e` edit, `d` delete in the template list). They use the same format: `# {name}`, then `{purpose: default purpose sentence}`, then `## How I work` and `## Never` bullets. A file with a built-in id replaces that template in this hub.
2. **Team-wide**: `.herdr-team/instructions.md` goes to the master and all its agents (`e` on setup's main screen). A file with only comments is ignored.
3. **Per agent**: the `## Extra` section of `SOUL.md`. It is kept when you re-apply a template.

The master also has instructions of its own: `.herdr-team/master.md` (the `Master` row on setup's main screen). They are added after the team-wide ones and override the master's built-in instructions. A file with only comments is ignored.

Extra Claude Code arguments go in `herdr-team.toml`. The top-level ones go to the master and every agent; `[master]` and each `[[agents]]` can add their own, which come after the defaults (for the same option, the later one wins). Edit them in the file; setup only shows them.

```toml
agent_args = ["--model", "sonnet"]          # default: the master and every agent

[master]
agent_args = ["--model", "opus"]            # the master only, after the default

[[agents]]
name = "reviewer"
agent_args = ["--model", "opus"]            # this agent only, after the default
```

`--append-system-prompt*` and `--system-prompt*` are not allowed in any `agent_args`: they would replace the team's instructions. Put extra instructions in `instructions.md`, `master.md` or the agent's `SOUL.md` instead.

Agents read these at start. Restart them after a change. For the master, `/exit` it and start the team again.

Hubs from older versions keep working: `[[bots]]` in `herdr-team.toml` is still read, and `.herdr-team/bots/` moves to `.herdr-team/agents/` on the next run.

## Commands

| Command | What it does |
|---|---|
| `herdr-team setup` | Team setup: create the team, add, change and remove agents and triggers (same as `team-setup`) |
| `herdr-team up` | Start the team; skips what is already running (same as `team-up`) |
| `herdr-team board` | Status board (same as `team-board`) |
| `herdr-team restart [agent...]` | Restart agents (all if none given) to apply changes. Busy agents are skipped |
| `herdr-team ask <agent> "<task>"` | Give an agent a task (master only) |
| `herdr-team wait [agent...]` | Wait until open tasks finish or an agent gets blocked |
| `herdr-team peek <agent>` | An agent's state, task, last tool call and recent messages |
| `herdr-team nudge <agent> "<message>"` | Redirect a working agent (master only) |
| `herdr-team question <agent> "<question>"` | Ask and wait for the answer |
| `herdr-team roster` | Team members and their states |

Commands find the hub by looking up from the current folder, or use `HERDR_TEAM_HUB`. Exit codes: `0` ok · `1` herdr error · `2` refused, unknown or not-running agent, or an agent is blocked · `3` timed out.

## Triggers

Add triggers in setup (agent → triggers). Each one runs its `run` command on a schedule; when the output changes, the agent gets a task. In `herdr-team.toml` a trigger looks like this:

```toml
[[agents.triggers]]
name = "new-prs"
every = "10m"                             # 30s, 10m, 2h or 1d
run = "gh pr list --json number,title"
prompt = "Review the pull requests that are new."
```

- `run` runs with `sh -c` in the hub folder every `every`. It is stopped after 120 seconds.
- The agent gets `prompt` and the output (up to 4000 characters) only when the command succeeds, prints something, and prints something different from last time.
- The first run only records the output to compare against. Nothing is sent.
- If the agent is busy, the trigger tries again on the next run.
- When a trigger's task is done or needs your approval, the master is told.
- If any agent has triggers, `up` opens a `watch` tab that runs them. Close that tab and triggers stop; the next `up` opens it again. Trigger edits apply without restarting it.
- To send the task on every run, use a command whose output always changes, e.g. `run = "date +%s"`. This uses tokens on every run.

## Collaborators

Two teams on one machine can work together. Each lists the other in its `herdr-team.toml`:

```toml
[[collaborators]]
name = "api"                      # the other team's project name
hub = "/Users/me/work/api-hub"    # absolute path of the other hub
```

They are linked only when both sides list each other; otherwise `roster` says what is missing. Restart the team after a change.

- Can: see the other team in `roster`, ask it with `herdr-team question <team>/<agent|master> "..."`, and read its `.herdr-team/context.md` (agents are told to read it before work).
- Can't: `ask` or `nudge` across teams. Work for another team goes through that team's master.
- A repo both teams use is owned by one team. Only the owner's agents change it; the other team reads it and asks the owner for changes.

## Security

- Triggers (`[[agents.triggers]]` in `herdr-team.toml`) run their `run` command with `sh -c` as you. Anyone who can write that file can run commands on your machine.
- Trigger output goes into the agent's prompt verbatim, so outside text such as PR titles can act as instructions.
- Only run teams from hubs you set up yourself.

## Development

```sh
python3 -m herdr_team selftest
```

## License

MIT. See [LICENSE](LICENSE).
