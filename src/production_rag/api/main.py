from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from production_rag.api.dependencies import authorize
from production_rag.bootstrap import build_services
from production_rag.contracts.service import ContractService
from production_rag.domain.errors import Busy, DependencyError, NoKnowledgeBase, RagError


def create_app(services=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.services = services or build_services()
        settings = app.state.services.settings
        app.state.contracts = ContractService(
            settings.data_dir / "contracts", settings.document_root
        )
        yield

    app = FastAPI(title="Agentic RAG", version="1.0.0", lifespan=lifespan)

    from production_rag.api.contract_middleware import ContractBoundaryMiddleware
    from production_rag.api.routes import contracts

    app.add_middleware(ContractBoundaryMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path == "/contracts" or request.url.path.startswith("/contracts/"):
            response = contracts.view(request, error=exc.detail)
            response.status_code = exc.status_code
            return response
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
        )

    @app.exception_handler(RagError)
    async def rag_error(request: Request, exc: RagError):
        status = (
            429
            if isinstance(exc, Busy)
            else (503 if isinstance(exc, (DependencyError, NoKnowledgeBase)) else 409)
        )
        return JSONResponse(
            status_code=status, content={"error": type(exc).__name__, "detail": str(exc)}
        )

    @app.exception_handler(ValueError)
    async def bad_request(request: Request, exc: ValueError):
        if request.url.path.startswith("/contracts/"):
            response = contracts.view(request, error=str(exc))
            response.status_code = 400
            return response
        return JSONResponse(
            status_code=400, content={"error": "invalid_request", "detail": str(exc)}
        )

    from production_rag.api.routes import documents, evaluations, health, memory, query

    app.include_router(health.router)
    app.include_router(contracts.router, dependencies=[Depends(authorize)])
    from production_rag.api.routes import alignment

    app.include_router(alignment.router, dependencies=[Depends(authorize)])
    for router in (query.router, documents.router, memory.router, evaluations.router):
        app.include_router(router, prefix="/api", dependencies=[Depends(authorize)])
    # 前端 build 后可由同一进程托管；API 路由先注册。
    ui = Path("ui/dist").resolve()
    if ui.is_dir():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    return app


app = create_app()
