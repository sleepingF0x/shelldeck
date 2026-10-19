#!/usr/bin/env python3
"""Run the stock CentOS Bash-peer matrix against a modern local Python controller.

Pull the requested linux/amd64 image first. No packages are installed in peers.
Use the optional MCP virtualenv so the two real SDK tests can also run.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docker_peer import docker_command


CASES = [
    "test_transfers.TransferIntegrationTests.test_cli_binary_round_trip_larger_than_output_limit_and_command_afterwards",
    "test_transfers.TransferIntegrationTests.test_empty_files_and_explicit_overwrite_both_directions",
    "test_transfers.TransferIntegrationTests.test_replay_never_rereads_or_resends_and_conflicts_bind_lease_and_paths",
    "test_transfers.TransferIntegrationTests.test_invalid_paths_fifo_symlinks_and_remote_directory_are_rejected",
    "test_transfers.TransferIntegrationTests.test_missing_tools_fail_cleanly_and_leave_bash_usable",
    "test_agent.AgentIntegrationTests.test_cli_two_step_task_on_existing_bash_preserves_session_and_target",
    "test_agent.AgentIntegrationTests.test_exit_failure_and_script_file_are_reported",
    "test_leases.LeaseIntegrationTests.test_cli_two_steps_keep_exclusivity_between_commands_and_release_to_human",
    "test_mcp.MCPIntegrationTests.test_sdk_handshake_schemas_two_step_bash_task_and_cli_interoperation",
    "test_mcp.MCPIntegrationTests.test_file_round_trip_metadata_errors_and_cli_interoperation",
    "centos_cases.CentOSFileCases.test_chunk_and_base64_padding_boundaries",
    "centos_cases.CentOSFileCases.test_32_mib_round_trip_with_independent_file_hashes",
    "centos_cases.CentOSFileCases.test_remote_checksum_failure_preserves_old_file_and_shell",
    "centos_cases.CentOSFileCases.test_disconnect_during_real_upload_preserves_old_file_and_cleans_stage",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="already pulled image, e.g. centos:6.10 or centos:7.9.2009")
    parser.add_argument("--context", help="local Docker context")
    parser.add_argument("--report", type=Path, help="save version/digest and exact test outcomes as JSON")
    parser.add_argument("--match", help="run only case names containing this text")
    args = parser.parse_args()
    cases = [name for name in CASES if not args.match or args.match in name]
    if not cases:
        parser.error("--match did not select any cases")
    started_at = datetime.now(timezone.utc).isoformat()
    os.environ["SHELLDECK_TEST_BASH_IMAGE"] = args.image
    if args.context:
        os.environ["SHELLDECK_TEST_DOCKER_CONTEXT"] = args.context
    image = json.loads(subprocess.check_output(docker_command() + ["image", "inspect", args.image]))[0]
    versions = subprocess.check_output(docker_command() + [
        "run", "--rm", "--pull=never", "--platform", "linux/amd64", "--network", "none", args.image,
        "/bin/bash", "-c", "cat /etc/centos-release; bash --version | head -n 1; "
        "rpm -q bash coreutils glibc; uname -r; python --version 2>&1; "
        "for tool in base64 sha256sum wc dd mktemp link ln mv perl python3; do command -v \"$tool\"; done; :"], text=True)
    print(versions, flush=True)
    outcomes = []

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            self.started = time.monotonic()
            self.status = "passed"
            super().startTest(test)

        def addFailure(self, test, err):
            self.status = "failed"
            super().addFailure(test, err)

        def addError(self, test, err):
            self.status = "failed"
            super().addError(test, err)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.status = "failed"
            super().addSubTest(test, subtest, err)

        def addSkip(self, test, reason):
            if self.status != "failed":
                self.status = "skipped"
            super().addSkip(test, reason)

        def stopTest(self, test):
            outcomes.append({"test": test.id(), "seconds": round(time.monotonic() - self.started, 3),
                             "status": self.status})
            super().stopTest(test)

    suite = unittest.defaultTestLoader.loadTestsFromNames(cases)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "image": args.image, "image_id": image["Id"], "repo_digests": image["RepoDigests"],
            "versions": versions, "controller_python": sys.version, "controller_platform": platform.platform(),
            "source_sha256": {name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
                              for name in ("shelldeck_transfer.py", "shelldeck.py", "shelldeck_control.py",
                                           "shelldeck_agent.py", "shelldeck_mcp.py", "shelldeck_history.py")},
            "tests": outcomes,
            "failures": [{"test": test.id(), "traceback": trace} for test, trace in result.failures + result.errors],
            "skips": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
            "success": result.wasSuccessful() and not result.skipped,
        }, indent=2) + "\n")
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
