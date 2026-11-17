import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256

from production_rag.contracts.alignment import propose, units


class StaleAlignment(Exception):
    pass


class AlignmentStore:
    def __init__(self, contracts):
        self.contracts = contracts

    def context(self, task, pair):
        if pair.standard_version == pair.supplier_version:
            raise ValueError("请明确选择两个不同版本的标准合同与供应商合同")
        a = self.contracts.get(task, pair.standard_version)
        b = self.contracts.get(task, pair.supplier_version)
        for record in (a, b):
            self.contracts.original(record)
            if record.result.status not in ("parsed", "partial"):
                raise ValueError("仅可核对已解析或部分覆盖的版本")
        return a, b

    def _db(self, task):
        folder = self.contracts._path(task)
        if not folder.is_dir():
            raise FileNotFoundError("任务不存在")
        connection = sqlite3.connect(folder / "alignment.sqlite", timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS events (pair TEXT, revision INTEGER, payload TEXT, PRIMARY KEY(pair,revision))"
        )
        return connection

    @staticmethod
    def key(pair):
        return sha256(pair.model_dump_json().encode()).hexdigest()

    def read(self, task, pair):
        a, b = self.context(task, pair)
        with closing(self._db(task)) as db, db:
            history = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM events WHERE pair=? ORDER BY revision", (self.key(pair),)
                )
            ]
        effective = []
        for event in history:
            effective = [
                e
                for e in effective
                if not set(e["standard_anchors"]) & set(event["standard_anchors"])
            ]
            effective.append(event)
        return {
            "task_id": task,
            **pair.model_dump(),
            "revision": len(history),
            "history": history,
            "decisions": effective,
            "proposals": propose(a, b),
            "standard": a,
            "supplier": b,
        }

    def save(self, task, pair, decision):
        a, b = self.context(task, pair)
        for anchors, record in ((decision.standard_anchors, a), (decision.supplier_anchors, b)):
            if len(anchors) != len(set(anchors)) or not set(anchors).issubset(units(record)):
                raise ValueError("锚点重复或不属于所选角色版本")
        if decision.action in ("confirm", "correct") and not decision.supplier_anchors:
            raise ValueError("确认或更正必须选择供应商块")
        if decision.action == "confirm":
            matches = {
                p["standard_anchor"]: [c["supplier_anchor"] for c in p["candidates"]]
                for p in propose(a, b)
            }
            if (
                len(decision.standard_anchors) != 1
                or len(decision.supplier_anchors) != 1
                or decision.supplier_anchors[0] not in matches[decision.standard_anchors[0]]
            ):
                raise ValueError("确认仅适用于单个已有候选；其他对应请使用更正")
        with closing(self._db(task)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute(
                "SELECT COUNT(*) FROM events WHERE pair=?", (self.key(pair),)
            ).fetchone()[0]
            if revision != decision.revision:
                raise StaleAlignment("页面已过期，请刷新后重新核对，未覆盖其他操作")
            event = {
                "candidate_snapshot": [
                    p for p in propose(a, b) if p["standard_anchor"] in decision.standard_anchors
                ],
                "candidate_method": "text-number-heading-neighbor-position-v1",
                **decision.model_dump(),
                "revision": revision + 1,
                "at": datetime.now(UTC).isoformat(),
                "task_id": task,
                **pair.model_dump(),
            }
            db.execute(
                "INSERT INTO events VALUES (?,?,?)",
                (self.key(pair), revision + 1, json.dumps(event, ensure_ascii=False)),
            )
        return self.read(task, pair)
