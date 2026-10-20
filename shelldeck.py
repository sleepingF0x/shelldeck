#!/usr/bin/env python3
"""ShellDeck: a standard-library-only Bash/TCP session console for Linux and macOS."""

from __future__ import annotations

import argparse
import base64
import collections
import dataclasses
import hashlib
from contextlib import closing
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import shlex
import signal
import socket
import sqlite3
import stat
import sys
import termios
import time
import tty
import unicodedata

from shelldeck_history import HistoryStore, OUTPUT_LIMIT, OUTPUT_PAGE_SIZE, PAGE_SIZE
from shelldeck_control import AgentError, AgentServer, PROTOCOL_VERSION
from shelldeck_transfer import FileTransfer, PENDING_STATES as PENDING_TRANSFER_STATES, validate_path


READ_SIZE = 65536
SEND_LIMIT = 65536
LINE_LIMIT = 8192
CONTROL_LIMIT = 65536
DEFAULT_IDLE_TIMEOUT = 900
JOB_LIMIT = 20
PROXY_HEADER_LIMIT = 107
PROXY_HEADER_TIMEOUT = 5
PENDING_JOB_STATES = {"queued", "sent", "running"}
PROMPT = b"shelldeck> "
HELP = """Commands:
  sessions / ls          List sessions, including retained disconnected sessions
  interact ID / use ID   Attach with local echo and line editing (plain Bash/TCP)
  interact ID raw        Attach without local echo (for a remote PTY)
  close ID               Close the connection and discard its saved output
  close all              Remove all sessions and saved output; keep listening
  name ID|IP NAME         Save a shared name for this IPv4 address
  unname ID|IP            Remove the saved name for this IPv4 address
  note ID|IP TEXT         Save a separate remark for this IPv4 address
  unnote ID|IP            Remove the remark, keeping the name
  names [ID|IP]           Show saved names and remarks, including offline hosts
  batch PATH [--sessions ID,...]  Run a local Bash script (default: all connected)
                         -s is an alias for --sessions; IDs come from sessions / ls
  jobs [ID]              Show batch delivery states and remote exit codes
  history [ID|IP] [FILE] [-p PAGE]  Query saved script runs (ID = current session)
  result RUN [PAGE]      Show one saved run and its output (default: last page)
  help                   Show this help
  quit / exit            Stop the listener and close every connection

While attached: Ctrl+] returns to this menu without closing the connection.
Menu/line editing: Left/Right move; Home/End jump; Backspace/Delete erase; Ctrl+U clears.
Line mode: Enter sends a line; Ctrl+C clears local input without sending a signal.
Raw mode: every byte except Ctrl+] is forwarded, including Ctrl+C and Ctrl+D.
Session management does not create a remote PTY or recover a broken TCP stream.
Idle timeout counts time without submitted input; remote output does not reset it.
"""

# Decode with Bash 3.2+ builtins so old clients need no base64 executable.
# printf -v preserves trailing newlines; exec starts the script with clean shell variables.
# Keep the transport command on one line, including this fixed decoder.
BATCH_DECODER = " ".join(line.strip() for line in r"""
_shelldeck_batch_decode() {
local LC_ALL=C;
local payload=$1 alphabet=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/;
local data padding i block value a b c d octets;
local -a pieces=();
data=${payload%%=*};
padding=${payload:${#data}};
[[ $data && $data != *[!a-zA-Z0-9+/]* ]] || return 1;
[[ $padding == '' || $padding == = || $padding == == ]] || return 1;
((${#payload} % 4 == 0)) || return 1;
for ((i=0; i<${#payload}; i+=4)); do
    block=${payload:i:4};
    a=${alphabet%%"${block:0:1}"*}; b=${alphabet%%"${block:1:1}"*};
    c=${alphabet%%"${block:2:1}"*}; d=${alphabet%%"${block:3:1}"*};
    value=$(((${#a} << 18) | (${#b} << 12) | ((${#c} & 63) << 6) | (${#d} & 63)));
    printf -v octets '\\%03o\\%03o\\%03o' "$((value >> 16))" "$(((value >> 8) & 255))" "$((value & 255))" || return 1;
    case $block in
        *==) octets=${octets:0:4};;
        *=) octets=${octets:0:8};;
    esac;
    [[ $octets != *'\000'* ]] || return 1;
    pieces+=("$octets");
done;
printf -v _shelldeck_batch_script '%b' "${pieces[@]}";
};
if _shelldeck_batch_decode "$1"; then
    [[ ! ${2:-} ]] || printf '\036%s:script\037' "$2";
    exec bash -c "$_shelldeck_batch_script" shelldeck-batch;
else printf '[batch] Invalid Base64 script.\n' >&2; exit 125; fi
""".strip().splitlines())


class Buffer:
    """A bounded byte FIFO; overflow drops oldest bytes, never splits routing."""

    def __init__(self, limit: int):
        self.limit = limit
        self.parts = collections.deque()
        self.size = 0
        self.dropped = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        if self.parts and len(self.parts[-1]) + len(data) <= 8192:
            self.parts[-1] += data
        else:
            self.parts.append(data)
        self.size += len(data)
        overflow = max(0, self.size - self.limit)
        self.dropped += overflow
        self.consume(overflow)

    def consume(self, count: int) -> None:
        while count:
            part = self.parts[0]
            taken = min(count, len(part))
            if taken == len(part):
                self.parts.popleft()
            else:
                self.parts[0] = part[taken:]
            count -= taken
            self.size -= taken

    def head(self) -> bytes:
        return self.parts[0][:READ_SIZE] if self.parts else b""


@dataclasses.dataclass
class BatchResult:
    job_id: int
    session_id: int
    peer: str
    marker: bytes
    state: str = "queued"
    exit_code: int = None
    pending: bytes = b""
    record_id: int = None
    received_at: float = None
    started_at: float = None
    finished_at: float = None
    log: Buffer = dataclasses.field(default_factory=lambda: Buffer(OUTPUT_LIMIT))

    def feed(self, data: bytes) -> bytes:
        """Remove only this run's control frames, retaining split frame prefixes."""
        data = self.pending + data
        self.pending = b""
        output = bytearray()

        def record(chunk):
            output.extend(chunk)
            if self.state == "running":
                self.log.append(chunk)

        while data:
            position = data.find(self.marker)
            if position < 0:
                keep = next((n for n in range(min(len(data), len(self.marker) - 1), 0, -1)
                             if data.endswith(self.marker[:n])), 0)
                record(data[:-keep] if keep else data)
                self.pending = data[-keep:] if keep else b""
                break
            record(data[:position])
            data = data[position:]
            end = data.find(b"\x1f", len(self.marker))
            if end < 0 and len(data) <= len(self.marker) + 16:
                self.pending = data
                break
            body = data[len(self.marker):end] if end >= 0 else b""
            if body == b"start" and self.state in ("queued", "sent"):
                self.state = "running"
                self.received_at = time.time()
            elif body == b"script" and self.state == "running" and self.started_at is None:
                self.started_at = time.time()
            elif (self.state == "running" and body.startswith(b"end:") and
                  1 <= len(body[4:]) <= 3 and body[4:].isdigit() and int(body[4:]) <= 255):
                self.exit_code = int(body[4:])
                self.state = "ok" if self.exit_code == 0 else "failed"
                self.finished_at = time.time()
                output.extend(data[end + 1:])
                break
            else:
                # An invalid frame is ordinary output. Never retain an unbounded tail.
                record(data[:1])
                data = data[1:]
                continue
            data = data[end + 1:]
        return bytes(output)


@dataclasses.dataclass
class SessionLease:
    id: str
    task: str
    deadline: float

    def info(self, include_id=False):
        remaining = max(0, self.deadline - time.monotonic())
        info = {"task": self.task, "expires_in_seconds": remaining,
                "expires_at": time.time() + remaining}
        if include_id:
            info["lease_id"] = self.id
        return info


def parse_proxy_v1(header: bytes) -> tuple[str, int]:
    """Read the IPv4 subset of PROXY v1; never fall back to the proxy's IP."""
    if len(header) > PROXY_HEADER_LIMIT or not header.endswith(b"\r\n"):
        raise ValueError("invalid PROXY v1 header length or terminator")
    try:
        parts = header[:-2].decode("ascii").split(" ")
        if len(parts) != 6 or parts[:2] != ["PROXY", "TCP4"]:
            raise ValueError
        source = str(ipaddress.IPv4Address(parts[2]))
        ipaddress.IPv4Address(parts[3])
        for port in parts[4:]:
            if re.fullmatch(r"0|[1-9][0-9]{0,4}", port) is None or int(port) > 65535:
                raise ValueError
    except (UnicodeError, ValueError):
        raise ValueError("expected a valid PROXY v1 TCP4 header") from None
    return source, int(parts[4])


