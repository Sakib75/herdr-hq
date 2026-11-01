#!/usr/bin/env python3
"""HQ: one agent you talk to in a popup, which hands your requests to the agents in your tabs.

HQ is a Claude Code agent running in its own headless Herdr session, so it stays
out of your spaces and agents lists. A small `herdr` wrapper first on HQ's PATH
points its commands at your main session, while Herdr's Claude status hook keeps
reporting to HQ's own session.

Subcommands (Herdr runs these from herdr-plugin.toml unless noted):
  toggle   open the HQ popup, or close it if it's open (bind a key to this action)
  window   inside the popup: start HQ if needed, then attach to it
  setup    inside a popup: check requirements and offer to add the key and badge
           (`setup --check` only prints the checks)
  stop     stop HQ's session
  startup  refresh the badge launcher when Herdr starts
  badge    one status line for the tab bar (run through the launcher in the state dir)
  watch    HQ runs this in the background after handing an agent work; it checks
           the agent without using tokens and exits only when there's news
  dispatched  called by HQ's herdr wrapper after `agent prompt` / `agent send-keys`
"""
import datetime
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty

try:
    import tomllib
except ImportError:  # Python < 3.11: setup still works, it just can't double-check its edit.
    tomllib = None

AGENT = "hq"
WORKSPACE_LABEL = "HQ"
DEFAULT_KEY = "ctrl+backslash"
MIN_HERDR = (0, 9, 0)

HERDR = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"
ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "hq")
STATE = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "herdr-hq")
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or STATE
PIDFILE = os.path.join(STATE, "window.pid")
LASTFILE = os.path.join(STATE, "last_status")
SHIM_DIR = os.path.join(STATE, "bin")
LAUNCHER = os.path.join(STATE, "hq-badge")
CLOSED_TABS = os.path.join(STATE, "closed-tabs.md")
WATCH_DIR = os.path.join(STATE, "watch")
WATCH_MAX_HOURS = 12

# ctrl+\ as a plain byte, or as a CSI-u sequence when the app turned on the kitty keyboard protocol.
HIDE_KEY = re.compile(rb"\x1c|\x1b\[92;5(?::\d+)*u")
# Claude's mode line lists running background shells ("· 1 shell"); Herdr still calls that agent idle.
BACKGROUND = re.compile(r"· \d+ shells?\b")


