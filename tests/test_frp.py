"""Optional real frp acceptance of the proxy fragment, using temporary local endpoints."""

import os
import json
import secrets
import select
import shutil
import socket
import subprocess
import sys
import time
import unittest

from test_agent import AgentFixture, ROOT
from test_proxy_protocol import ProxyPayloadChecks
from test_shelldeck import RunningConsole


FRPC = os.environ.get("SHELLDECK_FRPC") or shutil.which("frpc")
FRPS = os.environ.get("SHELLDECK_FRPS") or shutil.which("frps")
OPENSSL = shutil.which("openssl")


@unittest.skipUnless(FRPC and FRPS and OPENSSL, "real frp test requires frpc, frps and openssl")
class FrpIntegrationTests(ProxyPayloadChecks, AgentFixture):
    def start(self):
        self.console = RunningConsole("--proxy-protocol", "v1", "--agent-socket", str(self.socket_path),
                                      config_home=self.root)
        self.addCleanup(self.console.close)
        self.instance = self.call("sessions")["instance"]
        sockets = [socket.socket(), socket.socket()]
        try:
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            self.control_port, self.public_port = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        cert, key = self.certificate("server")
        self.frp_env = {**os.environ, "FRP_TEST_TOKEN": secrets.token_hex(32),
                        "FRP_TEST_CA_FILE": str(cert)}
        # The project ships only a proxy fragment. Test-owned endpoints supply
        # loopback listeners, random ports and credentials, and temporary TLS.
        auth = 'auth.method = "token"\nauth.token = "{{ .Envs.FRP_TEST_TOKEN }}"\n'
        server = (f'bindAddr = "127.0.0.1"\nbindPort = {self.control_port}\n'
                  f'proxyBindAddr = "127.0.0.1"\nallowPorts = [{{ single = {self.public_port} }}]\n'
                  + auth + 'transport.tls.force = true\n'
                  + f'transport.tls.certFile = {json.dumps(str(cert))}\n'
                  + f'transport.tls.keyFile = {json.dumps(str(key))}\n')
        proxy = (ROOT / "examples/frp/proxy.toml").read_text()
        proxy = proxy.replace("localPort = 19000", f"localPort = {self.console.port}")
        proxy = proxy.replace("remotePort = 9099", f"remotePort = {self.public_port}")
        client = (f'serverAddr = "127.0.0.1"\nserverPort = {self.control_port}\n'
                  + auth + 'transport.tls.enable = true\n'
                  + 'transport.tls.trustedCaFile = "{{ .Envs.FRP_TEST_CA_FILE }}"\n\n' + proxy)
        for name, binary, content in (("frps", FRPS, server), ("frpc", FRPC, client)):
            path = self.root / f"{name}.toml"
            path.write_text(content)
            checked = subprocess.run([binary, "verify", "-c", str(path)], env=self.frp_env,
                                     capture_output=True, timeout=10)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors="replace"))
        self.frps, _, _ = self.spawn("frps", FRPS)
        deadline = time.monotonic() + 10
        while True:
            self.assertIsNone(self.frps.poll(), "frps stopped before accepting the tunnel")
            try:
                with socket.create_connection(("127.0.0.1", self.control_port), timeout=0.2):
                    break
            except OSError:
                self.assertLess(time.monotonic(), deadline, "frps did not start")
                time.sleep(0.02)
        self.start_frpc()

    def certificate(self, name):
        cert, key = self.root / f"{name}.crt", self.root / f"{name}.key"
        created = subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                  "-sha256", "-days", "1", "-subj", "/CN=127.0.0.1",
                                  "-addext", "subjectAltName=IP:127.0.0.1",
                                  "-keyout", str(key), "-out", str(cert)], capture_output=True, timeout=15)
        self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
        key.chmod(0o600)
        return cert, key

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def spawn(self, name, binary):
        log_path = self.root / f"{name}.log"
        log = log_path.open("ab")
        self.addCleanup(log.close)
        offset = log.tell()
        process = subprocess.Popen([binary, "-c", str(self.root / f"{name}.toml")], env=self.frp_env,
                                   stdout=log, stderr=log)
        self.addCleanup(self.stop_process, process)
        return process, log_path, offset

    def start_frpc(self):
        self.frpc, log, offset = self.spawn("frpc", FRPC)
        deadline = time.monotonic() + 10
        while b"start proxy success" not in log.read_bytes()[offset:]:
            self.assertIsNone(self.frpc.poll(), "frpc stopped before starting the proxy")
            self.assertLess(time.monotonic(), deadline, "frpc did not start its TCP proxy")
            time.sleep(0.02)

    def peer(self):
        client = socket.create_connection(("127.0.0.1", self.public_port), timeout=3)
        self.console.clients.append(client)
        sid = len(self.console.clients)
        self.console.read_until(f"Session {sid} connected".encode())
        session = self.call("sessions")["sessions"][-1]
        # The source port must be the public-side client's, not frpc's local socket.
        address = client.getsockname()
        self.assertEqual(session["peer"], f"{address[0]}:{address[1]}")
        return client

    def test_multiple_clients_keep_original_peers_and_command_isolation(self):
        first, second = self.peer(), self.peer()
        result = self.execute(sid=2)
        self.assertTrue(result["ok"], result)
        token = self.token(second)
        self.assertFalse(select.select([first], [], [], 0.1)[0])
        second.sendall(token + b"start\x1f" + token + b"script\x1fvia-frp" + token + b"end:0\x1f")
        completed = self.result(result["run"]["run_id"])
        self.assertEqual(completed["output"], "via-frp")
        self.assertEqual(completed["peer"], f"127.0.0.1:{second.getsockname()[1]}")

    def test_tunnel_loss_preserves_unknown_result_and_restart_needs_new_session(self):
        first = self.peer()
        self.execute()
        token = self.token(first)
        first.sendall(token + b"start\x1fpartial")
        deadline = time.monotonic() + 5
        while self.result(complete=False)["output"] != "partial":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.stop_process(self.frpc)
        result = self.result()
        self.assertEqual(result["state"], "disconnected")
        self.assertIsNone(result["success"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(first.recv(1), b"")
        self.start_frpc()
        self.peer()
        listed = self.call("sessions")["sessions"]
        self.assertEqual([(s["id"], s["connected"]) for s in listed], [(1, False), (2, True)])

    def test_bad_token_and_untrusted_server_certificate_are_rejected(self):
        self.stop_process(self.frpc)
        other_cert, _ = self.certificate("untrusted")
        for variable, value in (("FRP_TEST_TOKEN", secrets.token_hex(32)),
                                ("FRP_TEST_CA_FILE", str(other_cert))):
            with self.subTest(variable=variable):
                rejected = subprocess.run([FRPC, "-c", str(self.root / "frpc.toml")],
                                          env={**self.frp_env, variable: value}, capture_output=True, timeout=10)
                self.assertNotEqual(rejected.returncode, 0, "invalid frp credentials were accepted")
        self.assertEqual(self.call("sessions")["sessions"], [])

    def test_independent_frpc_survives_foreground_quit_and_relaunch(self):
        self.console.stop()
        config = self.root / "config.json"
        config.write_text(json.dumps({"frpc_config": str(self.root / "frpc.toml"), "proxy_name": "shelldeck",
                                      "agent_socket": str(self.socket_path)}))
        command = self.root / "bin/shelldeck"
        installed = subprocess.run([sys.executable, str(ROOT / "scripts/install.py"), "--config", str(config),
                                    "--command-path", str(command)], capture_output=True, text=True, timeout=10)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        original_config = (self.root / "frpc.toml").read_bytes()
        original_frpc_pid = self.frpc.pid
        instances = []
        for attempt in range(2):
            self.console = RunningConsole(command=[str(command)], config_home=self.root,
                                          environment={"PATH": "/nonexistent"})
            self.addCleanup(self.console.close)
            self.instance = self.call("sessions")["instance"]
            instances.append(self.instance)
            peer = self.peer()
            submitted = self.execute(request_id=f"foreground-{attempt}")
            self.assertTrue(submitted["ok"], submitted)
            token = self.token(peer)
            peer.sendall(token + b"start\x1f" + token + b"script\x1fforeground" + token + b"end:0\x1f")
            self.assertEqual(self.result(submitted["run"]["run_id"])["output"], "foreground")
            if attempt == 0:
                self.console.command("quit")
                self.assertEqual(self.console.process.wait(timeout=5), 0)
            else:
                self.console.stop()
                self.assertEqual(self.console.process.returncode, 0)
            self.assertEqual(peer.recv(1), b"")
            self.assertFalse(self.socket_path.exists())
            self.assertIsNone(self.frpc.poll(), "exiting ShellDeck stopped the shared frpc")
            self.assertEqual(self.frpc.pid, original_frpc_pid)
            self.assertEqual((self.root / "frpc.toml").read_bytes(), original_config)
        self.assertNotEqual(instances[0], instances[1])


if __name__ == "__main__":
    unittest.main()
