from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from production_rag.contracts.service import ContractBusy, ContractService

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


def service(request: Request) -> ContractService:
    return request.app.state.contracts


def view(request, **context):
    return templates.TemplateResponse(request=request, name="contracts.html", context=context)


def _get(request, task, version):
    try:
        return service(request).get(task, version)
    except FileNotFoundError:
        raise HTTPException(404, "任务或版本不存在") from None


async def upload(request: Request, task: str | None = None):
    try:
        role_root = getattr(request.app.state, "document_roles", None)
        if role_root is not None and task:
            from production_rag.contracts.roles import known_roles

            if known_roles(role_root.parent).get(task) == "material":
                raise HTTPException(409, "附件任务不能转换为主合同任务，请新建主合同")
        async with request.form(max_files=1, max_fields=0) as form:
            file = form.get("file")
            if len(form.multi_items()) != 1 or not isinstance(file, UploadFile):
                raise HTTPException(400, "请仅上传一份 DOCX 或文本型 PDF 主文档")
            data = await file.read(service(request).limits.max_bytes + 1)
            if len(data) > service(request).limits.max_bytes:
                raise HTTPException(413, "文件超过 512 KiB 试验限制")
            record = await run_in_threadpool(
                service(request).upload, data, file.filename or "", task
            )
            if role_root is not None:
                from production_rag.contracts.roles import register

                register(role_root, record.task_id, "main_contract")
            return record
    except ContractBusy as exc:
        raise HTTPException(429, str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(404, "任务不存在") from None
    except OSError:
        raise HTTPException(
            503, "本地文件保存或读取失败；解析未完成，请检查存储空间和权限"
        ) from None


@router.get("/contracts")
def home(request: Request):
    return view(request)


@router.post("/contracts/upload")
async def web_upload(request: Request):
    record = await upload(request)
    return RedirectResponse(
        f"/contracts/{record.task_id}/versions/{record.version_id}", status_code=303
    )


@router.post("/contracts/{task}/upload")
async def web_version(request: Request, task: str):
    record = await upload(request, task)
    return RedirectResponse(f"/contracts/{task}/versions/{record.version_id}", status_code=303)


@router.get("/contracts/{task}/versions/{version}")
def preview(request: Request, task: str, version: str):
    record = _get(request, task, version)
    return view(request, record=record, versions=service(request).versions(task))


@router.get("/contracts/{task}/versions/{version}/source/{anchor}")
def source(request: Request, task: str, version: str, anchor: str):
    record = _get(request, task, version)
    try:
        located = service(request).locate(task, version, anchor)
    except FileNotFoundError:
        raise HTTPException(404, "此版本不存在该原文位置") from None
    return view(request, record=record, source=located)


@router.post("/api/contracts", status_code=201)
async def api_upload(request: Request):
    return await upload(request)


@router.post("/api/contracts/{task}/versions", status_code=201)
async def api_version(request: Request, task: str):
    return await upload(request, task)


@router.get("/api/contracts/{task}/versions")
def api_versions(request: Request, task: str):
    try:
        return service(request).versions(task)
    except FileNotFoundError:
        raise HTTPException(404, "任务不存在") from None


@router.get("/api/contracts/{task}/versions/{version}")
def api_preview(request: Request, task: str, version: str):
    return _get(request, task, version)


@router.get("/api/contracts/{task}/versions/{version}/blocks/{anchor}")
def api_source(request: Request, task: str, version: str, anchor: str):
    _get(request, task, version)
    try:
        return service(request).locate(task, version, anchor)
    except FileNotFoundError:
        raise HTTPException(404, "此版本不存在该原文位置") from None


@router.get("/api/contracts/{task}/versions/{version}/original")
def original(request: Request, task: str, version: str):
    record = _get(request, task, version)
    data = service(request).original(record)
    suffix = record.result.format if record.result.format in ("docx", "pdf") else "bin"
    # Attachment only, with a server-defined filename; never render uploaded active content.
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="original-{version[:12]}.{suffix}"',
        },
    )
