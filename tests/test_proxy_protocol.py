"""PROXY admission, original-source identity, and payloads over real sockets."""

import asyncio
import hashlib
import os
import select
import selectors
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from shelldeck import Console, PROXY_HEADER_LIMIT, PROXY_HEADER_TIMEOUT, parse_proxy_v1
from test_agent import AgentFixture
from test_shelldeck import RunningConsole, SCRIPT, receive
import test_mcp


def proxy_header(ip="192.0.2.10", port=41234):
    return f"PROXY TCP4 {ip} 203.0.113.20 {port} 9000\r\n".encode("ascii")


class ProxyHeaderTests(unittest.TestCase):
    def test_canonical_ipv4_and_port_boundaries(self):
        for port in (0, 1, 65535):
            self.assertEqual(parse_proxy_v1(proxy_header("255.255.255.255", port)),
                             ("255.255.255.255", port))

    def test_invalid_and_unsupported_headers_are_rejected(self):
        headers = [
            b"PROXY UNKNOWN\r\n", b"PROXY TCP6 ::1 ::1 123 9000\r\n",
            b"\r\n\r\n\x00\r\nQUIT\n\x21\x11\x00\x0c",
            b"Bash prompt> \r\n", b"PROXY  TCP4 192.0.2.1 203.0.113.1 1 2\r\n",
            b"PROXY TCP4 192.0.2.1 203.0.113.1 1 2 EXTRA\r\n",
            proxy_header().replace(b"TCP4 ", b"TCP4\t"),
            proxy_header().replace(b"192.0.2.10", b"192.0.2.999"),
            proxy_header().replace(b"192.0.2.10", b"192.000.2.10"),
            proxy_header().replace(b"203.0.113.20", b"invalid"),
            proxy_header().replace(b"41234", b"65536"),
            proxy_header().replace(b"41234", b"+1"),
            proxy_header().replace(b"41234", b"-1"),
            proxy_header().replace(b"41234", b"01"),
            proxy_header().replace(b"9000", b"65536"),
            proxy_header().replace(b"192.0.2.10", b"\xff"),
            proxy_header().replace(b"\r\n", b"\n"),
            proxy_header().replace(b"\r\n", b"\r"),
            b"x" * PROXY_HEADER_LIMIT + b"\r\n",
        ]
        for header in headers:
            with self.subTest(header=header), self.assertRaises(ValueError):
                parse_proxy_v1(header)

    def test_cli_rejects_public_listener_before_creating_config(self):
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([sys.executable, str(SCRIPT), "--proxy-protocol", "v1"],
                                    env={**os.environ, "XDG_CONFIG_HOME": root}, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"loopback", result.stderr)
            self.assertEqual(os.listdir(root), [])


class ProxyAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.listener.setblocking(False)
        self.console = Console(self.listener, None, 65536, 100, proxy_protocol="v1")
        self.addCleanup(self.close_console)

    def close_console(self):
        for sock in list(self.console.pending_proxies):
            self.console.drop_proxy(sock)
        for session in self.console.sessions.values():
            self.console.disconnect(session, notify=False)
        self.console.selector.close()
        self.console.history.close()

    def connect(self):
        before = set(self.console.pending_proxies)
        client = socket.create_connection(self.listener.getsockname(), timeout=2)
        self.addCleanup(client.close)
        self.assertTrue(select.select([self.listener], [], [], 2)[0])
        self.console.accept()
        accepted = set(self.console.pending_proxies) - before
        return client, next(iter(accepted)) if accepted else None

    def send_header(self, client, accepted, data):
        client.sendall(data)
        self.assertTrue(select.select([accepted], [], [], 2)[0])
        self.console.read_proxy_header(accepted, selectors.EVENT_READ)

    def assert_closed(self, client):
        try:
            self.assertEqual(client.recv(1), b"")
        except ConnectionResetError:
            pass

    def test_fragmented_header_creates_no_session_until_complete(self):
        client, accepted = self.connect()
        header = proxy_header()
        for byte in header[:-1]:
            self.send_header(client, accepted, bytes([byte]))
            self.assertEqual(self.console.sessions, {})
        self.send_header(client, accepted, header[-1:])
        self.assertEqual(self.console.sessions[1].peer, "192.0.2.10:41234")
        self.assertFalse(self.console.pending_proxies)

    def test_coalesced_header_preserves_all_binary_payload_and_later_proxy_text(self):
        client, accepted = self.connect()
        payload = bytes(range(256)) * 32 + proxy_header("198.51.100.80")
        self.send_header(client, accepted, proxy_header() + payload)
        session = self.console.sessions[1]
        while session.output.size < len(payload):
            self.assertTrue(select.select([accepted], [], [], 2)[0])
            self.console.read_session(session)
        self.assertEqual(b"".join(session.output.parts), payload)
        self.assertEqual(session.peer, "192.0.2.10:41234")

    def test_bad_header_does_not_allocate_id_or_evict_retained_session(self):
        self.console.max_sessions = 1
        first, accepted = self.connect()
        self.send_header(first, accepted, proxy_header())
        self.console.disconnect(self.console.sessions[1], notify=False)
        bad, pending = self.connect()
        self.send_header(bad, pending, b"PROXY UNKNOWN\r\n")
        self.assert_closed(bad)
        self.assertEqual(list(self.console.sessions), [1])
        self.assertEqual(self.console.next_id, 2)
        good, pending = self.connect()
        self.send_header(good, pending, proxy_header("198.51.100.2"))
        self.assertEqual(list(self.console.sessions), [2])
        self.assertEqual(self.console.sessions[2].ip, "198.51.100.2")

    def test_oversized_header_and_early_eof_leave_no_session(self):
        client, accepted = self.connect()
        self.send_header(client, accepted, b"X" * PROXY_HEADER_LIMIT)
        self.assert_closed(client)
        client, accepted = self.connect()
        self.send_header(client, accepted, b"PROXY TCP4")
        client.shutdown(socket.SHUT_WR)
        self.assertTrue(select.select([accepted], [], [], 2)[0])
        self.console.read_proxy_header(accepted, selectors.EVENT_READ)
        self.assert_closed(client)
        self.assertEqual(self.console.sessions, {})
        self.assertFalse(self.console.pending_proxies)

    def test_pending_limit_and_fixed_deadline_recover_capacity(self):
        self.console.max_sessions = 4
        pairs = [self.connect() for _ in range(self.console.max_sessions)]
        extra, rejected = self.connect()
        self.assertIsNone(rejected)
        self.assert_closed(extra)
        client, accepted = pairs[0]
        deadline = self.console.pending_proxies[accepted].deadline
        with mock.patch("shelldeck.time.monotonic", return_value=deadline - 0.01):
            self.send_header(client, accepted, b"PROXY ")
        self.assertEqual(self.console.pending_proxies[accepted].deadline, deadline)
        with mock.patch("shelldeck.time.monotonic", return_value=deadline + PROXY_HEADER_TIMEOUT):
            self.console.expire_proxy_headers()
        self.assertFalse(self.console.pending_proxies)
        self.assertEqual(self.console.sessions, {})
        good, accepted = self.connect()
        self.send_header(good, accepted, proxy_header())
        self.assertEqual(list(self.console.sessions), [1])

    def test_readable_header_cannot_bypass_deadline(self):
        client, accepted = self.connect()
        deadline = self.console.pending_proxies[accepted].deadline
        client.sendall(proxy_header())
        with mock.patch("shelldeck.time.monotonic", return_value=deadline):
            self.console.read_proxy_header(accepted, selectors.EVENT_READ)
        self.assertFalse(self.console.pending_proxies)
        self.assertEqual(self.console.sessions, {})
        self.assert_closed(client)

    def test_session_limit_counts_pending_and_existing_sessions_together(self):
        self.console.max_sessions = 1
        first, accepted = self.connect()
        self.send_header(first, accepted, proxy_header())
        extra, accepted = self.connect()
        self.assertIsNone(accepted)
        self.assert_closed(extra)
        self.assertEqual(list(self.console.sessions), [1])
        self.console.active = 1
        self.assertTrue(self.console.send(b"still here\n"))
        self.console.write_session(self.console.sessions[1])
        self.assertEqual(receive(first, 11), b"still here\n")

    def test_reconnect_burst_can_fill_configured_capacity(self):
        self.console.max_sessions = 32
        pairs = [self.connect() for _ in range(32)]
        self.assertTrue(all(accepted is not None for _, accepted in pairs))
        for index, (client, accepted) in enumerate(pairs):
            self.send_header(client, accepted, proxy_header(port=41000 + index))
        self.assertEqual(len(self.console.sessions), 32)
        self.assertFalse(self.console.pending_proxies)

    def test_close_all_drops_pending_headers_and_keeps_listener(self):
        client, _ = self.connect()
        self.console.command("close all")
        self.assert_closed(client)
        self.assertFalse(self.console.pending_proxies)
        good, accepted = self.connect()
        self.send_header(good, accepted, proxy_header())
        self.assertEqual(list(self.console.sessions), [1])

    def test_constructor_rejects_wildcard_listener(self):
        with socket.socket() as listener:
            listener.bind(("0.0.0.0", 0))
            with self.assertRaisesRegex(ValueError, "loopback"):
                Console(listener, None, 1024, 10, proxy_protocol="v1")


