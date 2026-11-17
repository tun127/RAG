"""Local offline entry. No settings or credential loading during startup."""

import json
import os
import re
import secrets
import uuid
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from time import monotonic
from typing import TypedDict

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from langgraph.graph import END, START, StateGraph
from starlette.datastructures import FormData

from production_rag.api.routes import alignment, contracts
from production_rag.contracts.alignment import text
from production_rag.contracts.roles import known_roles
from production_rag.contracts.service import ContractService
from production_rag.e2e_support import (
    material_form,
    material_inputs,
    partial_report,
)
from production_rag.knowledge_baseline.snapshot import build
from production_rag.model_runtime import ModelPolicy
from production_rag.pilot_review import presign
from production_rag.pilot_review.clients import client
from production_rag.pilot_review.context import blocks
from production_rag.pilot_review.execution import execute
from production_rag.pilot_review.inputs import SCOPE, load_run, save_run, source, write_new
from production_rag.pilot_review.providers import provider_for
from production_rag.product_ui.example import example_report
from production_rag.provenance import code_revision
from production_rag.review_inputs.models import digest
from production_rag.submissions import Submissions, fingerprint

ROOT = Path(__file__).resolve().parents[2]
LIMIT = "仅检查固定提取文本；未核实附件、版面及材料完整性。不得据此确定条款缺失。SIMULATED 不代表模型准确率。"
REVIEWER_NOTICE = "复核人标识由操作者自行填写，未经身份认证，不构成电子签名。"


def stored_mode(view):
    return view.get(
        "execution_mode", "SIMULATED" if view.get("report", {}).get("simulation", True) else "LIVE"
    )


def page(body, mode="SIMULATED"):
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>body{max-width:960px;margin:auto;padding:20px;font:17px sans-serif}pre{white-space:pre-wrap;overflow-wrap:anywhere}input,select,button,textarea{max-width:100%;margin:8px}article{border-top:1px solid #aaa;padding:16px}mark{background:#ffeb99}</style><p><b>'
        + escape(mode)
        + ' · 合同审查</b></p><nav><a href="/">审查入口</a> | <a href="/contracts">上传与原文</a></nav><p>'
        + LIMIT
        + "</p>"
        + body
    )


class Flow(TypedDict):
    phase: str


def run_graph(
    run,
    config,
    root,
    factory=client,
    provider=None,
    progress=None,
    coverage_complete=True,
    clock=monotonic,
    agent_factory=None,
):
    deadline = clock() + config.total_seconds
    shared_pool = None
    if config.integration_enabled:
        from production_rag.agent.budgets import RunBudget
        from production_rag.agent_review.product_boundary import ProductPool
        from production_rag.pilot_review.findings_v2 import budget_plan
        plan = budget_plan(run, config)
        shared_pool = ProductPool(RunBudget(config.total_seconds,
            config.total_output + (config.agent_output if config.review_strategy == 'agentic' else 0)),
            11 if config.bounded_live_validation else 36, {s['task_id']: s['cap'] for s in plan['slots']},
            agent_limit=config.agent_output if config.review_strategy == 'agentic' else 0)
    # Provider is bound to this immutable snapshot. No query may expand SCOPE.
    selected = provider or provider_for("rag")
    cache, trace = {}, []

    class FixedProvider:
        def retrieve(self, snapshot, query, **kwargs):
            return cache[query]

    def retrieve(state):
        for task in run.checklist:
            retrieval_started = clock()
            if clock() >= deadline:
                raise TimeoutError("run_deadline")
            result = selected.retrieve(
                run.knowledge,
                task.question,
                sources="policies",
                top_k=config.top_k,
                document_ids=run.policy_scope,
            )
            missing = set(task.required_policy_dependencies) - {
                c["document_id"] for c in result["contexts"]
            }
            trace.append(
                {
                    "task": task.task_id,
                    "attempt": 1,
                    "reason": "initial",
                    "hits": [h["unit_id"] for h in result["hits"]],
                    "elapsed_seconds": clock() - retrieval_started,
                }
            )
            allowed = missing & set(run.policy_scope)
            if allowed:
                query = task.question + " " + " ".join(sorted(allowed))
                if clock() >= deadline:
                    raise TimeoutError("run_deadline")
                supplement_started = clock()
                extra = selected.retrieve(
                    run.knowledge,
                    query,
                    sources="policies",
                    top_k=30,
                    document_ids=run.policy_scope,
                )
                old = {h["unit_id"] for h in result["hits"]}
                new = {h["unit_id"] for h in extra["hits"]}
                progress = bool(new - old) and {c["document_id"] for c in result["contexts"]} <= {
                    c["document_id"] for c in extra["contexts"]
                }
                trace.append(
                    {
                        "task": task.task_id,
                        "attempt": 2,
                        "reason": "declared_policy_dependency",
                        "outcome": "new_evidence" if progress else "no_progress_stop",
                        "hits": sorted(new),
                        "elapsed_seconds": clock() - supplement_started,
                    }
                )
                if progress:
                    result = extra
            cache[task.question] = result
        return {"phase": "retrieved"}

    output = {}

    def review(state):
        folder, report = execute(
            run,
            config,
            root,
            factory,
            provider=FixedProvider(),
            progress=progress,
            coverage_complete=coverage_complete,
            deadline=deadline,
            clock=clock,
            shared_pool=shared_pool,
        )
        if shared_pool is not None:
            from production_rag.agent_review.product import review_saved
            report = review_saved(run, report, folder, shared_pool, deadline,
                                  case=config.simulated_case, clock=clock, retrieval_ledger=trace,
                                  model_factory=agent_factory)
        output.update(folder=folder, report=report)
        write_new(folder / "graph-trace.json", trace)
        return {"phase": "validated_and_saved"}

    graph = StateGraph(Flow)
    graph.add_node("fixed_input", lambda s: {"phase": "fixed"})
    graph.add_node("retrieve_and_bounded_supplement", retrieve)
    graph.add_node("review_validate_save", review)
    graph.add_edge(START, "fixed_input")
    graph.add_edge("fixed_input", "retrieve_and_bounded_supplement")
    graph.add_edge("retrieve_and_bounded_supplement", "review_validate_save")
    graph.add_edge("review_validate_save", END)
    graph.compile().invoke({"phase": "pending"})
    return output["folder"], output["report"]


