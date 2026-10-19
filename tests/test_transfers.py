"""File bytes, publication guarantees and ownership through real Bash/TCP."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import sqlite3
import subprocess
import time
import unittest
from unittest import mock

from shelldeck_transfer import CHUNK_SIZE, FileTransfer, REMOTE_SCRIPT
from test_agent import AgentFixture


OWNER = "e" * 32


class TransferIntegrationTests(AgentFixture):
    def own(self, ttl=300):
        self.scope = {"instance": self.instance, "session_id": 1, "lease_id": OWNER}
        self.assertTrue(self.call("acquire", **self.scope, ttl_seconds=ttl)["ok"])

    def transfer(self, direction="upload", transfer_id="file-1", **fields):
        args = {"local_path": str(self.root / "local.bin"), "remote_path": str(self.root / "remote.bin")}
        return self.call(direction, **self.scope, transfer_id=transfer_id, **{**args, **fields})

    def finished(self, transfer_id="file-1"):
        deadline = time.monotonic() + 15
        while True:
            response = self.call("transfer", transfer_id=transfer_id)
            self.assertTrue(response["ok"], response)
            if not response["transfer"]["pending"]:
                return response["transfer"]
            self.assertLess(time.monotonic(), deadline, response)
            time.sleep(0.02)

    def transfer_cli(self, direction, transfer_id, local_path, remote_path, *extra):
        return self.cli(direction, "--instance", self.instance, "--session", "1", "--lease-id", OWNER,
                        "--transfer-id", transfer_id, "--local", str(local_path), "--remote", str(remote_path), *extra)

    @staticmethod
    def transfer_token(peer):
        data = bytearray()
        while not data.endswith(b"\n"):
            data.extend(peer.recv(65536))
        return re.search(rb"SHELLDECK_TRANSFER_[0-9a-f]{32}", data)[0]

    @staticmethod
    def frame(peer, token, body):
        peer.sendall(b"\x1e" + token + b":" + body + b"\x1f")

    def test_cli_binary_round_trip_larger_than_output_limit_and_command_afterwards(self):
        self.bash_peer()
        self.own()
        data = bytes(range(256)) * 8192 + b"\xef\xbb\xbf\x00\xff\r\n\n"
        local = self.root / "local.bin"
        local.write_bytes(data)
        remote = self.root / "中文 'quoted' $(touch INJECTED) `id`\n.bin"
        code, uploaded = self.transfer_cli("upload", "round-up", local, remote)
        self.assertIn(code, (0, 3), uploaded)
        result = self.finished("round-up")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["history_saved"], result)
        self.assertEqual(result["bytes_transferred"], len(data))
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(remote.read_bytes(), data)
        self.assertFalse((self.root / "INJECTED").exists())
        downloaded = self.root / "downloaded.bin"
        code, started = self.transfer_cli("download", "round-down", downloaded, remote)
        self.assertIn(code, (0, 3), started)
        result = self.finished("round-down")
        self.assertTrue(result["success"], result)
        self.assertEqual(downloaded.read_bytes(), data)
        self.assertEqual(downloaded.stat().st_mode & 0o777, 0o600)
        self.assertLess(len(json.dumps(result)), 2000)
        code, status = self.cli("transfer", "round-down", "--wait", "1")
        self.assertEqual(code, 0, status)
        run = self.call("exec", **self.scope, request_id="after-files", script="printf usable")
        self.assertEqual(self.result(run["run"]["run_id"])["output"], "usable")
        self.assertTrue(self.call("release", **self.scope)["released"])
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            rows = db.execute("SELECT parameters, result FROM file_transfers").fetchall()
        self.assertNotIn(OWNER, repr(rows))
        self.assertLess(len(repr(rows)), 5000)

    def test_empty_files_and_explicit_overwrite_both_directions(self):
        self.bash_peer()
        self.own()
        local, remote = self.root / "local.bin", self.root / "remote.bin"
        local.write_bytes(b"")
        remote.write_bytes(b"original remote")
        self.assertTrue(self.transfer()["ok"])
        failed = self.finished()
        self.assertFalse(failed["success"], failed)
        self.assertEqual(failed["error"]["code"], "destination_exists")
        self.assertEqual(remote.read_bytes(), b"original remote")
        self.transfer(transfer_id="overwrite-up", overwrite=True)
        self.assertTrue(self.finished("overwrite-up")["success"])
        self.assertEqual(remote.read_bytes(), b"")
        local.write_bytes(b"original local")
        denied = self.transfer("download", "deny-local")
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(local.read_bytes(), b"original local")
        self.transfer("download", "overwrite-down", overwrite=True)
        final = self.finished("overwrite-down")
        self.assertTrue(final["success"], final)
        self.assertEqual(final["total_bytes"], 0)
        self.assertEqual(local.read_bytes(), b"")
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])

    def test_replay_never_rereads_or_resends_and_conflicts_bind_lease_and_paths(self):
        peer = self.bash_peer()
        self.own()
        local = self.root / "local.bin"
        local.write_bytes(b"first")
        self.transfer()
        self.assertTrue(self.finished()["success"])
        local.unlink()
        replay = self.transfer()
        self.assertTrue(replay["replayed"], replay)
        self.assertEqual((self.root / "remote.bin").read_bytes(), b"first")
        self.assertEqual(self.transfer(remote_path=str(self.root / "other"))["error"]["code"], "transfer_conflict")
        self.call("release", **self.scope)
        self.assertTrue(self.transfer()["replayed"])
        self.scope["lease_id"] = "f" * 32
        self.call("acquire", **self.scope)
        self.assertEqual(self.transfer()["error"]["code"], "transfer_conflict")

    def test_lease_expiry_does_not_allow_command_or_manual_input_into_stream(self):
        peer = self.peer()
        self.own(ttl=1)
        self.transfer("download")
        token = self.transfer_token(peer)
        time.sleep(1.1)
        self.assertEqual(self.execute()["error"]["code"], "session_busy")
        new_owner = {**self.scope, "lease_id": "f" * 32}
        self.assertEqual(self.call("acquire", **new_owner)["error"]["code"], "session_busy")
        self.console.attach(1)
        self.console.type(b"should-not-enter-stream\r")
        self.console.read_until(b"File transfer in progress")
        self.console.detach()
        self.assertFalse(select.select([peer], [], [], 0.1)[0])
        self.frame(peer, token, b"error:invalid_source")
        self.frame(peer, token, b"end:1")
        self.assertFalse(self.finished()["success"])
        self.assertTrue(self.call("acquire", **new_owner)["ok"])
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])

    def test_download_checksum_failure_preserves_existing_destination(self):
        peer = self.peer()
        self.own()
        local = self.root / "local.bin"
        local.write_bytes(b"keep")
        self.transfer("download", overwrite=True)
        token = self.transfer_token(peer)
        digest = hashlib.sha256(b"good").hexdigest().encode()
        for frame in (b"meta:4:" + digest, b"data:" + base64.b64encode(b"evil"),
                      b"verified:4:" + digest, b"end:0"):
            self.frame(peer, token, frame)
        final = self.finished()
        self.assertFalse(final["success"], final)
        self.assertEqual(final["error"]["code"], "hash_mismatch")
        self.assertEqual(local.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])

    def test_download_destination_created_during_transfer_is_not_overwritten(self):
        peer = self.peer()
        self.own()
        self.transfer("download")
        token = self.transfer_token(peer)
        local = self.root / "local.bin"
        local.write_bytes(b"created by someone else")
        digest = hashlib.sha256(b"").hexdigest().encode()
        self.frame(peer, token, b"meta:0:" + digest)
        self.frame(peer, token, b"verified:0:" + digest)
        self.frame(peer, token, b"end:0")
        final = self.finished()
        self.assertEqual(final["error"]["code"], "publish_failed")
        self.assertEqual(local.read_bytes(), b"created by someone else")

    def test_disconnect_removes_partial_download_and_never_reports_success(self):
        peer = self.peer()
        self.own()
        local = self.root / "local.bin"
        local.write_bytes(b"keep")
        self.transfer("download", overwrite=True)
        token = self.transfer_token(peer)
        self.frame(peer, token, b"meta:10:" + b"0" * 64)
        self.frame(peer, token, b"data:" + base64.b64encode(b"part"))
        peer.close()
        final = self.finished()
        self.assertIsNone(final["success"])
        self.assertFalse(final["completed"])
        self.assertEqual(local.read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])

    def test_invalid_frame_closes_desynchronized_stream_and_preserves_destination(self):
        peer = self.peer()
        self.own()
        self.transfer("download")
        token = self.transfer_token(peer)
        self.frame(peer, token, b"meta:1:" + b"0" * 64)
        self.frame(peer, token, b"data:???")
        final = self.finished()
        self.assertEqual(final["state"], "protocol-error")
        self.assertEqual(final["error"]["code"], "protocol_error")
        self.assertFalse((self.root / "local.bin").exists())
        self.assertFalse(self.call("sessions")["sessions"][0]["connected"])

    def test_missing_tools_fail_cleanly_and_leave_bash_usable(self):
        self.bash_peer()
        restricted = self.root / "restricted-path"
        restricted.mkdir()
        os.symlink("/bin/bash", restricted / "bash")
        self.console.attach(1)
        self.console.type(("export PATH=" + shlex.quote(str(restricted)) + "; printf 'PATH-SET\\n'\n").encode())
        self.console.read_until(b"PATH-SET")
        self.console.detach()
        self.own()
        (self.root / "local.bin").write_bytes(b"data")
        self.transfer()
        result = self.finished()
        self.assertEqual(result["error"]["code"], "unsupported_tools")
        run = self.call("exec", **self.scope, request_id="after-tool-failure", script="printf still-usable")
        self.assertEqual(self.result(run["run"]["run_id"])["output"], "still-usable")

    def test_invalid_paths_fifo_symlinks_and_remote_directory_are_rejected(self):
        self.bash_peer()
        self.own()
        local = self.root / "local.bin"
        local.write_bytes(b"data")
        for path in ("relative", "/tmp/", "/tmp/x\0y", "/tmp/."):
            with self.subTest(path=path):
                self.assertFalse(self.transfer(remote_path=path)["ok"])
        local.unlink()
        os.mkfifo(local)
        self.assertFalse(self.transfer()["ok"])
        local.unlink()
        target = self.root / "target"
        target.write_bytes(b"keep")
        local.symlink_to(target)
        self.assertFalse(self.transfer()["ok"])
        self.assertFalse(self.transfer("download", overwrite=True)["ok"])
        local.unlink()
        local.write_bytes(b"data")
        remote = self.root / "remote.bin"
        remote.symlink_to(target)
        self.transfer(overwrite=True)
        self.assertEqual(self.finished()["error"]["code"], "invalid_destination")
        self.assertEqual(target.read_bytes(), b"keep")
        self.transfer("download", "directory", local_path=str(self.root / "absent"), remote_path=str(self.root))
        self.assertEqual(self.finished("directory")["error"]["code"], "invalid_source")

    def test_storage_failure_prevents_dispatch_and_cleans_staging(self):
        peer = self.peer()
        self.own()
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            db.execute("CREATE TRIGGER reject_transfer BEFORE INSERT ON file_transfers "
                       "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        response = self.transfer("download")
        self.assertEqual(response["error"]["code"], "history_unavailable")
        self.assertFalse(select.select([peer], [], [], 0.1)[0])
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])
        self.assertTrue(self.call("release", **self.scope)["released"])

    def test_lost_submission_reply_reuses_one_transfer_and_pending_history_recovers(self):
        peer = self.peer()
        self.own()
        payload = {"protocol_version": 1, "method": "download", **self.scope, "transfer_id": "file-1",
                   "local_path": str(self.root / "local.bin"), "remote_path": str(self.root / "remote.bin")}
        with socket.socket(socket.AF_UNIX) as control:
            control.connect(str(self.socket_path))
            control.sendall(json.dumps(payload).encode() + b"\n")
        token = self.transfer_token(peer)
        replay = self.transfer("download")
        self.assertTrue(replay["replayed"], replay)
        self.assertEqual(len(list(self.root.glob(".shelldeck-transfer*"))), 1)
        self.assertFalse(select.select([peer], [], [], 0.1)[0])
        digest = hashlib.sha256(b"").hexdigest().encode()
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            db.execute("BEGIN IMMEDIATE")
            for body in (b"meta:0:" + digest, b"verified:0:" + digest, b"end:0"):
                self.frame(peer, token, body)
            self.console.read_until(b"Execution history could not be saved")
            result = self.finished()
            self.assertTrue(result["success"])
            self.assertFalse(result["history_saved"])
            denied = self.transfer("download", "new-while-locked", local_path=str(self.root / "other"))
            self.assertEqual(denied["error"]["code"], "history_unavailable")
        self.assertTrue(self.finished()["history_saved"])

    def test_changing_upload_source_aborts_before_commit(self):
        peer = self.peer()
        self.own()
        local = self.root / "local.bin"
        local.write_bytes(b"original")
        self.transfer()
        token = self.transfer_token(peer)
        self.frame(peer, token, b"ready")
        received = bytearray()
        while not received.endswith(b"\n"):
            received.extend(peer.recv(65536))
        self.assertLess(len(received), 65536)
        local.write_bytes(b"modified and longer")
        self.frame(peer, token, b"ack:1")
        aborted = peer.recv(65536)
        self.assertEqual(aborted, b"#" + token + b":abort\n")
        self.frame(peer, token, b"error:client_aborted")
        self.frame(peer, token, b"end:1")
        result = self.finished()
        self.assertEqual(result["error"]["code"], "local_source_error")
        self.assertFalse(result["success"])

    def test_restart_reports_unknown_and_replay_never_dispatches_to_new_session(self):
        peer = self.peer()
        self.own()
        self.transfer("download")
        self.transfer_token(peer)
        old_scope = dict(self.scope)
        self.console.stop(signal.SIGKILL)
        self.socket_path.unlink()
        self.start()
        replacement = self.peer()
        status = self.call("transfer", transfer_id="file-1")["transfer"]
        self.assertEqual(status["state"], "unknown")
        self.assertIsNone(status["success"])
        replay = self.call("download", **old_scope, transfer_id="file-1",
                           local_path=str(self.root / "local.bin"), remote_path=str(self.root / "remote.bin"))
        self.assertTrue(replay["replayed"], replay)
        self.assertFalse(select.select([replacement], [], [], 0.1)[0])


class RemotePublicationTests(unittest.TestCase):
    def test_upload_abort_and_bad_hash_preserve_original_and_remove_stage(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            for ending in ("abort", "commit:" + "0" * 64):
                with self.subTest(ending=ending):
                    target.write_bytes(b"keep")
                    token = "SHELLDECK_TRANSFER_" + "1" * 32
                    content = (f"#{token}:data:1:" + base64.b64encode(b"new").decode() + "\n" +
                               f"#{token}:{ending}\n").encode()
                    process = subprocess.run(["bash", "-c", REMOTE_SCRIPT, "test-transfer", token,
                                               "upload", str(target), "1", "3", str(CHUNK_SIZE)],
                                              input=content, capture_output=True, timeout=5)
                    self.assertNotEqual(process.returncode, 0, process)
                    self.assertEqual(target.read_bytes(), b"keep")
                    self.assertEqual(list(Path(directory).glob(".shelldeck-transfer*")), [])

    def test_download_source_change_is_detected_before_verification(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"initial")
            wrapper = root / "base64"
            wrapper.write_text("#!/bin/bash\n/bin/cat > " + shlex.quote(str(root / "encoded-input")) +
                               "\nif [[ $(/bin/cat " + shlex.quote(str(root / "encoded-input")) + ") != f ]]; then " +
                               "printf changed > " + shlex.quote(str(source)) + "; fi" +
                               "\n/usr/bin/base64 \"$@\" < " + shlex.quote(str(root / "encoded-input")) + "\n")
            wrapper.chmod(0o700)
            token = "SHELLDECK_TRANSFER_" + "2" * 32
            process = subprocess.run(["bash", "-c", REMOTE_SCRIPT, "test-transfer", token,
                                       "download", str(source), "0", "0", str(CHUNK_SIZE)],
                                      capture_output=True, timeout=5, env={**os.environ, "PATH": directory + ":" + os.environ["PATH"]})
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"error:source_changed", process.stdout)
            self.assertNotIn(b":verified:", process.stdout)

    def test_local_disk_write_error_drains_without_publishing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_bytes(b"keep")
            transfer = FileTransfer("disk-full", "0" * 32, 1, "download", str(target), "/remote/file", True)
            transfer.prepare()
            real_file = transfer.file
            transfer.file = mock.Mock(wraps=real_file)
            transfer.file.write.side_effect = OSError("No space left on device")
            digest = hashlib.sha256(b"data").hexdigest().encode()
            for body in (b"meta:4:" + digest, b"data:" + base64.b64encode(b"data"),
                         b"verified:4:" + digest, b"end:0"):
                transfer.feed(transfer.marker + body + b"\x1f")
            self.assertEqual(transfer.error["code"], "local_write_error")
            self.assertEqual(transfer.state, "failed")
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertTrue(real_file.closed)
            self.assertEqual(list(Path(directory).glob(".shelldeck-transfer*")), [])


if __name__ == "__main__":
    unittest.main()
