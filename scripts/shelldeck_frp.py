#!/usr/bin/env python3
"""Configure and manage the local macOS ShellDeck/frpc service."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time


def default_config():
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "shelldeck-frp" / "service.json"


def private_write(path, data, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".shelldeck-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Service:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = json.loads(self.config_path.read_text())
        if not isinstance(self.config, dict):
            raise ValueError("service configuration must be a JSON object")
        self.env = dict(os.environ)
        environment = self.config.get("environment", {})
        if not isinstance(environment, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
            raise ValueError("environment must contain string keys and values")
        self.env.update(environment)
        self.project = self.path("project_root")
        self.frpc_config = self.path("frpc_config")
        self.socket = self.path("agent_socket")
        self.plist = self.path("launch_agent_path")
        self.command = self.path("command_path")
        self.logs = self.path("log_directory")
        for key in ("python_binary", "frpc_binary", "ssh_binary", "tmux_binary", "launchctl_binary"):
            setattr(self, key, self.executable(key))
        self.host = self.value("ssh_host")
        if self.host.startswith("-") or any(c.isspace() for c in self.host):
            raise ValueError("ssh_host must be an SSH alias or hostname")
        self.label = self.identifier("launch_agent_label")
        self.tmux_session = self.identifier("tmux_session")
        if "." in self.tmux_session:
            raise ValueError("tmux_session must not contain dots")
        self.tmux_target = "=" + self.tmux_session
        self.tmux = [self.tmux_binary, "-L", self.identifier("tmux_socket"), "-f", os.devnull]
        self.listen_host = str(ipaddress.IPv4Address(self.value("listen_host")))
        if not ipaddress.IPv4Address(self.listen_host).is_loopback:
            raise ValueError("listen_host must remain IPv4 loopback for PROXY protocol")
        self.port = self.config["listen_port"]
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("listen_port must be an integer from 1 to 65535")
        self.remote = self.config["remote_allowlist_command"]
        if not isinstance(self.remote, list) or not self.remote or any(
                not isinstance(part, str) or not part or "\x00" in part for part in self.remote):
            raise ValueError("remote_allowlist_command must be a nonempty argv array")
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + self.label
        self.stopping = False
        self.console_identity = None

    def value(self, key):
        value = self.config[key]
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"{key} must be a nonempty string")
        return value

    def identifier(self, key):
        value = self.value(key)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError(f"{key} contains unsupported characters")
        return value

    def path(self, key):
        value = Path(self.value(key)).expanduser()
        # Preserve executable symlinks: resolving a venv Python loses its environment.
        return Path(os.path.abspath(value if value.is_absolute() else self.config_path.parent / value))

    def executable(self, key):
        value = self.value(key)
        if "/" in value or value.startswith("~"):
            result = str(self.path(key))
        else:
            result = shutil.which(value, path=self.env.get("PATH"))
        if not result or not Path(result).is_file() or not os.access(result, os.X_OK):
            raise ValueError(f"{key} executable not found: {value}")
        return str(Path(result).absolute())

    def run(self, argv, **kwargs):
        return subprocess.run(argv, env=self.env, **kwargs)

    def console_running(self):
        return self.run(self.tmux + ["has-session", "-t", self.tmux_target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def ensure_console(self):
        current = self.run(self.tmux + ["display-message", "-p", "-t", self.tmux_target + ":",
                                        "#{pid} #{session_id}"], capture_output=True, text=True)
        identity = current.stdout.strip()
        if current.returncode == 0 and re.fullmatch(r"[0-9]+ \$[0-9]+", identity):
            if identity != self.console_identity:
                raise RuntimeError("tmux session already exists; stop its owner or use a dedicated tmux_socket/tmux_session")
            return
        command = [self.python_binary, str(self.project / "shelldeck.py"),
                   "-H", self.listen_host, "-p", str(self.port), "--proxy-protocol", "v1",
                   "--agent-socket", str(self.socket)]
        created = self.run(self.tmux + ["new-session", "-d", "-P", "-F", "#{pid} #{session_id}",
                                       "-s", self.tmux_session, "-x", "180", "-y", "45",
                                       "exec " + shlex.join(command)],
                           check=True, capture_output=True, text=True)
        identity = created.stdout.strip()
        if not re.fullmatch(r"[0-9]+ \$[0-9]+", identity):
            raise RuntimeError("tmux did not return the created session identity")
        self.console_identity = identity
        for _ in range(50):
            if self.socket.exists():
                return
            if not self.console_running():
                raise RuntimeError("ShellDeck exited; check the configured listener and agent socket")
            time.sleep(0.1)
        raise RuntimeError("ShellDeck agent socket did not appear")

    def supervise(self):
        def shutdown(_signum, _frame):
            self.stopping = True

        os.umask(0o077)
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        child = None
        try:
            self.ensure_console()
            child = subprocess.Popen([self.frpc_binary, "-c", str(self.frpc_config)], env=self.env)
            while not self.stopping:
                if child.poll() is not None:
                    return child.returncode or 1
                self.ensure_console()
                time.sleep(2)
            return 0
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if self.console_identity is not None:
                # IDs are stable within a tmux server; the PID also distinguishes
                # a restarted server which may reuse the same session ID.
                session_id = self.console_identity.split()[1]
                self.run(self.tmux + ["if-shell", "-F", "-t", session_id,
                                      "#{==:#{pid} #{session_id}," + self.console_identity + "}",
                                      "kill-session -t " + shlex.quote(session_id)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.console_identity = None

    def access(self, action):
        # SSH executes a shell command remotely; quote each configured argv item.
        self.run([self.ssh_binary, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                  self.host, shlex.join(self.remote + [action])], check=True, timeout=30)

    def check(self):
        for name in ("shelldeck.py", "shelldeck_agent.py"):
            if not (self.project / name).is_file():
                raise ValueError(f"project_root does not contain {name}")
        if not self.frpc_config.is_file():
            raise ValueError("frpc_config does not exist")
        self.run([self.frpc_binary, "verify", "-c", str(self.frpc_config)], check=True, timeout=15)

    def launch_agent(self):
        environment = dict(self.config.get("environment", {}))
        # launchd does not inherit the installing terminal's PATH. Preserve the
        # directories used to resolve configured command names without assuming
        # a package manager or CPU architecture.
        directories = [str(Path(getattr(self, key)).parent) for key in
                       ("python_binary", "frpc_binary", "ssh_binary", "tmux_binary", "launchctl_binary")]
        environment.setdefault("PATH", os.pathsep.join(dict.fromkeys(directories + os.defpath.split(os.pathsep))))
        return {
            "Label": self.label,
            "ProgramArguments": [self.python_binary, str(Path(__file__).resolve()),
                                 "--config", str(self.config_path), "supervise"],
            "WorkingDirectory": str(self.project),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "ExitTimeOut": 15, "Umask": 0o077,
            "StandardOutPath": str(self.logs / "service.log"),
            "StandardErrorPath": str(self.logs / "service.error.log"),
            "EnvironmentVariables": environment,
        }

    def install(self):
        self.check()
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        wrapper = ("#!/bin/sh\n# Generated by shelldeck-frp install; edit service.json instead.\nexec "
                   + shlex.join([self.python_binary, str(Path(__file__).resolve()),
                                 "--config", str(self.config_path)]) + ' "$@"\n')
        private_write(self.plist, plistlib.dumps(self.launch_agent()))
        private_write(self.command, wrapper.encode(), 0o700)
        print(f"Installed {self.command}\nLaunchAgent: {self.plist}")

    def start(self):
        self.check()
        if not self.plist.exists() or plistlib.loads(self.plist.read_bytes()) != self.launch_agent():
            raise ValueError("LaunchAgent does not match this configuration; run install first")
        self.access("refresh-local")
        loaded = self.run([self.launchctl_binary, "print", self.target],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not loaded:
            self.run([self.launchctl_binary, "bootstrap", self.domain, str(self.plist)], check=True)
        print(f"Started; use {self.command} status or {self.command} attach")

    def stop(self):
        self.run([self.launchctl_binary, "bootout", self.target], check=True)

    def status(self):
        loaded = self.run([self.launchctl_binary, "print", self.target], capture_output=True, text=True)
        print("LaunchAgent:", "loaded" if loaded.returncode == 0 else "stopped", flush=True)
        for line in loaded.stdout.splitlines():
            if line.startswith(("\tstate =", "\tpid =", "\tlast exit code =")):
                print(line.strip())
        print("ShellDeck console:", "running" if self.console_running() else "stopped", flush=True)
        if self.socket.exists():
            self.run([self.python_binary, str(self.project / "shelldeck_agent.py"),
                      "--socket", str(self.socket), "sessions"], check=True)
        else:
            print("Agent socket absent")

    def attach(self):
        os.execve(self.tmux_binary, self.tmux + ["attach-session", "-t", self.tmux_target], self.env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config(), help="service JSON path")
    parser.add_argument("command", nargs="?", default="status",
                        choices=("check", "install", "start", "stop", "status", "attach",
                                 "refresh-access", "access-status", "supervise"))
    args = parser.parse_args(argv)
    try:
        service = Service(args.config)
        if args.command in ("refresh-access", "access-status"):
            service.access("refresh-local" if args.command == "refresh-access" else "status")
        else:
            return getattr(service, args.command)() or 0
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"shelldeck-frp: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
