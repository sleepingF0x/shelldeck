"""Task ownership across commands, competing agents, human input and expiry."""

from concurrent.futures import ThreadPoolExecutor
import json
import select
import shlex
import signal
import socket
import sqlite3
import time

from test_agent import AgentFixture


OWNER = "a" * 32
OTHER = "b" * 32


class LeaseIntegrationTests(AgentFixture):
    def lease(self, method="acquire", lease_id=OWNER, **fields):
        return self.call(method, instance=self.instance, session_id=1, lease_id=lease_id, **fields)

    def owned_exec(self, request_id="owned-step", lease_id=OWNER, script="printf step"):
        return self.call("exec", instance=self.instance, session_id=1, lease_id=lease_id,
                         request_id=request_id, script=script)

    def finish(self, peer):
        token = self.token(peer)
        peer.sendall(token + b"start\x1f" + token + b"end:0\x1f")

    def test_cli_two_steps_keep_exclusivity_between_commands_and_release_to_human(self):
        self.bash_peer()
        scope = ("--instance", self.instance, "--session", "1", "--lease-id", OWNER)
        code, acquired = self.cli("acquire", *scope, "--task", "two step task", "--ttl", "60")
        self.assertEqual(code, 0, acquired)
        for index, script in enumerate(("printf 'verified\\n' > task.txt", "cat task.txt")):
            self.assertEqual(self.execute(request_id="intruder")["error"]["code"], "session_busy")
            code, result = self.cli("exec", *scope, "--request-id", f"step-{index}",
                                    "--command", script, "--wait", "3")
            self.assertEqual(code, 0, result)
        self.assertEqual(result["run"]["output"], "verified\n")
        code, renewed = self.cli("renew", *scope, "--ttl", "120")
        self.assertEqual(code, 0, renewed)
        self.assertGreater(renewed["lease"]["expires_in_seconds"], 110)
        code, released = self.cli("release", *scope)
        self.assertEqual(code, 0, released)
        self.assertTrue(released["released"])
        self.console.attach(1)
        self.console.type(b"printf 'BACK-TO-HUMAN\\n'\r")
        self.console.read_until(b"BACK-TO-HUMAN")

    def test_simultaneous_owners_cannot_both_acquire_and_list_does_not_expose_id(self):
        peer = self.peer()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda token: self.lease(lease_id=token), (OWNER, OTHER)))
        self.assertEqual(sum(r["ok"] for r in results), 1, results)
        listing = self.call("sessions")
        self.assertEqual(listing["sessions"][0]["busy_reason"], "session_leased")
        self.assertNotIn("lease_id", listing["sessions"][0]["lease"])
        self.assertNotIn(OWNER, json.dumps(listing))
        self.assertFalse(select.select([peer], [], [], 0.1)[0])

    def test_manual_batch_and_line_and_raw_input_cannot_bypass_lease(self):
        peer = self.peer()
        self.assertTrue(self.lease()["ok"])
        script = self.root / "manual.sh"
        script.write_text("printf unexpected")
        self.console.command("batch " + shlex.quote(str(script)))
        self.console.read_until(b"skipped")
        self.assertFalse(select.select([peer], [], [], 0.1)[0])
        for raw in (False, True):
            self.console.attach(1, raw=raw)
            self.console.type(b"printf should-not-send\r")
            self.console.read_until(b"input was not queued")
            self.assertFalse(select.select([peer], [], [], 0.1)[0])
            # A person may watch output without blocking the owning task's next step.
            result = self.owned_exec(request_id="while-viewing-" + str(raw))
            self.assertTrue(result["ok"], result)
            self.finish(peer)
            self.result(result["run"]["run_id"])
            self.console.detach()
        self.console.command("ls")
        shown = self.console.read_until(b"shelldeck> ")
        self.assertIn(b"Agent lease: agent task", shown)
        self.assertNotIn(OWNER.encode(), shown)

    def test_pending_command_blocks_release_and_takeover_even_after_expiry(self):
        peer = self.peer()
        self.lease(ttl_seconds=1)
        pending = self.owned_exec()
        self.assertTrue(pending["ok"], pending)
        self.assertEqual(self.lease("release")["error"]["code"], "session_busy")
        time.sleep(1.1)
        self.assertIsNone(self.call("sessions")["sessions"][0]["lease"])
        self.assertEqual(self.lease("renew")["error"]["code"], "lease_inactive")
        self.assertEqual(self.lease(lease_id=OTHER)["error"]["code"], "session_busy")
        self.assertEqual(self.execute()["error"]["code"], "session_busy")
        self.console.attach(1)
        self.console.type(b"not-yet\r")
        self.console.read_until(b"Batch in progress")
        self.console.detach()
        self.finish(peer)
        self.result()
        self.assertTrue(self.lease(lease_id=OTHER)["ok"])
        self.assertEqual(self.owned_exec(request_id="old-owner")["error"]["code"], "lease_mismatch")

    def test_acquire_retry_recovers_lost_reply_without_extending_or_reviving_lease(self):
        self.peer()
        payload = {"protocol_version": 1, "method": "acquire", "instance": self.instance,
                   "session_id": 1, "lease_id": OWNER, "ttl_seconds": 1}
        with socket.socket(socket.AF_UNIX) as control:
            control.connect(str(self.socket_path))
            control.sendall(json.dumps(payload).encode() + b"\n")
        self.console.read_until(b"reserved for")
        time.sleep(0.25)
        retry = self.lease(ttl_seconds=1)
        self.assertTrue(retry["replayed"], retry)
        self.assertEqual(retry["lease"]["lease_id"], OWNER)
        self.assertLess(retry["lease"]["expires_in_seconds"], 0.9)
        time.sleep(0.85)
        self.assertEqual(self.lease(ttl_seconds=1)["error"]["code"], "lease_inactive")
        self.assertTrue(self.lease(lease_id=OTHER)["ok"])

    def test_release_retry_never_releases_new_owner_and_old_id_cannot_reacquire(self):
        self.peer()
        self.lease()
        self.assertTrue(self.lease("release")["released"])
        self.assertEqual(self.lease()["error"]["code"], "lease_inactive")
        self.lease(lease_id=OTHER)
        self.assertFalse(self.lease("release")["released"])
        self.assertEqual(self.owned_exec()["error"]["code"], "lease_mismatch")
        self.assertTrue(self.lease("renew", OTHER)["ok"])

    def test_renew_during_execution_and_replay_after_release_never_resends(self):
        peer = self.peer()
        self.lease()
        submitted = self.owned_exec()
        self.assertTrue(self.lease("renew", ttl_seconds=120)["ok"])
        self.finish(peer)
        self.result()
        self.lease("release")
        replay = self.owned_exec()
        self.assertTrue(replay["replayed"], replay)
        self.assertEqual(replay["run"]["run_id"], submitted["run"]["run_id"])
        self.assertFalse(select.select([peer], [], [], 0.1)[0])

    def test_request_id_from_previous_task_cannot_be_mistaken_for_a_new_step(self):
        peer = self.peer()
        self.lease()
        self.owned_exec()
        self.finish(peer)
        self.result()
        self.lease("release")
        self.lease(lease_id=OTHER)
        conflict = self.owned_exec(lease_id=OTHER)
        self.assertFalse(conflict["ok"], conflict)
        self.assertEqual(conflict["error"]["code"], "request_conflict")
        self.assertFalse(select.select([peer], [], [], 0.1)[0])

    def test_acquisition_failure_does_not_reserve_session_and_token_is_not_stored(self):
        self.peer()
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            db.execute("CREATE TRIGGER reject_lease BEFORE INSERT ON agent_leases "
                       "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            self.assertEqual(self.lease()["error"]["code"], "history_unavailable")
            self.assertIsNone(self.call("sessions")["sessions"][0]["lease"])
            db.execute("DROP TRIGGER reject_lease")
        self.assertTrue(self.lease()["ok"])
        with sqlite3.connect(self.root / "shelldeck" / "history.db") as db:
            saved = db.execute("SELECT * FROM agent_leases").fetchall()
        self.assertNotIn(OWNER, repr(saved))

    def test_human_attachment_disconnect_and_restart_prevent_acquisition_or_renewal(self):
        peer = self.peer()
        self.console.attach(1)
        self.assertEqual(self.lease()["error"]["code"], "session_busy")
        self.console.detach()
        self.lease()
        peer.close()
        self.console.read_until(b"disconnected")
        self.assertIsNone(self.call("sessions")["sessions"][0]["lease"])
        self.assertEqual(self.lease("renew")["error"]["code"], "session_unavailable")
        old_instance = self.instance
        self.console.stop(signal.SIGTERM)
        self.start()
        self.peer()
        stale = self.call("acquire", session_id=1, instance=old_instance, lease_id=OWNER)
        self.assertEqual(stale["error"]["code"], "stale_instance")
        self.assertEqual(self.lease()["error"]["code"], "lease_conflict")
        self.assertTrue(self.lease(lease_id=OTHER)["ok"])

    def test_bad_lease_parameters_are_rejected_without_taking_ownership(self):
        self.peer()
        for fields in ({"ttl_seconds": True}, {"ttl_seconds": 0}, {"ttl_seconds": 3601},
                       {"ttl_seconds": 1.5}, {"task": "bad\nlabel"},
                       {"task": ""}, {"lease_id": "not-a-token"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.lease(**fields)["error"]["code"], "invalid_request")
        self.assertIsNone(self.call("sessions")["sessions"][0]["lease"])