def parse_env_file(text):
    """KEY=VALUE lines; blank lines and # comments are skipped, surrounding quotes dropped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("\"'")
    return out


def load_settings():
    settings = {"HQ_CWD": "~", "HQ_SESSION": "hq", "HQ_CLAUDE_ARGS": "", "HQ_WIDTH": "", "HQ_HEIGHT": "",
                "HQ_WATCH_INTERVAL": "60"}
    try:
        with open(os.path.join(CONFIG_DIR, "hq.env")) as f:
            settings.update(parse_env_file(f.read()))
    except OSError:
        pass
    for key in settings:
        if key in os.environ:
            settings[key] = os.environ[key]
    return settings


SETTINGS = load_settings()
SESSION = SETTINGS["HQ_SESSION"]
CWD = os.path.expanduser(SETTINGS["HQ_CWD"])
WATCH_INTERVAL = float(SETTINGS["HQ_WATCH_INTERVAL"])


def socket_paths(sessions, session_name, env_socket):
    """(main socket, HQ socket, HQ session running) from `herdr session list --json` output."""
    hq = next((s for s in sessions if s.get("name") == session_name), {})
    hq_socket = hq.get("socket_path")
    default = next((s.get("socket_path") for s in sessions if s.get("default")), None)
    main = env_socket if env_socket and env_socket != hq_socket else default
    return main, hq_socket, bool(hq.get("running"))


_sockets = None


def sockets(refresh=False):
    global _sockets
    if _sockets is None or refresh:
        r = subprocess.run([HERDR, "session", "list", "--json"], capture_output=True, text=True)
        try:
            sessions = json.loads(r.stdout).get("sessions", [])
        except json.JSONDecodeError:
            sessions = []
        _sockets = socket_paths(sessions, SESSION, os.environ.get("HERDR_SOCKET_PATH"))
    return _sockets


def herdr(*args, socket=None):
    """Run a herdr CLI command against one session and return its parsed `result`, or None on error."""
    socket = socket or sockets()[0]
    if not socket:
        return None
    r = subprocess.run([HERDR, *args], capture_output=True, text=True, env=dict(os.environ, HERDR_SOCKET_PATH=socket))
    try:
        out = json.loads(r.stdout or r.stderr or "{}")
    except json.JSONDecodeError:
        return None
    return out.get("result")


def hq(*args):
    hq_socket = sockets()[1]
    return herdr(*args, socket=hq_socket) if hq_socket else None


def get_agent(target=AGENT):
    r = hq("agent", "get", target)
    return r and r.get("agent")


def pid_alive(path):
    try:
        with open(path) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def window_open():
    return pid_alive(PIDFILE)


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def write_file(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def hq_command():
    """This script run with the plugin's env, for scripts that run outside Herdr's plugin runner."""
    env = {k: os.environ[k] for k in ("HERDR_PLUGIN_ID", "HERDR_PLUGIN_CONFIG_DIR", "HERDR_PLUGIN_STATE_DIR")
           if k in os.environ}
    env["HERDR_PLUGIN_ROOT"] = ROOT
    assigns = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
    return f"env {assigns} python3 {shlex.quote(os.path.join(ROOT, 'bin', 'hq.py'))}"


def write_launcher():
    """A stable path for the tab-bar badge; it points at whichever install is current."""
    write_file(LAUNCHER, "#!/bin/sh\n"
                         "# Written by the HQ Herdr plugin so the tab-bar badge follows plugin updates.\n"
                         f"exec {hq_command()} badge\n", 0o755)


def shim_text(main_socket, herdr_bin, hq_cmd):
    main = f"HERDR_SOCKET_PATH={shlex.quote(main_socket)}"
    return ("#!/bin/sh\n"
            "# HQ runs in its own Herdr session; send its herdr commands to the main one.\n"
            f"env -u HERDR_PANE_ID -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID {main} {shlex.quote(herdr_bin)} \"$@\"\n"
            "rc=$?\n"
            "# Note when HQ hands an agent work, so hq-watch can follow it.\n"
            "if [ $rc -eq 0 ] && [ \"$1\" = agent ]; then\n"
            f"  case \"$2\" in prompt|send-keys) {main} {hq_cmd} dispatched \"$3\" ;; esac\n"
            "fi\n"
            "exit $rc\n")


def watch_shim_text(main_socket, hq_cmd):
    return f"#!/bin/sh\nexec env HERDR_SOCKET_PATH={shlex.quote(main_socket)} {hq_cmd} watch \"$@\"\n"


def write_scripts():
    """The badge launcher, plus HQ's `herdr` wrapper and `hq-watch`, all pointing at the current install."""
    write_launcher()
    main_socket = sockets()[0]
    if main_socket:
        write_file(os.path.join(SHIM_DIR, "herdr"), shim_text(main_socket, HERDR, hq_command()), 0o755)
        write_file(os.path.join(SHIM_DIR, "hq-watch"), watch_shim_text(main_socket, hq_command()), 0o755)


def render_prompt(template):
    return template.replace("{closed_tabs}", CLOSED_TABS).replace("{watch_interval}", f"{WATCH_INTERVAL:g}")


def write_prompt():
    """HQ's instructions: your own prompt.md in the plugin config dir if present, else the bundled one."""
    src = os.path.join(CONFIG_DIR, "prompt.md")
    if not os.path.exists(src):
        src = os.path.join(ROOT, "prompt.md")
    with open(src) as f:
        out = os.path.join(STATE, "prompt.md")
        write_file(out, render_prompt(f.read()))
    return out


