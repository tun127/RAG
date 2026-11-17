from fastapi import APIRouter, Depends, HTTPException

from production_rag.api.dependencies import services

router = APIRouter(tags=["documents / jobs"])


@router.get("/documents")
def documents(svc=Depends(services)):
    if not svc.store.active_id():
        return {"snapshot_id": None, "documents": []}
    snapshot = svc.store.snapshot()
    return {
        "snapshot_id": snapshot.snapshot_id,
        "documents": [d.model_dump(exclude={"text"}) for d in snapshot.documents],
    }


@router.post("/documents/sync", status_code=202)
def sync(svc=Depends(services)):
    return {"job_id": svc.store.enqueue("ingest", {})}


@router.delete("/documents/{document_id}", status_code=202)
def delete_document(document_id: str, svc=Depends(services)):
    return {"job_id": svc.store.enqueue("delete_document", {"document_id": document_id})}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, svc=Depends(services)):
    job = svc.store.job(job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    return {k: v for k, v in job.items() if k not in {"lease_owner"}}
