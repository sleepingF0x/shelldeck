"""Bounded file streams over an already owned Bash/TCP session.

The console owns the operation, not its CLI/MCP caller. File bytes never enter
command history or tool responses. A separate random frame token scopes each
stream; uploads use one acknowledged chunk at a time to bound the send queue.
"""

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import tempfile
import time


CHUNK_SIZE = 24 * 1024
FRAME_LIMIT = CHUNK_SIZE * 4 // 3 + 160
PENDING_STATES = {"queued", "sent", "running", "verifying"}

# Bash 3.2-compatible. Binary bytes only pass through files/pipes, never Bash
# variables. All paths are absolute arguments; nothing interpolates shell code.
REMOTE_SCRIPT = r'''
set +xv
set -o pipefail
export LC_ALL=C
umask 077
token=$1 mode=$2 path=$3 overwrite=$4 expected=$5 chunk_size=$6
work=''
frame() { printf '\036%s:%s\037' "$token" "$1"; }
fail() { frame "error:$1"; exit 1; }
cleanup() { if [[ $work ]]; then rm -rf -- "$work"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
for tool in base64 wc; do command -v "$tool" >/dev/null 2>&1 || fail unsupported_tools; done
if command -v sha256sum >/dev/null 2>&1; then
    hash_cmd=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    hash_cmd=(shasum -a 256)
else fail unsupported_tools; fi
hash_file() {
    local out
    out=$("${hash_cmd[@]}" < "$1") || return 1
    out=${out%% *}
    [[ ${#out} == 64 && $out != *[!0-9a-f]* ]] || return 1
    printf '%s' "$out"
}
probe=$(hash_file /dev/null) || fail unsupported_tools
[[ $probe == e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ]] || fail unsupported_tools
if [[ $mode == upload ]]; then
    for tool in mktemp rm; do command -v "$tool" >/dev/null 2>&1 || fail unsupported_tools; done
    if [[ $(printf Zg== | base64 -d 2>/dev/null) == f ]]; then decode=(base64 -d)
    elif [[ $(printf Zg== | base64 -D 2>/dev/null) == f ]]; then decode=(base64 -D)
    else fail unsupported_tools; fi
    [[ ! -L $path && ( ! -e $path || -f $path ) ]] || fail invalid_destination
    [[ $overwrite == 1 || ! -e $path ]] || fail destination_exists
    parent=${path%/*}; [[ $parent ]] || parent=/
    work=$(mktemp -d "$parent/.shelldeck-transfer.XXXXXXXX") || fail io_error
    stage=$work/payload
    : > "$stage" || fail io_error
    # Probe exact-path publication. Plain mv/ln can interpret a raced directory
    # destination as a container, so do not use their ambiguous two-path form.
    if [[ $overwrite == 0 ]]; then
        if command -v link >/dev/null 2>&1 && link "$stage" "$work/probe" 2>/dev/null; then publish=link
        elif ln -T -- "$stage" "$work/probe" 2>/dev/null; then publish=ln
        else fail unsupported_publish; fi
    else
        : > "$work/probe" || fail io_error
        if mv -fT -- "$work/probe" "$work/moved" 2>/dev/null; then publish=mv
        elif command -v perl >/dev/null 2>&1 && perl -e 'rename($ARGV[0], $ARGV[1]) or exit 1' "$work/probe" "$work/moved"; then publish=perl
        else fail unsupported_publish; fi
    fi
    frame ready
    next=1
    while IFS= read -r -t 60 line; do
        [[ $line == "#$token:"* ]] || fail invalid_frame
        line=${line#"#$token:"}
        case $line in
            data:*)
                data=${line#data:}; index=${data%%:*}; data=${data#*:}
                [[ $index == "$next" && ${#data} -le $((chunk_size * 4 / 3)) && $data && $data != *[!a-zA-Z0-9+/=]* ]] || fail invalid_frame
                printf '%s' "$data" | "${decode[@]}" >> "$stage" || fail decode_error
                frame "ack:$next"
                next=$((next + 1))
                ;;
            commit:*)
                digest=${line#commit:}
                [[ ${#digest} == 64 && $digest != *[!0-9a-f]* ]] || fail invalid_frame
                frame verifying
                size=$(wc -c < "$stage") || fail io_error
                [[ $size =~ ^[[:space:]]*$expected[[:space:]]*$ ]] || fail size_mismatch
                actual=$(hash_file "$stage") || fail io_error
                [[ $actual == "$digest" ]] || fail hash_mismatch
                [[ ! -L $path && ( ! -e $path || -f $path ) ]] || fail invalid_destination
                case $publish in
                    link) link "$stage" "$path" || fail publish_failed;;
                    ln) ln -T -- "$stage" "$path" || fail publish_failed;;
                    mv) mv -fT -- "$stage" "$path" || fail publish_failed;;
                    perl) perl -e 'rename($ARGV[0], $ARGV[1]) or exit 1' "$stage" "$path" || fail publish_failed;;
                esac
                frame "verified:$expected:$actual"
                exit 0
                ;;
            abort) fail client_aborted;;
            *) fail invalid_frame;;
        esac
    done
    fail input_timeout
else
    command -v dd >/dev/null 2>&1 || fail unsupported_tools
    if [[ $(printf f | base64 -w0 2>/dev/null) == Zg== ]]; then encode=(base64 -w0)
    elif [[ $(printf f | base64 -b0 2>/dev/null) == Zg== ]]; then encode=(base64 -b0)
    else fail unsupported_tools; fi
    [[ -f $path && ! -L $path ]] || fail invalid_source
    size=$(wc -c < "$path") || fail io_error
    size=${size//[[:space:]]/}
    [[ $size =~ ^[0-9]+$ ]] || fail io_error
    before=$(hash_file "$path") || fail io_error
    exec 3< "$path" || fail io_error
    frame "meta:$size:$before"
    remaining=$size
    while ((remaining > 0)); do
        count=$chunk_size; ((remaining < count)) && count=$remaining
        # An open descriptor advances across chunks; no repeated seek/skip scan.
        # Disable wrapping at the encoder. Removing hundreds of line breaks with
        # Bash 4.1 global substitution can take seconds for a single chunk.
        data=$(dd bs="$count" count=1 <&3 2>/dev/null | "${encode[@]}") || fail io_error
        [[ $data ]] || fail source_changed
        frame "data:$data"
        remaining=$((remaining - count))
    done
    frame verifying
    after=$(hash_file "$path") || fail io_error
    final_size=$(wc -c < "$path") || fail io_error
    final_size=${final_size//[[:space:]]/}
    [[ $before == "$after" && $size == "$final_size" ]] || fail source_changed
    frame "verified:$size:$after"
fi
'''


