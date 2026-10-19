#!/usr/bin/env python3
"""JSON CLI for agents controlling an existing shelldeck console."""

import argparse
import asyncio
import math
from pathlib import Path
import sys

from shelldeck_control import encode_message, error_response, request_with_wait


def wait_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("must be a finite number between 0 and 3600")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="Unix socket from shelldeck --agent-socket")
    commands = parser.add_subparsers(dest="method", required=True)
    commands.add_parser("sessions", help="list sessions and the console instance ID")
    for method in ("acquire", "renew", "release"):
        lease = commands.add_parser(method, help=f"{method} exclusive ownership across task steps")
        lease.add_argument("--session", required=True, type=int, dest="session_id")
        lease.add_argument("--instance", required=True)
        lease.add_argument("--lease-id", required=True, help="random 32-character lowercase hex ID; keep it for the task")
        if method != "release":
            lease.add_argument("--ttl", type=int, default=300, dest="ttl_seconds", metavar="SECONDS",
                               help="lease duration, 1-3600 seconds; default 300")
        if method == "acquire":
            lease.add_argument("--task", default="agent task", help="short task label shown to the operator")
    execute = commands.add_parser("exec", help="run a noninteractive Bash command on exactly one session")
    execute.add_argument("--session", required=True, type=int, dest="session_id")
    execute.add_argument("--instance", required=True, help="instance returned by sessions")
    execute.add_argument("--request-id", required=True, help="unique command ID; reuse only for an identical retry")
    execute.add_argument("--lease-id", help="required when the session is reserved by a task")
    source = execute.add_mutually_exclusive_group(required=True)
    source.add_argument("--command", help="Bash source text")
    source.add_argument("--file", help="local UTF-8 script file; - reads stdin")
    execute.add_argument("--wait", type=wait_seconds, default=0, metavar="SECONDS",
                         help="wait for completion; timeout never cancels or resends the command")
    result = commands.add_parser("result", help="read a run, including partial output and unknown outcomes")
    result.add_argument("run_id", type=int, nargs="?")
    result.add_argument("--request-id", help="look up a command whose dispatch response was lost")
    result.add_argument("--wait", type=wait_seconds, default=0, metavar="SECONDS")
    for method in ("upload", "download"):
        transfer = commands.add_parser(method, help=f"{method} one regular file through an owned session")
        transfer.add_argument("--session", required=True, type=int, dest="session_id")
        transfer.add_argument("--instance", required=True)
        transfer.add_argument("--lease-id", required=True)
        transfer.add_argument("--transfer-id", required=True, help="unique operation ID; identical retries never resend")
        transfer.add_argument("--local", required=True, dest="local_path", help="path on the shelldeck console host")
        transfer.add_argument("--remote", required=True, dest="remote_path", help="absolute remote regular-file path")
        transfer.add_argument("--overwrite", action="store_true", help="replace destination only after verification")
        transfer.add_argument("--wait", type=wait_seconds, default=0, metavar="SECONDS")
    transfer = commands.add_parser("transfer", help="query file progress and final size/SHA-256 verification")
    transfer.add_argument("transfer_id")
    transfer.add_argument("--wait", type=wait_seconds, default=0, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.method == "result" and (args.run_id is None) == (args.request_id is None):
        parser.error("result requires exactly one of RUN_ID or --request-id")
    context = {}
    payload = {"method": args.method}
    try:
        if args.method in ("acquire", "renew", "release"):
            context = {"instance": args.instance, "session_id": args.session_id, "lease_id": args.lease_id}
            payload.update(context)
            if args.method != "release":
                payload["ttl_seconds"] = args.ttl_seconds
            if args.method == "acquire":
                payload["task"] = args.task
        elif args.method == "exec":
            context = {"request_id": args.request_id, "instance": args.instance}
            if args.lease_id is not None:
                context["lease_id"] = args.lease_id
            if args.command is not None:
                script = args.command
            else:
                if args.file == "-":
                    source_bytes = sys.stdin.buffer.read(65537)
                else:
                    with Path(args.file).expanduser().open("rb") as source_file:
                        source_bytes = source_file.read(65537)
                if len(source_bytes) > 65536:
                    raise ValueError("script exceeds the 64 KiB send limit")
                script = source_bytes.decode("utf-8-sig")
            payload.update(context, session_id=args.session_id, script=script)
        elif args.method == "result":
            context = {"run_id": args.run_id} if args.run_id is not None else {"request_id": args.request_id}
            payload.update(context)
        elif args.method in ("upload", "download"):
            context = {"instance": args.instance, "session_id": args.session_id,
                       "lease_id": args.lease_id, "transfer_id": args.transfer_id}
            payload.update(context, local_path=str(Path(args.local_path).expanduser().absolute()),
                           remote_path=args.remote_path, overwrite=args.overwrite)
        elif args.method == "transfer":
            context = {"transfer_id": args.transfer_id}
            payload.update(context)
        response = asyncio.run(request_with_wait(args.socket, payload, getattr(args, "wait", 0)))
        # Context makes an uncertain submission recoverable without inventing a new ID.
        response = {**context, **response}
    except (OSError, ValueError, RecursionError) as error:
        response = {**context, **error_response("client_error", str(error))}
    except KeyboardInterrupt:
        response = {**context, **error_response("interrupted", "Stopped waiting locally; remote execution was not cancelled")}
    sys.stdout.buffer.write(encode_message(response))
    if not response["ok"]:
        return 2
    operation = response.get("run", response.get("transfer"))
    if operation is None:
        return 0
    if not operation["completed"]:
        return 3
    return 0 if operation["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
