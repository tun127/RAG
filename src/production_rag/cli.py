"""CLI 与 API 共享应用服务，不复制业务逻辑。"""

import argparse
import json
import logging
from pathlib import Path

from production_rag.bootstrap import build_services
from production_rag.domain.errors import RagError


def main():
    parser = argparse.ArgumentParser(prog="rag")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ingest", help="同步配置的文档目录，发布快照")
    commands.add_parser("status", help="查看活动知识库")
    delete = commands.add_parser("delete-document")
    delete.add_argument("document_id")
    query = commands.add_parser("ask")
    query.add_argument("question")
    query.add_argument("--mode", choices=["baseline", "agent"], default="agent")
    query.add_argument("--retrieval", choices=["hybrid", "vector", "keyword"], default="hybrid")
    query.add_argument("--reranking", action="store_true")
    query.add_argument("--user", default="demo")
    query.add_argument("--session", default="default")
    query.add_argument("--remember-episode", action="store_true")
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--dataset", type=Path, default=Path("evals/dataset.jsonl"))
    evaluation.add_argument("--mode", choices=["baseline", "agent", "both"], default="both")
    evaluation.add_argument("--judge", action="store_true")
    server = commands.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    commands.add_parser("worker")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "serve":
        import uvicorn

        uvicorn.run("production_rag.api.main:app", host=args.host, port=args.port)
        return
    svc = build_services()
    try:
        match args.command:
            case "ingest":
                result = svc.ingest.sync()
            case "status":
                snapshot = svc.store.snapshot()
                result = {
                    "snapshot_id": snapshot.snapshot_id,
                    "config": snapshot.config,
                    "documents": [d.model_dump(exclude={"text"}) for d in snapshot.documents],
                    "chunks": len(snapshot.chunks),
                }
            case "delete-document":
                result = svc.ingest.delete(args.document_id)
            case "ask":
                result = svc.query.ask(
                    args.question,
                    mode=args.mode,
                    retrieval=args.retrieval,
                    reranking=args.reranking,
                    user_id=args.user,
                    session_id=args.session,
                    remember_episode=args.remember_episode,
                ).model_dump()
            case "evaluate":
                from production_rag.evaluation.runner import run_evaluation

                result = run_evaluation(
                    svc,
                    args.dataset,
                    ("baseline", "agent") if args.mode == "both" else (args.mode,),
                    judge=args.judge,
                )
            case "worker":
                from production_rag.workers.main import serve_worker

                serve_worker(svc)
                return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RagError, ValueError, OSError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
