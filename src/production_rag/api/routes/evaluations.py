import json

from fastapi import APIRouter, Depends, HTTPException

from production_rag.api.dependencies import services
from production_rag.api.schemas import EvalRequest

router = APIRouter(tags=["evaluation"])


@router.post("/evaluations", status_code=202)
def evaluate(body: EvalRequest, svc=Depends(services)):
    return {"job_id": svc.store.enqueue("evaluate", body.model_dump())}


@router.get("/evaluations")
def reports(svc=Depends(services)):
    paths = sorted(
        (svc.settings.data_dir / "reports").glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:30]
    return [
        {"run_id": p.stem, "summary": json.loads(p.read_text(encoding="utf-8"))["summary"]}
        for p in paths
    ]


@router.get("/evaluations/{run_id}")
def report(run_id: str, svc=Depends(services)):
    if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise HTTPException(404, "评估不存在")
    path = svc.settings.data_dir / "reports" / f"{run_id}.json"
    if not path.is_file():
        raise HTTPException(404, "评估不存在")
    return json.loads(path.read_text(encoding="utf-8"))
