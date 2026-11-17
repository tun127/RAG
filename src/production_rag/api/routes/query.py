from fastapi import APIRouter, Depends, HTTPException

from production_rag.api.dependencies import services, user_id
from production_rag.api.schemas import QueryRequest
from production_rag.domain.models import QueryResult

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResult)
def query(body: QueryRequest, svc=Depends(services), user=Depends(user_id)):
    return svc.query.ask(user_id=user, **body.model_dump())


@router.get("/traces/{request_id}")
def trace(request_id: str, svc=Depends(services), user=Depends(user_id)):
    result = svc.store.get_trace(user, request_id)
    if result is None:
        raise HTTPException(404, "轨迹不存在")
    return result