def create_app(runtime=None, model_policy=None, renderer=None):
    runtime = Path(runtime or os.environ.get("CONTRACT_E2E_RUNTIME", ROOT / "data/runtime/e2e"))
    active = set()
    policy_config = model_policy or ModelPolicy.from_environment()
    submissions = Submissions(runtime)
    app = FastAPI(title="企业采购合同审查 · SIMULATED")
    from production_rag.api.contract_middleware import ContractBoundaryMiddleware

    app.add_middleware(ContractBoundaryMiddleware)
    app.state.contracts = ContractService(
        runtime / "contracts", ROOT / "data/contract-knowledge-candidate"
    )
    app.state.document_roles = runtime / "document-roles"
    app.include_router(contracts.router)
    app.include_router(alignment.router)
    token = secrets.token_urlsafe(32)
    knowledge = build(ROOT / "data/contract-knowledge-candidate", mode="policies15")
    checklist = presign.presign_tasks(knowledge)
    task_meta = {task.task_id: task.model_dump(mode="json") for task in checklist}
    policy_documents = [
        {"document_id": document.document_id, "version": document.version}
        for document in knowledge.documents
        if document.document_id in SCOPE
    ]

    def with_times(v):
        rid = v["run_id"]
        created = v.get("created_at")
        stamps = [x for x in (created, v.get("completed_at")) if x]
        for path in [
            *(runtime / "jobs" / rid / "events").glob("*.json"),
            *(runtime / "opinions" / rid).glob("*.json"),
            *(runtime / "jobs" / rid).glob("failure.json"),
        ]:
            stamp = json.loads(path.read_bytes()).get("saved_at")
            if stamp:
                stamps.append(stamp)
        valid = []
        for stamp in stamps:
            try:
                parsed = datetime.fromisoformat(stamp)
                if parsed.utcoffset() is not None:
                    valid.append(parsed)
            except ValueError:
                pass
        return {
            **v,
            "created_at": created,
            "updated_at": max(valid).astimezone(UTC).isoformat() if valid else None,
        }

    def read(run_id):
        if not re.fullmatch("[a-f0-9]{32}", run_id):
            raise HTTPException(404)
        try:
            return with_times(json.loads((runtime / "runs" / run_id / "view.json").read_bytes()))
        except FileNotFoundError:
            job = runtime / "jobs" / run_id
            if not (job / "pending.json").exists():
                raise HTTPException(404) from None
            pending = json.loads((job / "pending.json").read_bytes())
            failure = (
                json.loads((job / "failure.json").read_bytes())
                if (job / "failure.json").exists()
                else None
            )
            state = (
                "failed" if failure else ("running" if run_id in active else "state_unconfirmed")
            )
            return with_times(partial_report(runtime, pending, state, failure))

    def opinions(run_id):
        return [
            {
                "reviewer": "历史记录未提供",
                "reviewer_notice": REVIEWER_NOTICE,
                **json.loads(p.read_bytes()),
            }
            for p in sorted((runtime / "opinions" / run_id).glob("*.json"))
        ]

    def ui_context(name, **fields):
        return {
            "contract_version": "contract-product-api-v1",
            "page": name,
            "csrf_token": token,
            "reviewer_notice": REVIEWER_NOTICE,
            "server_strategy": getattr(policy_config, 'strategy', None),
            **fields,
        }

    def parse_stats(parse):
        result = parse.get("result") or {}
        blocks = result.get("blocks") or []
        characters = 0
        for block in blocks:
            if block.get("kind") == "table":
                characters += sum(
                    len(cell.get("text") or "")
                    for row in block.get("rows") or []
                    for cell in row
                )
            else:
                characters += len(block.get("text") or "")
        return {
            "characters": characters,
            "blocks": len(blocks),
            "format": result.get("format"),
            "status": result.get("status"),
        }

    def selection_stats(record):
        return parse_stats({"result": record.model_dump(mode="json")["result"]})

    def materials_for(v):
        rows = v.get("report", {}).get("results") or []
        states = next((row["material_state"] for row in rows if row.get("material_state")), [])
        if not states:
            try:
                run = load_run(runtime / "inputs" / v["run_id"] / "input.json")
            except (FileNotFoundError, ValueError, KeyError, OSError):
                return []
            states = [
                {
                    "material_id": material.material_id,
                    "status": material.status,
                    "statement": material.statement,
                }
                for material in run.materials
            ]
        return states

    def material_mapping_for(v):
        return v.get("bridge", {}).get("material_mapping") or []

    def evidence_payload(v, run_id, task_id):
        rows = [row for row in v["report"]["results"] if row["task_id"] == task_id]
        if not rows:
            raise HTTPException(404, "检查项不存在")
        path = (
            runtime / v["execution"] / (task_id + "-request.json")
            if v.get("execution")
            else None
        )
        if path is None or not path.exists():
            raise HTTPException(409, "该项尚未执行，无生成请求或引用")
        request = json.loads(path.read_bytes())
        checked = rows[0].get("validation") or {}
        documents = [
            {
                "source": {
                    key: mapping["source"][key]
                    for key in (
                        "document_id",
                        "version",
                        "source_type",
                        "text",
                        "text_sha256",
                    )
                },
                "blocks": mapping["blocks"],
            }
            for mapping in request["maps"]
        ]
        bridge = v.get("bridge") or {}
        parse = bridge.get("parse") or {}
        anchors = []
        for item in bridge.get("mapping") or []:
            anchor = item.get("anchor")
            if parse.get("task_id") and parse.get("version_id") and anchor:
                anchors.append(
                    {
                        **item,
                        "url": f"/contracts/{parse['task_id']}/versions/"
                        f"{parse['version_id']}/source/{anchor}",
                    }
                )
        return ui_context(
            "evidence",
            run_id=run_id,
            task_id=task_id,
            mode=stored_mode(v),
            documents=documents,
            citations=checked.get("locations", []) if checked.get("valid") else [],
            validation_valid=bool(checked.get("valid")),
            validation_failure=rows[0].get("failure"),
            citation_diagnostics=checked.get("citation_diagnostics", []),
            result=rows[0],
            anchors=anchors,
            coverage=bridge,
            semantic_support="pending_human_review",
            materials=materials_for(v),
            material_mapping=material_mapping_for(v),
        )

    def run_context(run_id):
        v = read(run_id)
        return ui_context(
            "run",
            view={**v, "human_opinions": opinions(run_id)},
            mode=stored_mode(v),
            task_meta=task_meta,
            materials=materials_for(v),
            material_mapping=material_mapping_for(v),
            parse_stats=parse_stats(v.get("bridge", {}).get("parse") or {}),
            policy_documents=policy_documents,
        )

    def run_page_data(run_id, item=None, saved=False):
        context = run_context(run_id)
        v = context["view"]
        ids = [row["task_id"] for row in v.get("report", {}).get("results") or []]
        if item is not None and item not in ids:
            raise HTTPException(404, "检查项不存在")
        active = item or (ids[0] if ids else None)
        evidence = None
        if active:
            try:
                evidence = evidence_payload(v, run_id, active)
            except HTTPException as exc:
                evidence = {"error": str(exc.detail), "status_code": exc.status_code}
        return {
            **context,
            "page": "run",
            "selected_task_id": item,
            "evidence": evidence,
            "saved": saved,
        }

    example_cache = {}

    def example_context():
        if "value" not in example_cache:
            example_cache["value"] = example_report(knowledge)
        return example_cache["value"]

    if renderer is not None:
        from fastapi.exception_handlers import http_exception_handler
        from starlette.exceptions import HTTPException as StarletteHTTPException

        @app.exception_handler(StarletteHTTPException)
        async def rendered_error(request: Request, exc):
            if request.method == "GET" and not request.url.path.startswith(
                ("/api/", "/export/", "/product-assets/")
            ):
                response = renderer(
                    "error",
                    ui_context("error", status_code=exc.status_code, message=str(exc.detail)),
                )
                response.status_code = exc.status_code
                return response
            return await http_exception_handler(request, exc)

        @app.exception_handler(Exception)
        async def unexpected_error(request: Request, exc):
            message = "读取或处理失败，已有记录保留。请返回首页查看任务，勿重复提交审查；需管理员检查服务日志。"
            if request.method == "GET" and not request.url.path.startswith(
                ("/api/", "/export/", "/product-assets/")
            ):
                response = renderer("error", ui_context("error", status_code=500, message=message))
                response.status_code = 500
                return response
            return JSONResponse(status_code=500, content={"detail": message})

    @app.get("/api/e2e/home")
    def home_data():
        roles = known_roles(runtime)
        documents = [
            json.loads(p.read_bytes()) for p in (runtime / "contracts").glob("*/*/record.json")
        ]
        ids = {p.parent.name for p in (runtime / "jobs").glob("*/pending.json")}
        ids.update(p.parent.name for p in (runtime / "runs").glob("*/view.json"))
        histories = []
        material_tasks = {
            p.parent.name for p in (runtime / "document-roles").glob("*/material.json")
        }
        for rid in sorted(ids):
            v = read(rid)
            material_tasks.update(
                m["parse"]["task_id"]
                for m in v.get("bridge", {}).get("material_mapping", [])
                if m.get("parse")
            )
            histories.append(
                {
                    "run_id": rid,
                    "created_at": v.get("created_at"),
                    "updated_at": v.get("updated_at"),
                    "execution_mode": stored_mode(v),
                    "overall_status": v.get("overall_status", "historical"),
                    "parse": v.get("bridge", {}).get("parse", {}),
                    "status_counts": v["report"].get("status_counts", {}),
                    "fixture_version": v.get("fixture_version"),
                    "scenario": v.get("scenario"),
                }
            )
        return ui_context(
            "home",
            documents=[
                {**d, "document_role": "main_contract"}
                for d in documents
                if roles.get(d["task_id"]) == "main_contract" and d["task_id"] not in material_tasks
            ],
            unclassified_documents=[
                {**d, "document_role": "unknown"} for d in documents if d["task_id"] not in roles
            ],
            runs=histories,
            available_modes=["SIMULATED", "LIVE"] if policy_config.allow_live else ["SIMULATED"],
            live_enabled=policy_config.allow_live,
            model_config=policy_config.config().model_dump(mode="json"),
            ticket=uuid.uuid4().hex,
        )

    @app.get("/example")
    def example_page():
        if renderer is None:
            raise HTTPException(404, "示例报告仅在产品页面提供")
        return renderer("example", {**ui_context("example"), "example": example_context()})

    @app.get("/example/print")
    def example_print():
        if renderer is None:
            raise HTTPException(404, "示例报告仅在产品页面提供")
        return renderer(
            "example_report", {**ui_context("example_report"), "example": example_context()}
        )

    @app.post("/product/upload")
    async def product_upload(request: Request):
        record = await contracts.upload(request)
        return RedirectResponse(f"/select/{record.task_id}/{record.version_id}", 303)

    @app.post("/product/upload/{task}")
    async def product_version(request: Request, task: str):
        record = await contracts.upload(request, task)
        return RedirectResponse(f"/select/{record.task_id}/{record.version_id}", 303)

    @app.get("/api/e2e/selection/{task}/{version}")
    def selection_data(task: str, version: str):
        try:
            record = app.state.contracts.get(task, version)
        except FileNotFoundError:
            raise HTTPException(404, "任务或合同版本不存在") from None
        if known_roles(runtime).get(task) != "main_contract":
            raise HTTPException(
                409, "该文件不是已确认的主合同；旧记录角色未知时请重新作为主合同上传"
            )
        return ui_context(
            "selection",
            record=record.model_dump(mode="json"),
            submission_id=submissions.issue(task, version),
            policy=knowledge.snapshot_id,
            stage="签署前（现有六项试点）",
            checklist=[t.model_dump(mode="json") for t in checklist],
            policy_documents=policy_documents,
            parse_stats=selection_stats(record),
            available_modes=["SIMULATED", "LIVE"] if policy_config.allow_live else ["SIMULATED"],
            live_enabled=policy_config.allow_live,
            model_config=policy_config.config().model_dump(mode="json"),
        )

    @app.get("/api/e2e/runs/{run_id}")
    def run_data(run_id: str):
        return run_context(run_id)

    @app.get("/api/e2e/runs/{run_id}/evidence/{task_id}")
    def evidence_data(run_id: str, task_id: str):
        return evidence_payload(read(run_id), run_id, task_id)

    @app.get("/")
    def home():
        if renderer is not None:
            return renderer("home", home_data())
        records = []
        for p in (runtime / "contracts").glob("*/*/record.json"):
            r = json.loads(p.read_bytes())
            records.append(
                f'<li><a href="/select/{r["task_id"]}/{r["version_id"]}">{escape(r["original_name"])} · {r["version_id"][:12]}</a></li>'
            )
        runs = "".join(
            f'<li><a href="/runs/{p.parent.name}">{p.parent.name}</a></li>'
            for p in (runtime / "jobs").glob("*/pending.json")
        )
        return page(
            '<h1>采购合同审查</h1><form action="/contracts/upload" method="post" enctype="multipart/form-data"><input type="file" name="file" accept=".docx,.pdf" required><button>上传并解析</button></form><h2>选择解析版本</h2><ul>'
            + "".join(records)
            + "</ul><h2>固定审查记录</h2><ul>"
            + runs
            + "</ul>"
        )

    @app.get("/select/{task}/{version}")
    def select(task: str, version: str):
        if renderer is not None:
            return renderer("selection", selection_data(task, version))
        r = app.state.contracts.get(task, version)
        checklist = "".join(
            "<li>" + escape(t.question) + "</li>" for t in presign.presign_tasks(knowledge)
        )
        submission_id = submissions.issue(task, version)
        live_option = "<option>LIVE</option>" if policy_config.allow_live else ""
        display = policy_config.config()
        model_fields = (
            '<h2>执行模式</h2><select name="execution_mode"><option>SIMULATED</option>'
            + live_option
            + "</select><p>LIVE "
            + ("已由服务端允许" if policy_config.allow_live else "默认禁止")
            + "；提供方 "
            + escape(display.provider)
            + "，模型 "
            + escape(display.model)
            + "；固定付款/验收/例外六项，BM25及生成前依赖补查。每次最多"
            + str(display.per_call_output)
            + "输出token、总输出"
            + str(display.total_output)
            + "、最多"
            + str(display.max_attempts)
            + "次请求、总期限"
            + str(display.total_seconds)
            + "秒、单次最多"
            + str(display.timeout)
            + "秒、不重试。LIVE须选择正常配置，不使用模拟夹具。</p>"
        )
        return page(
            f'<h1>确认审查范围</h1><p>合同版本：{version}</p><a href="/contracts/{task}/versions/{version}">解析结果、覆盖警告与原文</a><p>{escape(r.result.coverage_scope)}</p><ul>{checklist}</ul><form method="post" action="/start" enctype="multipart/form-data"><input type="hidden" name="token" value="{token}"><input type="hidden" name="submission_id" value="{submission_id}">{model_fields}<input type="hidden" name="task" value="{task}"><input type="hidden" name="version" value="{version}"><label>阶段<select name="stage"><option value="签署前（现有六项试点）">签署前（1.0清单）</option></select></label><label>固定政策<select name="policy"><option>{knowledge.snapshot_id}</option></select></label>{material_form()}<button>确认模式、版本与预算，提交审查</button></form>'
        )

    async def begin_review(task, version, form, background, record=None):
        # 固定输入：阶段与政策快照由服务端决定，页面不再选择。
        stage = "签署前（现有六项试点）"
        policy = knowledge.snapshot_id
        try:
            r = record or app.state.contracts.get(task, version)
        except FileNotFoundError:
            raise HTTPException(404, "任务或合同版本不存在") from None
        if known_roles(runtime).get(task) != "main_contract":
            raise HTTPException(409, "附件或角色未确认的旧文件不能作为主合同开始审查")
        if r.result.status not in ("parsed", "partial"):
            raise HTTPException(400, "解析失败或格式不支持，不能提交审查")
        fixed, mapping = "", []
        for block in r.result.blocks:
            value = text(block)
            if value:
                start = len(fixed)
                fixed += value + "\n\n"
                mapping.append({"start": start, "end": start + len(value), "anchor": block.anchor})
        if not fixed.strip():
            raise HTTPException(400, "无可审查的提取文本")
        raw = fixed.encode()
        scenario = str(form.get("scenario", "normal"))
        if scenario not in ("normal", "budget_after_one", "payment_quote_v1", "crash_after_one"):
            raise HTTPException(400, "未知离线演示配置")
        try:
            config = policy_config.config(str(form.get("execution_mode", "SIMULATED")), scenario)
        except ValueError:
            raise HTTPException(403, "执行模式未获服务端允许，或LIVE夹带模拟配置") from None
        submission_id = str(form.get("submission_id", ""))
        submission_fingerprint = await fingerprint(form, config)
        existing = submissions.check(submission_id, task, version, submission_fingerprint)
        if existing:
            return RedirectResponse("/runs/" + existing, 303)
        materials, material_mapping = await material_inputs(form, app.state.contracts, runtime)
        run = presign.PresignRun(
            run_id=uuid.uuid4().hex,
            code_revision=code_revision(ROOT, simulation=config.simulation),
            contract=source(raw, "contract-0001", digest(raw), "contract"),
            materials=materials,
            trigger_hypotheses=[
                presign.Hypothesis(
                    condition=c,
                    state="unknown",
                    basis_kind="unassessed",
                    note="待人工复核，未自动判断",
                )
                for c in presign.TRIGGERS
            ],
            knowledge=knowledge,
            knowledge_id=knowledge.snapshot_id,
            checklist=presign.presign_tasks(knowledge),
        )
        existing = submissions.claim(
            submission_id, task, version, submission_fingerprint, run.run_id
        )
        if existing:
            return RedirectResponse("/runs/" + existing, 303)
        if hasattr(policy_config, 'claim_live_run'):
            policy_config.claim_live_run(run)
        save_run(run, runtime / "inputs")
        bridge = {
            "mapping_version": "parse-review-bridge-v1",
            "parse": r.model_dump(mode="json"),
            "fixed_text_sha256": digest(raw),
            "mapping": mapping,
            "review_blocks": blocks(run.contract, run.run_id),
            "limit": LIMIT,
            "material_mapping": material_mapping,
            "coverage_complete": r.result.coverage_complete,
        }
        job = runtime / "jobs" / run.run_id
        created_at = datetime.now(UTC).isoformat()
        write_new(
            job / "pending.json",
            {
                "run_id": run.run_id,
                "created_at": created_at,
                "stage": stage,
                "policy": policy,
                "bridge": bridge,
                "scenario": scenario,
                "config": config.model_dump(mode="json"),
                "execution_mode": "SIMULATED" if config.simulation else "LIVE",
                "submission_id": submission_id,
                "submission_fingerprint": submission_fingerprint,
                "fixture_version": "payment-source-bound-v2" if config.simulation else None,
                "fixture_sha256": digest((Path(__file__).parent / "e2e_support.py").read_bytes())
                if config.simulation
                else None,
            },
        )

        active.add(run.run_id)

        def work():
            def progress(rows):
                write_new(job / f"progress-{len(rows)}.json", rows)
                write_new(
                    job / "events" / f"progress-{len(rows)}.json",
                    {"saved_at": datetime.now(UTC).isoformat(), "completed_items": len(rows)},
                )
                if scenario == "crash_after_one" and len(rows) == 1:
                    raise RuntimeError("offline_injected_orchestration_failure")

            try:
                folder, report = run_graph(
                    run,
                    config,
                    runtime / "executions",
                    progress=progress,
                    coverage_complete=r.result.coverage_complete,
                    factory=policy_config.factory,
                    agent_factory=getattr(policy_config, 'agent_model', None),
                )
                view = {
                    "run_id": run.run_id,
                    "created_at": created_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "stage": stage,
                    "policy": policy,
                    "bridge": bridge,
                    "execution": str(folder.relative_to(runtime)),
                    "report": report,
                    "limit": LIMIT,
                    "input_version": run.protocol,
                    "checklist_version": presign.TASK_VERSION,
                    "trigger_hypotheses": [
                        h.model_dump(mode="json") for h in run.trigger_hypotheses
                    ],
                    "config": config.model_dump(mode="json"),
                    "execution_mode": "SIMULATED" if config.simulation else "LIVE",
                    "submission_id": submission_id,
                    "scenario": scenario,
                    "fixture_version": "payment-source-bound-v2" if config.simulation else None,
                    "fixture_sha256": digest(
                        (Path(__file__).parent / "e2e_support.py").read_bytes()
                    )
                    if config.simulation
                    else None,
                    "overall_status": "completed"
                    if report["status_counts"]["success"] == len(run.checklist)
                    else "partial",
                }
                write_new(runtime / "runs" / run.run_id / "view.json", view)
            except Exception as exc:
                write_new(
                    job / "failure.json",
                    {
                        "status": "failed",
                        "saved_at": datetime.now(UTC).isoformat(),
                        "category": "run_deadline"
                        if isinstance(exc, TimeoutError)
                        else "orchestration_failed",
                        "reason": "执行依赖不可用或编排异常；已保存固定输入及逐项进度，未形成完整审查。",
                    },
                )

            finally:
                active.discard(run.run_id)

        background.add_task(work)
        return RedirectResponse("/runs/" + run.run_id, 303)

    @app.post("/api/e2e/reviews")
    @app.post("/start")
    async def start(
        request: Request,
        background: BackgroundTasks,
        task: str = Form(),
        version: str = Form(),
        policy: str = Form(),
        stage: str = Form(),
        token_value: str = Form(alias="token"),
    ):
        if (
            token_value != token
            or policy != knowledge.snapshot_id
            or stage != "签署前（现有六项试点）"
        ):
            raise HTTPException(400, "固定输入或表单无效")
        form = await request.form()
        return await begin_review(task, version, form, background)

    @app.post("/review")
    async def quick_review(request: Request, background: BackgroundTasks):
        """一次上传即固定版本、套用当前清单与政策并开始审查。"""
        # 票据走查询串，表单只保留一个文件，沿用既有上传校验。
        ticket = str(request.query_params.get("ticket") or "") or uuid.uuid4().hex
        reserved = submissions.lookup(ticket)
        if reserved and reserved["run_id"]:
            # 同一页面重复提交或重放：复用已开始的运行，不再调用模型。
            return RedirectResponse("/runs/" + reserved["run_id"], 303)
        try:
            record = await contracts.upload(request)
        except HTTPException as exc:
            if renderer is None:
                raise
            detail = exc.detail if isinstance(exc.detail, str) else "文件无法读取"
            response = renderer(
                "home",
                {
                    **home_data(),
                    "parse_error": {
                        "message": "文件无法解析，本次没有开始审查。",
                        "name": "",
                        "reasons": [detail],
                    },
                },
            )
            response.status_code = 400
            return response
        if record.result.status not in ("parsed", "partial") or not record.result.blocks:
            if renderer is None:
                return RedirectResponse("/", 303)
            reasons = [issue.message for issue in record.result.issues if issue.message]
            if not reasons:
                reasons.append(record.result.coverage_scope or "没有可审查的提取文本")
            response = renderer(
                "home",
                {
                    **home_data(),
                    "parse_error": {
                        "message": "文件无法解析，本次没有开始审查。",
                        "name": record.original_name,
                        "reasons": reasons,
                    },
                },
            )
            response.status_code = 400
            return response
        submit = FormData(
            [
                ("submission_id", ticket),
                ("ticket", ticket),
                ("execution_mode", "SIMULATED" if request.query_params.get("mode") == "SIMULATED" else ("LIVE" if policy_config.allow_live else "SIMULATED")),
                ("scenario", "normal"),
            ]
        )
        if reserved:
            # 上次已预留但没拿到运行编号（例如进程中断），沿用原固定输入继续。
            return await begin_review(
                reserved["task"], reserved["version"], submit, background
            )
        submissions.reserve(ticket, record.task_id, record.version_id)
        return await begin_review(
            record.task_id, record.version_id, submit, background, record
        )

    @app.get("/review/{run_id}")
    def review_page(run_id: str, item: str | None = None, saved: int = 0):
        if renderer is None:
            return RedirectResponse("/runs/" + run_id, 303)
        return renderer("review", run_page_data(run_id, item, bool(saved)))

    @app.get("/runs/{run_id}")
    def result(run_id: str, item: str | None = None, saved: int = 0):
        if renderer is not None:
            return renderer("result", run_page_data(run_id, item, bool(saved)))
        v = read(run_id)
        body = f'<h1>固定审查记录</h1><p>{run_id}</p><p>阶段：{escape(v["stage"])}；政策：{v["policy"]}</p><a href="/export/{run_id}">导出JSON</a> | <a href="/print/{run_id}">可打印报告</a><p>逐项结果与未完成原因均保留；请核对运行状态。缺政策依赖 DP-AUTH 须人工处理。</p>'
        body += "<p>解析覆盖：" + escape(v["bridge"]["parse"]["result"]["coverage_scope"]) + "</p>"
        body += "".join(
            "<p>解析警告：" + escape(i["message"]) + "</p>"
            for i in v["bridge"]["parse"]["result"]["issues"]
        )
        body += (
            "<p>固定模型与预算：</p><pre>"
            + escape(
                json.dumps(
                    v.get("config", v.get("report", {}).get("config", {})),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            + "</pre>"
        )
        body += "<p>运行状态：" + escape(v.get("overall_status", "历史固定结果")) + "</p>"
        if v.get("overall_status") == "running":
            body += '<meta http-equiv="refresh" content="3">'
        body += "<p>当前仅提供生成前依赖补查；未实现生成后再审。触发条件均为待复核声明，不代表自动认定。</p>"
        body += (
            "<details><summary>输入、清单与触发声明</summary><pre>"
            + escape(
                json.dumps(
                    {
                        k: v.get(k)
                        for k in (
                            "input_version",
                            "checklist_version",
                            "trigger_hypotheses",
                            "scenario",
                        )
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            + "</pre></details>"
        )
        for row in v["report"]["results"]:
            tid = row["task_id"]
            body += f'<article><h2>{tid}</h2><p>执行：{escape(row["execution_status"])}；候选：{escape(str(row["business_conclusion"]))}；原因：{escape(str(row["failure"]))}</p><a href="/evidence/{run_id}/{tid}">打开合同事实与政策原文</a><details><summary>执行与引用校验详情</summary><pre>{escape(json.dumps(row, ensure_ascii=False, indent=2))}</pre></details><form method="post" action="/opinion/{run_id}/{tid}"><input type="hidden" name="token" value="{token}"><select name="decision"><option>确认</option><option>更正</option><option>待复核</option></select><label>复核人标识（姓名或内部编号）<input name="reviewer" maxlength="100" required></label><p>{REVIEWER_NOTICE}</p><textarea name="note" maxlength="5000" required></textarea><button>记录人工意见</button></form></article>'
        body += (
            "<h2>人工意见（独立追加）</h2><pre>"
            + escape(json.dumps(opinions(run_id), ensure_ascii=False, indent=2))
            + "</pre>"
        )
        return page(body, stored_mode(v))

    @app.get("/evidence/{run_id}/{task_id}")
    def evidence(run_id: str, task_id: str):
        if renderer is not None:
            return renderer("evidence", evidence_data(run_id, task_id))
        v = read(run_id)
        if task_id not in {r["task_id"] for r in v["report"]["results"]}:
            raise HTTPException(404)
        request_path = (
            runtime / v["execution"] / (task_id + "-request.json") if v.get("execution") else None
        )
        if request_path is None or not request_path.exists():
            raise HTTPException(409, "该项尚未执行，无生成请求或引用")
        req = json.loads(request_path.read_bytes())
        row = next(r for r in v["report"]["results"] if r["task_id"] == task_id)
        checked = row.get("validation") or {}
        citations = checked.get("locations", []) if checked.get("valid") else []
        body = "<h1>本次固定文本与政策依据</h1><p>高亮与有效性请结合结果中的严格引用校验详情；位置有效不证明支持结论。PDF不提供视觉坐标。</p>"
        for citation in citations:
            body += (
                '<p>已严格定位的引用：<a href="#doc-'
                + escape(citation["document_id"])
                + '">'
                + escape(citation["document_id"])
                + "</a>：<mark>"
                + escape(citation.get("quote", ""))
                + "</mark></p>"
            )
        for m in req["maps"]:
            s = m["source"]
            body += (
                '<h2 id="doc-'
                + escape(s["document_id"])
                + '">'
                + escape(s["document_id"])
                + "</h2><p>"
                + escape(s["text_sha256"])
                + "</p><pre>"
                + escape(s["text"])
                + "</pre>"
            )
        for item in v["bridge"]["mapping"]:
            p = v["bridge"]["parse"]
            body += f'<p><a href="/contracts/{p["task_id"]}/versions/{p["version_id"]}/source/{item["anchor"]}">提取文本 {item["start"]}–{item["end"]} 的产品原文锚点</a></p>'
        return page(body, stored_mode(v))

    @app.post("/opinion/{run_id}/{task_id}")
    def opinion(
        run_id: str,
        task_id: str,
        decision: str = Form(""),
        note: str = Form(""),
        reviewer: str = Form(""),
        token_value: str = Form("", alias="token"),
        next_page: str = Form("", alias="next"),
    ):
        reviewer = reviewer.strip()
        if not reviewer or len(reviewer) > 100:
            raise HTTPException(
                400,
                {
                    "message": "复核人标识去除首尾空白后须为 1 至 100 个字符。",
                    "field": "reviewer",
                },
            )
        v = read(run_id)
        rows = [r for r in v["report"]["results"] if r["task_id"] == task_id]
        if token_value != token:
            raise HTTPException(
                400, {"message": "页面令牌无效，请刷新页面后重试。", "field": "token"}
            )
        if len(rows) != 1:
            raise HTTPException(404, "检查项不存在")
        if decision not in ("确认", "更正", "待复核"):
            raise HTTPException(
                400, {"message": "请选择有效的复核决定。", "field": "decision"}
            )
        if not note.strip() or len(note) > 5000:
            raise HTTPException(
                400,
                {
                    "message": "理由不能为空，且不得超过 5,000 个字符。",
                    "field": "note",
                },
            )
        write_new(
            runtime / "opinions" / run_id / (uuid.uuid4().hex + ".json"),
            {
                "run_id": run_id,
                "task_id": task_id,
                "result_sha256": digest(json.dumps(rows[0], sort_keys=True).encode()),
                "decision": decision,
                "reviewer": reviewer,
                "reviewer_notice": REVIEWER_NOTICE,
                "note": note,
                "saved_at": datetime.now(UTC).isoformat(),
            },
        )
        if next_page == "review":
            return RedirectResponse(f"/review/{run_id}?item={task_id}&saved=1", 303)
        return RedirectResponse(f"/runs/{run_id}?item={task_id}&saved=1", 303)

    @app.get("/export/{run_id}")
    def export(run_id: str):
        return JSONResponse(
            {**read(run_id), "human_opinions": opinions(run_id)},
            headers={"Content-Disposition": 'attachment; filename="review.json"'},
        )

    @app.get("/print/{run_id}")
    def printable(run_id: str, download: bool = False):
        if renderer is not None:
            response = renderer("report", run_context(run_id))
            if download:
                css = (Path(__file__).parent / "product_ui/static/product.css").read_text(encoding="utf-8")
                html = response.body.decode("utf-8").replace(
                    '<link rel="stylesheet" href="/product-assets/product.css">',
                    "<style>" + css + "</style>",
                ).replace(
                    '<script defer src="/product-assets/product.js"></script>',
                    '<script defer>window.addEventListener("DOMContentLoaded",()=>document.querySelectorAll("[data-print]").forEach(b=>b.onclick=()=>window.print()))</script>',
                )
                response = HTMLResponse(html)
                response.headers["Content-Disposition"] = 'attachment; filename="contract-review.html"'
            return response
        v = read(run_id)
        return page(
            '<h1>合同审查报告</h1><button onclick="window.print()">打印</button><pre>'
            + escape(
                json.dumps(
                    {**read(run_id), "human_opinions": opinions(run_id)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            + "</pre>",
            stored_mode(v),
        )

    return app


app = create_app()
