"""Local execution metadata and bounded remote output; no separate script-source field."""

from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


OUTPUT_LIMIT = 65536
PAGE_SIZE = 20
OUTPUT_PAGE_SIZE = 4096


class HistoryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.instance = secrets.token_hex(16)
        self.memory = sqlite3.connect(":memory:") if path is None else None
        self.error = ""
        try:
            if self.path is not None:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            self.initialize()
        except (OSError, sqlite3.Error) as error:
            self.error = str(error)

    @contextmanager
    def connection(self):
        if self.path is None:
            self.memory.row_factory = sqlite3.Row
            yield self.memory
        else:
            uri = self.path.absolute().as_uri() + "?mode=rw"
            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as db:
                db.row_factory = sqlite3.Row
                yield db

    def initialize(self):
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS batch_jobs ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, submitted_at REAL NOT NULL, "
                       "script_path TEXT NOT NULL, script_name TEXT NOT NULL, sha256 TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS script_runs ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, "
                       "instance TEXT NOT NULL, session_id INTEGER NOT NULL, ip TEXT NOT NULL, "
                       "peer TEXT NOT NULL, connected_at REAL NOT NULL, state TEXT NOT NULL, "
                       "exit_code INTEGER, received_at REAL, started_at REAL, finished_at REAL, "
                       "output BLOB NOT NULL DEFAULT X'', dropped INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_ip ON script_runs(ip, id)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_session ON script_runs(instance, session_id, id)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_job ON script_runs(job_id)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_script_name ON batch_jobs(script_name)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_script_path ON batch_jobs(script_path)")
            db.execute("CREATE TABLE IF NOT EXISTS agent_requests ("
                       "request_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE, lease_key TEXT)")
            if "lease_key" not in {row["name"] for row in db.execute("PRAGMA table_info(agent_requests)")}:
                db.execute("ALTER TABLE agent_requests ADD COLUMN lease_key TEXT")
            # Remember consumed lease IDs so delayed acquisition retries cannot revive
            # a released/expired lease. The bearer ID itself is not stored on disk.
            db.execute("CREATE TABLE IF NOT EXISTS agent_leases ("
                       "key TEXT PRIMARY KEY, instance TEXT NOT NULL, session_id INTEGER NOT NULL, "
                       "task TEXT NOT NULL, ttl_seconds INTEGER NOT NULL, created_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS file_transfers ("
                       "transfer_id TEXT PRIMARY KEY, instance TEXT NOT NULL, "
                       "parameters TEXT NOT NULL, result TEXT NOT NULL)")
        self.error = ""

    def ready(self):
        if self.error:
            self.initialize()

    def create_batch(self, path, digest, entries, request_id=None, lease_id=None):
        if request_id is not None and len(entries) != 1:
            raise ValueError("an agent request must target exactly one session")
        self.ready()
        submitted = time.time()
        ids = []
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            job_id = db.execute("INSERT INTO batch_jobs (submitted_at, script_path, script_name, sha256) "
                                "VALUES (?, ?, ?, ?)",
                                (submitted, str(path), path.name, digest)).lastrowid
            for session, result in entries:
                finished = submitted if result.state == "skipped-busy" else None
                cursor = db.execute("INSERT INTO script_runs "
                                    "(job_id, instance, session_id, ip, peer, connected_at, state, finished_at) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (job_id, self.instance, session.id, session.ip, session.peer,
                                     session.connected_at, result.state, finished))
                ids.append((result, cursor.lastrowid, finished))
            if request_id is not None:
                db.execute("INSERT INTO agent_requests (request_id, run_id, lease_key) VALUES (?, ?, ?)",
                           (request_id, ids[0][1], self.lease_key(lease_id)))
        # Assign IDs only after the entire batch has been committed.
        for result, record_id, finished in ids:
            result.job_id, result.record_id, result.finished_at = job_id, record_id, finished
        return job_id

    def update(self, results, transfers=()):
        self.ready()
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            for result in results:
                cursor = db.execute("UPDATE script_runs SET state=?, exit_code=?, received_at=?, started_at=?, "
                                    "finished_at=?, output=?, dropped=? WHERE id=? AND instance=?",
                                    (result.state, result.exit_code, result.received_at, result.started_at,
                                     result.finished_at, b"".join(result.log.parts), result.log.dropped,
                                     result.record_id, self.instance))
                if cursor.rowcount != 1:
                    raise sqlite3.DatabaseError(f"Execution record {result.record_id} is missing or belongs to another console")
            for transfer in transfers:
                cursor = db.execute("UPDATE file_transfers SET result=? WHERE transfer_id=? AND instance=?",
                                    (json.dumps(transfer.info(), ensure_ascii=True), transfer.transfer_id, self.instance))
                if cursor.rowcount != 1:
                    raise sqlite3.DatabaseError(f"Transfer {transfer.transfer_id} is missing or belongs to another console")

    def create_transfer(self, parameters, transfer):
        self.ready()
        with self.connection() as db, db:
            db.execute("INSERT INTO file_transfers VALUES (?, ?, ?, ?)",
                       (transfer.transfer_id, self.instance, json.dumps(parameters, sort_keys=True),
                        json.dumps(transfer.info(), ensure_ascii=True)))

    def get_transfer(self, transfer_id):
        self.ready()
        with self.connection() as db:
            row = db.execute("SELECT parameters, result FROM file_transfers WHERE transfer_id=?",
                             (transfer_id,)).fetchone()
        if row is None:
            return None
        return {"parameters": json.loads(row["parameters"]), "result": json.loads(row["result"])}

    def filters(self, session=None, ip=None, script=None):
        clauses, values = [], []
        if session is not None:
            clauses.extend(("r.instance=?", "r.session_id=?"))
            values.extend((self.instance, session))
        if ip is not None:
            clauses.append("r.ip=?")
            values.append(ip)
        if script is not None:
            if "/" in script:
                clauses.append("j.script_path=?")
                values.append(str(Path(script).expanduser().absolute()))
            else:
                clauses.append("j.script_name=?")
                values.append(script)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), values

    def query(self, session=None, ip=None, script=None, page=1):
        self.ready()
        where, values = self.filters(session, ip, script)
        source = " FROM script_runs r JOIN batch_jobs j ON j.id=r.job_id"
        with self.connection() as db:
            count = db.execute("SELECT COUNT(*)" + source + where, values).fetchone()[0]
            rows = db.execute("SELECT r.id, r.job_id, r.instance, r.session_id, r.ip, r.started_at, "
                              "r.state, r.exit_code, j.script_path, j.script_name, j.sha256, j.submitted_at" +
                              source + where + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
                              values + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
            condition = " AND " if where else " WHERE "
            started = {row[0] for row in db.execute(
                "SELECT DISTINCT r.session_id" + source + where + condition +
                "r.instance=? AND r.started_at IS NOT NULL", values + [self.instance])}
        return rows, count, started

    def get(self, record_id):
        self.ready()
        with self.connection() as db:
            return db.execute("SELECT r.*, j.script_path, j.script_name, j.sha256, j.submitted_at "
                              "FROM script_runs r JOIN batch_jobs j ON j.id=r.job_id WHERE r.id=?",
                              (record_id,)).fetchone()

    def get_request(self, request_id):
        self.ready()
        with self.connection() as db:
            row = db.execute("SELECT run_id, lease_key FROM agent_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        run = self.get(row["run_id"])
        return {**dict(run), "lease_key": row["lease_key"]} if run is not None else None

    @staticmethod
    def lease_key(lease_id):
        return hashlib.sha256(lease_id.encode("ascii")).hexdigest() if lease_id is not None else None

    def get_lease(self, lease_id):
        self.ready()
        with self.connection() as db:
            return db.execute("SELECT * FROM agent_leases WHERE key=?",
                              (self.lease_key(lease_id),)).fetchone()

    def create_lease(self, lease_id, session_id, task, ttl_seconds):
        self.ready()
        with self.connection() as db, db:
            db.execute("INSERT INTO agent_leases VALUES (?, ?, ?, ?, ?, ?)",
                       (self.lease_key(lease_id), self.instance,
                        session_id, task, ttl_seconds, time.time()))

    def close(self):
        if self.memory is not None:
            self.memory.close()
