# HQ for Herdr

**One agent you talk to, a keypress away.** HQ is a Claude Code agent in a floating popup. You ask it things instead of jumping between tabs, and it hands each request to the agent already working on it.

<!-- demo GIF -->

- **One key.** `ctrl+\` shows HQ; press it again to hide it. HQ keeps running while hidden.
- **A dispatcher, not another worker.** If you ask about work a tab did, HQ passes it to that tab's agent. It starts a new tab only when you ask, and does the work itself only when no tab owns it.
- **Follows up on its own.** After HQ hands an agent work, a small background check looks at that agent every minute without using any tokens. HQ is only woken when there's news: the agent finished (idle on 2 checks in a row, then HQ stops checking it until it sends that agent a new task), it's waiting on a permission prompt or question, or it's gone. HQ then tells you what it found.
- **Out of your way.** HQ runs in its own Herdr session, so it never shows up in your spaces or agents lists. A badge at the top right of the tab bar shows its state.
- **Closed tabs aren't lost.** Before HQ closes a tab for you, it logs the Claude session, so "pick up the API refactor again" reopens it with `--resume`.

## Requirements

- macOS or Linux, with Herdr 0.9.0 or newer
- Python 3.9+ (standard library only)
- [Claude Code](https://docs.anthropic.com/claude-code) (`claude` on your PATH)
- Recommended: Herdr's Claude integration (`herdr integration install claude`), for accurate working / done / needs-you states

## Install

```sh
herdr plugin install Sakib75/herdr-hq
herdr plugin pane open --plugin sakib75.hq --entrypoint setup
```

The setup popup checks the requirements and offers to add two things to `~/.config/herdr/config.toml`: the `ctrl+\` shortcut and the tab-bar badge. It backs up your config, asks Herdr to validate the change, and reloads. To do it by hand instead, it prints the exact lines.

The first time HQ starts, Claude Code may ask whether you trust the folder; answer in the popup.

## Use

| Key | What it does |
|---|---|
| `ctrl+\` | Show or hide HQ. The first press starts it. |
| `ctrl+b q` | Also hides HQ. |
| `ctrl+b ctrl+b` | Sends a literal `ctrl+b` to Claude inside HQ. |

Things to ask:

- "What's running? What's blocked?" HQ reads your agents and answers.
- "Fix the failing test in the payments PR." HQ finds the tab that made that PR and hands the request to it.
- "Start a new tab in the api workspace and add rate limiting." HQ opens the tab, starts an agent and gives it the task.

The badge shows `◆ HQ` followed by `open`, `● working`, `◐ needs you`, `✓ done`, `○ idle` or `off`, plus `· watching N` while HQ is following up on N agents. With Herdr toasts on (`[ui.toast] delivery = "herdr"`), HQ also pops up a toast when it finishes or needs you.

## Settings

Put an `hq.env` file in the plugin's config folder (`herdr plugin config-dir sakib75.hq`):

```sh
HQ_CWD=~/code               # where HQ starts (default: your home folder)
HQ_CLAUDE_ARGS=--model opus # extra arguments for claude
HQ_SESSION=hq               # name of HQ's own Herdr session
HQ_WIDTH=70%                # popup size
HQ_HEIGHT=80%
HQ_WATCH_INTERVAL=60        # seconds between checks on agents HQ handed work to
```

To change how HQ behaves, copy `prompt.md` from this repo into the same config folder and edit it. `{closed_tabs}` is replaced with the path of the closed-tabs log. HQ reads its prompt when it starts: type `/exit` in HQ, then press `ctrl+\`.

## How it works

- `ctrl+\` runs the plugin's `toggle` action. That opens a Herdr popup running `bin/hq.py window`.
- The window starts HQ's own headless Herdr session (`hq`) and a Claude agent in it if they aren't running, then attaches to that agent through a small pty proxy. The proxy watches for `ctrl+\` and closes the popup at once.
- Inside HQ, `herdr` is a wrapper that points at your main session, so `herdr agent list`, `agent prompt` and `tab create` act on your real tabs. Herdr's Claude status hook still reports to HQ's own session.
- After `herdr agent prompt` or `agent send-keys`, the wrapper records the hand-off and reminds HQ to start `hq-watch <agent>` as a Claude Code background command. `hq-watch` reads the agent's Herdr state on each check. An agent that is idle at its prompt but still has a background shell running (`· 1 shell` in Claude's footer) counts as busy. When `hq-watch` exits with its one-line result, Claude Code wakes HQ.
- State lives in the plugin's state folder: `closed-tabs.md`, the rendered prompt, the wrapper and the badge launcher. A startup hook keeps the launcher pointed at the current install, so the badge survives plugin updates.

## Trust and safety

HQ can read every agent's screen in your Herdr session, send them prompts, and open tabs. Its rule never to approve another agent's permission prompt without asking you is an instruction in `prompt.md`, not something the code enforces. HQ runs with your normal Claude Code permission settings. Read `prompt.md` and `bin/hq.py` before installing; both are short.

## Limits

- Claude Code only, for now.
- No Windows: the popup proxy uses Unix ptys.
- Clicking outside the popup doesn't close it, because Herdr ignores clicks outside a popup. Use `ctrl+\`.
- `hq-watch` reads Herdr's agent states, so it follows agents Herdr can classify; an agent stuck in `unknown` is watched for up to 12 hours.
- If HQ's session stops (for example after a reboot), HQ starts fresh next time. Your other tabs aren't affected.

## Uninstall

```sh
herdr plugin action invoke stop --plugin sakib75.hq
herdr session delete hq
herdr plugin uninstall sakib75.hq
```

Then remove the HQ shortcut and badge lines from `config.toml`. Setup left a backup next to it.

## License

MIT
