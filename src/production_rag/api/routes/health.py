import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from production_rag.api.dependencies import services

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live():
    return {"status": "alive"}


@router.get("/health/ready")
def ready(svc=Depends(services)):
    checks = {"knowledge_base": bool(svc.store.active_id())}
    try:
        response = httpx.get(svc.settings.ollama_url + "/api/tags", timeout=3)
        response.raise_for_status()
        models = {m["name"] for m in response.json()["models"]}
        checks["models"] = {svc.settings.chat_model, svc.settings.embedding_model} <= models
    except Exception:
        checks["models"] = False
    if svc.settings.vector_backend == "milvus":
        try:
            from pymilvus import MilvusClient

            client = MilvusClient(
                uri=svc.settings.milvus_uri, token=svc.settings.milvus_token, timeout=3
            )
            try:
                active = svc.store.snapshot()
                checks["milvus"] = client.has_collection(active.config["collection"], timeout=3)
            finally:
                client.close()
        except Exception:
            checks["milvus"] = False
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
