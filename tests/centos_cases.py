"""Additional real-peer checks, run explicitly by run_centos_tests.py."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import time

from test_agent import AgentFixture
from test_transfers import OWNER


class CentOSFileCases(AgentFixture):
    def own(self):
        self.bash_peer()
        self.scope = {"instance": self.instance, "session_id": 1, "lease_id": OWNER}
        self.assertTrue(self.call("acquire", **self.scope, ttl_seconds=900)["ok"])

    def start_transfer(self, direction, transfer_id, local, remote, overwrite=False):
        response = self.call(direction, **self.scope, transfer_id=transfer_id,
                             local_path=str(local), remote_path=str(remote), overwrite=overwrite)
        self.assertTrue(response["ok"], response)

    def finish_transfer(self, transfer_id, timeout=240):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            response = self.call("transfer", transfer_id=transfer_id)
            self.assertTrue(response["ok"], response)
            result = response["transfer"]
            if not result["pending"]:
                return result
            time.sleep(0.2)
        self.fail(f"Transfer did not finish within {timeout}s: {result}")

    @staticmethod
    def file_digest(path):
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def assert_shell_usable(self):
        run = self.call("exec", **self.scope, request_id="after-transfer-failure", script="printf usable")
        self.assertTrue(run["ok"], run)
        self.assertEqual(self.result(run["run"]["run_id"])["output"], "usable")

    def prepend_tools(self, scripts):
        tools_dir = self.root / "test-tools"
        tools_dir.mkdir()
        for name, script in scripts.items():
            tool = tools_dir / name
            tool.write_text("#!/bin/bash\n" + script)
            tool.chmod(0o700)
        # Set the parent interactive Bash environment before obtaining ownership.
        self.console.attach(1)
        self.console.type(("export PATH=" + shlex.quote(str(tools_dir)) +
                           ":$PATH; printf 'TEST-PATH-SET\\n'\n").encode())
        self.console.read_until(b"TEST-PATH-SET")
        self.console.detach()

    def test_chunk_and_base64_padding_boundaries(self):
        self.own()
        for size in (0, 1, 2, 3, 24575, 24576, 24577, 65535, 65536, 65537):
            with self.subTest(size=size):
                local = self.root / f"input-{size}"
                remote = self.root / f"remote-{size}"
                downloaded = self.root / f"received-{size}"
                data = (bytes(range(256)) * ((size + 255) // 256))[:size]
                local.write_bytes(data)
                for direction, path in (("upload", local), ("download", downloaded)):
                    transfer_id = f"boundary-{direction}-{size}"
                    self.start_transfer(direction, transfer_id, path, remote)
                    result = self.finish_transfer(transfer_id, timeout=30)
                    self.assertTrue(result["success"], result)
                    self.assertEqual(result["total_bytes"], size)
                    self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(downloaded.read_bytes(), data)

    def test_32_mib_round_trip_with_independent_file_hashes(self):
        self.own()
        source = self.root / "large-input.bin"
        remote = self.root / "large-remote.bin"
        downloaded = self.root / "large-received.bin"
        block = bytes(range(256)) * 4096
        with source.open("wb") as file:
            for _ in range(32):
                file.write(block)
        digest = self.file_digest(source)
        timings = {}
        for direction, path in (("upload", source), ("download", downloaded)):
            started = time.monotonic()
            self.start_transfer(direction, "large-" + direction, path, remote)
            # This checks file integrity, not a throughput SLA. Old x86 user
            # spaces can be much slower under emulation or concurrent workloads.
            result = self.finish_transfer("large-" + direction, timeout=600)
            timings[direction + "_seconds"] = round(time.monotonic() - started, 3)
            self.assertTrue(result["success"], result)
            self.assertEqual(result["bytes_transferred"], 32 * 1024 * 1024)
            self.assertEqual(result["sha256"], digest)
        self.assertEqual(self.file_digest(remote), digest)
        self.assertEqual(self.file_digest(downloaded), digest)
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])
        print(json.dumps({"image": os.environ.get("SHELLDECK_TEST_BASH_IMAGE"), "bytes": source.stat().st_size,
                          "sha256": digest, **timings}), flush=True)

    def test_remote_checksum_failure_preserves_old_file_and_shell(self):
        self.bash_peer()
        # Deliberately corrupt decoded payload bytes, keeping their length.
        # Leave the capability probe intact so the real receiver reaches SHA-256.
        self.prepend_tools({"base64": '''input=$(/bin/cat)
if [[ ${#input} -gt 16000 ]]; then printf '%s' "$input" | /usr/bin/base64 "$@" | /usr/bin/tr A B
else printf '%s' "$input" | /usr/bin/base64 "$@"
fi
'''})
        self.scope = {"instance": self.instance, "session_id": 1, "lease_id": OWNER}
        self.assertTrue(self.call("acquire", **self.scope)["ok"])
        source, remote = self.root / "input", self.root / "existing"
        source.write_bytes(b"A" * 65536)
        remote.write_bytes(b"keep-original")
        self.start_transfer("upload", "corrupt-payload", source, remote, overwrite=True)
        result = self.finish_transfer("corrupt-payload", timeout=30)
        self.assertFalse(result["success"], result)
        self.assertEqual(result["error"]["code"], "hash_mismatch")
        self.assertEqual(remote.read_bytes(), b"keep-original")
        self.assertEqual(list(self.root.glob(".shelldeck-transfer*")), [])
        self.assert_shell_usable()

    def test_disconnect_during_real_upload_preserves_old_file_and_cleans_stage(self):
        self.own()
        source, remote = self.root / "input", self.root / "existing"
        source.write_bytes(b"X" * (8 * 1024 * 1024))
        remote.write_bytes(b"keep-original")
        self.start_transfer("upload", "disconnect-upload", source, remote, overwrite=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.call("transfer", transfer_id="disconnect-upload")["transfer"]
            if result["bytes_transferred"]:
                self.assertTrue(result["pending"], result)
                break
            time.sleep(0.05)
        else:
            self.fail("No progress before disconnect: " + repr(result))
        self.console.command("close 1")
        result = self.finish_transfer("disconnect-upload", timeout=5)
        self.assertIsNone(result["success"], result)
        self.assertEqual(remote.read_bytes(), b"keep-original")
        deadline = time.monotonic() + 10
        while list(self.root.glob(".shelldeck-transfer.*")) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(list(self.root.glob(".shelldeck-transfer.*")), [])