def validate_proxy_listener(host: str) -> None:
    try:
        if ipaddress.IPv4Address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("--proxy-protocol requires an IPv4 loopback listener; use -H 127.0.0.1 with local frpc")


@dataclasses.dataclass
class PendingProxy:
    deadline: float
    header: bytearray = dataclasses.field(default_factory=bytearray)


@dataclasses.dataclass
class Session:
    id: int
    sock: socket.socket
    peer: str
    output: Buffer
    created: float = dataclasses.field(default_factory=time.monotonic)
    last_input: float = dataclasses.field(default_factory=time.monotonic)
    connected: bool = True
    close_reason: str = ""
    outgoing: bytearray = dataclasses.field(default_factory=bytearray)
    batch: BatchResult = None
    connected_at: float = dataclasses.field(default_factory=time.time)
    lease: SessionLease = None
    transfer: FileTransfer = None

    @property
    def ip(self) -> str:
        return self.peer.rsplit(":", 1)[0]


class IPNames:
    """Persistent per-IP labels, updated transactionally in SQLite."""

    def __init__(self, path=None):
        self.path = path
        self.records = {}
        self.error = ""
        self.retry_initialization = False
        if path is not None:
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                with closing(self._connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("CREATE TABLE IF NOT EXISTS hosts "
                               "(ip TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                               "note TEXT NOT NULL DEFAULT '')")
                    db.execute("CREATE TABLE IF NOT EXISTS metadata "
                               "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    if not db.execute("SELECT 1 FROM metadata WHERE key = 'names_json_imported'").fetchone():
                        legacy = path.with_name("names.json")
                        try:
                            values = json.loads(legacy.read_text(encoding="utf-8"))
                        except FileNotFoundError:
                            values = {}
                        if not isinstance(values, dict):
                            raise ValueError("names.json must map IPv4 addresses to names")
                        for ip, name in values.items():
                            self.validate(ip, name, "name")
                            db.execute("INSERT INTO hosts (ip, name) VALUES (?, ?) "
                                       "ON CONFLICT(ip) DO NOTHING", (ip, name))
                        db.execute("INSERT INTO metadata VALUES ('names_json_imported', '1')")
                    records = self._read(db)
                self.records = records
            except (OSError, ValueError, sqlite3.Error) as error:
                self.retry_initialization = isinstance(error, sqlite3.OperationalError) and str(error).startswith(
                    ("database is locked", "database table is locked", "database schema is locked"))
                action = "Retry a name/note/names command shortly." if self.retry_initialization else "Fix the file and restart."
                self.error = f"Cannot load names database {path}: {error}. {action}"

    def _connect(self):
        # Creation goes through __init__ with mode 0600, never a later read/write.
        return sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=0)

    def _ensure_ready(self) -> None:
        if self.retry_initialization:
            recovered = IPNames(self.path)
            self.records = recovered.records
            self.error = recovered.error
            self.retry_initialization = recovered.retry_initialization
        if self.error:
            raise ValueError(self.error)

    @property
    def values(self) -> dict:
        return {ip: row[0] for ip, row in self.records.items() if row[0]}

    @staticmethod
    def validate(ip: str, value: str, field: str, allow_empty=False) -> None:
        try:
            ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            raise ValueError("expected a valid IPv4 address") from None
        limit = 64 if field == "name" else 512
        if allow_empty and value == "":
            return
        if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.strip() or not value.isprintable():
            raise ValueError(f"{field} must contain 1-{limit} printable characters")

    def _read(self, db) -> dict:
        records = {}
        for ip, name, note in db.execute("SELECT ip, name, note FROM hosts ORDER BY ip"):
            self.validate(ip, name, "name", allow_empty=True)
            self.validate(ip, note, "note", allow_empty=True)
            records[ip] = (name, note)
        return records

    def refresh(self) -> None:
        self._ensure_ready()
        if self.path is not None:
            with closing(self._connect()) as db:
                self.records = self._read(db)

    def set(self, ip: str, value=None, field="name") -> None:
        self._ensure_ready()
        if field not in ("name", "note"):
            raise ValueError("unknown field")
        self.validate(ip, value if value is not None else "", field, allow_empty=value is None)
        value = value or ""
        if self.path is None:
            row = list(self.records.get(ip, ("", "")))
            row[0 if field == "name" else 1] = value
            if any(row):
                self.records[ip] = tuple(row)
            else:
                self.records.pop(ip, None)
            return
        # Each statement changes only the selected field, preserving other writers.
        sql = {"name": "INSERT INTO hosts (ip, name) VALUES (?, ?) ON CONFLICT(ip) DO UPDATE SET name=excluded.name",
               "note": "INSERT INTO hosts (ip, note) VALUES (?, ?) ON CONFLICT(ip) DO UPDATE SET note=excluded.note"}[field]
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(sql, (ip, value))
            db.execute("DELETE FROM hosts WHERE ip=? AND name='' AND note=''", (ip,))
            records = self._read(db)
        self.records = records


class Terminal:
    def __init__(self):
        self.input_fd = sys.stdin.fileno()
        self.output_fd = sys.stdout.fileno()
        self.attributes = None
        self.blocking = {}

    def __enter__(self):
        if not os.isatty(self.input_fd) or not os.isatty(self.output_fd):
            raise ValueError("run shelldeck in an interactive terminal (stdin and stdout must be TTYs)")
        self.attributes = termios.tcgetattr(self.input_fd)
        try:
            tty.setraw(self.input_fd, termios.TCSANOW)
            # Keep newline rendering for ordinary shells that emit LF, not CRLF.
            attributes = termios.tcgetattr(self.input_fd)
            attributes[1] = self.attributes[1]
            termios.tcsetattr(self.input_fd, termios.TCSANOW, attributes)
            # stdin/stdout can share one open file description (for example a PTY).
            self.blocking = {fd: os.get_blocking(fd) for fd in (self.input_fd, self.output_fd)}
            for fd in self.blocking:
                os.set_blocking(fd, False)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.attributes is not None:
                termios.tcsetattr(self.input_fd, termios.TCSANOW, self.attributes)
        finally:
            for fd, blocking in self.blocking.items():
                os.set_blocking(fd, blocking)


class Console:
    def __init__(self, listener: socket.socket, terminal: Terminal, buffer_limit: int,
                 max_sessions: int, idle_timeout: int = DEFAULT_IDLE_TIMEOUT, names=None, history=None,
                 agent_socket=None, proxy_protocol=None, client_endpoint=None):
        if proxy_protocol is not None:
            if proxy_protocol != "v1":
                raise ValueError("only PROXY protocol v1 is supported")
            validate_proxy_listener(listener.getsockname()[0])
        self.listener = listener
        self.proxy_protocol = proxy_protocol
        self.client_endpoint = client_endpoint
        self.pending_proxies = {}
        self.terminal = terminal
        self.buffer_limit = buffer_limit
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.names = names if names is not None else IPNames()
        self.history = history if history is not None else HistoryStore()
        self.history_updates = {}
        self.transfer_updates = {}
        self.history_error = ""
        self.history_flush_at = 0
        self.selector = selectors.DefaultSelector()
        self.sessions = {}
        self.next_id = 1
        self.active = None
        self.raw = False
        self.input_paused = False
        self.line = bytearray()
        self.line_cursor = 0
        self.pending_utf8 = bytearray()
        self.line_overflow = False
        self.escape_state = 0
        self.escape_parameters = bytearray()
        self.control = Buffer(CONTROL_LIMIT)
        self.output_registered = False
        self.running = True
        self.jobs = {}
        self.agent_socket = agent_socket
        self.agent_server = None

    def emit(self, text: str) -> None:
        self.control.append(text.encode("utf-8"))

    def prompt(self) -> None:
        self.control.append(PROMPT)

    def name_suffix(self, session: Session) -> str:
        name = self.names.records.get(session.ip, ("", ""))[0]
        return f" ({name})" if name else ""

    def notice(self, text: str) -> None:
        # Remote bytes are never rendered in the menu or in a different session.
        if self.active is None:
            self.control.append(b"\r\x1b[2K")
            self.emit(text + "\n")
            self.prompt()
            self.control.append(bytes(self.line))
            self.shift_cursor(-self.text_width(self.line[self.line_cursor:]))

    def run(self) -> None:
        self.selector.register(self.listener, selectors.EVENT_READ, "listener")
        self.selector.register(self.terminal.input_fd, selectors.EVENT_READ, "input")
        host, port = self.listener.getsockname()[:2]
        self.emit(f"Mode: {'remote (via frp)' if self.proxy_protocol else 'local (direct TCP)'}\n")
        self.emit(f"Listening on {host}:{port}\nType help for commands. Ctrl+] detaches a session.\n")
        if self.proxy_protocol:
            self.emit(f"Client endpoint: {self.client_endpoint or 'VPS endpoint from the frpc configuration'}\n")
            self.emit("PROXY v1 TCP4 required from local frpc; source addresses are supplied by the proxy.\n")
        else:
            address = "<this machine's IPv4>" if host == "0.0.0.0" else host
            self.emit(f"Client endpoint: {address}:{port}\n")
        timeout_label = f"{self.idle_timeout}s without submitted input" if self.idle_timeout else "disabled"
        self.emit(f"Idle timeout: {timeout_label}.\n")
        if self.names.error:
            self.emit(f"[!] {self.names.error}\n")
        if self.history.error:
            self.emit(f"[!] History unavailable: {self.history.error}. Batch dispatch requires a writable history database.\n")
        self.prompt()
        try:
            if self.agent_socket is not None:
                self.agent_server = AgentServer(self.agent_socket, self.selector, self.agent_request)
                self.notice(f"Agent socket: {self.agent_server.path}")
            while self.running:
                self.update_output_interest()
                for key, mask in self.selector.select(timeout=0.2):
                    if not self.running:
                        break
                    if key.data == "listener":
                        self.accept()
                    elif key.data == "input":
                        self.read_input()
                    elif key.data == "output":
                        self.write_output()
                    elif callable(key.data):
                        key.data(key.fileobj, mask)
                    else:
                        session = key.data
                        if session.connected and mask & selectors.EVENT_READ:
                            self.read_session(session)
                        if session.connected and mask & selectors.EVENT_WRITE:
                            self.write_session(session)
                # Handle ready input first, then expire sessions even when other
                # connections or the terminal are continuously generating events.
                self.expire_idle_sessions()
                self.expire_proxy_headers()
                self.expire_leases()
                self.flush_history()
                if self.agent_server is not None:
                    self.agent_server.expire()
        finally:
            if self.agent_server is not None:
                self.agent_server.close()
            for sock in list(self.pending_proxies):
                self.drop_proxy(sock)
            for session in list(self.sessions.values()):
                self.disconnect(session, notify=False)
            if not self.flush_history(force=True):
                print(f"\n[!] Some execution results could not be saved: {self.history_error}", file=sys.stderr)
            self.history.close()
            self.selector.close()

    def accept(self) -> None:
        # Bound work per turn so an accept flood cannot starve existing sessions.
        for _ in range(16):
            try:
                sock, address = self.listener.accept()
            except BlockingIOError:
                return
            sock.setblocking(False)
            if self.proxy_protocol:
                if not ipaddress.IPv4Address(address[0]).is_loopback:
                    sock.close()
                    self.notice("[!] PROXY connection rejected: peer is not loopback.")
                    continue
                if len(self.pending_proxies) + sum(s.connected for s in self.sessions.values()) >= self.max_sessions:
                    sock.close()
                    self.notice("[!] Session limit reached (including pending PROXY headers); new connection rejected.")
                    continue
                self.pending_proxies[sock] = PendingProxy(time.monotonic() + PROXY_HEADER_TIMEOUT)
                self.selector.register(sock, selectors.EVENT_READ, self.read_proxy_header)
            else:
                self.add_session(sock, address)

    def add_session(self, sock, address, initial_data=b"") -> None:
        # Reclaim old records only after the source address has been validated.
        if len(self.sessions) >= self.max_sessions:
            stale = next((sid for sid, s in self.sessions.items()
                          if not s.connected and sid != self.active), None)
            if stale is not None:
                del self.sessions[stale]
            else:
                sock.close()
                self.notice("[!] Session limit reached; new connection rejected.")
                return
        session = Session(self.next_id, sock, f"{address[0]}:{address[1]}", Buffer(self.buffer_limit))
        session.output.append(initial_data)
        self.next_id += 1
        self.sessions[session.id] = session
        self.selector.register(sock, selectors.EVENT_READ, session)
        self.notice(f"[+] Session {session.id} connected from {session.peer}{self.name_suffix(session)}")

    def drop_proxy(self, sock, reason=None) -> None:
        if self.pending_proxies.pop(sock, None) is not None:
            self.selector.unregister(sock)
            sock.close()
            if reason:
                self.notice(f"[!] PROXY connection rejected: {reason}.")

    def expire_proxy_headers(self) -> None:
        now = time.monotonic()
        for sock, pending in list(self.pending_proxies.items()):
            if now >= pending.deadline:
                self.drop_proxy(sock, "header timeout")

    def read_proxy_header(self, sock, _mask) -> None:
        pending = self.pending_proxies.get(sock)
        if pending is None:
            return
        # Check here too: a readable socket must not bypass its deadline.
        if time.monotonic() >= pending.deadline:
            self.drop_proxy(sock, "header timeout")
            return
        try:
            data = sock.recv(PROXY_HEADER_LIMIT - len(pending.header))
        except BlockingIOError:
            return
        except OSError:
            self.drop_proxy(sock, "connection lost before header")
            return
        if not data:
            self.drop_proxy(sock, "connection closed before header")
            return
        pending.header.extend(data)
        end = pending.header.find(b"\r\n")
        if end < 0:
            if len(pending.header) >= PROXY_HEADER_LIMIT:
                self.drop_proxy(sock, "header too long")
            return
        try:
            address = parse_proxy_v1(bytes(pending.header[:end + 2]))
        except ValueError as error:
            self.drop_proxy(sock, str(error))
            return
        del self.pending_proxies[sock]
        self.selector.unregister(sock)
        self.add_session(sock, address, bytes(pending.header[end + 2:]))

    def read_session(self, session: Session) -> None:
        try:
            data = session.sock.recv(READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            self.disconnect(session)
            return
        if data:
            if session.transfer is not None:
                transfer = session.transfer
                try:
                    data, outgoing = transfer.feed(data)
                    if outgoing:
                        if len(session.outgoing) + len(outgoing) > SEND_LIMIT:
                            raise ValueError("file transfer send queue exceeds the limit")
                        session.outgoing.extend(outgoing)
                        session.last_input = time.monotonic()
                        self.selector.modify(session.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, session)
                except ValueError as error:
                    transfer.fail("protocol_error", str(error))
                    self.disconnect(session, reason="protocol-error")
                    return
                self.transfer_updates[transfer.transfer_id] = transfer
                if transfer.state not in PENDING_TRANSFER_STATES:
                    session.transfer = None
                    self.notice(f"[Transfer {transfer.transfer_id}] Session {session.id}: {transfer.state}.")
            elif session.batch is not None:
                result = session.batch
                data = result.feed(data)
                self.record_history(result)
                if result.state not in PENDING_JOB_STATES:
                    session.batch = None
                    self.notice(f"[Job {result.job_id}] Session {session.id}: {result.state}, exit {result.exit_code}.")
            session.output.append(data)
        else:
            self.disconnect(session)

    def disconnect(self, session: Session, notify: bool = True, reason: str = "disconnected") -> None:
        if not session.connected:
            return
        self.selector.unregister(session.sock)
        session.sock.close()
        session.connected = False
        session.close_reason = reason
        session.outgoing.clear()
        session.lease = None
        if session.transfer is not None:
            transfer = session.transfer
            transfer.disconnect(reason)
            self.transfer_updates[transfer.transfer_id] = transfer
            session.transfer = None
        if session.batch is not None:
            result = session.batch
            if result.state == "running":
                result.log.append(result.pending)
            result.state = reason
            result.finished_at = time.time()
            session.output.append(result.pending)
            result.pending = b""
            self.record_history(result)
            session.batch = None
        if notify:
            self.notice(f"[-] Session {session.id} disconnected; saved output is available via interact {session.id}.")

    def expire_idle_sessions(self) -> None:
        if not self.idle_timeout:
            return
        now = time.monotonic()
        for session in self.sessions.values():
            if not session.connected or now - session.last_input < self.idle_timeout:
                continue
            self.disconnect(session, notify=False, reason="idle-timeout")
            message = (f"Session {session.id} idle timeout after {self.idle_timeout}s without submitted input; "
                       f"saved output is available via interact {session.id}.")
            if self.active == session.id:
                # Do not wait for a blocked terminal to drain before leaving a
                # timed-out session; unread bytes stay in that session's buffer.
                self.detach(message)
            else:
                self.notice("[-] " + message)

    def send(self, data: bytes) -> bool:
        session = self.sessions[self.active]
        if not session.connected:
            return False
        if self.current_lease(session) is not None:
            self.emit("\n[Session leased by an agent task; viewing only, input was not queued.]\n")
            return False
        if session.transfer is not None:
            self.emit("\n[File transfer in progress; input was not queued.]\n")
            return False
        if session.batch is not None:
            self.emit("\n[Batch in progress; input was not queued. Use Ctrl+] then jobs to check progress.]\n")
            return False
        if len(session.outgoing) + len(data) > SEND_LIMIT:
            self.input_paused = True
            self.emit("\n[Send queue full; input paused. This input was not queued. "
                      "Ctrl+] returns to menu; check remote state before retrying.]\n")
            return False
        session.outgoing.extend(data)
        if data:
            session.last_input = time.monotonic()
        self.selector.modify(session.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, session)
        return True

    def write_session(self, session: Session) -> None:
        try:
            sent = session.sock.send(session.outgoing)
        except BlockingIOError:
            return
        except OSError:
            self.disconnect(session)
            return
        if sent == 0:
            self.disconnect(session)
            return
        del session.outgoing[:sent]
        if not session.outgoing:
            if session.transfer is not None and session.transfer.state == "queued":
                session.transfer.state = "sent"
                self.transfer_updates[session.transfer.transfer_id] = session.transfer
            if session.batch is not None and session.batch.state == "queued":
                session.batch.state = "sent"
                self.record_history(session.batch)
            self.selector.modify(session.sock, selectors.EVENT_READ, session)

    def update_output_interest(self) -> None:
        session = self.sessions.get(self.active)
        if session is not None and not session.connected and not session.output.size:
            self.detach(f"Session {session.id} disconnected.")
            session = None
        ready = bool(self.control.size or (session is not None and session.output.size))
        if ready and not self.output_registered:
            self.selector.register(self.terminal.output_fd, selectors.EVENT_WRITE, "output")
            self.output_registered = True
        elif not ready and self.output_registered:
            self.selector.unregister(self.terminal.output_fd)
            self.output_registered = False

    def write_output(self) -> None:
        buffer = self.control
        if not buffer.size:
            session = self.sessions.get(self.active)
            if session is None:
                return
            buffer = session.output
        if not buffer.size:
            return
        try:
            written = os.write(self.terminal.output_fd, buffer.head())
        except BlockingIOError:
            return
        buffer.consume(written)

    def read_input(self) -> None:
        try:
            data = os.read(self.terminal.input_fd, 4096)
        except BlockingIOError:
            return
        if not data:
            self.running = False
            return
        cursor = 0
        while cursor < len(data):
            if not self.running:
                return
            if self.active is not None and self.input_paused:
                if data[cursor] == 0x1D:
                    self.detach()
                cursor += 1
                continue
            if self.active is not None and self.raw:
                boundary = data.find(b"\x1d", cursor)
                end = len(data) if boundary < 0 else boundary
                if end > cursor and not self.send(data[cursor:end]):
                    return
                if boundary < 0:
                    return
                self.detach()
                cursor = boundary + 1
                continue
            byte = data[cursor]
            cursor += 1
            if self.active is not None and byte == 0x1D:
                self.detach()
            else:
                self.edit_line(byte)

    def edit_line(self, byte: int) -> None:
        # Echo whole UTF-8 characters: inserting cursor controls between their
        # bytes would corrupt the display when editing in the middle of a line.
        if self.pending_utf8:
            if 0x80 <= byte <= 0xBF:
                self.pending_utf8.append(byte)
                first = self.pending_utf8[0]
                size = 2 if first < 0xE0 else 3 if first < 0xF0 else 4
                if len(self.pending_utf8) == size:
                    self.insert_text(bytes(self.pending_utf8))
                    self.pending_utf8.clear()
                return
            self.insert_text(bytes(self.pending_utf8))
            self.pending_utf8.clear()
        # Control keys must still work after an incomplete escape sequence.
        if byte < 32 or byte == 127:
            self.escape_state = 0
            self.escape_parameters.clear()
        if self.escape_state:
            if self.escape_state == 1:
                self.escape_state = 2 if byte in (ord("["), ord("O")) else 0
            elif 0x40 <= byte <= 0x7E:
                if self.escape_state == 2:
                    self.edit_escape(byte, bytes(self.escape_parameters))
                self.escape_state = 0
                self.escape_parameters.clear()
            elif self.escape_state == 2:
                if 0x20 <= byte <= 0x3F and len(self.escape_parameters) < 16:
                    self.escape_parameters.append(byte)
                else:
                    self.escape_state = 3  # Discard oversized/invalid sequences through the final byte.
            return
        if byte == 0x1B:
            self.escape_state = 1
        elif byte in (10, 13):
            line = bytes(self.line)
            self.move_line_cursor(len(self.line))
            self.line.clear()
            self.line_cursor = 0
            self.control.append(b"\n")
            if self.line_overflow:
                self.line_overflow = False
                self.emit(f"[Input exceeded {LINE_LIMIT} bytes; entire line discarded.]\n")
                if self.active is None:
                    self.prompt()
                return
            if self.active is None:
                self.command(line.decode("utf-8", errors="replace"))
            else:
                self.send(line + b"\n")
        elif byte in (8, 127):
            self.erase_character()
        elif byte == 0x15:
            self.move_line_cursor(0)
            width = self.text_width(self.line)
            self.line.clear()
            self.redraw_line_tail(width)
            self.line_overflow = False
        elif byte == 0x03:
            self.move_line_cursor(len(self.line))
            self.line.clear()
            self.line_cursor = 0
            self.line_overflow = False
            self.control.append(b"^C\n")
            if self.active is None:
                self.prompt()
            else:
                self.emit("[Local input cleared; no remote signal sent. Ctrl+] returns to menu.]\n")
        elif byte == 0x04:
            if self.active is not None:
                self.detach()
            elif not self.line:
                self.running = False
        elif 0xC2 <= byte <= 0xF4:
            self.pending_utf8.append(byte)
        elif byte >= 32:
            self.insert_text(bytes((byte,)))

    @staticmethod
    def text_width(data: bytes) -> int:
        return sum(0 if unicodedata.combining(c) else
                   2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
                   for c in bytes(data).decode("utf-8", errors="replace"))

    def shift_cursor(self, columns: int) -> None:
        if columns:
            self.control.append(f"\x1b[{abs(columns)}{'C' if columns > 0 else 'D'}".encode())

    def move_line_cursor(self, position: int) -> None:
        if position < self.line_cursor:
            self.shift_cursor(-self.text_width(self.line[position:self.line_cursor]))
        else:
            self.shift_cursor(self.text_width(self.line[self.line_cursor:position]))
        self.line_cursor = position

    def character_start(self, position: int) -> int:
        start = max(0, position - 1)
        while start and self.line[start] & 0xC0 == 0x80:
            start -= 1
        return start

    def character_end(self, position: int) -> int:
        end = min(len(self.line), position + 1)
        while end < len(self.line) and self.line[end] & 0xC0 == 0x80:
            end += 1
        return end

    def redraw_line_tail(self, erased_width: int = 0) -> None:
        tail = bytes(self.line[self.line_cursor:])
        self.control.append(tail + b" " * erased_width)
        self.shift_cursor(-(self.text_width(tail) + erased_width))

    def insert_text(self, data: bytes) -> None:
        if len(self.line) + len(data) > LINE_LIMIT:
            self.line_overflow = True
            self.control.append(b"\a")
            return
        self.line[self.line_cursor:self.line_cursor] = data
        self.line_cursor += len(data)
        self.control.append(data)
        self.redraw_line_tail()

    def edit_escape(self, final: int, parameters: bytes) -> None:
        if final in (ord("C"), ord("D")) and (not parameters or parameters.isdigit()):
            count = min(int(parameters or b"1") or 1, LINE_LIMIT)
            position = self.line_cursor
            for _ in range(count):
                position = self.character_start(position) if final == ord("D") else self.character_end(position)
            self.move_line_cursor(position)
        elif (final == ord("H") and parameters in (b"", b"1")) or (final == ord("~") and parameters in (b"1", b"7")):
            self.move_line_cursor(0)
        elif (final == ord("F") and parameters in (b"", b"1")) or (final == ord("~") and parameters in (b"4", b"8")):
            self.move_line_cursor(len(self.line))
        elif final == ord("~") and parameters == b"3":
            self.erase_character(forward=True)

    def erase_character(self, forward: bool = False) -> None:
        if forward:
            start, end = self.line_cursor, self.character_end(self.line_cursor)
        else:
            start, end = self.character_start(self.line_cursor), self.line_cursor
        if start == end:
            return
        width = self.text_width(self.line[start:end])
        self.move_line_cursor(start)
        del self.line[start:end]
        self.redraw_line_tail(width)

    def detach(self, reason: str = "") -> None:
        sid = self.active
        self.active = None
        self.raw = False
        self.input_paused = False
        self.line.clear()
        self.line_cursor = 0
        self.pending_utf8.clear()
        self.line_overflow = False
        self.escape_state = 0
        self.escape_parameters.clear()
        self.emit(f"\n[Detached session {sid}; unsent local input cleared.]\n")
        if reason:
            self.emit(reason + "\n")
        self.prompt()

    def command(self, line: str) -> None:
        words = line.split()
        if not words:
            self.prompt()
            return
        name = words[0]
        if name in ("help", "?") and len(words) == 1:
            self.emit(HELP)
        elif name in ("name", "unname", "note", "unnote"):
            self.name_command(line)
        elif name == "names":
            self.show_names(words)
        elif name == "batch":
            self.batch_command(line)
        elif name == "jobs":
            self.show_jobs(words)
        elif name == "history":
            self.show_history(line)
        elif name == "result":
            self.show_result(words)
        elif name in ("sessions", "ls") and len(words) == 1:
            try:
                self.names.refresh()
            except (OSError, ValueError, sqlite3.Error) as error:
                self.emit(f"[!] Cannot refresh names: {error}\n")
            self.emit("ID    PEER                     STATE          BUFFERED   DROPPED    AGE       IDLE       NAME\n")
            now = time.monotonic()
            for session in self.sessions.values():
                state = "connected" if session.connected else session.close_reason
                age = int(now - session.created)
                idle = f"{int(now - session.last_input)}s" if session.connected else "-"
                self.emit(f"{session.id:<5} {session.peer:<24} {state:<14} "
                          f"{session.output.size:<10} {session.output.dropped:<10} {str(age) + 's':<9} "
                          f"{idle:<10} {self.names.records.get(session.ip, ('', ''))[0] or '-'}\n")
                lease = self.current_lease(session)
                if lease is not None:
                    self.emit(f"      Agent lease: {lease.task} ({lease.info()['expires_in_seconds']:.0f}s remaining)\n")
            if not self.sessions:
                self.emit("(no sessions)\n")
        elif name in ("quit", "exit") and len(words) == 1:
            self.running = False
        elif words == ["close", "all"]:
            count = len(self.sessions)
            for sock in list(self.pending_proxies):
                self.drop_proxy(sock)
            for session in self.sessions.values():
                self.disconnect(session, notify=False)
            self.sessions.clear()
            self.emit(f"Closed all sessions ({count} removed). Listener remains active.\n")
        elif name in ("interact", "use", "i", "close"):
            valid = len(words) == 2 or (len(words) == 3 and name != "close" and words[2] == "raw")
            if not valid or len(words[1]) > 20 or not words[1].isascii() or not words[1].isdigit():
                self.emit("Usage: interact ID [raw] | close ID | close all\n")
            else:
                session = self.sessions.get(int(words[1]))
                if session is None:
                    self.emit("No such session. Use sessions to list IDs.\n")
                elif name == "close":
                    self.disconnect(session, notify=False)
                    del self.sessions[session.id]
                    self.emit(f"Closed session {session.id}.\n")
                else:
                    self.active = session.id
                    self.raw = len(words) == 3
                    self.input_paused = False
                    mode = "raw (no local echo)" if self.raw else "line (local echo)"
                    self.emit(f"[Session {session.id} {session.peer}{self.name_suffix(session)}; {mode}; Ctrl+] detaches]\n")
                    if self.current_lease(session) is not None:
                        self.emit("[Agent task owns this session; viewing only until its lease ends.]\n")
                    if session.output.dropped:
                        self.emit(f"[Buffer overflow: {session.output.dropped} oldest bytes dropped in total.]\n")
                    return
        else:
            self.emit("Unknown command. Type help.\n")
        if self.running:
            self.prompt()

    def batch_command(self, line: str) -> None:
        try:
            parts = shlex.split(line)
            if len(parts) not in (2, 4) or (len(parts) == 4 and parts[2] not in ("--sessions", "-s")):
                raise ValueError("Usage: batch PATH [--sessions ID,... | -s ID,...] (quote paths containing spaces)")
            if len(parts) == 4:
                values = [value.strip() for value in parts[3].split(",")]
                if any(not value.isascii() or not value.isdigit() or len(value) > 20 or int(value) < 1
                       for value in values):
                    raise ValueError("sessions must be comma-separated positive session IDs, e.g. --sessions 1,3,5")
                ids = list(dict.fromkeys(int(value) for value in values))
                unavailable = [sid for sid in ids if sid not in self.sessions or not self.sessions[sid].connected]
                if unavailable:
                    raise ValueError("sessions not found or disconnected: " + ", ".join(map(str, unavailable)) +
                                     "; no scripts were sent. Use sessions / ls to check IDs")
                targets = [self.sessions[sid] for sid in ids]
            else:
                targets = [s for s in self.sessions.values() if s.connected]
            if not targets:
                raise ValueError("No connected sessions.")
            try:
                path = Path(parts[1]).expanduser().absolute()
            except RuntimeError as error:
                raise ValueError("cannot expand the script path; check the user or use an absolute path") from error
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ValueError("script must be a regular file")
                source = file.read(SEND_LIMIT + 1)
            script = self.normalize_script(source)
            job_id, results = self.start_batch(path, script, targets)
        except (OSError, ValueError, sqlite3.Error) as error:
            self.emit(f"[!] Batch not started: {error}\n")
            return
        self.emit(f"[Job {job_id}] {sum(r.state == 'queued' for r in results)} queued, "
                  f"{sum(r.state == 'skipped-busy' for r in results)} skipped. Use jobs {job_id} for results.\n")

    @staticmethod
    def normalize_script(source: bytes) -> str:
        if len(source) > SEND_LIMIT:
            raise ValueError("script exceeds the 64 KiB send limit")
        script = source.decode("utf-8-sig").replace("\r\n", "\n")
        if not script.strip() or "\x00" in script:
            raise ValueError("script must be nonempty UTF-8 text without NUL bytes")
        return script

    def start_batch(self, path: Path, script: str, targets, request_id=None, lease_id=None):
        """Persist before dispatch; both human and agent commands use this transport."""
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        packets = []
        for session in targets:
            token = "SHELLDECK_BATCH_" + secrets.token_hex(16)
            start = "\\036" + token + ":start\\037"
            end = "\\036" + token + ":end:%s\\037"
            # Keep transport tracing outside the archived interval and preserve shell options.
            command = (f"( set +xv; printf {shlex.quote(start)}; if bash -c {shlex.quote(BATCH_DECODER)} "
                       f"shelldeck-decode {shlex.quote(encoded)} {shlex.quote(token)} </dev/null; "
                       f"then printf {shlex.quote(end)} 0; else printf {shlex.quote(end)} \"$?\"; fi )\n")
            packet = command.encode("utf-8")
            if len(packet) > SEND_LIMIT:
                raise ValueError("Base64 script and decoder exceed the 64 KiB send limit; split the script into smaller files")
            packets.append((session, packet, b"\x1e" + token.encode("ascii") + b":"))
        stale = None
        if len(self.jobs) >= JOB_LIMIT:
            stale = next((jid for jid, (_, results) in self.jobs.items()
                          if all(r.state not in PENDING_JOB_STATES for r in results)), None)
            if stale is None:
                raise ValueError("too many unfinished jobs; wait for completion or close stalled sessions")
        if not self.flush_history(force=True):
            raise ValueError(f"execution history is not writable: {self.history_error}")
        entries = []
        for session, packet, marker in packets:
            result = BatchResult(0, session.id, session.peer, marker)
            lease = self.current_lease(session)
            if (session.batch is not None or session.transfer is not None or session.outgoing or
                    (lease is not None and lease.id != lease_id) or
                    (lease_id is not None and (lease is None or lease.id != lease_id))):
                result.state = "skipped-busy"
            entries.append((session, result))
        job_id = self.history.create_batch(path, hashlib.sha256(script.encode("utf-8")).hexdigest(),
                                           entries, request_id=request_id, lease_id=lease_id)
        if stale is not None:
            del self.jobs[stale]
        results = [result for _, result in entries]
        for (session, packet, marker), result in zip(packets, results):
            if result.state == "skipped-busy":
                continue
            session.batch = result
            session.outgoing.extend(packet)
            session.last_input = time.monotonic()
            self.selector.modify(session.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, session)
        self.jobs[job_id] = (str(path), results)
        return job_id, results

    @staticmethod
    def agent_id(value, label):
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise AgentError("invalid_request", f"{label} must be a positive integer")
        return value

    @staticmethod
    def agent_request_id(value):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
            raise AgentError("invalid_request", "request_id must contain 1-128 ASCII letters, digits, underscores or hyphens")
        return value

    def current_lease(self, session):
        lease = session.lease
        if lease is not None and time.monotonic() >= lease.deadline:
            session.lease = None
            self.notice(f"[Lease] Session {session.id}: {lease.task} expired; any pending command still blocks input.")
        return session.lease

    def expire_leases(self):
        for session in self.sessions.values():
            self.current_lease(session)

    @staticmethod
    def agent_lease_id(value):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise AgentError("invalid_request", "lease_id must be a fresh random 32-character lowercase hex string")
        return value

    def agent_session(self, sid, instance):
        if instance != self.history.instance:
            raise AgentError("stale_instance", "console restarted; list sessions again before choosing a target")
        session = self.sessions.get(sid)
        if session is None or not session.connected:
            raise AgentError("session_unavailable", "session does not exist or is disconnected")
        return session

    def require_lease(self, session, lease_id):
        lease = self.current_lease(session)
        if lease is None:
            raise AgentError("lease_inactive", "lease expired or was released; acquire with a new lease_id")
        if lease.id != lease_id:
            raise AgentError("lease_mismatch", "session belongs to a different lease")
        return lease

    def agent_lease_request(self, method, request, response, sid, instance):
        response["session_id"] = sid
        lease_id = self.agent_lease_id(request.get("lease_id"))
        session = self.agent_session(sid, instance)
        lease = self.current_lease(session)
        ttl = request.get("ttl_seconds", 300)
        if method != "release" and (type(ttl) is not int or not 1 <= ttl <= 3600):
            raise AgentError("invalid_request", "ttl_seconds must be an integer between 1 and 3600")
        if method == "acquire":
            task = request.get("task", "agent task")
            if (not isinstance(task, str) or not task.strip() or len(task) > 128 or
                    not task.isprintable()):
                raise AgentError("invalid_request", "task must be 1-128 printable characters on one line")
            previous = self.history.get_lease(lease_id)
            if previous is not None:
                if (previous["instance"], previous["session_id"], previous["task"], previous["ttl_seconds"]) != (instance, sid, task, ttl):
                    raise AgentError("lease_conflict", "lease_id was already used with different acquisition parameters")
                self.require_lease(session, lease_id)
                response["replayed"] = True
            else:
                reason = self.agent_busy_reason(session)
                if reason:
                    raise AgentError("session_busy", reason)
                self.history.create_lease(lease_id, sid, task, ttl)
                lease = session.lease = SessionLease(lease_id, task, time.monotonic() + ttl)
                response["replayed"] = False
                self.notice(f"[Lease] Session {sid} reserved for {task} ({ttl}s).")
        elif method == "renew":
            lease = self.require_lease(session, lease_id)
            lease.deadline = time.monotonic() + ttl
        else:
            if lease is None or lease.id != lease_id:
                previous = self.history.get_lease(lease_id)
                if previous is None or (previous["instance"], previous["session_id"]) != (instance, sid):
                    raise AgentError("lease_mismatch", "no matching acquisition for this session")
                # A lost release response can be retried without touching a new owner.
                response.update(released=False, lease=None)
                return response
            if session.batch is not None or session.transfer is not None or session.outgoing:
                raise AgentError("session_busy", "execution_pending; release after the command finishes")
            session.lease = None
            self.notice(f"[Lease] Session {sid} released by {lease.task}.")
            response.update(released=True, lease=None)
            return response
        response["lease"] = lease.info(include_id=True)
        return response

    def agent_busy_reason(self, session, lease_id=None):
        lease = self.current_lease(session)
        if lease is not None and lease.id != lease_id:
            return "session_leased"
        if session.transfer is not None:
            return "transfer_pending"
        if session.batch is not None:
            return "execution_pending"
        if session.outgoing:
            return "input_queued"
        if self.active == session.id and lease is None:
            return "human_attached"
        return None

    def agent_result(self, row):
        if row is None:
            raise AgentError("not_found", "No such run or request_id")
        row = dict(row)
        pending = self.history_updates.get(row["id"])
        if pending is not None:
            for field in ("state", "exit_code", "received_at", "started_at", "finished_at"):
                row[field] = getattr(pending, field)
            row["output"], row["dropped"] = b"".join(pending.log.parts), pending.log.dropped
        state = self.history_state(row)
        completed = state in ("ok", "failed") and row["exit_code"] is not None
        output = bytes(row["output"])
        return {"run_id": row["id"], "job_id": row["job_id"], "instance": row["instance"],
                "session_id": row["session_id"], "peer": row["peer"], "sha256": row["sha256"],
                "state": state, "last_reported_state": row["state"],
                "pending": state in PENDING_JOB_STATES, "completed": completed,
                "success": row["exit_code"] == 0 if completed else None, "exit_code": row["exit_code"],
                "history_saved": pending is None, "output": output.decode("utf-8", errors="replace"),
                "output_bytes": len(output), "output_dropped_bytes": row["dropped"],
                **{key: row[key] for key in ("connected_at", "submitted_at", "received_at", "started_at", "finished_at")}}

    def transfer_result(self, transfer_id, previous=None):
        pending = self.transfer_updates.get(transfer_id)
        if pending is not None:
            info = pending.info()
        else:
            previous = previous if previous is not None else self.history.get_transfer(transfer_id)
            if previous is None:
                raise AgentError("not_found", "No such transfer_id")
            info = dict(previous["result"])
        if info["pending"] and info["instance"] != self.history.instance:
            info.update(last_reported_state=info["state"], state="unknown", pending=False,
                        completed=False, success=None,
                        error={"code": "console_restarted", "message": "Previous console ended before confirmation; do not resubmit with a new ID"})
        return {**info, "history_saved": pending is None}

    def start_transfer(self, method, request, response, sid, instance):
        transfer_id = self.agent_request_id(request.get("transfer_id"))
        lease_id = self.agent_lease_id(request.get("lease_id"))
        local_path = validate_path(request.get("local_path"), "local_path")
        remote_path = validate_path(request.get("remote_path"), "remote_path")
        overwrite = request.get("overwrite", False)
        if type(overwrite) is not bool:
            raise AgentError("invalid_request", "overwrite must be a boolean")
        parameters = {"instance": instance, "session_id": sid, "lease_key": self.history.lease_key(lease_id),
                      "direction": method, "local_path": local_path, "remote_path": remote_path, "overwrite": overwrite}
        previous = self.history.get_transfer(transfer_id)
        response.update(transfer_id=transfer_id, replayed=previous is not None)
        if previous is not None:
            if previous["parameters"] != parameters:
                raise AgentError("transfer_conflict", "transfer_id already belongs to different paths, target, direction, overwrite setting or lease")
            response["transfer"] = self.transfer_result(transfer_id, previous)
            return response
        session = self.agent_session(sid, instance)
        self.require_lease(session, lease_id)
        reason = self.agent_busy_reason(session, lease_id)
        if reason:
            raise AgentError("session_busy", reason)
        if not self.flush_history(force=True):
            raise AgentError("history_unavailable", self.history_error)
        transfer = FileTransfer(transfer_id, instance, sid, method, local_path, remote_path, overwrite)
        try:
            transfer.prepare()
            packet = transfer.command()
            if len(packet) > SEND_LIMIT:
                raise ValueError("file transfer helper exceeds the send limit")
            self.history.create_transfer(parameters, transfer)
        except BaseException:
            transfer.cleanup()
            raise
        # Persist the identity before sending any bytes; lost replies never resend.
        session.transfer = transfer
        session.outgoing.extend(packet)
        session.last_input = time.monotonic()
        self.selector.modify(session.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, session)
        self.notice(f"[Transfer {transfer_id}] Session {sid}: {method} queued.")
        response["transfer"] = self.transfer_result(transfer_id)
        return response

    def agent_request(self, request):
        """All session selection and dispatch happen on the console's event-loop thread."""
        try:
            if not isinstance(request, dict):
                raise AgentError("invalid_request", "request must be a JSON object")
            if type(request.get("protocol_version")) is not int or request["protocol_version"] != PROTOCOL_VERSION:
                raise AgentError("unsupported_version", "protocol_version must be 1")
            method = request.get("method")
            ownership = {"instance", "session_id", "lease_id"}
            fields = {"sessions": set(), "exec": ownership | {"request_id", "script"},
                      "result": {"run_id", "request_id"}, "acquire": ownership | {"task", "ttl_seconds"},
                      "renew": ownership | {"ttl_seconds"}, "release": ownership,
                      "upload": ownership | {"transfer_id", "local_path", "remote_path", "overwrite"},
                      "download": ownership | {"transfer_id", "local_path", "remote_path", "overwrite"},
                      "transfer": {"transfer_id"}}
            if not isinstance(method, str) or method not in fields:
                raise AgentError("unknown_method", "unknown method; use sessions, acquire, renew, release, exec, result, upload, download or transfer")
            if set(request) - fields[method] - {"method", "protocol_version"}:
                raise AgentError("invalid_request", "unexpected request fields")
            response = {"ok": True, "protocol_version": PROTOCOL_VERSION, "instance": self.history.instance}
            if method == "sessions":
                try:
                    self.names.refresh()
                except (OSError, ValueError, sqlite3.Error) as error:
                    response["names_warning"] = str(error)
                now = time.monotonic()
                response["idle_timeout_seconds"] = self.idle_timeout
                response["sessions"] = [
                    {"id": s.id, "peer": s.peer, "name": self.names.records.get(s.ip, ("", ""))[0],
                     "note": self.names.records.get(s.ip, ("", ""))[1], "connected": s.connected,
                     "state": "connected" if s.connected else s.close_reason,
                     "busy_reason": self.agent_busy_reason(s), "connected_at": s.connected_at,
                     "lease": s.lease.info() if s.lease is not None else None,
                     "idle_seconds": max(0, now - s.last_input) if s.connected else None}
                    for s in self.sessions.values()]
                return response
            self.flush_history(force=True)
            if method == "transfer":
                transfer_id = self.agent_request_id(request.get("transfer_id"))
                response.update(transfer_id=transfer_id, transfer=self.transfer_result(transfer_id))
                return response
            if method == "result":
                if ("run_id" in request) == ("request_id" in request):
                    raise AgentError("invalid_request", "provide exactly one of run_id or request_id")
                if "run_id" in request:
                    row = self.history.get(self.agent_id(request["run_id"], "run_id"))
                else:
                    row = self.history.get_request(self.agent_request_id(request["request_id"]))
                response["run"] = self.agent_result(row)
                return response
            sid = self.agent_id(request.get("session_id"), "session_id")
            instance = request.get("instance")
            if not isinstance(instance, str) or re.fullmatch(r"[0-9a-f]{32}", instance) is None:
                raise AgentError("invalid_request", "instance must be the value returned by sessions")
            if method in ("acquire", "renew", "release"):
                return self.agent_lease_request(method, request, response, sid, instance)
            if method in ("upload", "download"):
                return self.start_transfer(method, request, response, sid, instance)
            request_id = self.agent_request_id(request.get("request_id"))
            lease_id = self.agent_lease_id(request["lease_id"]) if "lease_id" in request else None
            if not isinstance(request.get("script"), str):
                raise AgentError("invalid_request", "script must be a string")
            script = self.normalize_script(request["script"].encode("utf-8"))
            previous = self.history.get_request(request_id)
            response["request_id"] = request_id
            response["replayed"] = previous is not None
            if previous is not None:
                digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
                if ((previous["instance"], previous["session_id"], previous["sha256"]) != (instance, sid, digest) or
                        previous["lease_key"] != self.history.lease_key(lease_id)):
                    raise AgentError("request_conflict", "request_id already belongs to a different command, target or lease")
                response["run"] = self.agent_result(previous)
                return response
            session = self.agent_session(sid, instance)
            if lease_id is not None:
                self.require_lease(session, lease_id)
            reason = self.agent_busy_reason(session, lease_id)
            if reason:
                raise AgentError("session_busy", reason)
            _, results = self.start_batch(Path("agent-" + request_id), script, [session],
                                          request_id=request_id, lease_id=lease_id)
            result = results[0]
            self.notice(f"[Agent {request_id}] Job {result.job_id}, Run {result.record_id}, Session {sid} queued.")
            response["run"] = self.agent_result(self.history.get(result.record_id))
            return response
        except AgentError:
            raise
        except sqlite3.Error as error:
            raise AgentError("history_unavailable", str(error)) from error
        except OSError as error:
            raise AgentError("storage_error", str(error)) from error
        except ValueError as error:
            raise AgentError("invalid_request", str(error)) from error

    def show_jobs(self, words) -> None:
        if len(words) > 2 or (len(words) == 2 and
                             (not words[1].isascii() or not words[1].isdigit() or len(words[1]) > 20)):
            self.emit("Usage: jobs [ID]\n")
            return
        selected = int(words[1]) if len(words) == 2 else None
        found = False
        for job_id, (path, results) in self.jobs.items():
            if selected is not None and selected != job_id:
                continue
            found = True
            self.emit(f"Job {job_id}: {path!r}\nSESSION  PEER                     STATE          EXIT  RUN\n")
            for result in results:
                code = str(result.exit_code) if result.exit_code is not None else "-"
                self.emit(f"{result.session_id:<9}{result.peer:<25}{result.state:<15}{code:<6}{result.record_id}\n")
        if not found:
            self.emit("(no matching jobs)\n")

    def record_history(self, result: BatchResult) -> None:
        if result.record_id is not None:
            self.history_updates[result.record_id] = result

    def flush_history(self, force=False) -> bool:
        if not self.history_updates and not self.transfer_updates:
            return True
        now = time.monotonic()
        pending_only = (all(result.state in PENDING_JOB_STATES for result in self.history_updates.values()) and
                        all(t.state in PENDING_TRANSFER_STATES for t in self.transfer_updates.values()))
        if not force and now < self.history_flush_at and (pending_only or self.history_error):
            return not self.history_error
        self.history_flush_at = now + 0.25
        try:
            self.history.update(list(self.history_updates.values()), list(self.transfer_updates.values()))
        except (OSError, ValueError, sqlite3.Error) as error:
            message = str(error)
            if message != self.history_error:
                self.notice(f"[!] Execution history could not be saved: {message}. Pending results remain in memory; new batches are blocked.")
            self.history_error = message
            return False
        self.history_updates.clear()
        self.transfer_updates.clear()
        if self.history_error:
            self.notice("[+] Pending execution history saved.")
        self.history_error = ""
        return True

    @staticmethod
    def history_time(value) -> str:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value is not None else "-"

    def history_state(self, row) -> str:
        if row["instance"] != self.history.instance and row["state"] in PENDING_JOB_STATES:
            return "unknown"
        return row["state"]

    def show_history(self, line: str) -> None:
        usage = "Usage: history [SESSION|IP] [SCRIPT] [-p PAGE]; SESSION refers to this console instance."
        try:
            words = shlex.split(line)[1:]
            page = 1
            if "-p" in words:
                position = words.index("-p")
                value = words[position + 1] if position + 1 < len(words) else ""
                if not value.isascii() or not value.isdigit() or len(value) > 6 or int(value) < 1:
                    raise ValueError(usage)
                page = int(value)
                del words[position:position + 2]
            if len(words) > 2 or any(word.startswith("-") for word in words):
                raise ValueError(usage)
            session, ip, script = None, None, None
            if words:
                first = words[0]
                if first.isascii() and first.isdigit():
                    if len(first) > 18 or int(first) < 1:
                        raise ValueError(usage)
                    session = int(first)
                    words.pop(0)
                else:
                    try:
                        ip = str(ipaddress.IPv4Address(first))
                        words.pop(0)
                    except ipaddress.AddressValueError:
                        pass
            if len(words) > 1:
                raise ValueError(usage)
            if words:
                script = words[0]
            if not self.flush_history(force=True):
                self.emit(f"[!] Showing saved records only; some newer results are unsaved: {self.history_error}\n")
            rows, count, started = self.history.query(session, ip, script, page)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            self.emit(f"[!] Cannot query history: {error}\n")
            return
        pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        self.emit(f"Execution history: {count} records; page {page}/{pages}. Use result RUN for details.\n")
        self.emit("RUN    JOB    SESSION  IP               START  STATE           EXIT  SCRIPT\n")
        for row in rows:
            sid = str(row["session_id"]) if row["instance"] == self.history.instance else f"old:{row['session_id']}"
            code = str(row["exit_code"]) if row["exit_code"] is not None else "-"
            self.emit(f"{row['id']:<7}{row['job_id']:<7}{sid:<9}{row['ip']:<17}"
                      f"{'yes' if row['started_at'] is not None else '-':<7}{self.history_state(row):<16}"
                      f"{code:<6}{row['script_name']!r}\n")
        if not rows:
            self.emit("(no matching execution records on this page)\n")
        unconfirmed = [s.id for s in self.sessions.values() if s.connected and s.id not in started
                       and (session is None or s.id == session) and (ip is None or s.ip == ip)]
        if unconfirmed:
            self.emit("Current sessions with no saved script-start confirmation for this filter: " +
                      ", ".join(map(str, unconfirmed)) + ".\n")
        self.emit("START confirms entry into the script; EXIT '-' is unknown. old: IDs belong to earlier/other consoles.\n")

    def show_result(self, words) -> None:
        try:
            if (len(words) not in (2, 3) or any(not value.isascii() or not value.isdigit() or
                    len(value) > 18 or int(value) < 1 for value in words[1:])):
                raise ValueError("Usage: result RUN [OUTPUT_PAGE]")
            if not self.flush_history(force=True):
                self.emit(f"[!] Showing a saved record; newer results may be unsaved: {self.history_error}\n")
            row = self.history.get(int(words[1]))
            if row is None:
                raise ValueError("No such execution record. Use history to find RUN IDs.")
            output = bytes(row["output"])
            decoded_output = output.decode("utf-8", errors="replace")
            pages = max(1, (len(decoded_output) + OUTPUT_PAGE_SIZE - 1) // OUTPUT_PAGE_SIZE)
            page = int(words[2]) if len(words) == 3 else pages
            if page > pages:
                raise ValueError(f"Output has {pages} pages.")
        except (OSError, ValueError, sqlite3.Error) as error:
            self.emit(f"[!] Cannot show result: {error}\n")
            return
        code = str(row["exit_code"]) if row["exit_code"] is not None else "unknown"
        current = row["instance"] == self.history.instance
        self.emit(f"Run {row['id']} / Job {row['job_id']} / Session {row['session_id']} "
                  f"({'current' if current else 'earlier/other'} console)\n"
                  f"Peer: {row['peer']}\nScript: {row['script_path']!r}\nSHA256: {row['sha256']}\n"
                  f"Connected: {self.history_time(row['connected_at'])}\n"
                  f"Submitted: {self.history_time(row['submitted_at'])}\n"
                  f"Batch received: {self.history_time(row['received_at'])}\n"
                  f"Script started: {self.history_time(row['started_at'])}\n"
                  f"Finished/connection lost: {self.history_time(row['finished_at'])}\n"
                  f"State: {self.history_state(row)} (last reported: {row['state']}); exit: {code}\n"
                  f"Output: {len(output)} retained bytes, {row['dropped']} earlier bytes discarded; page {page}/{pages}.\n")
        chunk = decoded_output[(page - 1) * OUTPUT_PAGE_SIZE:page * OUTPUT_PAGE_SIZE]
        safe = "".join(c if c.isprintable() or c in "\n\t" else f"\\u{ord(c):04x}" for c in chunk)
        self.emit(safe + ("\n" if safe and not safe.endswith("\n") else ""))
        if not output:
            self.emit("(no saved batch output)\n")
        if row["exit_code"] is None:
            self.emit("No final exit code was received; this does not establish whether the remote process stopped.\n")

    def resolve_ip(self, target: str) -> str:
        if target.isascii() and target.isdigit() and len(target) <= 20:
            session = self.sessions.get(int(target))
            if session is None:
                raise ValueError("No such session. Use sessions to list IDs, or specify an IP.")
            target = session.ip
        IPNames.validate(target, "", "name", allow_empty=True)
        return target

    def show_names(self, words) -> None:
        if len(words) > 2:
            self.emit("Usage: names [ID|IP]\n")
            return
        try:
            target = self.resolve_ip(words[1]) if len(words) == 2 else None
            self.names.refresh()
            rows = [(ip, row) for ip, row in sorted(self.names.records.items()) if target is None or ip == target]
        except (OSError, ValueError, sqlite3.Error) as error:
            self.emit(f"[!] Cannot read names: {error}\n")
            return
        self.emit("IP                NAME\n")
        for ip, (name, note) in rows:
            self.emit(f"{ip:<17} {name or '-'}\n  Note: {note or '-'}\n")
        if not rows:
            self.emit("(no saved names or notes)\n")

    def name_command(self, line: str) -> None:
        parts = line.split(maxsplit=2)
        setting = parts[0] in ("name", "note")
        if len(parts) != (3 if setting else 2):
            self.emit("Usage: name ID|IP NAME | note ID|IP TEXT | unname ID|IP | unnote ID|IP\n")
            return
        field = "note" if parts[0] in ("note", "unnote") else "name"
        value = parts[2].strip() if setting else None
        try:
            target = self.resolve_ip(parts[1])
            self.names.set(target, value, field)
        except (OSError, ValueError, sqlite3.Error) as error:
            self.emit(f"[!] {field.title()} unchanged: {error}\n")
            return
        if value is None:
            self.emit(f"{field.title()} removed for {target}.\n")
        else:
            self.emit(f"{field.title()} saved for {target}: {value}\n")


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--remote", action="store_true", help="receive VPS connections through the configured frpc proxy")
    modes.add_argument("--local", action="store_true", help="receive TCP connections directly on this machine")
    parser.add_argument("--config", type=Path, help="ShellDeck JSON settings (auto-discovered when omitted)")
    parser.add_argument("-p", "--port", "--listen", type=int, default=argparse.SUPPRESS, metavar="PORT",
                        help="local mode TCP port (default: 9000); 0 chooses a free port")
    parser.add_argument("-H", "--host", default=argparse.SUPPRESS, help="local mode IPv4 address (default: 0.0.0.0)")
    parser.add_argument("-b", "--buffer-kib", type=positive, default=1024, help="maximum unread output per session in KiB")
    parser.add_argument("-m", "--max-sessions", type=positive, default=100, help="maximum retained sessions; oldest inactive closed record is evicted first")
    parser.add_argument("-t", "--idle-timeout", type=nonnegative, default=DEFAULT_IDLE_TIMEOUT, metavar="SECONDS",
                        help="disconnect each session after this many seconds without submitted input; 0 disables")
    parser.add_argument("--agent-socket", metavar="PATH", help="override the shared private JSON agent socket")
    parser.add_argument("--proxy-protocol", choices=("v1",), default=argparse.SUPPRESS,
                        help="remote mode only; PROXY v1 is already required by the frpc configuration")
    args = parser.parse_args()
    if not (args.remote or args.local):
        parser.print_help()
        return 0
    if args.remote and (hasattr(args, "host") or hasattr(args, "port")):
        parser.error("--host/--port are only valid with --local; change frpc_config for --remote")
    if args.local and hasattr(args, "proxy_protocol"):
        parser.error("--proxy-protocol requires --remote; --local accepts ordinary TCP")
    from shelldeck_config import read_config
    try:
        configured = read_config(args.config, remote=args.remote)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.remote:
        args.host, args.port = configured["host"], configured["port"]
        args.proxy_protocol = configured["proxy_protocol"]
    else:
        args.host = getattr(args, "host", "0.0.0.0")
        args.port = getattr(args, "port", 9000)
        args.proxy_protocol = None
    if args.agent_socket is None:
        args.agent_socket = configured["agent_socket"]
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if args.proxy_protocol:
        try:
            validate_proxy_listener(args.host)
        except ValueError as error:
            parser.error(str(error))
    handlers = {}
    try:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        config_root = Path(config_home) if config_home and Path(config_home).is_absolute() else Path.home() / ".config"
        names = IPNames(config_root / "shelldeck" / "names.db")
        history = HistoryStore(config_root / "shelldeck" / "history.db")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.host, args.port))
            listener.listen(128)
            listener.setblocking(False)
            with Terminal() as terminal:
                console = Console(listener, terminal, args.buffer_kib * 1024, args.max_sessions,
                                  args.idle_timeout, names, history, args.agent_socket, args.proxy_protocol,
                                  configured.get("client_endpoint"))

                def stop(_signum, _frame):
                    console.running = False

                for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    handlers[signum] = signal.signal(signum, stop)
                console.run()
    except (OSError, ValueError, termios.error) as error:
        print(f"shelldeck: {error}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    print("\nListener stopped; all connections closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
