"""Real SDK stdio client -> MCP adapter -> Unix socket -> existing Bash session."""

import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import select
import sys
import unittest

from test_agent import AgentFixture, ROOT

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


@unittest.skipUnless(ClientSession is not None, "optional MCP SDK: use .venv-mcp/bin/python")
class MCPIntegrationTests(AgentFixture):
    @asynccontextmanager
    async def client(self, socket_path=None):
        params = StdioServerParameters(command=sys.executable,
                                        args=[str(ROOT / "shelldeck_mcp.py"), "--socket", str(socket_path or self.socket_path)])
        with (self.root / "mcp-stderr.log").open("a") as log:
            async with stdio_client(params, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=10) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.server_info.name, "shelldeck")
                    yield session

    def scope(self):
        return {"instance": self.instance, "session_id": 1, "lease_id": "c" * 32}

    def data(self, result, error=False):
        self.assertEqual(result.is_error, error, result)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        return result.structured_content

    def test_sdk_handshake_schemas_two_step_bash_task_and_cli_interoperation(self):
        self.bash_peer()

        async def scenario():
            async with self.client() as client:
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                self.assertEqual(set(tools), {"list_sessions", "acquire_session", "renew_session",
                                              "release_session", "execute_command", "get_result",
                                              "upload_file", "download_file", "get_transfer"})
                self.assertIn("lease_id", tools["execute_command"].input_schema["required"])
                self.assertTrue(tools["get_result"].annotations.read_only_hint)
                self.assertTrue(tools["execute_command"].annotations.destructive_hint)
                self.assertTrue(all(t.output_schema["type"] == "object" for t in tools.values()))
                sessions = self.data(await client.call_tool("list_sessions", {}))
                self.assertEqual(sessions["instance"], self.instance)
                self.assertEqual(sessions["sessions"][0]["id"], 1)
                scope = self.scope()
                acquired = self.data(await client.call_tool("acquire_session", {**scope, "task": "MCP acceptance"}))
                self.assertEqual(acquired["lease"]["lease_id"], scope["lease_id"])
                self.assertEqual(self.execute()["error"]["code"], "session_busy")
                first_args = {**scope, "request_id": "mcp-create", "script": "printf 'mcp-ok\\n' > mcp-result.txt",
                              "wait_seconds": 3}
                first = self.data(await client.call_tool("execute_command", first_args))
                self.assertTrue(first["run"]["success"], first)
                self.data(await client.call_tool("renew_session", {**scope, "ttl_seconds": 120}))
                second = self.data(await client.call_tool("execute_command", {
                    **scope, "request_id": "mcp-verify", "script": "cat mcp-result.txt", "wait_seconds": 3}))
                self.assertEqual(second["run"]["output"], "mcp-ok\n")
                self.assertTrue(second["run"]["history_saved"])
                result = self.data(await client.call_tool("get_result", {"request_id": "mcp-verify"}))
                self.assertEqual(result["run"]["run_id"], second["run"]["run_id"])
                released = self.data(await client.call_tool("release_session", scope))
                self.assertTrue(released["released"])
                replay = self.data(await client.call_tool("execute_command", first_args))
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["run"]["run_id"], first["run"]["run_id"])

        asyncio.run(scenario())
        code, result = self.cli_exec("printf 'cli-ok\\n'", request_id="cli-after-mcp")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["output"], "cli-ok\n")

    def test_bad_arguments_busy_lease_and_nonzero_exit_are_tool_errors(self):
        self.bash_peer()

        async def scenario():
            async with self.client() as client:
                scope = self.scope()
                for fields in ({**scope, "session_id": True}, {**scope, "ttl_seconds": 0}):
                    rejected = await client.call_tool("acquire_session", fields)
                    self.assertTrue(rejected.is_error, rejected)
                self.assertIsNone(self.call("sessions")["sessions"][0]["lease"])
                denied = self.data(await client.call_tool("execute_command", {
                    **scope, "request_id": "mcp-unowned", "script": "printf forbidden"}), error=True)
                self.assertEqual(denied["error"]["code"], "lease_inactive")
                self.data(await client.call_tool("acquire_session", scope))
                busy = self.data(await client.call_tool("acquire_session", {
                    **scope, "lease_id": "d" * 32}), error=True)
                self.assertEqual(busy["error"]["code"], "session_busy")
                failure = self.data(await client.call_tool("execute_command", {
                    **scope, "request_id": "mcp-exit-seven", "script": "printf 'failed\\n'; exit 7",
                    "wait_seconds": 3}), error=True)
                self.assertTrue(failure["run"]["completed"])
                self.assertEqual(failure["run"]["exit_code"], 7)
                self.assertEqual(failure["run"]["output"], "failed\n")
                invalid_lookup = self.data(await client.call_tool("get_result", {}), error=True)
                self.assertEqual(invalid_lookup["error"]["code"], "invalid_request")

        asyncio.run(scenario())

    def test_wait_allows_concurrent_renew_and_mcp_restart_does_not_release_or_cancel(self):
        peer = self.peer()

        async def scenario():
            scope = self.scope()
            async with self.client() as client:
                self.data(await client.call_tool("acquire_session", scope))
                running = asyncio.create_task(client.call_tool("execute_command", {
                    **scope, "request_id": "mcp-pending", "script": "printf later", "wait_seconds": 2}))
                token = await asyncio.to_thread(self.token, peer)
                renewed = await asyncio.wait_for(client.call_tool("renew_session", scope), timeout=1.5)
                self.data(renewed)
                self.data(await client.call_tool("release_session", scope), error=True)
                pending = self.data(await running)
                self.assertTrue(pending["wait_timed_out"])
                self.assertTrue(pending["run"]["pending"])
            self.assertEqual(self.execute()["error"]["code"], "session_busy")
            async with self.client() as replacement:
                recovered = self.data(await replacement.call_tool("get_result", {"request_id": "mcp-pending"}))
                self.assertTrue(recovered["run"]["pending"])
                peer.sendall(token + b"start\x1fafter-reconnect" + token + b"end:0\x1f")
                final = self.data(await replacement.call_tool("get_result", {
                    "request_id": "mcp-pending", "wait_seconds": 3}))
                self.assertTrue(final["run"]["success"])
                self.assertEqual(final["run"]["output"], "after-reconnect")
                self.data(await replacement.call_tool("release_session", scope))
                self.assertFalse(select.select([peer], [], [], 0.1)[0], "MCP restart dispatched a duplicate")

        asyncio.run(scenario())

    def test_missing_control_socket_returns_structured_error_with_recovery_ids(self):
        async def scenario():
            async with self.client(self.root / "absent.sock") as client:
                self.assertEqual(len((await client.list_tools()).tools), 9)
                failure = self.data(await client.call_tool("execute_command", {
                    **self.scope(), "request_id": "recover-me", "script": "printf test"}), error=True)
                self.assertEqual(failure["error"]["code"], "client_error")
                self.assertEqual(failure["request_id"], "recover-me")
                self.assertEqual(failure["lease_id"], self.scope()["lease_id"])

        asyncio.run(scenario())

    def test_file_round_trip_metadata_errors_and_cli_interoperation(self):
        self.bash_peer()
        data = bytes(range(256)) * 400 + b"\x00\xff\r\n"
        local = self.root / "upload.bin"
        remote = self.root / "remote.bin"
        received = self.root / "received.bin"
        local.write_bytes(data)

        async def scenario():
            async with self.client() as client:
                scope = self.scope()
                self.data(await client.call_tool("acquire_session", scope))
                uploaded = self.data(await client.call_tool("upload_file", {
                    **scope, "transfer_id": "mcp-upload", "local_path": str(local),
                    "remote_path": str(remote), "wait_seconds": 5}))
                self.assertTrue(uploaded["transfer"]["success"], uploaded)
                self.assertEqual(uploaded["transfer"]["sha256"], hashlib.sha256(data).hexdigest())
                self.assertLess(len(json.dumps(uploaded)), 2500)
                downloaded = self.data(await client.call_tool("download_file", {
                    **scope, "transfer_id": "mcp-download", "local_path": str(received),
                    "remote_path": str(remote), "wait_seconds": 5}))
                self.assertTrue(downloaded["transfer"]["success"], downloaded)
                self.assertEqual(received.read_bytes(), data)
                existing = self.data(await client.call_tool("upload_file", {
                    **scope, "transfer_id": "mcp-exists", "local_path": str(local),
                    "remote_path": str(remote), "wait_seconds": 5}), error=True)
                self.assertEqual(existing["transfer"]["error"]["code"], "destination_exists")
                invalid = await client.call_tool("download_file", {
                    **scope, "transfer_id": "bad-bool", "local_path": str(received),
                    "remote_path": str(remote), "overwrite": "false"})
                self.assertTrue(invalid.is_error)
                self.data(await client.call_tool("release_session", scope))
                recovered = self.data(await client.call_tool("get_transfer", {"transfer_id": "mcp-download"}))
                self.assertTrue(recovered["transfer"]["success"])

        asyncio.run(scenario())
        code, result = self.cli("transfer", "mcp-upload")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["transfer"]["history_saved"])

    def test_transfer_wait_and_adapter_restart_preserve_operation(self):
        from test_transfers import TransferIntegrationTests
        peer = self.peer()

        async def scenario():
            async with self.client() as client:
                scope = self.scope()
                self.data(await client.call_tool("acquire_session", scope))
                pending = self.data(await client.call_tool("download_file", {
                    **scope, "transfer_id": "waiting-file", "local_path": str(self.root / "download"),
                    "remote_path": "/remote/file", "wait_seconds": 0.1}))
                self.assertTrue(pending["wait_timed_out"], pending)
                token = await asyncio.to_thread(TransferIntegrationTests.transfer_token, peer)
                self.data(await client.call_tool("renew_session", scope))
                self.data(await client.call_tool("release_session", scope), error=True)
                waiting = asyncio.create_task(client.call_tool("get_transfer", {
                    "transfer_id": "waiting-file", "wait_seconds": 30}))
                await asyncio.sleep(0.1)
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
            async with self.client() as replacement:
                result = self.data(await replacement.call_tool("get_transfer", {"transfer_id": "waiting-file"}))
                self.assertTrue(result["transfer"]["pending"])
                self.assertFalse(select.select([peer], [], [], 0.1)[0], "adapter restart resent transfer")
                digest = hashlib.sha256(b"").hexdigest().encode()
                for body in (b"meta:0:" + digest, b"verified:0:" + digest, b"end:0"):
                    TransferIntegrationTests.frame(peer, token, body)
                final = self.data(await replacement.call_tool("get_transfer", {
                    "transfer_id": "waiting-file", "wait_seconds": 3}))
                self.assertTrue(final["transfer"]["success"], final)
                self.data(await replacement.call_tool("release_session", scope))

        asyncio.run(scenario())

    def test_cancelled_tool_wait_preserves_run_and_lease_without_blocking_other_tools(self):
        peer = self.peer()

        async def scenario():
            async with self.client() as client:
                scope = self.scope()
                self.data(await client.call_tool("acquire_session", scope))
                submitted = self.data(await client.call_tool("execute_command", {
                    **scope, "request_id": "mcp-cancel-wait", "script": "printf eventually", "wait_seconds": 0}))
                token = await asyncio.to_thread(self.token, peer)
                waiting = asyncio.create_task(client.call_tool("get_result", {
                    "run_id": submitted["run"]["run_id"], "wait_seconds": 30}))
                await asyncio.sleep(0.3)
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
                renewed = await asyncio.wait_for(client.call_tool("renew_session", scope), timeout=1.5)
                self.data(renewed)
                self.assertEqual(self.execute()["error"]["code"], "session_busy")
                peer.sendall(token + b"start\x1feventually" + token + b"end:0\x1f")
                final = self.data(await client.call_tool("get_result", {
                    "request_id": "mcp-cancel-wait", "wait_seconds": 3}))
                self.assertEqual(final["run"]["output"], "eventually")
                self.assertTrue(final["run"]["success"])
                self.data(await client.call_tool("release_session", scope))

        asyncio.run(scenario())
