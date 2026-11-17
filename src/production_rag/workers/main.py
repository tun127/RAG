"""SQLite 持久化队列：租约 + 心跳；执行语义为至少一次，发布操作有 CAS 保护。"""

import logging
from pathlib import Path
from threading import Event, Thread

logger = logging.getLogger(__name__)


def execute(services, job):
    context = (job["id"], job["lease_owner"])
    if job["kind"] == "ingest":
        return services.ingest.sync(job_context=context)
    if job["kind"] == "delete_document":
        return services.ingest.delete(job["payload"]["document_id"], job_context=context)
    if job["kind"] == "evaluate":
        from production_rag.evaluation.runner import run_evaluation

        # API 不接收任意文件路径，固定评估集由部署者管理。
        return run_evaluation(
            services,
            Path("evals/dataset.jsonl"),
            tuple(job["payload"].get("modes", ["baseline", "agent"])),
            judge=job["payload"].get("judge", False),
        )
    raise ValueError("未知作业类型")


def run_once(services) -> bool:
    job = services.store.claim()
    if job is None:
        return False
    stop = Event()

    def heartbeat():
        while not stop.wait(20):
            if not services.store.renew(job["id"], job["lease_owner"]):
                logger.error("job lease lost: %s", job["id"])
                return

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = execute(services, job)
        services.store.finish(job["id"], job["lease_owner"], result=result)
    except Exception as exc:
        logger.exception("job failed: %s", job["id"])
        services.store.finish(job["id"], job["lease_owner"], error=str(exc)[:1000])
    finally:
        stop.set()
        thread.join(timeout=1)
    return True


def serve_worker(services):
    wait = Event()
    try:
        while True:
            if not run_once(services):
                wait.wait(1)
    except KeyboardInterrupt:
        pass