def ensure_server():
    """Start HQ's headless Herdr session unless it's already up."""
    if hq("workspace", "list") is not None:
        return True
    # Drop Herdr pane vars and any Claude session markers, or HQ would think it's a child session.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "CLAUDE", "AI_AGENT"))}
    env["HERDR_SESSION"] = SESSION
    subprocess.Popen([HERDR, "server"], env=env, cwd=os.path.expanduser("~"), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        time.sleep(0.1)
        sockets(refresh=True)
        if hq("workspace", "list") is not None:
            return True
    return False


def ensure_agent():
    """Start the HQ agent in its own session unless it's already running."""
    if get_agent():
        return True
    if not ensure_server():
        return False
    # HQ's session holds nothing but HQ, so replace whatever is left in it.
    old = [w["workspace_id"] for w in (hq("workspace", "list") or {}).get("workspaces", [])]
    created = hq("workspace", "create", "--label", WORKSPACE_LABEL, "--cwd", CWD, "--no-focus")
    if not created:
        return False
    for w in old:
        hq("workspace", "close", w)
    pane = created["root_pane"]["pane_id"]
    write_scripts()
    args = ["--append-system-prompt-file", write_prompt(), *shlex.split(SETTINGS["HQ_CLAUDE_ARGS"])]
    # Launch through the shell rather than `agent start`, so the wrapper is first on PATH.
    time.sleep(1)
    hq("pane", "run", pane, f"PATH={shlex.quote(SHIM_DIR)}:$PATH claude {shlex.join(args)}")
    for _ in range(100):
        agent = get_agent(pane)
        if agent and agent.get("agent") == "claude":
            hq("agent", "rename", pane, AGENT)
            return get_agent() is not None
        time.sleep(0.3)
    return False


def toggle():
    write_scripts()
    if window_open():
        with open(PIDFILE) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        return
    size = []
    if SETTINGS["HQ_WIDTH"]:
        size += ["--width", SETTINGS["HQ_WIDTH"]]
    if SETTINGS["HQ_HEIGHT"]:
        size += ["--height", SETTINGS["HQ_HEIGHT"]]
    herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "window", *size)


def window():
    write_scripts()
    write_file(PIDFILE, str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        if not get_agent():
            sys.stdout.write("\x1b[2J\x1b[H\r\n  ◆ HQ: starting…\r\n")
            sys.stdout.flush()
            if not shutil.which("claude"):
                fail("Claude Code (`claude`) isn't on PATH. Install it, then try again.")
                return
            if not ensure_agent():
                fail(f"Couldn't start HQ. Run the setup check: "
                     f"herdr plugin pane open --plugin {PLUGIN_ID} --entrypoint setup")
                return
        attach()
    finally:
        remove(PIDFILE)


def fail(message):
    sys.stdout.write(f"\r\n  {message}\r\n\r\n  Press any key to close.\r\n")
    sys.stdout.flush()
    saved = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        os.read(0, 16)
    finally:
        termios.tcsetattr(0, termios.TCSAFLUSH, saved)


def attach():
    """Attach to the HQ terminal through a pty, so ctrl+\\ can close the popup."""
    hq_socket = sockets()[1]
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["HERDR_SOCKET_PATH"] = hq_socket
        os.execvp(HERDR, [HERDR, "agent", "attach", AGENT, "--takeover"])

    def sync_size(*_):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))

    sync_size()
    signal.signal(signal.SIGWINCH, sync_size)
    saved = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        while True:
            ready, _, _ = select.select([0, fd], [], [])
            if fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                os.write(1, data)
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    break
                hit = HIDE_KEY.search(data)
                if hit:
                    if hit.start():
                        os.write(fd, data[:hit.start()])
                    break
                os.write(fd, data)
    finally:
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        termios.tcsetattr(0, termios.TCSAFLUSH, saved)
        detach(pid, fd)


def detach(pid, fd):
    """Drop the attach client at once; HQ keeps running in its session.

    The client ignores both a written ctrl+b q and SIGTERM, and waiting for
    either made hiding take ~1.6 s, so go straight to SIGKILL.
    """
    os.close(fd)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass


def stop():
    subprocess.run([HERDR, "session", "stop", SESSION], capture_output=True)
    for path in (PIDFILE, LASTFILE):
        remove(path)


def startup():
    write_scripts()


# --- following up on handed-off work --------------------------------------------

def main_agent(target):
    r = herdr("agent", "get", target)
    return r and r.get("agent")


def busy_in_background(pane):
    """True if the agent is idle at its prompt but still has background shells running."""
    r = subprocess.run([HERDR, "agent", "read", pane, "--source", "visible", "--lines", "8"],
                       capture_output=True, text=True, env=dict(os.environ, HERDR_SOCKET_PATH=sockets()[0] or ""))
    return footer_lists_shells(r.stdout)


