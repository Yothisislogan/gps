"""Build the Meilisearch index.

Indexing happens into a **temporary index that is then swapped**, not into the
live one.  Meilisearch's ``/swap-indexes`` makes the cutover atomic, which means
search is never briefly empty or half-populated during a nightly rebuild — the
same rule the tile pipeline follows with ``os.replace``.

Documents come from ``data/exports/pois_index.jsonl`` (written by
``pipeline.pois.export_geojson``), so this job needs neither PostGIS nor the
taxonomy: it is pure transport plus settings.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx

from common.config import get_settings
from pipeline.common.io import setup_logging
from pipeline.search.synonyms import SPANISH_STOP_WORDS, build_synonyms

__all__ = ["INDEX_SETTINGS", "MeiliAdmin", "main"]

log = logging.getLogger(__name__)

#: Index settings, per docs/SPEC.md section 8.
#:
#: The ranking rules keep Meilisearch's defaults and append ``popularity:desc``
#: last, so relevance still decides and popularity only breaks ties.  Sorting by
#: distance is requested per query (``_geoPoint``) rather than baked in here,
#: because a search with no position must not be ordered by proximity to nowhere.
INDEX_SETTINGS: dict[str, Any] = {
    "searchableAttributes": ["name", "alt_names", "name_norm", "address_text", "category", "city"],
    "filterableAttributes": ["kind", "category", "group", "city", "verified", "former", "_geo"],
    "sortableAttributes": ["popularity", "_geo"],
    "rankingRules": [
        "words",
        "typo",
        "proximity",
        "attribute",
        "sort",
        "exactness",
        "popularity:desc",
    ],
    "stopWords": list(SPANISH_STOP_WORDS),
    # Typo tolerance matters more here than in most products: half of Nicaraguan
    # search traffic omits accents, and place names are long. The default
    # two-typo threshold at 9 characters is lowered so "gueguense" still finds
    # "Güegüense" — the accent-folded name_norm field does the rest.
    "typoTolerance": {
        "enabled": True,
        "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8},
    },
    "pagination": {"maxTotalHits": 200},
}


class MeiliAdmin:
    """The handful of admin calls the indexer needs."""

    def __init__(self, base_url: str, api_key: str = "", *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MeiliAdmin:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(
            method,
            f"{self.base_url}{path}",
            headers={**self._headers, **kwargs.pop("headers", {})},
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        return response

    def health(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def create_index(self, uid: str, primary_key: str = "id") -> None:
        response = self._client.post(
            f"{self.base_url}/indexes",
            headers=self._headers,
            json={"uid": uid, "primaryKey": primary_key},
        )
        # 201 = created, 202 = task queued, 409 = already there. All are fine.
        if response.status_code not in (200, 201, 202, 409):
            raise RuntimeError(f"could not create index {uid}: {response.text[:300]}")
        if response.status_code in (200, 201, 202):
            self.wait(response.json().get("taskUid"))

    def delete_index(self, uid: str) -> None:
        response = self._client.delete(f"{self.base_url}/indexes/{uid}", headers=self._headers)
        if response.status_code in (200, 202):
            self.wait(response.json().get("taskUid"))

    def update_settings(self, uid: str, settings: dict[str, Any]) -> None:
        response = self._request("PATCH", f"/indexes/{uid}/settings", json=settings)
        self.wait(response.json().get("taskUid"))

    def add_documents(self, uid: str, ndjson: str) -> int:
        response = self._request(
            "POST",
            f"/indexes/{uid}/documents",
            content=ndjson.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )
        return int(response.json()["taskUid"])

    def swap(self, left: str, right: str) -> None:
        """Atomically exchange two indexes' contents."""
        response = self._request("POST", "/swap-indexes", json=[{"indexes": [left, right]}])
        self.wait(response.json().get("taskUid"))

    def wait(self, task_uid: int | None, *, timeout: float = 600.0) -> dict[str, Any]:
        """Block until a task finishes; raise on failure.

        Meilisearch queues everything, so a job that returns without waiting has
        not actually indexed anything — and would happily swap an empty index
        into place.
        """
        if task_uid is None:
            return {}
        deadline = time.monotonic() + timeout
        while True:
            payload = self._client.get(
                f"{self.base_url}/tasks/{task_uid}", headers=self._headers
            ).json()
            status = payload.get("status")
            if status == "succeeded":
                return payload
            if status in {"failed", "canceled"}:
                raise RuntimeError(f"meilisearch task {task_uid} {status}: {payload.get('error')}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"meilisearch task {task_uid} still {status} after {timeout}s")
            time.sleep(0.5)

    def document_count(self, uid: str) -> int:
        response = self._client.get(f"{self.base_url}/indexes/{uid}/stats", headers=self._headers)
        return (
            int(response.json().get("numberOfDocuments", 0)) if response.status_code == 200 else 0
        )


def read_documents(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def batched(documents: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for document in documents:
        batch.append(document)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.search.build_index",
        description="Rebuild the Meilisearch index atomically from the POI export.",
    )
    parser.add_argument(
        "--input", type=Path, default=settings.data_dir / "exports" / "pois_index.jsonl"
    )
    parser.add_argument("--url", default=settings.meili_url)
    parser.add_argument("--key", default=settings.meili_key)
    parser.add_argument("--index", default=settings.meili_index)
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument(
        "--min-documents",
        type=int,
        default=100,
        help="refuse to swap in an index smaller than this (a truncated export would empty search)",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    if not args.input.exists():
        log.error("no index export at %s; run pipeline.pois.export_geojson first", args.input)
        return 1

    staging = f"{args.index}_build"
    try:
        with MeiliAdmin(args.url, args.key) as meili:
            if not meili.health():
                log.error("meilisearch is not reachable at %s", args.url)
                return 1

            # Start from a clean staging index so a previous failed run cannot
            # leave stale documents behind to be swapped in.
            meili.delete_index(staging)
            meili.create_index(staging)
            meili.update_settings(staging, INDEX_SETTINGS)
            meili.update_settings(staging, {"synonyms": build_synonyms()})

            total = 0
            tasks: list[int] = []
            for batch in batched(read_documents(args.input), args.batch_size):
                ndjson = "\n".join(json.dumps(document, ensure_ascii=False) for document in batch)
                tasks.append(meili.add_documents(staging, ndjson))
                total += len(batch)
                log.info("queued %d documents (%d total)", len(batch), total)

            for task in tasks:
                meili.wait(task)

            indexed = meili.document_count(staging)
            log.info("staging index holds %d documents", indexed)
            if indexed < args.min_documents:
                log.error(
                    "refusing to publish an index with %d documents (minimum %d); "
                    "the live index is untouched",
                    indexed,
                    args.min_documents,
                )
                return 1

            meili.create_index(args.index)
            meili.swap(args.index, staging)
            meili.delete_index(staging)
            log.info("swapped %d documents into %s", indexed, args.index)
    except Exception:
        log.exception("index build failed; the live index is untouched")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
