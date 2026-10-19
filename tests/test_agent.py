"""Agent control through real Unix sockets, TCP peers, Bash, and the JSON CLI."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import select
import selectors
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from shelldeck_control import AgentServer, CLIENT_LIMIT, CLIENT_TIMEOUT, REQUEST_LIMIT, request
from shelldeck_history import OUTPUT_LIMIT
from test_shelldeck import RunningConsole


ROOT = Path(__file__).resolve().parents[1]


class AgentFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shelldeck-agent-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        if os.environ.get("SHELLDECK_TEST_BASH_IMAGE"):
            # Match the path seen inside the bind mount, including macOS /tmp's
            # physical /private/tmp spelling used by Bash's PWD.
            self.root = self.root.resolve()
        self.socket_path = self.root / "agent" / "control.sock"
        self.start()

    def start(self):
        self.console = RunningConsole("--agent-socket", str(self.socket_path), config_home=self.root)
        self.addCleanup(self.console.close)
        self.instance = self.call("sessions")["instance"]

    def call(self, method, **fields):
        return request(self.socket_path, {"method": method, **fields})

    def execute(self, script="printf hello", sid=1, request_id="check-1", instance=None):
        return self.call("exec", instance=instance or self.instance, session_id=sid,
                         request_id=request_id, script=script)

    def result(self, run_id=1, complete=True):
        deadline = time.monotonic() + 5
        while True:
            response = self.call("result", run_id=run_id)
            self.assertTrue(response["ok"], response)
            if not complete or not response["run"]["pending"]:
                return response["run"]
            self.assertLess(time.monotonic(), deadline, response)
            time.sleep(0.02)

    def peer(self):
        client = self.console.connect()
        self.console.read_until(f"Session {len(self.console.clients)} connected".encode())
        return client

    def bash_peer(self, name="first"):
        if os.environ.get("SHELLDECK_TEST_BASH_IMAGE"):
            from docker_peer import start_bash_peer
            return start_bash_peer(self, os.environ["SHELLDECK_TEST_BASH_IMAGE"], name)
        if not shutil.which("bash"):
            self.skipTest("Bash is required")
        client = self.peer()
        child = subprocess.Popen([shutil.which("bash"), "--noprofile", "--norc", "-i"],
                                 stdin=client, stdout=client, stderr=client, cwd=self.root,
                                 env={**os.environ, "NODE": name, "PS1": "AGENT-TEST> "},
                                 start_new_session=True)
        def finish():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        self.addCleanup(finish)
        return client

    @staticmethod
    def token(peer):
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = peer.recv(65536)
            if not chunk:
                raise AssertionError("peer closed before receiving a command")
            data.extend(chunk)
        return b"\x1e" + re.search(rb"SHELLDECK_BATCH_[0-9a-f]{32}", data)[0] + b":"

    def cli(self, *arguments):
        completed = subprocess.run([sys.executable, str(ROOT / "shelldeck_agent.py"), "--socket",
                                    str(self.socket_path), *arguments], capture_output=True, timeout=8)
        self.assertEqual(completed.stderr, b"", completed.stderr)
        return completed.returncode, json.loads(completed.stdout)

    def cli_exec(self, source, request_id="cli-check", **options):
        return self.cli("exec", "--session", "1", "--instance", self.instance,
                        "--request-id", request_id, "--command", source,
                        "--wait", str(options.get("wait", 3)))


class AgentIntegrationTests(AgentFixture):
    def test_cli_two_step_task_on_existing_bash_preserves_session_and_target(self):
        self.bash_peer()
        self.bash_peer("second")
        self.console.command("name 1 Existing host")
        self.console.read_until(b"Name saved")
        listing = self.call("sessions")
        self.assertEqual([s["id"] for s in listing["sessions"]], [1, 2])
        self.assertEqual(listing["sessions"][0]["name"], "Existing host")
        code, first = self.cli_exec("printf '%s\\n' \"$NODE\" > agent-task.txt; printf 'created\\n'")
        self.assertEqual(code, 0, first)
        self.assertTrue(first["run"]["completed"])
        self.assertTrue(first["run"]["history_saved"])
        self.assertIn("created", first["run"]["output"])
        code, second = self.cli_exec("IFS= read -r value < agent-task.txt; printf '%s|%s\\n' \"$value\" \"$PWD\"",
                                     request_id="cli-verify")
        self.assertEqual(code, 0, second)
        self.assertIn("first|" + str(self.root.resolve()), second["run"]["output"])
        self.console.attach(1)
        self.console.type(b"printf 'HUMAN-STILL-CONNECTED\\n'\r")
        self.console.read_until(b"HUMAN-STILL-CONNECTED")
        self.assertEqual(self.call("sessions")["sessions"][0]["busy_reason"], "human_attached")

    def test_exit_failure_and_script_file_are_reported(self):
        self.bash_peer()
        source = self.root / "quoted script.sh"
        source.write_bytes(b"\xef\xbb\xbf" + "printf '%s\\n' \"中文 'quote'\" >&2\r\nexit 7\r\n".encode())
        code, response = self.cli("exec", "--session", "1", "--instance", self.instance,
                                  "--request-id", "file-check", "--file", str(source), "--wait", "3")
        self.assertEqual(code, 1, response)
        self.assertEqual(response["run"]["exit_code"], 7)
        self.assertFalse(response["run"]["success"])
        self.assertIn("中文 'quote'", response["run"]["output"])

    def test_timeout_keeps_run_busy_and_result_can_be_queried_later(self):
        peer = self.peer()
        code, response = self.cli_exec("printf later", wait=0.1)
        self.assertEqual(code, 3, response)
        self.assertTrue(response["wait_timed_out"])
        self.assertFalse(response["run"]["completed"])
        self.assertIsNone(response["run"]["exit_code"])
        self.assertEqual(self.execute(request_id="second")["error"]["code"], "session_busy")
        token = self.token(peer)
        peer.sendall(token + b"start\x1f" + token + b"script\x1flater" + token + b"end:0\x1f")
        code, final = self.cli("result", "--request-id", "cli-check", "--wait", "3")
        self.assertEqual(code, 0, final)
        self.assertEqual(final["run"]["output"], "later")

    def test_concurrent_duplicate_requests_dispatch_once_and_conflicts_do_not_send(self):
        peer = self.peer()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: self.execute(), range(2)))
        self.assertTrue(all(r["ok"] for r in responses), responses)
        self.assertEqual(sorted(r["replayed"] for r in responses), [False, True])
        self.assertEqual({r["run"]["run_id"] for r in responses}, {1})
        token = self.token(peer)
        conflict = self.execute("printf different")
        self.assertEqual(conflict["error"]["code"], "request_conflict")
        peer.sendall(token + b"start\x1f" + token + b"end:0\x1f")
        self.result()
        self.assertTrue(self.execute()["replayed"])
        self.assertFalse(select.select([peer], [], [], 0.1)[0], "duplicate dispatch")

    def test_lost_submission_reply_is_recovered_by_request_id(self):
        peer = self.peer()
        packet = {"protocol_version": 1, "method": "exec", "instance": self.instance,
                  "session_id": 1, "request_id": "lost-reply", "script": "printf once"}
        with socket.socket(socket.AF_UNIX) as control:
            control.connect(str(self.socket_path))
            control.sendall(json.dumps(packet).encode() + b"\n")
        self.token(peer)
        found = self.call("result", request_id="lost-reply")
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["run"]["run_id"], 1)
        self.assertTrue(self.execute("printf once", request_id="lost-reply")["replayed"])
        self.assertFalse(select.select([peer], [], [], 0.1)[0])

    def test_targets_busy_human_and_stale_instance_are_rejected(self):
        first, second = self.peer(), self.peer()
        self.assertEqual(self.execute(sid=3)["error"]["code"], "session_unavailable")
        self.assertEqual(self.execute(instance="0" * 32)["error"]["code"], "stale_instance")
        self.console.attach(1)
        self.assertEqual(self.execute()["error"]["code"], "session_busy")
        self.console.detach()
        self.assertTrue(self.execute(sid=2)["ok"])
        self.token(second)
        self.assertFalse(select.select([first], [], [], 0.1)[0])

    def test_disconnect_does_not_claim_remote_completion(self):
        peer = self.peer()
        self.execute()
        token = self.token(peer)
        peer.sendall(token + b"start\x1fpartial")
        peer.close()
        result = self.result()
        self.assertEqual(result["state"], "disconnected")
        self.assertFalse(result["completed"])
        self.assertIsNone(result["success"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["output"], "partial")

    def test_restart_preserves_idempotency_and_marks_unfinished_run_unknown(self):
        peer = self.peer()
        self.execute()
        self.token(peer)
        old_instance = self.instance
        self.console.stop(signal.SIGKILL)
        # Remove only this test's known abandoned socket after its owner has exited.
        self.socket_path.unlink()
        self.start()
        replacement = self.peer()
        replay = self.execute(instance=old_instance)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run"]["state"], "unknown")
        self.assertFalse(replay["run"]["completed"])
        stale = self.execute(instance=old_instance, request_id="new-command")
        self.assertEqual(stale["error"]["code"], "stale_instance")
        self.assertFalse(select.select([replacement], [], [], 0.1)[0])

    def test_history_transaction_failure_prevents_send_and_allows_same_id_retry(self):
        peer = self.peer()
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            db.execute("CREATE TRIGGER reject_agent BEFORE INSERT ON agent_requests "
                       "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            failed = self.execute()
            self.assertEqual(failed["error"]["code"], "history_unavailable")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM script_runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batch_jobs").fetchone()[0], 0)
            self.assertFalse(select.select([peer], [], [], 0.1)[0])
            db.execute("DROP TRIGGER reject_agent")
        self.assertTrue(self.execute()["ok"])
        self.token(peer)

    def test_unsaved_completion_is_explicit_and_eventually_persists(self):
        peer = self.peer()
        self.execute()
        token = self.token(peer)
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            db.execute("BEGIN IMMEDIATE")
            peer.sendall(token + b"start\x1ffinal-output" + token + b"end:7\x1f")
            self.console.read_until(b"Execution history could not be saved")
            result = self.result()
            self.assertEqual((result["exit_code"], result["output"]), (7, "final-output"))
            self.assertFalse(result["history_saved"])
            self.assertFalse(self.execute(request_id="new-while-locked")["ok"])
        self.assertTrue(self.result()["history_saved"])

    def test_output_truncation_and_terminal_controls_are_json_data(self):
        peer = self.peer()
        self.execute()
        token = self.token(peer)
        output = b"x" * (OUTPUT_LIMIT + 10) + b"\x1b[2Jtail\xff"
        peer.sendall(token + b"start\x1f" + output + token + b"end:0\x1f")
        result = self.result()
        self.assertEqual(result["output_bytes"], OUTPUT_LIMIT)
        self.assertEqual(result["output_dropped_bytes"], len(output) - OUTPUT_LIMIT)
        self.assertTrue(result["output"].endswith("\x1b[2Jtail\ufffd"))
        code, response = self.cli("result", "1")
        self.assertEqual(code, 0, response)

    def test_invalid_protocol_requests_do_not_break_console_or_dispatch(self):
        peer = self.peer()
        cases = [[], {"method": "sessions"}, {"protocol_version": True, "method": "sessions"},
                 {"protocol_version": 1, "method": []},
                 {"protocol_version": 1, "method": "sessions", "script": "exit"},
                 {"protocol_version": 1, "method": "result", "run_id": True}]
        for value in cases:
            with self.subTest(value=value), socket.socket(socket.AF_UNIX) as control:
                control.settimeout(3)
                control.connect(str(self.socket_path))
                control.sendall(json.dumps(value).encode() + b"\n")
                with control.makefile("rb") as reader:
                    self.assertFalse(json.loads(reader.readline())["ok"])
        for script in ("", "\x00", "\ud800", "x" * 65537, "x" * 49000):
            self.assertFalse(self.execute(script)["ok"])
        self.assertTrue(self.call("sessions")["ok"])
        self.assertFalse(select.select([peer], [], [], 0.1)[0])


class AgentTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="ncat-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "control.sock"
        self.selector = selectors.DefaultSelector()
        self.addCleanup(self.selector.close)

    def server(self, dispatch=lambda _: {"ok": True, "protocol_version": 1}):
        server = AgentServer(self.path, self.selector, dispatch)
        self.addCleanup(server.close)
        return server

    def test_permissions_and_existing_files_are_preserved(self):
        self.path.write_text("keep me")
        with self.assertRaises(OSError):
            AgentServer(self.path, self.selector, lambda _: {})
        self.assertEqual(self.path.read_text(), "keep me")
        self.path.unlink()
        server = self.server()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(OSError):
            AgentServer(self.path, self.selector, lambda _: {})
        self.assertTrue(self.path.exists())
        self.assertIsNotNone(server.identity)

    def test_shared_directory_is_rejected_without_chmod(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            AgentServer(self.path, self.selector, lambda _: {})
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.path.exists())

    def test_slow_client_limit_and_expiry_do_not_dispatch(self):
        dispatched = []
        server = self.server(lambda value: dispatched.append(value))
        for _ in range(CLIENT_LIMIT + 1):
            client = socket.socket(socket.AF_UNIX)
            self.addCleanup(client.close)
            client.connect(str(self.path))
            server.accept(None, None)
        self.assertEqual(len(server.clients), CLIENT_LIMIT)
        with mock.patch("shelldeck_control.time.monotonic", return_value=time.monotonic() + CLIENT_TIMEOUT + 1):
            server.expire()
        self.assertEqual(len(server.clients), 0)
        self.assertEqual(dispatched, [])

    def test_oversized_and_split_requests(self):
        seen = []
        server = self.server(lambda value: seen.append(value) or {"ok": True, "protocol_version": 1})
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(self.path))
            server.accept(None, None)
            accepted = next(iter(server.clients))
            for part in (b'{"method":', b'"sessions"}', b'\n'):
                client.sendall(part)
                server.service(accepted, selectors.EVENT_READ)
            self.assertEqual(seen, [{"method": "sessions"}])
            server.service(accepted, selectors.EVENT_WRITE)
            self.assertTrue(json.loads(client.recv(1024))["ok"])
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(self.path))
            server.accept(None, None)
            accepted = next(iter(server.clients))
            for _ in range(REQUEST_LIMIT // 4096 + 1):
                client.sendall(b"x" * 4096)
                server.service(accepted, selectors.EVENT_READ)
            server.service(accepted, selectors.EVENT_WRITE)
            self.assertEqual(json.loads(client.recv(1024))["error"]["code"], "request_too_large")
            self.assertEqual(len(seen), 1)

    def test_expired_request_cannot_dispatch_before_periodic_cleanup(self):
        seen = []
        server = self.server(lambda value: seen.append(value) or {"ok": True, "protocol_version": 1})
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(self.path))
            server.accept(None, None)
            accepted = next(iter(server.clients))
            client.sendall(b'{"method":"exec"}\n')
            with mock.patch("shelldeck_control.time.monotonic", return_value=server.clients[accepted]["deadline"]):
                server.service(accepted, selectors.EVENT_READ)
            self.assertEqual(seen, [])
            self.assertNotIn(accepted, server.clients)


if __name__ == "__main__":
    unittest.main()