def footer_lists_shells(screen):
    """Look only below Claude's input box, so a past "1 shell still running" in the transcript doesn't count."""
    return bool(BACKGROUND.search(screen.rsplit("───", 1)[-1]))


def watch_files(pane_id):
    key = os.path.join(WATCH_DIR, re.sub(r"[^A-Za-z0-9_-]", "_", pane_id))
    return key + ".json", key + ".pid"


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    write_file(path + ".tmp", json.dumps(data))
    os.replace(path + ".tmp", path)


def dispatched(target):
    """Note that HQ just gave an agent work, and tell HQ how to follow it."""
    agent = main_agent(target)
    if not agent:
        return
    recfile, pidfile = watch_files(agent["pane_id"])
    rec = read_json(recfile)
    rec["dispatched_at"] = time.time()
    write_json(recfile, rec)
    name = agent.get("name") or agent["pane_id"]
    if pid_alive(pidfile):
        print(f"hq: already watching {name}; its result will come back to you.", file=sys.stderr)
    else:
        print(f"hq: now start `hq-watch {name}` with Bash run_in_background, so its result comes back to you "
              "without polling.", file=sys.stderr)


def watch(target):
    """Check an agent HQ handed work to, every WATCH_INTERVAL seconds, without using tokens.

    It prints one line and exits only when there is news, which wakes HQ:
    the agent was idle on 2 checks in a row (finished; watching stops), it is
    blocked on a prompt it hasn't reported yet, or it is gone.
    """
    agent = main_agent(target)
    if not agent:
        print(f"hq-watch: no agent {target} in the main session.")
        return
    pane = agent["pane_id"]
    name = agent.get("name") or pane
    recfile, pidfile = watch_files(pane)
    if pid_alive(pidfile):
        print(f"hq-watch: already watching {name}.")
        return
    write_file(pidfile, str(os.getpid()))
    every = f"{WATCH_INTERVAL:g} s"
    try:
        started = time.time()
        task = read_json(recfile).get("dispatched_at")
        idle = missing = 0
        status = agent.get("agent_status")
        while time.time() - started < WATCH_MAX_HOURS * 3600:
            time.sleep(WATCH_INTERVAL)
            rec = read_json(recfile)
            if rec.get("dispatched_at") != task:
                # HQ sent it something new since the last check: start counting again.
                task, idle = rec.get("dispatched_at"), 0
                continue
            agent = main_agent(pane)
            if not agent:
                missing += 1
                if missing >= 2:
                    remove(recfile)
                    print(f"hq-watch: {name} is gone (its tab closed or the agent exited). Not watching it any more.")
                    return
                continue
            missing = 0
            status = agent.get("agent_status")
            if status == "blocked":
                idle = 0
                if rec.get("blocked_seq") != agent.get("state_change_seq"):
                    rec["blocked_seq"] = agent.get("state_change_seq")
                    write_json(recfile, rec)
                    print(f"hq-watch: {name} is blocked on a permission prompt or question. Read it and tell the "
                          f"user; don't answer it yourself. Then start `hq-watch {name}` again.")
                    return
            elif status in ("idle", "done") and not busy_in_background(pane):
                idle += 1
                if idle >= 2:
                    remove(recfile)
                    print(f"hq-watch: {name} was idle on 2 checks in a row ({every} apart), so it's finished. "
                          f"Read it with `herdr agent read {name} --source recent-unwrapped --lines 80` and tell "
                          "the user what it found. Not watching it any more.")
                    return
            else:
                idle = 0
        print(f"hq-watch: {name} is still {status} after {WATCH_MAX_HOURS} h, so I stopped watching it. "
              f"Start `hq-watch {name}` again to keep watching.")
    finally:
        remove(pidfile)


def watching():
    try:
        names = os.listdir(WATCH_DIR)
    except OSError:
        return 0
    return sum(pid_alive(os.path.join(WATCH_DIR, n)) for n in names if n.endswith(".pid"))


def notify(title, body, sound):
    herdr("notification", "show", title, "--body", body, "--sound", sound)


