from fastapi import APIRouter, Depends, HTTPException

from production_rag.api.dependencies import services, user_id
from production_rag.api.schemas import MemoryRequest

router = APIRouter(tags=["memory"])


@router.get("/memory")
def list_memory(svc=Depends(services), user=Depends(user_id)):
    return svc.store.memories(user)


@router.put("/memory")
def put_memory(body: MemoryRequest, svc=Depends(services), user=Depends(user_id)):
    return svc.memory.put(user_id=user, **body.model_dump())


@router.delete("/memory/{memory_id}", status_code=204)
def delete_memory(memory_id: str, svc=Depends(services), user=Depends(user_id)):
    if not svc.store.delete_memory(user, memory_id):
        raise HTTPException(404, "记忆不存在")
