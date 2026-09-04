"""Tests for the pipeline I/O layer — mostly about not publishing broken files."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pipeline.common.io import (
    Timer,
    atomic_output,
    atomic_write_bytes,
    atomic_write_text,
    ensure_dir,
    human_bytes,
    read_geojsonseq,
    require_binary,
    run,
    write_geojson,
    write_geojsonseq,
)


class TestAtomicOutput:
    def test_publishes_on_success(self, tmp_path: Path):
        target = tmp_path / "tiles" / "base.pmtiles"
        with atomic_output(target) as tmp:
            tmp.write_bytes(b"tiles")
        assert target.read_bytes() == b"tiles"
        assert not tmp.exists()

    def test_leaves_previous_file_untouched_on_failure(self, tmp_path: Path):
        target = tmp_path / "base.pmtiles"
        target.write_bytes(b"yesterday")
        with pytest.raises(RuntimeError), atomic_output(target) as tmp:
            tmp.write_bytes(b"half a file")
            raise RuntimeError("planetiler died")
        # The live file must still be yesterday's *complete* build, not a stub.
        assert target.read_bytes() == b"yesterday"
        assert not target.with_name("base.pmtiles.tmp").exists()

    def test_refuses_to_publish_when_nothing_was_written(self, tmp_path: Path):
        target = tmp_path / "base.pmtiles"
        target.write_bytes(b"yesterday")
        with pytest.raises(FileNotFoundError), atomic_output(target):
            pass  # a build step that silently produced no output
        assert target.read_bytes() == b"yesterday"

    def test_temp_file_sits_next_to_the_target(self, tmp_path: Path):
        # Same directory => same filesystem => os.replace is actually atomic.
        target = tmp_path / "nested" / "out.bin"
        with atomic_output(target) as tmp:
            assert tmp.parent == target.parent
            tmp.write_bytes(b"x")

    def test_clears_a_stale_temp_file_from_a_killed_build(self, tmp_path: Path):
        target = tmp_path / "out.bin"
        stale = tmp_path / "out.bin.tmp"
        stale.write_bytes(b"stale garbage from a killed run")
        with atomic_output(target) as tmp:
            assert not tmp.exists()
            tmp.write_bytes(b"fresh")
        assert target.read_bytes() == b"fresh"

    def test_replaces_atomically_over_an_existing_file(self, tmp_path: Path):
        target = tmp_path / "out.bin"
        atomic_write_bytes(target, b"one")
        atomic_write_bytes(target, b"two")
        assert target.read_bytes() == b"two"


class TestWriters:
    def test_atomic_write_text_roundtrip(self, tmp_path: Path):
        path = atomic_write_text(tmp_path / "a.txt", "Güegüense")
        assert path.read_text(encoding="utf-8") == "Güegüense"

    def test_write_geojson_keeps_accents_unescaped(self, tmp_path: Path):
        target = tmp_path / "gazetteer.geojson"
        write_geojson(
            target,
            [
                {
                    "type": "Feature",
                    "properties": {"name": "Rotonda El Güegüense"},
                    "geometry": {"type": "Point", "coordinates": [-86.2807, 12.1352]},
                }
            ],
            name="gazetteer",
        )
        raw = target.read_text(encoding="utf-8")
        assert "Güegüense" in raw, "ensure_ascii would mangle Spanish names in tiles"
        parsed = json.loads(raw)
        assert parsed["type"] == "FeatureCollection"
        assert parsed["name"] == "gazetteer"
        assert len(parsed["features"]) == 1

    def test_geojsonseq_roundtrip(self, tmp_path: Path):
        target = tmp_path / "pois.geojsonl"
        features = [{"type": "Feature", "properties": {"i": i}, "geometry": None} for i in range(5)]
        assert write_geojsonseq(target, features) == 5
        assert list(read_geojsonseq(target)) == features

    def test_geojsonseq_reads_osmium_record_separators(self, tmp_path: Path):
        # osmium export -f geojsonseq writes RFC 8142 (0x1e-prefixed) records.
        target = tmp_path / "osmium.geojsonseq"
        target.write_text('\x1e{"type": "Feature", "id": 1}\n\x1e{"type": "Feature", "id": 2}\n')
        assert [f["id"] for f in read_geojsonseq(target)] == [1, 2]

    def test_geojsonseq_skips_blank_lines(self, tmp_path: Path):
        target = tmp_path / "sparse.geojsonl"
        target.write_text('{"type": "Feature"}\n\n\n{"type": "Feature"}\n')
        assert len(list(read_geojsonseq(target))) == 2

    def test_geojsonseq_is_atomic(self, tmp_path: Path):
        target = tmp_path / "pois.geojsonl"
        write_geojsonseq(target, [{"type": "Feature", "properties": {}, "geometry": None}])

        def exploding():
            yield {"type": "Feature", "properties": {}, "geometry": None}
            raise ValueError("conflation blew up mid-export")

        with pytest.raises(ValueError):
            write_geojsonseq(target, exploding())
        assert len(list(read_geojsonseq(target))) == 1


class TestProcessHelpers:
    def test_run_returns_output(self):
        result = run(["python3", "-c", "print('hola')"], capture=True)
        assert result.stdout.strip() == "hola"

    def test_run_raises_on_failure(self):
        with pytest.raises(subprocess.CalledProcessError):
            run(["python3", "-c", "import sys; sys.exit(3)"], capture=True)

    def test_run_can_ignore_failure(self):
        assert run(["python3", "-c", "import sys; sys.exit(3)"], check=False).returncode == 3

    def test_run_merges_env_instead_of_replacing_it(self):
        result = run(
            [
                "python3",
                "-c",
                "import os; print(os.environ.get('NICANAV_X'), bool(os.environ.get('PATH')))",
            ],
            env={"NICANAV_X": "1"},
            capture=True,
        )
        assert result.stdout.strip() == "1 True"

    def test_require_binary_finds_python(self):
        assert os.path.basename(require_binary("python3")).startswith("python3")

    def test_require_binary_explains_itself(self):
        with pytest.raises(FileNotFoundError, match="apt install osmium-tool"):
            require_binary("osmium-that-does-not-exist", hint="apt install osmium-tool")


class TestMisc:
    def test_ensure_dir_is_idempotent(self, tmp_path: Path):
        target = tmp_path / "a" / "b"
        assert ensure_dir(target) == ensure_dir(target) == target
        assert target.is_dir()

    @pytest.mark.parametrize(
        ("size", "expected"), [(0, "0 B"), (512, "512 B"), (1536, "1.5 KB"), (1234567, "1.2 MB")]
    )
    def test_human_bytes(self, size: int, expected: str):
        assert human_bytes(size) == expected

    def test_timer_records_elapsed_and_reraises(self):
        with Timer("noop") as timer:
            pass
        assert timer.seconds >= 0.0
        with pytest.raises(RuntimeError), Timer("boom"):
            raise RuntimeError("x")
