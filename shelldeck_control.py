"""Bounded, one-request-per-connection JSON transport on a private Unix socket."""

import asyncio
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import time


PROTOCOL_VERSION = 1
REQUEST_LIMIT = 512 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
CLIENT_LIMIT = 16
CLIENT_TIMEOUT = 5


class AgentError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def error_response(code, message):
    return {"ok": False, "protocol_version": PROTOCOL_VERSION,
            "error": {"code": code, "message": message}}


def encode_message(value):
    # Escape terminal controls, including bytes that came from the remote shell.
    return (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


class AgentServer:
    def __init__(self, path, selector, dispatch):
        self.path = Path(path).expanduser().absolute()
        self.selector = selector
        self.dispatch = dispatch
        self.clients = {}
        self.identity = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = self.path.parent.stat()
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise ValueError("agent socket parent must be owned by the current user with mode 0700")
            # Never replace an existing listener, ordinary file, or stale socket automatically.
            self.listener.bind(str(self.path))
            node = self.path.lstat()
            self.identity = (node.st_dev, node.st_ino)
            self.path.chmod(0o600)
            self.listener.listen(CLIENT_LIMIT)
            self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ, self.accept)
        except (OSError, ValueError):
            self.listener.close()
            self.remove_socket()
            raise

    def accept(self, _sock, _mask):
        for _ in range(CLIENT_LIMIT):
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                return
            if len(self.clients) >= CLIENT_LIMIT:
                sock.close()
                continue
            sock.setblocking(False)
            self.clients[sock] = {"input": bytearray(), "output": bytearray(),
                                  "deadline": time.monotonic() + CLIENT_TIMEOUT}
            self.selector.register(sock, selectors.EVENT_READ, self.service)

    def service(self, sock, mask):
        client = self.clients.get(sock)
        if client is None:
            return
        if time.monotonic() >= client["deadline"]:
            self.drop(sock)
            return
        try:
            if mask & selectors.EVENT_READ:
                chunk = sock.recv(65536)
                if not chunk:
                    self.drop(sock)
                    return
                client["input"].extend(chunk)
                if len(client["input"]) > REQUEST_LIMIT:
                    self.reply(sock, error_response("request_too_large", "JSON request exceeds 512 KiB"))
                    return
                if b"\n" in client["input"]:
                    line, remainder = bytes(client["input"]).split(b"\n", 1)
                    try:
                        if remainder.strip():
                            raise ValueError("send exactly one JSON request per connection")
                        request = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError) as error:
                        response = error_response("invalid_json", str(error))
                    else:
                        try:
                            response = self.dispatch(request)
                        except AgentError as error:
                            response = error_response(error.code, str(error))
                    self.reply(sock, response)
            elif mask & selectors.EVENT_WRITE:
                sent = sock.send(client["output"])
                if not sent:
                    self.drop(sock)
                    return
                del client["output"][:sent]
                if not client["output"]:
                    self.drop(sock)
        except BlockingIOError:
            return
        except OSError:
            self.drop(sock)

    def reply(self, sock, response):
        packet = encode_message(response)
        if len(packet) > RESPONSE_LIMIT:
            packet = encode_message(error_response("response_too_large", "JSON response exceeds 2 MiB"))
        client = self.clients[sock]
        client["input"].clear()
        client["output"] = bytearray(packet)
        self.selector.modify(sock, selectors.EVENT_WRITE, self.service)

    def drop(self, sock):
        if sock in self.clients:
            del self.clients[sock]
            self.selector.unregister(sock)
            sock.close()

    def expire(self):
        now = time.monotonic()
        for sock, client in list(self.clients.items()):
            if now >= client["deadline"]:
                self.drop(sock)

    def remove_socket(self):
        if self.identity is not None:
            try:
                node = self.path.lstat()
                if (node.st_dev, node.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass

    def close(self):
        for sock in list(self.clients):
            self.drop(sock)
        self.selector.unregister(self.listener)
        self.listener.close()
        self.remove_socket()


def request(socket_path, payload, timeout=CLIENT_TIMEOUT):
    """No automatic retries: a missing response does not mean dispatch failed."""
    packet = encode_message({"protocol_version": PROTOCOL_VERSION, **payload})
    if len(packet) > REQUEST_LIMIT:
        raise ValueError("JSON request exceeds 512 KiB")
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(Path(socket_path).expanduser()))
        sock.sendall(packet)
        data = bytearray()
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("agent response timed out")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("agent socket closed before a complete response")
            data.extend(chunk)
            if len(data) > RESPONSE_LIMIT:
                raise ValueError("JSON response exceeds 2 MiB")
    response = json.loads(data)
    if (not isinstance(response, dict) or type(response.get("ok")) is not bool or
            response.get("protocol_version") != PROTOCOL_VERSION):
        raise ValueError("invalid agent response")
    return response


async def request_with_wait(socket_path, payload, wait_seconds=0):
    """Submit exactly once, then poll by run/transfer ID; never cancel remote work."""
    response = await asyncio.to_thread(request, socket_path, payload)
    context = {key: payload[key] for key in ("instance", "session_id", "lease_id", "request_id", "run_id", "transfer_id")
               if key in payload}
    result_key = "transfer" if "transfer" in response else "run"
    if response["ok"] and result_key in response:
        id_key = "transfer_id" if result_key == "transfer" else "run_id"
        context[id_key] = response[result_key][id_key]
        lookup = {"method": "transfer" if result_key == "transfer" else "result", id_key: context[id_key]}
        replayed = response.get("replayed")
        deadline = time.monotonic() + wait_seconds
        while response["ok"] and response[result_key]["pending"] and wait_seconds > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.2, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            response = await asyncio.to_thread(request, socket_path, lookup,
                                               timeout=min(CLIENT_TIMEOUT, remaining))
        if response["ok"]:
            response["wait_timed_out"] = wait_seconds > 0 and response[result_key]["pending"]
            if replayed is not None:
                response["replayed"] = replayed
    return {**context, **response}
