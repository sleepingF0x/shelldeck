"""Opt-in stock-image Bash peers. No package installation or remote Python.

Only a temporary test directory is mounted. The container establishes the TCP
connection itself with Bash /dev/tcp; docker stdio is not the session transport.
"""

import os
import secrets
import subprocess
import sys


def docker_command():
    context = os.environ.get("SHELLDECK_TEST_DOCKER_CONTEXT")
    return ["docker"] + (["--context", context] if context else [])


def start_bash_peer(case, image, node):
    name = "shelldeck-compat-" + secrets.token_hex(8)
    network = os.environ.get("SHELLDECK_TEST_DOCKER_NETWORK", "host" if sys.platform == "linux" else "bridge")
    host = os.environ.get("SHELLDECK_TEST_DOCKER_HOST", "127.0.0.1" if network == "host" else "host.docker.internal")
    root = str(case.root.resolve())
    session_id = max((s["id"] for s in case.call("sessions")["sessions"]), default=0) + 1
    log = (case.root / (name + ".log")).open("wb")
    case.addCleanup(log.close)
    process = subprocess.Popen(docker_command() + [
        "run", "--rm", "--pull=never", "--platform", "linux/amd64", "--name", name,
        "--network", network, "--mount", f"type=bind,source={root},target={root}",
        "--workdir", root, "--env", "NODE=" + node, "--env", "PS1=AGENT-TEST> ",
        image, "/bin/bash", "--noprofile", "--norc", "-c",
        'exec /bin/bash --noprofile --norc -i < /dev/tcp/"$1"/"$2" >&0 2>&0',
        "shelldeck-test-peer", host, str(case.console.port)], stdout=log, stderr=log)

    def cleanup():
        subprocess.run(docker_command() + ["rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15)
        process.wait(timeout=10)

    case.addCleanup(cleanup)
    try:
        case.console.read_until(f"Session {session_id} connected".encode(), timeout=30)
    except AssertionError as error:
        log.flush()
        raise AssertionError((case.root / (name + ".log")).read_text(errors="replace")) from error
    return process