def badge():
    main_socket, hq_socket, running = sockets()
    # HQ's session shares the config, so its own tab bar would run this too.
    if hq_socket and os.environ.get("HERDR_SOCKET_PATH") == hq_socket:
        return
    agent = get_agent() if running else None
    status = "open" if window_open() else (agent or {}).get("agent_status", "off")
    try:
        with open(LASTFILE) as f:
            last = f.read().strip()
    except OSError:
        last = ""
    write_file(LASTFILE, status)
    if last == "working" and status in ("done", "idle"):
        notify("◆ HQ finished", (agent or {}).get("terminal_title_stripped", "")[:100], "done")
    elif status == "blocked" and last != "blocked":
        notify("◆ HQ needs you", "Open HQ to answer it", "request")
    label = {
        "open": "open",
        "working": "● working",
        "blocked": "◐ needs you",
        "done": "✓ done",
        "idle": "○ idle",
        "unknown": "starting",
        "off": "off",
    }.get(status, status)
    n = watching()
    print(f"◆ HQ {label}" + (f" · watching {n}" if n else ""))


# --- setup -------------------------------------------------------------------

def config_path():
    return os.environ.get("HERDR_CONFIG_PATH") or os.path.expanduser("~/.config/herdr/config.toml")


def badge_entry(launcher):
    return f'{{ type = "command", command = {json.dumps(launcher)}, interval_seconds = 2, timeout_seconds = 3 }}'


def key_block(plugin_id, key):
    return ("\n# HQ: show or hide it (added by HQ setup)\n"
            "[[keys.command]]\n"
            f"key = {json.dumps(key)}\n"
            'type = "plugin_action"\n'
            f"command = {json.dumps(plugin_id + '.toggle')}\n"
            'description = "show / hide HQ"\n')


def config_state(text, plugin_id, launcher, key):
    """What's already in config.toml: (has HQ key, has HQ badge, key used by something else, can add badge)."""
    try:
        data = tomllib.loads(text) if tomllib else {}
    except tomllib.TOMLDecodeError:
        data = {}
    commands = (data.get("keys") or {}).get("command") or []
    has_key = any(c.get("command") == f"{plugin_id}.toggle" for c in commands) or (
        not tomllib and f'"{plugin_id}.toggle"' in text)
    key_taken = any(c.get("key") == key and c.get("command") != f"{plugin_id}.toggle" for c in commands)
    ui = data.get("ui") or {}
    has_badge = any(launcher in str(e.get("command", "")) for e in ui.get("tab_bar_right") or []) or (
        not tomllib and launcher in text)
    # Only touch configs we can edit safely: no tab_bar_right yet, and `ui` is either absent
    # or a plain [ui] table we can insert a line under.
    has_ui_header = re.search(r"(?m)^\s*\[ui\]\s*$", text) is not None
    can_add_badge = "tab_bar_right" not in text and (has_ui_header or "ui" not in data or set(ui) <= {
        k for k in ui if isinstance(ui[k], dict)})
    return has_key, has_badge, key_taken, can_add_badge


def add_to_config(text, plugin_id, launcher, key):
    """Return config text with HQ's key binding and badge added where they're missing."""
    has_key, has_badge, key_taken, can_add_badge = config_state(text, plugin_id, launcher, key)
    if not has_badge and can_add_badge:
        line = f"tab_bar_right = [{badge_entry(launcher)}]  # HQ badge (added by HQ setup)\n"
        m = re.search(r"(?m)^\s*\[ui\]\s*$\n?", text)
        if m:
            text = text[:m.end()] + line + text[m.end():]
        else:
            text = text.rstrip("\n") + "\n\n[ui]\n" + line
    if not has_key and not key_taken:
        text = text.rstrip("\n") + "\n" + key_block(plugin_id, key)
    return text


