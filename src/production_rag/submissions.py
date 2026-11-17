"""Persistent form tickets; SQLite atomic claim prevents duplicate model execution."""

import json
import sqlite3
import uuid
from contextlib import contextmanager

from fastapi import HTTPException

from production_rag.review_inputs.models import digest


class Submissions:
    def __init__(self, runtime):
        runtime.mkdir(parents=True, exist_ok=True)
        self.path = runtime / "submissions.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, task TEXT, version TEXT, fingerprint TEXT, run_id TEXT)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue(self, task, version):
        ticket = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO submissions(id,task,version) VALUES(?,?,?)", (ticket, task, version)
            )
        return ticket

    def reserve(self, ticket, task, version):
        """按页面票据预留：同一票据重放复用同一运行，新页面票据视为新操作。"""
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO submissions(id,task,version) VALUES(?,?,?)",
                (ticket, task, version),
            )

    def lookup(self, ticket):
        with self.connect() as db:
            row = db.execute(
                "SELECT task,version,run_id FROM submissions WHERE id=?", (ticket,)
            ).fetchone()
        if row is None:
            return None
        return {"task": row[0], "version": row[1], "run_id": row[2]}

    def check(self, ticket, task, version, fingerprint):
        with self.connect() as db:
            row = db.execute(
                "SELECT task,version,fingerprint,run_id FROM submissions WHERE id=?", (ticket,)
            ).fetchone()
        if row is None or row[:2] != (task, version):
            raise HTTPException(400, "提交编号无效，请重新打开版本选择页")
        if row[2] is not None and row[2] != fingerprint:
            raise HTTPException(409, "同一提交编号的内容已冻结；修改材料或模式请打开新表单")
        return row[3]

    def claim(self, ticket, task, version, fingerprint, run_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT task,version,fingerprint,run_id FROM submissions WHERE id=?", (ticket,)
            ).fetchone()
            if row is None or row[:2] != (task, version):
                raise HTTPException(400, "提交编号无效")
            if row[2] is not None and row[2] != fingerprint:
                raise HTTPException(409, "该提交内容已冻结")
            if row[3]:
                return row[3]
            db.execute(
                "UPDATE submissions SET fingerprint=?,run_id=? WHERE id=?",
                (fingerprint, run_id, ticket),
            )
        return None


async def fingerprint(form, config):
    values = []
    for key, value in form.multi_items():
        if key in ("token", "submission_id"):
            continue
        if hasattr(value, "read"):
            raw = await value.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise HTTPException(413, "附件超过512KiB")
            await value.seek(0)
            value = {"filename": value.filename, "sha256": digest(raw)}
        values.append((key, value))
    values.sort(key=lambda item: item[0])
    return digest(
        json.dumps(
            {"form": values, "config": config.model_dump(mode="json")},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    )
