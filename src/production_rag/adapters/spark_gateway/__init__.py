"""Isolated Spark X2.6 text adapter; no application wiring."""
from .client import SparkChat, SparkError
from .settings import SparkSettings

__all__ = ["SparkChat", "SparkError", "SparkSettings"]
