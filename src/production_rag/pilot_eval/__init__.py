"""Read-only evaluation adapter for frozen M3.2 pilot JSON artifacts."""

from .contracts import ContractError
from .evaluate import evaluate_pilot
from .render import render_markdown

__all__ = ["ContractError", "evaluate_pilot", "render_markdown"]
