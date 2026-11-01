import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Keep the module away from the real state dir and any running Herdr session.
os.environ["HERDR_PLUGIN_STATE_DIR"] = tempfile.mkdtemp()
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = tempfile.mkdtemp()
os.environ["HERDR_BIN_PATH"] = "/nonexistent/herdr"

spec = importlib.util.spec_from_file_location("hq", os.path.join(ROOT, "bin", "hq.py"))
hq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hq)

try:
    import tomllib
except ImportError:
    tomllib = None

LAUNCHER = "/home/u/.local/state/herdr-hq/hq-badge"


class HideKey(unittest.TestCase):
    def test_matches_plain_byte_and_csi_u(self):
        for data in (b"\x1c", b"abc\x1c", b"\x1b[92;5u", b"\x1b[92;5:1u"):
            self.assertIsNotNone(hq.HIDE_KEY.search(data), data)

    def test_ignores_other_keys(self):
        for data in (b"\\", b"\x1b[91;5u", b"\x1b[92;3u", b"hello"):
            self.assertIsNone(hq.HIDE_KEY.search(data), data)


class Settings(unittest.TestCase):
    def test_env_file(self):
        parsed = hq.parse_env_file('# comment\n\nHQ_CWD = "~/code"\nHQ_CLAUDE_ARGS=--model opus\nnot a setting\n')
        self.assertEqual(parsed, {"HQ_CWD": "~/code", "HQ_CLAUDE_ARGS": "--model opus"})


class Sockets(unittest.TestCase):
    SESSIONS = [
        {"default": True, "name": "default", "running": True, "socket_path": "/c/herdr.sock"},
        {"default": False, "name": "hq", "running": True, "socket_path": "/c/sessions/hq/herdr.sock"},
    ]

    def test_main_from_default_session(self):
        self.assertEqual(hq.socket_paths(self.SESSIONS, "hq", None), ("/c/herdr.sock", "/c/sessions/hq/herdr.sock", True))

    def test_env_socket_wins_unless_it_is_hq(self):
        self.assertEqual(hq.socket_paths(self.SESSIONS, "hq", "/other.sock")[0], "/other.sock")
        self.assertEqual(hq.socket_paths(self.SESSIONS, "hq", "/c/sessions/hq/herdr.sock")[0], "/c/herdr.sock")

    def test_missing_hq_session(self):
        self.assertEqual(hq.socket_paths(self.SESSIONS[:1], "hq", None), ("/c/herdr.sock", None, False))


class Prompt(unittest.TestCase):
    def test_bundled_prompt_renders_closed_tabs_path(self):
        with open(os.path.join(ROOT, "prompt.md")) as f:
            text = hq.render_prompt(f.read())
        self.assertIn(hq.CLOSED_TABS, text)
        self.assertIn("every 60 seconds", text)
        self.assertNotIn("{closed_tabs}", text)
        self.assertNotIn("{watch_interval}", text)

    def test_wrapper_points_at_main_socket(self):
        text = hq.shim_text("/c/my herdr.sock", "/opt/bin/herdr", "python3 /p/hq.py")
        self.assertIn("HERDR_SOCKET_PATH='/c/my herdr.sock'", text)
        self.assertIn("-u HERDR_PANE_ID", text)
        self.assertIn('/opt/bin/herdr "$@"', text)


