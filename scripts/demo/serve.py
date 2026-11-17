"""Start the standalone public demo with the fixed simulation policy."""

from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from production_rag.agent_review.server import IntegrationPolicy
from production_rag.e2e import create_app
from production_rag.product_ui.renderer import HERE, create_renderer

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    runtime = ROOT / "data" / "runtime" / "demo-release-simulated"
    policy = IntegrationPolicy(strategy="agentic", case="correction")
    app = create_app(runtime, model_policy=policy, renderer=create_renderer())
    app.mount("/product-assets", StaticFiles(directory=HERE / "static"))
    uvicorn.run(app, host="127.0.0.1", port=8801)


if __name__ == "__main__":
    main()
