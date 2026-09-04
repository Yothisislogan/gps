"""Filesystem, subprocess and logging helpers shared by every pipeline job.

Two rules this module exists to enforce:

1. **Nothing is ever built in place.**  nginx serves ``base.pmtiles`` while the
   nightly build writes ``base.pmtiles.tmp``; only a completed, fsynced file is
   ``os.replace``\\ d over the live one.  A half-written PMTiles does not fail
   loudly — it serves corrupt tiles — so atomicity is not optional.
2. **External commands are logged and checked.**  A silently failing
   ``osmium``/``planetiler``/``tippecanoe`` invocation would publish yesterday's
   data as today's, which is worse than a red cron mail.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "Timer",
    "atomic_output",
    "atomic_write_bytes",
    "atomic_write_text",
    "ensure_dir",
    "human_bytes",
    "read_geojsonseq",
    "require_binary",
    "run",
    "setup_logging",
    "write_geojson",
    "write_geojsonseq",
]

log = logging.getLogger(__name__)


def setup_logging(level: str | int = "INFO", *, stream=None) -> None:
    """Configure root logging once, in a format cron mail can be read in."""
    logging.basicConfig(
        level=level if isinstance(level, int) else getattr(logging, str(level).upper(), 20),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=stream or sys.stderr,
        force=True,
    )


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    """``mkdir -p`` returning the Path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def human_bytes(size: float) -> str:
    """``1234567`` -> ``"1.2 MB"`` — for build logs, not for arithmetic."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


class Timer:
    """Context manager that logs how long a build step took.

    >>> with Timer("planetiler"):      # doctest: +SKIP
    ...     run(["java", "-jar", "planetiler.jar"])
    """

    def __init__(self, label: str, logger: logging.Logger | None = None) -> None:
        self.label = label
        self.log = logger or log
        self.seconds = 0.0

    def __enter__(self) -> Timer:
        self._start = time.monotonic()
        self.log.info("%s: start", self.label)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.seconds = time.monotonic() - self._start
        if exc_type is None:
            self.log.info("%s: done in %.1fs", self.label, self.seconds)
        else:
            self.log.error("%s: failed after %.1fs (%s)", self.label, self.seconds, exc)


def require_binary(name: str, *, hint: str = "") -> str:
    """Resolve an external tool or die with an actionable message.

    The pipeline shells out to ``osmium``, ``java``, ``tippecanoe`` and
    ``pg_dump``; discovering a missing one halfway through a nightly build wastes
    the whole run, so jobs call this up front.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(
            f"required binary {name!r} not found on PATH" + (f" — {hint}" if hint else "")
        )
    return resolved


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command, logging it first.

    ``env`` is *merged* into the current environment rather than replacing it,
    because the pipeline needs PATH and JAVA_HOME to survive.
    """
    printable = " ".join(str(part) for part in cmd)
    log.info("$ %s", printable)
    merged = {**os.environ, **dict(env or {})}
    result = subprocess.run(
        [str(part) for part in cmd],
        check=False,
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        if capture:
            log.error("command failed (%s): %s", result.returncode, (result.stderr or "").strip())
        raise subprocess.CalledProcessError(
            result.returncode, printable, result.stdout, result.stderr
        )
    return result


@contextlib.contextmanager
def atomic_output(path: str | os.PathLike[str], *, suffix: str = ".tmp") -> Iterator[Path]:
    """Yield a temporary path that is moved onto ``path`` only on success.

    The temp file is created in the *same directory* so the final move is a
    rename within one filesystem (``os.replace`` is atomic there; a cross-device
    move is not).  On any exception the temp file is removed and the previously
    published file is left untouched.

    >>> with atomic_output("data/tiles/base.pmtiles") as tmp:   # doctest: +SKIP
    ...     build_tiles(output=tmp)
    """
    target = Path(path)
    ensure_dir(target.parent)
    tmp = target.with_name(target.name + suffix)
    if tmp.exists():
        if tmp.is_dir():
            shutil.rmtree(tmp)
        else:
            tmp.unlink()
    try:
        yield tmp
        if not tmp.exists():
            raise FileNotFoundError(f"{tmp} was never written; refusing to publish {target}")
        if tmp.is_file():
            with open(tmp, "rb") as handle:
                os.fsync(handle.fileno())
        os.replace(tmp, target)
        log.info(
            "published %s (%s)",
            target,
            human_bytes(target.stat().st_size) if target.is_file() else "directory",
        )
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            if tmp.is_dir():
                shutil.rmtree(tmp)
            else:
                tmp.unlink()
        raise


def atomic_write_bytes(path: str | os.PathLike[str], payload: bytes) -> Path:
    """Write bytes atomically; returns the published path."""
    target = Path(path)
    with atomic_output(target) as tmp:
        tmp.write_bytes(payload)
    return target


def atomic_write_text(path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8") -> Path:
    """Write text atomically; returns the published path."""
    return atomic_write_bytes(path, text.encode(encoding))


def write_geojson(
    path: str | os.PathLike[str],
    features: Iterable[Mapping[str, Any]],
    *,
    name: str | None = None,
) -> Path:
    """Write a FeatureCollection atomically.

    Used for exports small enough to hold in memory (the gazetteer, closures).
    For the POI export — hundreds of thousands of features that stream straight
    into tippecanoe — use :func:`write_geojsonseq`.
    """
    collection: dict[str, Any] = {"type": "FeatureCollection", "features": list(features)}
    if name:
        collection["name"] = name
    return atomic_write_text(path, json.dumps(collection, ensure_ascii=False))


def write_geojsonseq(path: str | os.PathLike[str], features: Iterable[Mapping[str, Any]]) -> int:
    """Write newline-delimited GeoJSON features atomically; returns the count.

    ``tippecanoe`` reads this format directly and it never needs the whole
    dataset resident, which matters on a small box.
    """
    count = 0
    with atomic_output(path) as tmp, open(tmp, "w", encoding="utf-8") as handle:
        for feature in features:
            handle.write(json.dumps(feature, ensure_ascii=False))
            handle.write("\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def read_geojsonseq(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Iterate newline-delimited GeoJSON, tolerating the RS-prefixed variant.

    ``osmium export -f geojsonseq`` emits RFC 8142 records prefixed with the
    record-separator byte ``\\x1e``; plain ``.geojsonl`` files do not.  Both are
    read here so callers do not care which producer wrote the file.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip().lstrip("\x1e").strip()
            if stripped:
                yield json.loads(stripped)
