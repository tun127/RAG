"""Single integrated product entry. Credentials are loaded only upon authorized LIVE submit."""

from fastapi.staticfiles import StaticFiles

from production_rag.e2e import create_app
from production_rag.product_ui.renderer import HERE, create_renderer

app = create_app(renderer=create_renderer())
app.mount("/product-assets", StaticFiles(directory=HERE / "static"), name="product-assets")