class Wrapper(unittest.TestCase):
    """Run the real wrapper with a fake herdr and a fake hq.py."""

    def run_wrapper(self, *args, herdr_rc=0):
        d = tempfile.mkdtemp()
        fake = os.path.join(d, "herdr")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho \"herdr $*\"\nexit {herdr_rc}\n")
        os.chmod(fake, 0o755)
        wrapper = os.path.join(d, "wrapper")
        with open(wrapper, "w") as f:
            f.write(hq.shim_text("/main.sock", fake, "echo hq.py"))
        os.chmod(wrapper, 0o755)
        return subprocess.run([wrapper, *args], capture_output=True, text=True)

    def test_prompt_is_recorded_and_exit_code_kept(self):
        r = self.run_wrapper("agent", "prompt", "worker", "do it")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.splitlines(), ["herdr agent prompt worker do it", "hq.py dispatched worker"])

    def test_send_keys_is_recorded(self):
        r = self.run_wrapper("agent", "send-keys", "worker", "enter")
        self.assertIn("hq.py dispatched worker", r.stdout)

    def test_other_commands_are_not(self):
        r = self.run_wrapper("agent", "list")
        self.assertEqual(r.stdout.splitlines(), ["herdr agent list"])

    def test_failed_prompt_is_not_recorded(self):
        r = self.run_wrapper("agent", "prompt", "worker", "do it", herdr_rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("dispatched", r.stdout)


def agent(status, seq=1):
    return {"pane_id": "w1:p2", "name": "worker", "agent_status": status, "state_change_seq": seq}


class Watch(unittest.TestCase):
    def setUp(self):
        self.saved = hq.WATCH_DIR, hq.WATCH_INTERVAL, hq.main_agent, hq.busy_in_background
        hq.WATCH_DIR = tempfile.mkdtemp()
        hq.WATCH_INTERVAL = 0
        self.recfile, self.pidfile = hq.watch_files("w1:p2")
        self.checks = 0

    def tearDown(self):
        hq.WATCH_DIR, hq.WATCH_INTERVAL, hq.main_agent, hq.busy_in_background = self.saved

    def watch(self, *checks, busy=()):
        """Run hq-watch against scripted check results; `busy` lists checks with a background shell."""
        script = [agent("working"), *checks]

        def main_agent(target):
            self.checks += 1
            item = script.pop(0)
            return item() if callable(item) else item

        hq.main_agent = main_agent
        hq.busy_in_background = lambda pane: self.checks - 1 in busy
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hq.watch("worker")
        self.assertEqual(script, [], "watch stopped before using every scripted check")
        self.assertFalse(os.path.exists(self.pidfile))
        return out.getvalue()

    def test_idle_twice_means_finished(self):
        out = self.watch(agent("working"), agent("idle"), agent("done"))
        self.assertIn("idle on 2 checks in a row", out)
        self.assertFalse(os.path.exists(self.recfile))

    def test_working_between_idles_resets_the_count(self):
        out = self.watch(agent("idle"), agent("working"), agent("idle"), agent("idle"))
        self.assertIn("finished", out)

    def test_background_shell_keeps_it_busy(self):
        out = self.watch(agent("idle"), agent("idle"), agent("idle"), agent("idle"), busy=(1, 2))
        self.assertIn("finished", out)

    def test_blocked_is_reported_once(self):
        out = self.watch(agent("blocked", seq=5))
        self.assertIn("is blocked", out)
        self.assertEqual(hq.read_json(self.recfile)["blocked_seq"], 5)
        out = self.watch(agent("blocked", seq=5), agent("blocked", seq=5), agent("idle"), agent("idle"))
        self.assertIn("finished", out)
        out = self.watch(agent("blocked", seq=9))
        self.assertIn("is blocked", out)

    def test_gone_after_two_missed_checks(self):
        out = self.watch(None, None)
        self.assertIn("is gone", out)

    def test_new_task_restarts_the_count(self):
        def idle_then_new_task():
            hq.write_json(self.recfile, {"dispatched_at": 123.0})
            return agent("idle")
        # The new task arrives after the first idle check, so that one doesn't count.
        out = self.watch(idle_then_new_task, agent("idle"), agent("idle"))
        self.assertIn("finished", out)

    def test_footer_shells(self):
        screen = ("⏺ done · 1 shell still running\n" + "─" * 20 + "\n❯\n" + "─" * 20 +
                  "\n  Ctx 6%\n  ⏵⏵ auto mode on · 1 shell · ← 1 agent\n")
        self.assertTrue(hq.footer_lists_shells(screen))
        self.assertTrue(hq.footer_lists_shells(screen.replace("· 1 shell ·", "· 2 shells ·")))
        self.assertFalse(hq.footer_lists_shells(screen.replace(" · 1 shell ·", " ·")))

    def test_dispatched_records_and_reminds(self):
        hq.main_agent = lambda target: agent("working")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hq.dispatched("worker")
        self.assertIn("start `hq-watch worker`", err.getvalue())
        self.assertIn("dispatched_at", hq.read_json(self.recfile))
        hq.write_file(self.pidfile, str(os.getpid()))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hq.dispatched("worker")
        self.assertIn("already watching worker", err.getvalue())


@unittest.skipUnless(tomllib, "needs tomllib (Python 3.11+)")
class Config(unittest.TestCase):
    def add(self, text):
        out = hq.add_to_config(text, "me.hq", LAUNCHER, "ctrl+backslash")
        data = tomllib.loads(out)
        return out, data

    def assert_complete(self, data):
        self.assertIn({"key": "ctrl+backslash", "type": "plugin_action", "command": "me.hq.toggle",
                       "description": "show / hide HQ"}, data["keys"]["command"])
        self.assertIn(LAUNCHER, [e["command"] for e in data["ui"]["tab_bar_right"]])

    def test_empty_config(self):
        self.assert_complete(self.add("")[1])

    def test_existing_ui_table(self):
        out, data = self.add('[theme]\nname = "nord"\n\n[ui]\nsidebar_width = 30\n\n[ui.toast]\ndelivery = "herdr"\n')
        self.assert_complete(data)
        self.assertEqual(data["ui"]["sidebar_width"], 30)
        self.assertEqual(data["ui"]["toast"]["delivery"], "herdr")

    def test_only_ui_subtables(self):
        self.assert_complete(self.add('[ui.toast]\ndelivery = "herdr"\n')[1])

    def test_existing_tab_bar_right_is_left_alone(self):
        text = '[ui]\ntab_bar_right = [{ type = "hostname" }]\n'
        out, data = self.add(text)
        self.assertTrue(out.startswith(text))
        self.assertEqual(data["ui"]["tab_bar_right"], [{"type": "hostname"}])
        self.assertEqual(data["keys"]["command"][0]["command"], "me.hq.toggle")

    def test_key_taken_by_something_else(self):
        text = '[[keys.command]]\nkey = "ctrl+backslash"\ntype = "popup"\ncommand = "lazygit"\n'
        out, data = self.add(text)
        self.assertEqual(len(data["keys"]["command"]), 1)

    def test_already_set_up_is_unchanged(self):
        once = self.add("")[0]
        self.assertEqual(hq.add_to_config(once, "me.hq", LAUNCHER, "ctrl+backslash"), once)


class Manifest(unittest.TestCase):
    @unittest.skipUnless(tomllib, "needs tomllib (Python 3.11+)")
    def test_manifest_fields(self):
        with open(os.path.join(ROOT, "herdr-plugin.toml"), "rb") as f:
            m = tomllib.load(f)
        for key in ("id", "name", "version", "min_herdr_version"):
            self.assertTrue(m.get(key), key)
        self.assertEqual({a["id"] for a in m["actions"]}, {"toggle", "stop"})
        self.assertEqual({p["id"] for p in m["panes"]}, {"window", "setup"})


if __name__ == "__main__":
    unittest.main()