def validate_path(value, label):
    if (not isinstance(value, str) or not value.startswith("/") or "\0" in value or
            len(value.encode("utf-8")) > 4096 or value.endswith("/")):
        raise ValueError(f"{label} must be an absolute file path without NUL, at most 4096 UTF-8 bytes")
    if Path(value).name in ("", ".", "..") or value.endswith("/.") or value.endswith("/.."):
        raise ValueError(f"{label} must name a regular file")
    return value


def check_destination(path, overwrite):
    try:
        node = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(node.st_mode):
        raise ValueError("destination must be a regular file, not a symlink or directory")
    if not overwrite:
        raise FileExistsError("destination exists; use overwrite explicitly to replace it")


class FileTransfer:
    def __init__(self, transfer_id, instance, session_id, direction, local_path, remote_path, overwrite=False):
        self.transfer_id = transfer_id
        self.instance = instance
        self.session_id = session_id
        self.direction = direction
        self.local_path = validate_path(local_path, "local_path")
        self.remote_path = validate_path(remote_path, "remote_path")
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        self.overwrite = overwrite
        self.token = "SHELLDECK_TRANSFER_" + secrets.token_hex(16)
        self.marker = b"\x1e" + self.token.encode("ascii") + b":"
        self.pending = b""
        self.state = "queued"
        self.exit_code = None
        self.error = None
        self.total_bytes = None
        self.bytes_transferred = 0
        self.sha256 = None
        self.submitted_at = time.time()
        self.finished_at = None
        self.file = None
        self.temporary_path = None
        self.hasher = hashlib.sha256()
        self.sequence = 0
        self.sent_bytes = 0
        self.ready = False
        self.verified = False
        self.committed = False
        self.source_stat = None

    def prepare(self):
        if self.direction == "upload":
            fd = os.open(self.local_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                node = os.fstat(fd)
                if not stat.S_ISREG(node.st_mode):
                    raise ValueError("source must be a regular file, not a symlink or directory")
                self.file = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            self.total_bytes = node.st_size
            self.source_stat = self.stat_identity(node)
        else:
            check_destination(self.local_path, self.overwrite)
            fd, self.temporary_path = tempfile.mkstemp(prefix=".shelldeck-transfer-", suffix=".part",
                                                       dir=str(Path(self.local_path).parent))
            self.file = os.fdopen(fd, "wb")

    @staticmethod
    def stat_identity(node):
        return node.st_dev, node.st_ino, node.st_size, node.st_mtime_ns, node.st_ctime_ns

    def command(self):
        # File transfer already requires base64. Using it for the fixed helper
        # avoids seconds of Bash string/loop work on CentOS 6; ordinary exec
        # retains its dependency-free decoder. Probe GNU/BSD flags explicitly.
        script = ("set -- " + " ".join(shlex.quote(str(value)) for value in
                  (self.token, self.direction, self.remote_path, int(self.overwrite), self.total_bytes or 0,
                   CHUNK_SIZE)) + "\n" + REMOTE_SCRIPT)
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        stdin = "" if self.direction == "upload" else " </dev/null"
        end = "\\036" + self.token + ":end:%s\\037"
        error = "\\036" + self.token + ":error:unsupported_tools\\037"
        launch = ("set +xv; set -o pipefail; "
                  "if [[ $(printf Zg== | base64 -d 2>/dev/null) == f ]]; then decode=(base64 -d); "
                  "elif [[ $(printf Zg== | base64 -D 2>/dev/null) == f ]]; then decode=(base64 -D); "
                  f"else printf {shlex.quote(error)}; exit 1; fi; "
                  f"script=$(printf %s {shlex.quote(encoded)} | \"${{decode[@]}}\") || exit 1; "
                  "exec bash -c \"$script\" shelldeck-transfer")
        return (f"( set +xv; if bash -c {shlex.quote(launch)} shelldeck-transfer-start{stdin}; "
                f"then printf {shlex.quote(end)} 0; else printf {shlex.quote(end)} \"$?\"; fi )\n").encode("utf-8")

    def info(self):
        pending = self.state in PENDING_STATES
        completed = self.state in ("ok", "failed")
        return {"transfer_id": self.transfer_id, "instance": self.instance, "session_id": self.session_id,
                "direction": self.direction, "local_path": self.local_path, "remote_path": self.remote_path,
                "overwrite": self.overwrite, "state": self.state, "pending": pending, "completed": completed,
                "success": self.state == "ok" if completed else None, "exit_code": self.exit_code,
                "bytes_transferred": self.bytes_transferred, "total_bytes": self.total_bytes,
                "sha256": self.sha256, "error": self.error, "temporary_path": self.temporary_path,
                "submitted_at": self.submitted_at, "finished_at": self.finished_at}

    def fail(self, code, message):
        if self.error is None:
            self.error = {"code": code, "message": message}
        self.cleanup()

    def cleanup(self):
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None
        if self.temporary_path is not None:
            try:
                os.unlink(self.temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                # Keep the path visible so a failed cleanup can be recovered.
                return
            self.temporary_path = None

    def disconnect(self, reason):
        self.fail("connection_lost", "Connection ended before confirmation; remote publication may be unknown")
        self.state = reason
        self.finished_at = time.time()
        self.pending = b""

    def upload_next(self):
        if self.error:
            return f"#{self.token}:abort\n".encode("ascii")
        try:
            chunk = self.file.read(CHUNK_SIZE)
            if chunk:
                self.sent_bytes += len(chunk)
                if self.sent_bytes > self.total_bytes:
                    raise ValueError("upload source changed during transfer")
                self.hasher.update(chunk)
                self.sequence += 1
                return (f"#{self.token}:data:{self.sequence}:".encode("ascii") + base64.b64encode(chunk) + b"\n")
            if (self.sent_bytes != self.total_bytes or
                    self.stat_identity(os.fstat(self.file.fileno())) != self.source_stat):
                raise ValueError("upload source changed during transfer")
            self.sha256 = self.hasher.hexdigest()
            self.file.close()
            self.file = None
            self.state = "verifying"
            return f"#{self.token}:commit:{self.sha256}\n".encode("ascii")
        except (OSError, ValueError) as error:
            self.fail("local_source_error", str(error))
            return f"#{self.token}:abort\n".encode("ascii")

    def frame(self, body):
        """Handle one bounded ASCII frame; return the next upload packet, if any."""
        if body == b"ready" and self.direction == "upload" and not self.ready:
            self.ready = True
            self.state = "running"
            return self.upload_next()
        if body.startswith(b"ack:") and self.direction == "upload" and self.ready:
            if body[4:] != str(self.sequence).encode("ascii") or self.state != "running":
                raise ValueError("unexpected upload acknowledgment")
            self.bytes_transferred = self.sent_bytes
            return self.upload_next()
        if body.startswith(b"meta:") and self.direction == "download" and not self.ready:
            size, digest = self.parse_digest(body[5:])
            self.total_bytes, self.sha256 = size, digest
            self.ready = True
            self.state = "running"
            return b""
        if body.startswith(b"data:") and self.direction == "download" and self.ready and not self.verified:
            try:
                chunk = base64.b64decode(body[5:], validate=True)
            except binascii.Error as error:
                raise ValueError("invalid Base64 file chunk") from error
            if not chunk or len(chunk) > CHUNK_SIZE or self.bytes_transferred + len(chunk) > self.total_bytes:
                raise ValueError("unexpected file chunk size")
            if self.error is None:
                try:
                    self.file.write(chunk)
                    self.hasher.update(chunk)
                except OSError as error:
                    self.fail("local_write_error", str(error))
            self.bytes_transferred += len(chunk)
            return b""
        if body == b"verifying" and self.ready:
            self.state = "verifying"
            return b""
        if body.startswith(b"verified:") and self.ready and not self.verified:
            size, digest = self.parse_digest(body[9:])
            if size != self.total_bytes or size != self.bytes_transferred or digest != self.sha256:
                raise ValueError("remote size or SHA-256 confirmation differs")
            if self.direction == "download" and self.error is None and self.hasher.hexdigest() != digest:
                self.fail("hash_mismatch", "download bytes do not match the remote SHA-256")
            self.verified = True
            return b""
        if body.startswith(b"error:") and re.fullmatch(rb"[a-z_]{1,64}", body[6:]):
            code = body[6:].decode("ascii")
            self.fail(code, "Remote file transfer failed: " + code)
            return b""
        if body.startswith(b"end:") and re.fullmatch(rb"[0-9]{1,3}", body[4:]) and int(body[4:]) <= 255:
            self.finish(int(body[4:]))
            return b""
        raise ValueError("unexpected file transfer frame")

    @staticmethod
    def parse_digest(value):
        match = re.fullmatch(rb"([0-9]{1,19}):([0-9a-f]{64})", value)
        if match is None or int(match[1]) > 2**63 - 1:
            raise ValueError("invalid file size or SHA-256")
        return int(match[1]), match[2].decode("ascii")

    def finish(self, exit_code):
        self.exit_code = exit_code
        if exit_code != 0:
            self.fail("remote_failed", f"Remote helper exited {exit_code}")
        elif not self.verified:
            self.fail("unverified", "Remote helper ended without size and SHA-256 confirmation")
        if self.error is None and self.direction == "download":
            try:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.file.close()
                self.file = None
                check_destination(self.local_path, self.overwrite)
                if self.overwrite:
                    os.replace(self.temporary_path, self.local_path)
                else:
                    os.link(self.temporary_path, self.local_path, follow_symlinks=False)
                self.committed = True
            except (OSError, ValueError) as error:
                self.fail("publish_failed", str(error))
        self.cleanup()
        self.state = "ok" if self.error is None else "failed"
        self.finished_at = time.time()

    def feed(self, data):
        """Strip file frames before terminal/history buffers can truncate them.

        Invalid framing is fatal to the TCP stream: no safe point exists at which
        to hand its remaining bytes back to Bash. The console closes that session.
        """
        data = self.pending + data
        self.pending = b""
        output = bytearray()
        outgoing = bytearray()
        while data:
            position = data.find(self.marker)
            if position < 0:
                keep = next((n for n in range(min(len(data), len(self.marker) - 1), 0, -1)
                             if data.endswith(self.marker[:n])), 0)
                output.extend(data[:-keep] if keep else data)
                self.pending = data[-keep:] if keep else b""
                break
            output.extend(data[:position])
            data = data[position:]
            end = data.find(b"\x1f", len(self.marker))
            if end < 0:
                if len(data) > FRAME_LIMIT:
                    raise ValueError("file transfer frame exceeds the chunk limit")
                self.pending = data
                break
            if end > FRAME_LIMIT:
                raise ValueError("file transfer frame exceeds the chunk limit")
            outgoing.extend(self.frame(data[len(self.marker):end]))
            data = data[end + 1:]
            if self.state not in PENDING_STATES:
                output.extend(data)
                break
        return bytes(output), bytes(outgoing)
