"""Download configuration is external; tests serve only a harmless local fixture."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pv3.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required")
class DownloadConfigTests(unittest.TestCase):
    def test_private_config_controls_address_key_path_output_and_work_directory(self):
        with tempfile.TemporaryDirectory(prefix="shelldeck-download-") as directory:
            root = Path(directory).resolve()
            tools = root / "tools"
            tools.mkdir()
            for tool in ("chmod", "mktemp", "mv", "rm"):
                (tools / tool).symlink_to(shutil.which(tool))
            requests = []
            content = b"#!/bin/sh\nprintf 'LOCAL_FIXTURE_OK\\n'; pwd\n"

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append((self.path, self.headers.get("X-PV")))
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

                def log_message(self, *args):
                    pass

            server = HTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                output = root / "download 'quoted' fixture"
                config = root / "private settings.env"
                values = {"PV_HOST": "127.0.0.1", "PV_PORT": str(server.server_port),
                          "PV_HTTPS_HOST": "localhost", "PV_KEY": "LocalFixtureKey",
                          "PV_PATH": "/test-fixture", "PV_OUTPUT": str(output),
                          "PV_WORK_DIR": str(root), "PV_TIMEOUT": "10"}
                config.write_text("".join(k + "=" + shlex.quote(v) + "\n" for k, v in values.items()))
                config.chmod(0o600)
                env = {k: v for k, v in os.environ.items() if not k.startswith("PV_")}
                env.update(PV_CONFIG=str(config), PATH=str(tools))
                result = subprocess.run([BASH, str(SCRIPT)], env=env, cwd=SCRIPT.parent,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LOCAL_FIXTURE_OK\n" + str(root), result.stdout)
                self.assertEqual(requests, [("/test-fixture", "LocalFixtureKey")])
                self.assertEqual(output.read_bytes(), content)
            finally:
                server.shutdown()
                worker.join(timeout=2)
                server.server_close()

    def test_missing_configuration_fails_without_a_built_in_server_fallback(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PV_")}
        result = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid host", result.stderr)
        self.assertNotIn("[http]", result.stderr)
        self.assertNotIn("[https]", result.stderr)


if __name__ == "__main__":
    unittest.main()
