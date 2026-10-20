"""Installed foreground command, real terminal, shared frpc configuration."""

import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import unittest

from shelldeck_control import request
from test_shelldeck import RunningConsole


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
        self.frpc.write_text('''auth.token = "{{ .Envs.PRIVATE_FRP_TOKEN }}"
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

    def test_installed_command_runs_in_foreground_and_quit_releases_listener(self):
        original = self.frpc.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command.stat().st_mode & 0o777, 0o700)
        console = RunningConsole(command=[str(self.command)], config_home=self.root,
                                 environment={"PATH": "/nonexistent"})
        self.addCleanup(console.close)
        self.assertEqual(console.port, self.port)
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
        console = RunningConsole(command=[str(self.command)], config_home=self.root,
                                 environment={"PATH": "/nonexistent"})
        self.addCleanup(console.close)
        self.assertEqual(console.port, self.port)
        self.assertTrue(request(self.socket, {"method": "sessions"})["ok"])

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
        console = RunningConsole(command=[str(self.command)], config_home=self.root)
        self.addCleanup(console.close)
        self.assertEqual(console.port, new_port)
        console.stop(signal.SIGHUP)
        self.assertEqual(console.process.returncode, 0)
        self.assertFalse(self.socket.exists())

    def test_conflicting_listener_options_fail_and_non_tty_does_not_leave_a_listener(self):
        self.assertEqual(self.install().returncode, 0)
        for option in (["--host", "0.0.0.0"], ["--port", "0"]):
            result = subprocess.run([str(self.command), *option], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("conflicts with frpc_config", result.stderr)
        result = subprocess.run([str(self.command)], capture_output=True, text=True, timeout=5,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive terminal", result.stderr)
        self.assertFalse(self.socket.exists())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))

    def test_second_launch_cannot_displace_a_running_console(self):
        self.assertEqual(self.install().returncode, 0)
        console = RunningConsole(command=[str(self.command)], config_home=self.root)
        self.addCleanup(console.close)
        instance = request(self.socket, {"method": "sessions"})["instance"]
        result = subprocess.run([str(self.command)], capture_output=True, text=True, timeout=5,
                                env={**os.environ, "XDG_CONFIG_HOME": str(self.root)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(console.process.poll())
        self.assertEqual(request(self.socket, {"method": "sessions"})["instance"], instance)

    def test_invalid_proxy_is_rejected_without_installing_a_command(self):
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
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.frpc.write_text(json.dumps(candidate))
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.command.exists())

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
        console = RunningConsole(command=[str(self.command)], config_home=self.root)
        self.addCleanup(console.close)
        self.assertEqual(console.port, self.port)
        self.assertTrue(request(self.socket, {"method": "sessions"})["ok"])


if __name__ == "__main__":
    unittest.main()
