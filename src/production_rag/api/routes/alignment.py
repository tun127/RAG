from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from production_rag.api.routes.contracts import service, templates
from production_rag.contracts.alignment import Decision, Pair, text, units
from production_rag.contracts.alignment_store import AlignmentStore, StaleAlignment
from production_rag.contracts.diff import differences

router = APIRouter()


def diff_data(request, task, standard_version, supplier_version, revision):
    pair = Pair(standard_version=standard_version, supplier_version=supplier_version)
    try:
        result = differences(operate(request, task, pair), revision)
        if operate(request, task, pair)["revision"] != revision:
            raise StaleAlignment("生成期间人工对应已变化，请刷新")
        return result
    except StaleAlignment as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/api/contracts/{task}/diff")
def api_diff(
    request: Request, task: str, standard_version: str, supplier_version: str, revision: int
):
    return diff_data(request, task, standard_version, supplier_version, revision)


@router.get("/contracts/{task}/diff")
def web_diff(
    request: Request, task: str, standard_version: str, supplier_version: str, revision: int
):
    data = diff_data(request, task, standard_version, supplier_version, revision)
    return templates.TemplateResponse(
        request=request, name="diff.html", context={"data": data, "units": units}
    )


def operate(request, task, pair, decision=None):
    store = AlignmentStore(service(request))
    try:
        return store.save(task, pair, decision) if decision else store.read(task, pair)
    except FileNotFoundError:
        raise HTTPException(404, "任务、版本或锚点不存在") from None
    except StaleAlignment as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/contracts/{task}/alignment")
def page(request: Request, task: str, standard_version: str = "", supplier_version: str = ""):
    try:
        versions = service(request).versions(task)
    except FileNotFoundError:
        raise HTTPException(404, "任务不存在") from None
    pair = Pair(standard_version=standard_version, supplier_version=supplier_version)
    data = operate(request, task, pair) if standard_version and supplier_version else None
    return templates.TemplateResponse(
        request=request,
        name="alignment.html",
        context={
            "task": task,
            "versions": versions,
            "pair": pair,
            "data": data,
            "units": units,
            "text": text,
        },
    )


@router.get("/api/contracts/{task}/alignment")
def api_get(request: Request, task: str, standard_version: str, supplier_version: str):
    return operate(
        request, task, Pair(standard_version=standard_version, supplier_version=supplier_version)
    )


@router.post("/api/contracts/{task}/alignment")
async def api_save(request: Request, task: str):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "请提交对象")
    pair = Pair.model_validate(
        {k: payload.get(k, "") for k in ("standard_version", "supplier_version")}
    )
    decision = Decision.model_validate(
        {k: v for k, v in payload.items() if k not in ("standard_version", "supplier_version")}
    )
    return await run_in_threadpool(operate, request, task, pair, decision)


@router.post("/contracts/{task}/alignment")
async def web_save(request: Request, task: str):
    async with request.form(max_files=0, max_fields=100) as form:
        pair = Pair(
            standard_version=form.get("standard_version", ""),
            supplier_version=form.get("supplier_version", ""),
        )
        decision = Decision(
            revision=form.get("revision"),
            action=form.get("action"),
            standard_anchors=form.getlist("standard_anchors"),
            supplier_anchors=form.getlist("supplier_anchors"),
            note=form.get("note", ""),
        )
    await run_in_threadpool(operate, request, task, pair, decision)
    return RedirectResponse(
        f"/contracts/{task}/alignment?standard_version={pair.standard_version}&supplier_version={pair.supplier_version}",
        status_code=303,
    )
