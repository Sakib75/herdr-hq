# Changelog

## 0.1.0

First release.

- `ctrl+\` popup with a Claude Code agent that runs in its own Herdr session and stays out of your spaces and agents lists.
- Dispatcher prompt: requests about a tab's work go to that tab's agent. A new tab opens only when you ask for one.
- Closed-tabs log, so HQ can reopen a closed tab's Claude session with `--resume`.
- Follows up on its own: after handing an agent work, HQ starts `hq-watch`, a background check that uses no tokens and wakes HQ only when the agent finishes, gets blocked or is gone. An agent idle on 2 checks in a row stops being watched until HQ sends it a new task.
- Tab-bar badge, plus toasts when HQ finishes or needs you.
- Setup popup that checks requirements and adds the shortcut and badge to `config.toml`, validated by Herdr and backed up first.
- `hq.env` settings and an optional custom `prompt.md`.
