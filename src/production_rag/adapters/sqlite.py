"""SQLite 管元数据、活动快照、记忆、轨迹和持久化作业；每次操作独立连接。"""

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from production_rag.domain.errors import NoKnowledgeBase, PublishConflict
from production_rag.domain.models import Chunk, Document, MemoryItem


@dataclass
class Snapshot:
    snapshot_id: str
    config: dict
    documents: list[Document]
    chunks: list[Chunk]
    vectors: list[list[float]]


class SqliteStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(
                    id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS documents(
                    snapshot TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(snapshot,id));
                CREATE TABLE IF NOT EXISTS chunks(
                    snapshot TEXT, id TEXT, payload TEXT NOT NULL, vector TEXT NOT NULL,
                    PRIMARY KEY(snapshot,id));
                CREATE TABLE IF NOT EXISTS traces(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL);
                CREATE TABLE IF NOT EXISTS memories(
                    user_id TEXT, kind TEXT, key TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(user_id,kind,key));
                CREATE TABLE IF NOT EXISTS turns(
                    user_id TEXT, session_id TEXT, question TEXT, answer TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS turns_session ON turns(user_id,session_id,created);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, lease_owner TEXT, lease_until REAL,
                    result TEXT, error TEXT, created REAL NOT NULL, attempts INTEGER DEFAULT 0);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def active_id(self) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='active'").fetchone()
        return row[0] if row else None

    def snapshot(self, snapshot_id: str | None = None) -> Snapshot:
        snapshot_id = snapshot_id or self.active_id()
        if not snapshot_id:
            raise NoKnowledgeBase("尚未导入知识库，请先运行 ingest")
        with self.connect() as db:
            row = db.execute(
                "SELECT config FROM snapshots WHERE id=? AND status='ready'", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise NoKnowledgeBase("指定快照不存在或尚未发布")
            documents = [
                Document.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT payload FROM documents WHERE snapshot=? ORDER BY id", (snapshot_id,)
                )
            ]
            rows = db.execute(
                "SELECT payload,vector FROM chunks WHERE snapshot=? ORDER BY id", (snapshot_id,)
            ).fetchall()
        return Snapshot(
            snapshot_id,
            json.loads(row[0]),
            documents,
            [Chunk.model_validate_json(r[0]) for r in rows],
            [json.loads(r[1]) for r in rows],
        )

    def stage(self, snapshot: Snapshot):
        with self.connect() as db:
            db.execute(
                "INSERT INTO snapshots(id,config,status,created) VALUES(?,?,'building',?)",
                (snapshot.snapshot_id, json.dumps(snapshot.config), time.time()),
            )
            db.executemany(
                "INSERT INTO documents VALUES(?,?,?)",
                [
                    (snapshot.snapshot_id, d.document_id, d.model_dump_json())
                    for d in snapshot.documents
                ],
            )
            db.executemany(
                "INSERT INTO chunks VALUES(?,?,?,?)",
                [
                    (snapshot.snapshot_id, c.chunk_id, c.model_dump_json(), json.dumps(v))
                    for c, v in zip(snapshot.chunks, snapshot.vectors, strict=True)
                ],
            )

    def publish(self, snapshot_id: str, expected_active: str | None, job_context=None):
        # 比较并交换：并行导入不能用过期的基础版本覆盖别人刚发布的版本。
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if job_context:
                job_id, owner = job_context
                lease = db.execute(
                    "SELECT 1 FROM jobs WHERE id=? AND lease_owner=? "
                    "AND state='running' AND lease_until>?",
                    (job_id, owner, time.time()),
                ).fetchone()
                if lease is None:
                    raise PublishConflict("作业租约已失效，禁止发布过期 worker 的快照")
            row = db.execute("SELECT value FROM settings WHERE key='active'").fetchone()
            if (row[0] if row else None) != expected_active:
                raise PublishConflict("导入期间知识库发生变化，请重新运行导入")
            changed = db.execute(
                "UPDATE snapshots SET status='ready' WHERE id=? AND status='building'",
                (snapshot_id,),
            ).rowcount
            if changed != 1:
                raise PublishConflict("只有构建中的快照可以发布")
            db.execute("INSERT OR REPLACE INTO settings VALUES('active',?)", (snapshot_id,))

    def fail_snapshot(self, snapshot_id: str, error: str):
        with self.connect() as db:
            db.execute(
                "UPDATE snapshots SET status='failed',error=? WHERE id=? AND status='building'",
                (error[:1000], snapshot_id),
            )

    def save_trace(self, user_id: str, result):
        with self.connect() as db:
            db.execute(
                "INSERT INTO traces VALUES(?,?,?,?)",
                (result.answer.request_id, user_id, result.model_dump_json(), time.time()),
            )

    def get_trace(self, user_id: str, request_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM traces WHERE id=? AND user_id=?", (request_id, user_id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put_memory(self, item: MemoryItem):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO memories VALUES(?,?,?,?)",
                (item.user_id, item.kind, item.key, item.model_dump_json()),
            )

    def memories(self, user_id: str) -> list[MemoryItem]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM memories WHERE user_id=?", (user_id,)).fetchall()
        items = [MemoryItem.model_validate_json(r[0]) for r in rows]
        return sorted(
            (m for m in items if m.expires_at is None or m.expires_at > time.time()),
            key=lambda m: m.updated_at,
            reverse=True,
        )

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        with self.connect() as db:
            # id 不脱离用户作用域执行。
            for row in db.execute(
                "SELECT kind,key,payload FROM memories WHERE user_id=?", (user_id,)
            ):
                if json.loads(row[2])["memory_id"] == memory_id:
                    db.execute(
                        "DELETE FROM memories WHERE user_id=? AND kind=? AND key=?",
                        (user_id, row[0], row[1]),
                    )
                    return True
        return False

    def enqueue(self, kind: str, payload: dict) -> str:
        job_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,kind,payload,state,created) VALUES(?,?,?,'pending',?)",
                (job_id, kind, json.dumps(payload), time.time()),
            )
        return job_id

    def save_turn(self, user_id, session_id, question, answer):
        with self.connect() as db:
            db.execute(
                "INSERT INTO turns VALUES(?,?,?,?,?)",
                (user_id, session_id, question, answer, time.time()),
            )
            db.execute(
                "DELETE FROM turns WHERE user_id=? AND session_id=? AND rowid NOT IN "
                "(SELECT rowid FROM turns WHERE user_id=? AND session_id=? "
                "ORDER BY created DESC LIMIT 8)",
                (user_id, session_id, user_id, session_id),
            )

    def recent_turns(self, user_id, session_id):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT question,answer FROM turns WHERE user_id=? AND session_id=? "
                    "ORDER BY created DESC LIMIT 3",
                    (user_id, session_id),
                )
            ][::-1]

    def job(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def claim(self, lease_seconds: float = 120) -> dict | None:
        now, owner = time.time(), uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE state='pending' OR "
                "(state='running' AND lease_until<?) ORDER BY created LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET state='running',lease_owner=?,lease_until=?,attempts=attempts+1 "
                "WHERE id=?",
                (owner, now + lease_seconds, row[0]),
            )
        return self.job(row[0])

    def renew(self, job_id: str, owner: str, seconds: float = 120) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=? AND lease_owner=? "
                    "AND state='running' AND lease_until>?",
                    (time.time() + seconds, job_id, owner, time.time()),
                ).rowcount
                == 1
            )

    def finish(self, job_id: str, owner: str, *, result=None, error=None) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE jobs SET state=?,result=?,error=? WHERE id=? "
                    "AND lease_owner=? AND state='running' AND lease_until>?",
                    (
                        "failed" if error else "completed",
                        json.dumps(result),
                        error,
                        job_id,
                        owner,
                        time.time(),
                    ),
                ).rowcount
                == 1
            )
