"""Tests for the Meilisearch indexer.

Driven entirely by httpx.MockTransport: the point is to pin the *protocol* — the
verbs, the task-waiting and the atomic swap — because getting any of those wrong
produces an index that looks fine and is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pipeline.search.build_index import INDEX_SETTINGS, MeiliAdmin, batched, main, read_documents
from pipeline.search.synonyms import SYNONYM_GROUPS, build_synonyms


class FakeMeili:
    """A Meilisearch stand-in that records the calls and finishes every task."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.documents: dict[str, list[dict]] = {}
        self.settings: dict[str, dict] = {}
        self.swaps: list[list[str]] = []
        self.deleted: list[str] = []
        self.next_task = 0
        self.doc_count_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))

        if path == "/health":
            return httpx.Response(200, json={"status": "available"})
        if path.startswith("/tasks/"):
            return httpx.Response(200, json={"status": "succeeded", "uid": 1})
        if request.method == "POST" and path == "/indexes":
            self.next_task += 1
            self.documents.setdefault(json.loads(request.content)["uid"], [])
            return httpx.Response(202, json={"taskUid": self.next_task})
        if request.method == "DELETE" and path.startswith("/indexes/"):
            self.deleted.append(path.split("/")[2])
            self.next_task += 1
            return httpx.Response(202, json={"taskUid": self.next_task})
        if request.method == "PATCH" and path.endswith("/settings"):
            index = path.split("/")[2]
            self.settings.setdefault(index, {}).update(json.loads(request.content))
            self.next_task += 1
            return httpx.Response(202, json={"taskUid": self.next_task})
        if request.method == "POST" and path.endswith("/documents"):
            index = path.split("/")[2]
            body = request.content.decode()
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
            self.documents.setdefault(index, []).extend(rows)
            self.next_task += 1
            return httpx.Response(202, json={"taskUid": self.next_task})
        if path.endswith("/stats"):
            index = path.split("/")[2]
            count = (
                self.doc_count_override
                if self.doc_count_override is not None
                else len(self.documents.get(index, []))
            )
            return httpx.Response(200, json={"numberOfDocuments": count})
        if request.method == "POST" and path == "/swap-indexes":
            self.swaps.append(json.loads(request.content)[0]["indexes"])
            self.next_task += 1
            return httpx.Response(202, json={"taskUid": self.next_task})
        return httpx.Response(404, json={"message": f"unhandled {path}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake_meili(monkeypatch: pytest.MonkeyPatch) -> FakeMeili:
    fake = FakeMeili()
    original_init = MeiliAdmin.__init__

    def patched(self, base_url, api_key="", *, timeout=60.0):
        original_init(self, base_url, api_key, timeout=timeout)
        self._client = fake.client()

    monkeypatch.setattr(MeiliAdmin, "__init__", patched)
    return fake


def write_documents(path: Path, count: int) -> None:
    lines = [
        json.dumps(
            {
                "id": f"poi:{i}",
                "kind": "poi",
                "name": f"Fritanga {i}",
                "lat": 12.1,
                "lon": -86.2,
                "_geo": {"lat": 12.1, "lng": -86.2},
            }
        )
        for i in range(count)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSettings:
    def test_geo_is_filterable_and_sortable(self):
        # Position bias is what makes "farmacia" useful: the nearest pharmacy,
        # not an alphabetical list of every pharmacy in the country.
        assert "_geo" in INDEX_SETTINGS["filterableAttributes"]
        assert "_geo" in INDEX_SETTINGS["sortableAttributes"]

    def test_popularity_only_breaks_ties(self):
        rules = INDEX_SETTINGS["rankingRules"]
        assert rules[-1] == "popularity:desc"
        assert rules[:6] == ["words", "typo", "proximity", "attribute", "sort", "exactness"]

    def test_typo_tolerance_is_loosened_for_accentless_typing(self):
        sizes = INDEX_SETTINGS["typoTolerance"]["minWordSizeForTypos"]
        assert sizes["twoTypos"] <= 8, "long Spanish place names typed without accents need this"

    def test_searchable_attributes_match_the_documents_the_exporter_writes(self):
        from pipeline.pois.export_geojson import to_index_document

        document = to_index_document(
            {"id": "a", "name": "X", "category": "otro", "lat": 12.1, "lon": -86.2}
        )
        for attribute in INDEX_SETTINGS["searchableAttributes"]:
            assert attribute in document, f"{attribute} is searchable but never written"


class TestSynonyms:
    def test_expansion_is_symmetric(self):
        synonyms = build_synonyms()
        assert "gasolinera" in synonyms["bomba"]
        assert "bomba" in synonyms["gasolinera"]

    def test_nicaraguan_vocabulary_is_present(self):
        synonyms = build_synonyms()
        # A Nicaraguan looking for fuel types "bomba"; tyre repair is "vulca".
        for term in ("bomba", "vulca", "pulperia", "botica", "cajero"):
            assert term in synonyms, f"{term} is how people actually search"

    def test_no_group_maps_a_term_to_itself(self):
        for term, values in build_synonyms().items():
            assert term not in values

    def test_groups_have_at_least_two_members(self):
        for group in SYNONYM_GROUPS:
            assert len(group) >= 2


class TestBatching:
    def test_batches(self):
        assert [len(b) for b in batched(iter([{}] * 25), 10)] == [10, 10, 5]

    def test_empty(self):
        assert list(batched(iter([]), 10)) == []

    def test_reads_documents_skipping_blank_lines(self, tmp_path: Path):
        path = tmp_path / "docs.jsonl"
        path.write_text('{"id": 1}\n\n{"id": 2}\n', encoding="utf-8")
        assert [d["id"] for d in read_documents(path)] == [1, 2]


class TestIndexBuild:
    def test_builds_into_staging_and_swaps(self, tmp_path: Path, fake_meili: FakeMeili):
        path = tmp_path / "pois_index.jsonl"
        write_documents(path, 150)

        assert main(["--input", str(path), "--index", "nicanav"]) == 0

        # Documents go into the staging index, never straight into the live one:
        # search must never be briefly empty during a nightly rebuild.
        assert len(fake_meili.documents["nicanav_build"]) == 150
        assert fake_meili.documents.get("nicanav") in (None, [])
        assert ["nicanav", "nicanav_build"] in fake_meili.swaps
        assert "nicanav_build" in fake_meili.deleted

    def test_settings_use_patch_not_post(self, tmp_path: Path, fake_meili: FakeMeili):
        path = tmp_path / "pois_index.jsonl"
        write_documents(path, 150)
        main(["--input", str(path), "--index", "nicanav"])
        # The engine accepts PATCH; the published OpenAPI says POST and yields
        # 405s, which read as a silent no-op.
        assert any(method == "PATCH" for method, _ in fake_meili.calls)
        assert not any(
            method == "POST" and p.endswith("/settings") for method, p in fake_meili.calls
        )

    def test_synonyms_are_applied(self, tmp_path: Path, fake_meili: FakeMeili):
        path = tmp_path / "pois_index.jsonl"
        write_documents(path, 150)
        main(["--input", str(path), "--index", "nicanav"])
        assert "bomba" in fake_meili.settings["nicanav_build"]["synonyms"]

    def test_refuses_to_publish_a_truncated_index(self, tmp_path: Path, fake_meili: FakeMeili):
        path = tmp_path / "pois_index.jsonl"
        write_documents(path, 5)
        # A truncated export must not empty search for the whole country.
        assert main(["--input", str(path), "--index", "nicanav", "--min-documents", "100"]) == 1
        assert fake_meili.swaps == []

    def test_stale_staging_index_is_cleared_first(self, tmp_path: Path, fake_meili: FakeMeili):
        path = tmp_path / "pois_index.jsonl"
        write_documents(path, 150)
        main(["--input", str(path), "--index", "nicanav"])
        # Deleted before the build as well as after, so a previous failed run
        # cannot leave documents behind to be swapped in.
        assert fake_meili.deleted.count("nicanav_build") == 2

    def test_missing_input_is_an_error_not_a_crash(self, tmp_path: Path, fake_meili: FakeMeili):
        assert main(["--input", str(tmp_path / "nope.jsonl")]) == 1
        assert fake_meili.swaps == []


class TestTaskWaiting:
    def test_a_failed_task_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "failed", "error": {"code": "bad"}})

        admin = MeiliAdmin("http://meili:7700")
        admin._client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(RuntimeError, match="failed"):
            admin.wait(1)

    def test_waiting_on_nothing_is_a_no_op(self):
        assert MeiliAdmin("http://meili:7700").wait(None) == {}

    def test_health_is_false_when_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        admin = MeiliAdmin("http://meili:7700")
        admin._client = httpx.Client(transport=httpx.MockTransport(handler))
        assert admin.health() is False