def herdr_version():
    r = subprocess.run([HERDR, "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout)
    return tuple(int(x) for x in m.groups()) if m else None


def config_diagnostics(path):
    r = subprocess.run([HERDR, "config", "check"], capture_output=True, text=True,
                       env=dict(os.environ, HERDR_CONFIG_PATH=path))
    return set((r.stdout + r.stderr).splitlines()) - {"config: ok", "config: issues found"}


def setup(check_only=False):
    write_launcher()
    out = sys.stdout.write
    out("\n  ◆ HQ setup\n\n")
    ok = True

    def line(mark, text):
        out(f"  {mark} {text}\n")

    version = herdr_version()
    if version and version >= MIN_HERDR:
        line("✓", f"Herdr {'.'.join(map(str, version))}")
    else:
        ok = False
        line("✗", f"Herdr {'.'.join(map(str, MIN_HERDR))} or newer is needed (found {version}). Run `herdr update`.")
    claude = shutil.which("claude")
    if claude:
        line("✓", f"Claude Code: {claude}")
    else:
        ok = False
        line("✗", "Claude Code (`claude`) isn't on PATH.")
    integ = subprocess.run([HERDR, "integration", "status"], capture_output=True, text=True).stdout
    m = re.search(r"(?m)^claude: (.*)$", integ)
    status = m.group(1) if m else "unknown"
    if status.startswith("installed") or status.startswith("ok") or status.startswith("up to date"):
        line("✓", f"Herdr's Claude integration: {status}")
    else:
        line("!", f"Herdr's Claude integration: {status}. HQ's status is more accurate with it: "
                  "`herdr integration install claude`")

    path = config_path()
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    has_key, has_badge, key_taken, can_add_badge = config_state(text, PLUGIN_ID, LAUNCHER, DEFAULT_KEY)
    line("✓" if has_key else "✗",
         "Shortcut bound to HQ" if has_key else
         f"No shortcut for HQ. {DEFAULT_KEY} is taken in your config; bind another key by hand." if key_taken else
         f"No shortcut for HQ yet ({DEFAULT_KEY} will be added).")
    line("✓" if has_badge else "✗",
         "Tab-bar badge" if has_badge else
         "No tab-bar badge yet." + ("" if can_add_badge else " Your [ui] tab_bar_right needs it added by hand."))
    if "delivery" not in text or re.search(r'(?m)^\s*delivery\s*=\s*"off"', text):
        line("i", 'Toasts are off ([ui.toast] delivery), so the "HQ finished / needs you" pop-ups won\'t show.')

    manual = []
    if not has_key and key_taken:
        manual.append(key_block(PLUGIN_ID, "<your key>"))
    if not has_badge and not can_add_badge:
        manual.append(f"\n# add to [ui] tab_bar_right:\n{badge_entry(LAUNCHER)}\n")
    if manual:
        out("\n  Add these to " + path + " by hand:\n" + "".join(manual).replace("\n", "\n    ") + "\n")

    new_text = add_to_config(text, PLUGIN_ID, LAUNCHER, DEFAULT_KEY)
    if check_only or new_text == text:
        out("\n  " + ("Ready. Press your shortcut to open HQ.\n" if ok and has_key and has_badge else "") + "\n")
        return
    out(f"\n  Add the missing lines to {path} now? A backup is saved first. [y/N] ")
    sys.stdout.flush()
    if not sys.stdin.readline().strip().lower().startswith("y"):
        out("\n  Nothing changed.\n")
        wait_key()
        return
    draft = path + ".hq-draft"
    write_file(draft, new_text)
    added_issues = config_diagnostics(draft) - (config_diagnostics(path) if text else set())
    if tomllib:
        try:
            tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as e:
            added_issues.add(str(e))
    if added_issues:
        os.remove(draft)
        out("\n  Herdr didn't accept the change, so nothing was saved:\n    " + "\n    ".join(sorted(added_issues)) + "\n")
        wait_key()
        return
    if text:
        backup = f"{path}.bak-hq-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(path, backup)
        out(f"\n  Backup: {backup}\n")
    os.replace(draft, path)
    subprocess.run([HERDR, "server", "reload-config"], capture_output=True)
    out("  Saved and reloaded. Press your shortcut to open HQ.\n")
    wait_key()


def wait_key():
    sys.stdout.write("\n  Press Enter to close.")
    sys.stdout.flush()
    sys.stdin.readline()


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    target = sys.argv[2] if len(sys.argv) > 2 else ""
    commands = {"toggle": toggle, "window": window, "stop": stop, "startup": startup, "badge": badge,
                "setup": lambda: setup(check_only="--check" in sys.argv),
                "watch": lambda: watch(target), "dispatched": lambda: dispatched(target)}
    if command not in commands:
        sys.exit(__doc__)
    commands[command]()
