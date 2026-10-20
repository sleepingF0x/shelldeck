"""Installed foreground command, real terminal, shared frpc configuration."""

import json
import os
from pathlib import Path
import socket
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest

from shelldeck_control import request
from test_shelldeck import RunningConsole, receive


ROOT = Path(__file__).resolve().parents[1]


class ForegroundLauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shelldeck-launch-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "settings 'one'.json"
        self.frpc = self.root / "client.toml"
        self.command = self.root / "bin/shelldeck 'one'"
        self.socket = self.root / "agent/control.sock"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.frpc.write_text('''serverAddr = "frp.example.net"
auth.token = "{{ .Envs.PRIVATE_FRP_TOKEN }}"
[[proxies]]
name = "existing"
type = "tcp"
localIP = "127.0.0.1"
localPort = %d
remotePort = 9099
transport.proxyProtocolVersion = "v1"
''' % self.port)
        self.config.write_text(json.dumps({"frpc_config": self.frpc.name, "proxy_name": "existing",
                                          "agent_socket": "agent/control.sock"}))

    def install(self, *extra):
        return subprocess.run([sys.executable, str(ROOT / "scripts/install.py"),
                               "--config", str(self.config), "--command-path", str(self.command), *extra],
                              capture_output=True, text=True, timeout=10, cwd=self.root,
                              env={**os.environ, "PATH": "/nonexistent", "XDG_CONFIG_HOME": str(self.root)})

    def test_mode_is_explicit_and_help_does_not_read_config_or_open_listener(self):
        self.assertEqual(self.install().returncode, 0)
        self.config.write_text("not JSON")
        for arguments in ([], ["-p", str(self.port)], ["--help"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(self.command), *arguments], capture_output=True,
                                        text=True, timeout=5,
                                        env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--remote", result.stdout)
                self.assertIn("--local", result.stdout)
        result = subprocess.run([str(self.command), "--remote", "--local"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed", result.stderr)
        self.assertFalse(self.socket.exists())
        self.assertFalse((self.root / "shelldeck").exists())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))

    def test_local_uses_shared_agent_settings_without_reading_frpc(self):
        self.assertEqual(self.install().returncode, 0)
        self.frpc.unlink()
        console = RunningConsole("--local", "-H", "127.0.0.1", "-p", "0",
                                 command=[str(self.command)], config_home=self.root,
                                 environment={"PATH": "/nonexistent"})
        self.addCleanup(console.close)
        self.assertIn(b"Mode: local", console.output)
        self.assertIn(f"Client endpoint: 127.0.0.1:{console.port}".encode(), console.output)
        peer = console.connect()
        peer.sendall(b"direct-payload\n")
        console.read_until(b"Session 1 connected")
        listed = request(self.socket, {"method": "sessions"})
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["sessions"][0]["peer"], f"127.0.0.1:{peer.getsockname()[1]}")
        console.attach(1)
        console.type(b"printf direct\r")
        self.assertEqual(receive(peer, 14), b"printf direct\n")

    def test_install_and_local_mode_work_without_any_frp_or_settings_file(self):
        self.config.unlink()
        self.frpc.unlink()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"),
                                 "--command-path", str(self.command)], capture_output=True, text=True,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root), "PATH": "/nonexistent"})
        self.assertEqual(result.returncode, 0, result.stderr)
        console = RunningConsole("--local", "-H", "127.0.0.1", "-p", "0",
                                 command=[str(self.command)], config_home=self.root,
                                 environment={"PATH": "/nonexistent"})
        self.addCleanup(console.close)
        default_socket = self.root / "shelldeck/agent/control.sock"
        self.assertTrue(request(default_socket, {"method": "sessions"})["ok"])
        result = subprocess.run([str(self.command), "--remote"], capture_output=True, text=True,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)}, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("config.json", result.stderr)
        self.assertIsNone(console.process.poll())

    def test_local_rejects_proxy_protocol_and_invalid_explicit_settings(self):
        self.assertEqual(self.install().returncode, 0)
        for arguments in (["--proxy-protocol", "v1"], ["--config", str(self.root / "missing.json")]):
            result = subprocess.run([str(self.command), "--local", *arguments],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
        for content in ("[]", "not JSON", '{"agent_socket": null}'):
            self.config.write_text(content)
            result = subprocess.run([str(self.command), "--local"], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.socket.exists())
        self.assertFalse((self.root / "shelldeck").exists())

    @unittest.skipUnless(shutil.which("bash"), "Bash is required")
    def test_switching_modes_keeps_names_notes_history_and_agent_socket(self):
        self.assertEqual(self.install().returncode, 0)
        previous_run = None
        for mode in ("remote", "local", "remote"):
            options = ["--remote"] if mode == "remote" else ["--local", "-H", "127.0.0.1", "-p", "0"]
            console = RunningConsole(*options, command=[str(self.command)], config_home=self.root)
            self.addCleanup(console.close)
            if previous_run is not None:
                result = request(self.socket, {"method": "result", "run_id": previous_run})
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["run"]["success"], result)
            peer = console.connect()
            if mode == "remote":
                peer.sendall(b"PROXY TCP4 127.0.0.1 203.0.113.20 41234 9099\r\n")
            console.read_until(b"Session 1 connected")
            if previous_run is None:
                console.command("name 1 retained-name")
                console.read_until(b"Name saved")
                console.command("note 1 retained-note")
                console.read_until(b"Note saved")
            listing = request(self.socket, {"method": "sessions"})
            self.assertEqual(listing["sessions"][0]["name"], "retained-name")
            self.assertEqual(listing["sessions"][0]["note"], "retained-note")
            child = subprocess.Popen([shutil.which("bash"), "--noprofile", "--norc"],
                                     stdin=peer, stdout=peer, stderr=peer, start_new_session=True)
            try:
                completed = subprocess.run([sys.executable, str(ROOT / "shelldeck_agent.py"), "--socket",
                                            str(self.socket), "exec", "--session", "1",
                                            "--instance", listing["instance"],
                                            "--request-id", "switch-" + listing["instance"],
                                            "--command", "printf " + mode, "--wait", "3"],
                                           capture_output=True, text=True, timeout=5)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                run = json.loads(completed.stdout)["run"]
                self.assertTrue(run["success"], run)
                self.assertEqual(run["output"], mode)
                previous_run = run["run_id"]
                console.command("quit")
                self.assertEqual(console.process.wait(timeout=5), 0)
                self.assertFalse(self.socket.exists())
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)

    def test_installed_command_runs_in_foreground_and_quit_releases_listener(self):
        original = self.frpc.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command.stat().st_mode & 0o777, 0o700)
        console = RunningConsole(command=[str(self.command), "--remote"], config_home=self.root,
                                 environment={"PATH": "/nonexistent"})
        self.addCleanup(console.close)
        self.assertEqual(console.port, self.port)
        self.assertIn(b"Mode: remote", console.output)
        self.assertIn(b"Client endpoint: frp.example.net:9099", console.output)
        self.assertTrue(request(self.socket, {"method": "sessions"})["ok"])
        console.command("quit")
        self.assertEqual(console.process.wait(timeout=5), 0)
        self.assertFalse(self.socket.exists())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))
        self.assertEqual(self.frpc.read_bytes(), original)
        self.assertFalse((self.root / "systemd").exists())

    def test_install_reuses_legacy_config_without_its_old_programs(self):
        legacy = self.root / "shelldeck-frp/service.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"frpc_config": str(self.frpc), "proxy_name": "existing",
                                      "agent_socket": str(self.socket),
                                      "tmux_binary": "/missing/tmux", "ssh_binary": "/missing/ssh",
                                      "launchctl_binary": "/missing/launchctl",
                                      "python_binary": "/missing/old-python",
                                      "frpc_binary": "/missing/frpc", "listen_port": 12345}))
        result = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"),
                                 "--command-path", str(self.command)], capture_output=True, text=True,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root), "PATH": "/nonexistent"})
        self.assertEqual(result.returncode, 0, result.stderr)
        console = RunningConsole(command=[str(self.command), "--remote"], config_home=self.root,
                                 environment={"PATH": "/nonexistent"})
        self.addCleanup(console.close)
        self.assertEqual(console.port, self.port)
        self.assertTrue(request(self.socket, {"method": "sessions"})["ok"])

    def test_config_discovery_happens_at_launch_and_explicit_config_takes_precedence(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"),
                                 "--command-path", str(self.command)], capture_output=True, text=True,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
        self.assertEqual(result.returncode, 0, result.stderr)
        current = self.root / "shelldeck/config.json"
        legacy = self.root / "shelldeck-frp/service.json"
        for path in (legacy, current):
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"agent_socket": "agent/control.sock"}')
            console = RunningConsole("--local", "-H", "127.0.0.1", "-p", "0",
                                     command=[str(self.command)], config_home=self.root)
            self.addCleanup(console.close)
            self.assertTrue(request(path.parent / "agent/control.sock", {"method": "sessions"})["ok"])
            console.stop()
        current.write_text("invalid JSON")
        result = subprocess.run([str(self.command), "--local"], capture_output=True, text=True, timeout=5,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
        self.assertEqual(result.returncode, 2)  # Do not silently fall back to legacy.
        self.assertEqual(self.install().returncode, 0)  # Bind self.config explicitly.
        override = self.root / "override.json"
        override.write_text('{"agent_socket": "override-agent/control.sock"}')
        console = RunningConsole("--local", "-H", "127.0.0.1", "-p", "0", "--config", str(override),
                                 command=[str(self.command)], config_home=self.root)
        self.addCleanup(console.close)
        self.assertTrue(request(self.root / "override-agent/control.sock", {"method": "sessions"})["ok"])
        self.assertFalse(self.socket.exists())

    def test_install_preserves_unrelated_command_and_can_update_its_own_wrapper(self):
        self.command.parent.mkdir()
        self.command.write_text("#!/bin/sh\nprintf other-program\n")
        original = self.command.read_bytes()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command.read_bytes(), original)
        self.command.unlink()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        original = self.command.read_bytes()
        result = self.install("--config", str(self.root / "absent.json"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command.read_bytes(), original)

    def test_each_launch_rereads_shared_config_and_hangup_stops_the_process(self):
        self.assertEqual(self.install().returncode, 0)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            new_port = probe.getsockname()[1]
        self.frpc.write_text(self.frpc.read_text().replace(f"localPort = {self.port}", f"localPort = {new_port}"))
        console = RunningConsole(command=[str(self.command), "--remote"], config_home=self.root)
        self.addCleanup(console.close)
        self.assertEqual(console.port, new_port)
        console.stop(signal.SIGHUP)
        self.assertEqual(console.process.returncode, 0)
        self.assertFalse(self.socket.exists())

    def test_conflicting_listener_options_fail_and_non_tty_does_not_leave_a_listener(self):
        self.assertEqual(self.install().returncode, 0)
        for option in (["--host", "0.0.0.0"], ["--port", "0"], ["-p", str(self.port)]):
            result = subprocess.run([str(self.command), "--remote", *option], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("only valid with --local", result.stderr)
        result = subprocess.run([str(self.command), "--remote"], capture_output=True, text=True, timeout=5,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive terminal", result.stderr)
        self.assertFalse(self.socket.exists())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))

    def test_second_launch_cannot_displace_a_running_console(self):
        self.assertEqual(self.install().returncode, 0)
        console = RunningConsole(command=[str(self.command), "--remote"], config_home=self.root)
        self.addCleanup(console.close)
        instance = request(self.socket, {"method": "sessions"})["instance"]
        result = subprocess.run([str(self.command), "--remote"], capture_output=True, text=True, timeout=5,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
        self.assertNotEqual(result.returncode, 0)
        # A different TCP listener must not bypass protection of the shared socket.
        with self.assertRaisesRegex(AssertionError, "Address already in use"):
            RunningConsole("--local", "-H", "127.0.0.1", "-p", "0",
                           command=[str(self.command)], config_home=self.root)
        self.assertIsNone(console.process.poll())
        self.assertEqual(request(self.socket, {"method": "sessions"})["instance"], instance)

    def test_stale_default_socket_requires_explicit_cleanup_before_restart(self):
        self.config.write_text("{}")
        self.assertEqual(self.install().returncode, 0)
        options = ["--local", "-H", "127.0.0.1", "-p", "0"]
        first = RunningConsole(*options, command=[str(self.command)], config_home=self.root)
        self.addCleanup(first.close)
        path = self.root / "shelldeck/agent/control.sock"
        instance = request(path, {"method": "sessions"})["instance"]
        first.stop(signal.SIGKILL)
        identity = path.stat().st_ino
        with self.assertRaisesRegex(AssertionError, "Address already in use"):
            RunningConsole(*options, command=[str(self.command)], config_home=self.root)
        self.assertEqual(path.stat().st_ino, identity)
        # The owner above has exited. Follow the documented recovery procedure.
        path.unlink()
        second = RunningConsole(*options, command=[str(self.command)], config_home=self.root)
        self.addCleanup(second.close)
        self.assertNotEqual(request(path, {"method": "sessions"})["instance"], instance)

    def test_invalid_proxy_is_rejected_at_remote_startup(self):
        proxy = {"name": "existing", "type": "tcp", "localIP": "127.0.0.1", "localPort": self.port,
                 "transport": {"proxyProtocolVersion": "v1"}}
        self.frpc = self.root / "client.json"
        self.config.write_text(json.dumps({"frpc_config": self.frpc.name, "proxy_name": "existing"}))
        cases = [[], {}, {"proxies": []}, {"proxies": [proxy, proxy]},
                 {"proxies": [proxy], "start": ["other"]}, {"proxies": [proxy], "start": "existing"}]
        for change in ({"localIP": "0.0.0.0"}, {"localIP": "::1"}, {"localIP": "{{ .Envs.LOCAL_IP }}"},
                       {"localPort": True}, {"localPort": 0}, {"localPort": 65536},
                       {"type": "udp"}, {"transport": {"proxyProtocolVersion": "v2"}},
                       {"plugin": {"type": "unix_domain_socket"}}):
            cases.append({"proxies": [{**proxy, **change}]})
        self.assertEqual(self.install().returncode, 0)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.frpc.write_text(json.dumps(candidate))
                result = subprocess.run([str(self.command), "--remote"], capture_output=True, text=True, timeout=5,
                                        env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.root / "shelldeck").exists())

    def test_json_selects_one_proxy_and_ignores_unrelated_secrets(self):
        self.frpc = self.root / "client.json"
        self.frpc.write_text(json.dumps({"auth": {"token": "private-test-secret"},
                                        "start": [], "proxies": [
                                            {"name": "unrelated", "type": "udp", "localPort": 8080},
                                            {"name": "existing", "type": "tcp", "localIP": "127.0.0.1",
                                             "localPort": self.port, "transport": {"proxyProtocolVersion": "v1"}}]}))
        self.config.write_text(json.dumps({"frpc_config": self.frpc.name, "proxy_name": "existing",
                                          "agent_socket": str(self.socket)}))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-test-secret", result.stdout + result.stderr)
        console = RunningConsole(command=[str(self.command), "--remote"], config_home=self.root)
        self.addCleanup(console.close)
        self.assertEqual(console.port, self.port)
        self.assertTrue(request(self.socket, {"method": "sessions"})["ok"])


if __name__ == "__main__":
    unittest.main()
