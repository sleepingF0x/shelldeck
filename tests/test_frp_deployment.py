"""VPS allowlist configuration and atomic updates."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("frp_allowlist", ROOT / "examples/frp/allowlist.py")
allowlist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(allowlist)


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
