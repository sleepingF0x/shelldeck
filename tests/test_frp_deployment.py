"""Deployment configuration at executable, launchd, SSH, and firewall boundaries."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


service = module("frp_service", "scripts/shelldeck_frp.py")
allowlist = module("frp_allowlist", "examples/frp/allowlist.py")


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shelldeck-config-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = self.root / "another checkout 'quoted'"
        self.project.mkdir()
        for name in ("shelldeck.py", "shelldeck_agent.py"):
            (self.project / name).touch()
        self.bin = self.root / "tools"
        self.bin.mkdir()
        self.trace = self.root / "calls.jsonl"
        fake = "#!" + sys.executable + "\n" + '''import json,os,pathlib,sys
with open(os.environ['SHELLDECK_TEST_TRACE'],'a') as log:
    log.write(json.dumps(sys.argv)+'\\n')
if pathlib.Path(sys.argv[0]).name == 'tmux-test' and 'new-session' in sys.argv:
    pathlib.Path(os.environ['SHELLDECK_TEST_SOCKET']).touch()
    print('123 $1')
if 'has-session' in sys.argv or 'print' in sys.argv:
    raise SystemExit(1)
'''
        for name in ("frpc-test", "tmux-test", "ssh-test", "launchctl-test"):
            path = self.bin / name
            path.write_text(fake)
            path.chmod(0o700)
        (self.root / "client.toml").write_text("# fixture; frpc verify is a process stand-in\n")
        self.config = json.loads((ROOT / "examples/frp/service.json").read_text())
        self.config.update(project_root=self.project.name, python_binary=sys.executable,
                           frpc_binary="tools/frpc-test", frpc_config="client.toml",
                           ssh_host="alternate-test-relay", ssh_binary="ssh-test",
                           remote_allowlist_command=["python-custom", "/srv/relay 'x'/access.py",
                                                     "--config", "/etc/relay $(literal)/access.json"],
                           agent_socket="control.sock", tmux_binary="tools/tmux-test",
                           tmux_socket="different-mux", tmux_session="different-session",
                           listen_host="127.0.0.2", listen_port=43123,
                           launchctl_binary="tools/launchctl-test", launch_agent_label="test.relay.other",
                           launch_agent_path="launch/other.plist", command_path="bin/relay entry 'one'",
                           log_directory="logs/elsewhere",
                           environment={"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                                        "TERM": "vt100", "SHELLDECK_TEST_TRACE": str(self.trace),
                                        "SHELLDECK_TEST_SOCKET": str(self.root / "control.sock")})
        self.path = self.root / "service 'custom' $(literal).json"
        self.write_config()

    def write_config(self):
        self.path.write_text(json.dumps(self.config))

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_install_and_generated_wrapper_use_custom_config_from_another_cwd(self):
        instance = service.Service(self.path)
        with contextlib.redirect_stdout(io.StringIO()):
            instance.install()
        plist = plistlib.loads(instance.plist.read_bytes())
        self.assertEqual(plist["WorkingDirectory"], str(self.project))
        self.assertEqual(plist["Label"], "test.relay.other")
        self.assertEqual(plist["ProgramArguments"][-3:], ["--config", str(self.path), "supervise"])
        self.assertEqual(plist["StandardOutPath"], str(self.root / "logs/elsewhere/service.log"))
        self.assertEqual(plist["EnvironmentVariables"]["TERM"], "vt100")
        result = subprocess.run([str(instance.command), "refresh-access"], cwd=ROOT,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        ssh = self.calls()[-1]
        self.assertEqual(Path(ssh[0]).name, "ssh-test")
        self.assertEqual(ssh[-2], "alternate-test-relay")
        self.assertEqual(shlex.split(ssh[-1]), self.config["remote_allowlist_command"] + ["refresh-local"])
        self.assertEqual(instance.command.stat().st_mode & 0o777, 0o700)
        self.assertEqual(instance.plist.stat().st_mode & 0o777, 0o600)

    def test_shell_command_uses_configured_python_project_listener_and_tmux(self):
        instance = service.Service(self.path)
        instance.ensure_console()
        call = next(argv for argv in self.calls() if "new-session" in argv)
        self.assertIn("different-mux", call)
        self.assertIn("different-session", call)
        shell = shlex.split(call[-1])
        self.assertEqual(shell[:3], ["exec", sys.executable, str(self.project / "shelldeck.py")])
        self.assertEqual(shell[shell.index("-H") + 1], "127.0.0.2")
        self.assertEqual(shell[shell.index("-p") + 1], "43123")
        self.assertEqual(shell[-1], str(self.root / "control.sock"))

    def test_generated_launch_environment_resolves_command_names_without_terminal_path(self):
        self.config["environment"].pop("PATH")
        self.config["tmux_binary"] = "tmux-test"
        self.write_config()
        with mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.defpath}), \
                contextlib.redirect_stdout(io.StringIO()):
            instance = service.Service(self.path)
            instance.install()
        environment = {**os.environ, **instance.launch_agent()["EnvironmentVariables"]}
        self.assertIn(str(self.bin), environment["PATH"].split(os.pathsep))
        result = subprocess.run([str(instance.command), "status"], env=environment,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ShellDeck console: stopped", result.stdout)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for session isolation")
    def test_tmux_prefix_does_not_reuse_or_remove_a_different_session(self):
        self.config.update(tmux_binary=shutil.which("tmux"), tmux_socket=self.root.name)
        self.write_config()
        instance = service.Service(self.path)
        self.addCleanup(subprocess.run, instance.tmux + ["kill-server"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        other = instance.tmux_session + "-extra"
        subprocess.run(instance.tmux + ["new-session", "-d", "-s", other, "sleep 60"],
                       check=True, capture_output=True, timeout=5, env=instance.env)
        self.assertFalse(instance.console_running())
        with mock.patch.object(instance, "ensure_console", side_effect=RuntimeError("startup failed")), \
                mock.patch.object(service.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                instance.supervise()
        retained = subprocess.run(instance.tmux + ["has-session", "-t", "=" + other],
                                  capture_output=True, timeout=5)
        self.assertEqual(retained.returncode, 0, retained.stderr)
        (self.project / "shelldeck.py").write_text(
            "import os,pathlib,time\npathlib.Path(os.environ['SHELLDECK_TEST_SOCKET']).touch()\ntime.sleep(60)\n")
        with mock.patch.object(service.signal, "signal"):
            self.assertEqual(instance.supervise(), 1)
        self.assertFalse(instance.console_running(), "owned console survived frpc shutdown")
        retained = subprocess.run(instance.tmux + ["has-session", "-t", "=" + other],
                                  capture_output=True, timeout=5)
        self.assertEqual(retained.returncode, 0, retained.stderr)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for session ownership")
    def test_second_supervisor_preserves_an_existing_console(self):
        self.config.update(tmux_binary=shutil.which("tmux"), tmux_socket=self.root.name)
        self.write_config()
        instance = service.Service(self.path)
        self.addCleanup(subprocess.run, instance.tmux + ["kill-server"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        subprocess.run(instance.tmux + ["new-session", "-d", "-s", instance.tmux_session, "sleep 60"],
                       check=True, capture_output=True, timeout=5)
        with mock.patch.object(service.signal, "signal"):
            try:
                instance.supervise()
            except RuntimeError:
                pass
        self.assertTrue(instance.console_running(), "second supervisor removed the existing console")
        self.assertFalse(any(Path(call[0]).name == "frpc-test" for call in self.calls()))

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for session ownership")
    def test_old_supervisor_preserves_replacement_console_with_or_without_server_restart(self):
        (self.project / "shelldeck.py").write_text(
            "import os,pathlib,time\npathlib.Path(os.environ['SHELLDECK_TEST_SOCKET']).touch()\ntime.sleep(60)\n")
        for keep_server in (True, False):
            with self.subTest(keep_server=keep_server):
                self.config.update(tmux_binary=shutil.which("tmux"),
                                   tmux_socket=self.root.name + ("-kept" if keep_server else "-restarted"))
                self.write_config()
                original = service.Service(self.path)
                self.addCleanup(subprocess.run, original.tmux + ["kill-server"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if keep_server:
                    subprocess.run(original.tmux + ["new-session", "-d", "-s", "keeper", "sleep 60"],
                                   check=True, capture_output=True, timeout=5, env=original.env)
                original.ensure_console()
                identity_command = original.tmux + ["display-message", "-p", "-t", original.tmux_target + ":",
                                                    "#{pid} #{session_id}"]
                first_identity = subprocess.check_output(identity_command, text=True, timeout=5).split()
                subprocess.run(original.tmux + ["kill-session", "-t", original.tmux_target],
                               check=True, capture_output=True, timeout=5)
                replacement = service.Service(self.path)
                replacement.ensure_console()
                next_identity = subprocess.check_output(identity_command, text=True, timeout=5).split()
                self.assertNotEqual(first_identity, next_identity)
                if keep_server:
                    self.assertEqual(first_identity[0], next_identity[0])
                else:
                    self.assertNotEqual(first_identity[0], next_identity[0])
                    self.assertEqual(first_identity[1], next_identity[1])
                with mock.patch.object(service.signal, "signal"):
                    try:
                        original.supervise()
                    except RuntimeError:
                        pass
                self.assertTrue(replacement.console_running(), "old supervisor removed the replacement console")

    def test_start_uses_generated_launch_agent_and_requires_regeneration_after_path_change(self):
        instance = service.Service(self.path)
        with contextlib.redirect_stdout(io.StringIO()):
            instance.install()
            instance.start()
        launched = next(argv for argv in self.calls() if "bootstrap" in argv)
        self.assertEqual(launched[-1], str(instance.plist))
        self.trace.write_text("")
        self.config["log_directory"] = "new-logs"
        self.write_config()
        with self.assertRaisesRegex(ValueError, "run install first"):
            service.Service(self.path).start()
        self.assertFalse(any("ssh-test" in Path(call[0]).name for call in self.calls()))
        self.assertFalse(any("bootstrap" in call for call in self.calls()))

    def test_unsafe_listener_and_malformed_commands_fail_before_execution(self):
        for key, value in (("listen_host", "0.0.0.0"), ("listen_host", "192.0.2.5"),
                           ("listen_port", True), ("listen_port", 65536),
                           ("ssh_host", "-oProxyCommand=bad"), ("remote_allowlist_command", "shell text"),
                           ("tmux_session", "other:0"), ("tmux_session", "other.session")):
            with self.subTest(key=key, value=value):
                original = self.config[key]
                self.config[key] = value
                self.write_config()
                with self.assertRaises(ValueError):
                    service.Service(self.path)
                self.config[key] = original
        self.assertFalse(self.trace.exists())

    def test_missing_custom_config_never_falls_back_to_live_configuration(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/shelldeck_frp.py"),
                                 "--config", str(self.root / "absent.json"), "start"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absent.json", result.stderr)
        self.assertFalse(self.trace.exists())


class AllowlistConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {"local_source": "192.0.2.8", "callback_sources": ["198.51.100.0/24"],
                       "control_port": 31001, "callback_port": 31002, "note": "preserve metadata"}
        directory = tempfile.TemporaryDirectory(prefix="shelldeck-allowlist-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "custom access.json"
        self.path.write_text(json.dumps(self.config))

    def test_refresh_changes_only_authenticated_source_and_preserves_custom_ports_and_metadata(self):
        with mock.patch.object(allowlist.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"SSH_CONNECTION": "203.0.113.9 23456 198.51.100.8 65030"}), \
                mock.patch.object(allowlist, "apply") as apply, contextlib.redirect_stdout(io.StringIO()):
            allowlist.main(["--config", str(self.path), "refresh-local"])
        expected = {**self.config, "local_source": "203.0.113.9"}
        self.assertEqual(json.loads(self.path.read_text()), expected)
        apply.assert_called_once_with(expected)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_write_failure_reinstates_previous_filter_and_leaves_saved_config(self):
        with mock.patch.object(allowlist.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"SSH_CONNECTION": "203.0.113.9 23456 198.51.100.8 65030"}), \
                mock.patch.object(allowlist, "apply") as apply, \
                mock.patch.object(allowlist, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                allowlist.main(["--config", str(self.path), "refresh-local"])
        self.assertEqual(apply.call_args_list[-1].args[0], self.config)
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_custom_ports_are_in_one_scoped_transaction_and_dry_run_failure_does_not_apply(self):
        tables = json.dumps({"nftables": [{"table": {"family": "inet", "name": "shelldeck_frp"}}]})
        with mock.patch.object(allowlist.subprocess, "check_output", return_value=tables), \
                mock.patch.object(allowlist.subprocess, "run") as run:
            allowlist.apply(allowlist.validate(self.config))
        self.assertEqual(run.call_count, 2)
        transaction = run.call_args_list[1].kwargs["input"]
        self.assertTrue(transaction.startswith("delete table inet shelldeck_frp\n"))
        self.assertNotIn("flush ruleset", transaction)
        self.assertIn("tcp dport 31001", transaction)
        self.assertIn("tcp dport 31002", transaction)
        self.assertNotIn("dport 7000", transaction)
        self.assertNotIn("dport 9000", transaction)
        with mock.patch.object(allowlist.subprocess, "check_output", return_value=tables), \
                mock.patch.object(allowlist.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "nft")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                allowlist.apply(self.config)
        self.assertEqual(run.call_count, 1)
        self.assertIn("-c", run.call_args.args[0])

    def test_invalid_ports_and_sources_fail_before_firewall_changes(self):
        for change in ({"control_port": True}, {"callback_port": 0}, {"control_port": 65536},
                       {"callback_port": 31001}, {"local_source": "not-an-address"},
                       {"callback_sources": ["198.51.100.1/24"]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                allowlist.validate({**self.config, **change})

    def test_refresh_without_authenticated_ssh_does_not_touch_rules_or_config(self):
        with mock.patch.object(allowlist.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(allowlist, "apply") as apply, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                allowlist.main(["--config", str(self.path), "refresh-local"])
        apply.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), self.config)


if __name__ == "__main__":
    unittest.main()