class ProxyFixture(AgentFixture):
    def start(self):
        self.console = RunningConsole("--proxy-protocol", "v1", "--agent-socket", str(self.socket_path),
                                      config_home=self.root)
        self.addCleanup(self.console.close)
        self.instance = self.call("sessions")["instance"]

    def peer(self, ip=None):
        client = self.console.connect()
        sid = len(self.console.clients)
        client.sendall(proxy_header(ip or f"192.0.2.{sid}", 41000 + sid))
        self.console.read_until(f"Session {sid} connected".encode())
        return client


class ProxyPayloadChecks:
    """The same application checks also run through actual frpc/frps when installed."""

    def test_binary_round_trip_and_command_after_transfer(self):
        self.bash_peer()
        scope = {"instance": self.instance, "session_id": 1, "lease_id": "a" * 32}
        self.assertTrue(self.call("acquire", **scope)["ok"])
        payload = bytes(range(256)) * 513 + b"\x00\xff\r\n" + proxy_header()
        source, remote, destination = (self.root / name for name in ("source.bin", "remote.bin", "download.bin"))
        source.write_bytes(payload)
        for direction, local, transfer_id in (("upload", source, "proxy-up"),
                                               ("download", destination, "proxy-down")):
            code, result = self.cli(direction, "--instance", self.instance, "--session", "1",
                                    "--lease-id", scope["lease_id"], "--transfer-id", transfer_id,
                                    "--local", str(local), "--remote", str(remote), "--wait", "10")
            self.assertEqual(code, 0, result)
            self.assertTrue(result["transfer"]["success"], result)
            self.assertEqual(result["transfer"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(destination.read_bytes(), payload)
        result = self.call("exec", **scope, request_id="after-proxy-transfer", script="printf still-usable")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.result(result["run"]["run_id"])["output"], "still-usable")


class ProxyIntegrationTests(ProxyPayloadChecks, ProxyFixture):
    def test_distinct_sources_reach_names_notes_history_and_agent(self):
        self.bash_peer("first")
        self.bash_peer("second")
        self.console.command("name 1 第一台")
        self.console.read_until(b"Name saved for 192.0.2.1")
        self.console.command("note 1 first-note")
        self.console.read_until(b"Note saved for 192.0.2.1")
        listed = self.call("sessions")["sessions"]
        self.assertEqual([(s["peer"], s["name"], s["note"]) for s in listed],
                         [("192.0.2.1:41001", "第一台", "first-note"), ("192.0.2.2:41002", "", "")])
        for sid, name in ((1, "first"), (2, "second")):
            result = self.execute('printf "%s" "$NODE"', sid=sid, request_id=f"source-{sid}")
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.result(result["run"]["run_id"])["output"], name)
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            self.assertEqual(db.execute("SELECT ip, peer FROM script_runs ORDER BY id").fetchall(),
                             [("192.0.2.1", "192.0.2.1:41001"), ("192.0.2.2", "192.0.2.2:41002")])
        self.console.command("history 192.0.2.1")
        output = self.console.read_until(b"agent-source-1")
        self.assertNotIn(b"agent-source-2", output)

    def test_pending_header_does_not_block_existing_session_or_agent(self):
        first = self.peer()
        pending = self.console.connect()
        pending.sendall(b"PROXY TCP4 ")
        self.assertEqual(len(self.call("sessions")["sessions"]), 1)
        self.console.attach(1)
        self.console.type(b"first-only\r")
        self.assertEqual(receive(first, 11), b"first-only\n")
        self.assertFalse(select.select([pending], [], [], 0.1)[0])
        self.console.stop()
        self.assertEqual(pending.recv(1), b"")

    def test_missing_and_partial_headers_expire_in_real_event_loop(self):
        silent, partial = self.console.connect(), self.console.connect()
        partial.sendall(b"PROXY TCP4 ")
        silent.settimeout(PROXY_HEADER_TIMEOUT + 2)
        partial.settimeout(PROXY_HEADER_TIMEOUT + 2)
        self.assertEqual(silent.recv(1), b"")
        self.assertEqual(partial.recv(1), b"")
        self.assertEqual(self.call("sessions")["sessions"], [])
        good = self.console.connect()
        good.sendall(proxy_header())
        self.console.read_until(b"Session 1 connected")
        self.assertEqual(self.call("sessions")["sessions"][0]["id"], 1)

    def test_disconnect_keeps_unknown_result_and_reconnect_uses_new_id_and_saved_name(self):
        first = self.peer()
        self.console.command("name 1 retained-name")
        self.console.read_until(b"Name saved for 192.0.2.1")
        self.execute()
        token = self.token(first)
        first.sendall(token + b"start\x1fpartial")
        first.shutdown(socket.SHUT_RDWR)
        first.close()
        result = self.result()
        self.assertEqual(result["state"], "disconnected")
        self.assertIsNone(result["success"])
        self.assertIsNone(result["exit_code"])
        self.peer("192.0.2.1")
        listed = self.call("sessions")["sessions"]
        self.assertFalse(listed[0]["connected"])
        self.assertEqual((listed[1]["id"], listed[1]["name"]), (2, "retained-name"))

    def test_plain_mode_does_not_interpret_proxy_looking_payload(self):
        direct = RunningConsole()
        self.addCleanup(direct.close)
        client = direct.connect()
        direct.read_until(b"Session 1 connected from 127.0.0.1:")
        client.sendall(proxy_header())
        direct.command("interact 1")
        direct.read_until(b"PROXY TCP4 192.0.2.10")

    @unittest.skipUnless(test_mcp.ClientSession is not None, "optional MCP SDK: use .venv-mcp/bin/python")
    def test_mcp_lists_original_source_and_executes_through_proxy_mode(self):
        self.bash_peer()

        async def scenario():
            async with test_mcp.MCPIntegrationTests.client(self) as client:
                listed = await client.call_tool("list_sessions", {})
                self.assertEqual(listed.structured_content["sessions"][0]["peer"], "192.0.2.1:41001")
                scope = {"instance": self.instance, "session_id": 1, "lease_id": "b" * 32}
                acquired = await client.call_tool("acquire_session", {**scope, "task": "proxy test"})
                self.assertFalse(acquired.is_error)
                result = await client.call_tool("execute_command", {
                    **scope, "request_id": "proxy-mcp", "script": "printf proxy-mcp-ok", "wait_seconds": 3})
                self.assertEqual(result.structured_content["run"]["output"], "proxy-mcp-ok")
                self.assertTrue(result.structured_content["run"]["success"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
