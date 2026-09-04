"""Shared building blocks used by both the pipeline jobs and the FastAPI service.

Nothing in :mod:`common` may import from :mod:`api` or :mod:`pipeline` — the
dependency arrow points one way so the pipeline can run without FastAPI
installed and the API can run without DuckDB installed.
"""

__all__ = ["config", "geo", "models", "polyline", "text"]
