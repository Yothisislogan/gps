#!/usr/bin/env python3
"""Build isolated data generations and promote them through one nginx switch.

No production resources are provisioned by importing this module. The operator
must initialize a registry for an existing, verified server before refresh.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def atomic(path: Path, content: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def command(args, *, env=None, cwd=None, input=None, output=None):
    # Child output may include database connection details. Keep failure logs in
    # the protected release directory; do not echo subprocess arguments/secrets.
    result = subprocess.run(
        args,
        env=env,
        cwd=cwd,
        input=input,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError(
            f"{Path(args[0]).name} failed (exit {result.returncode}); inspect the protected build log"
        )
    return result.stdout


def environment(release):
    values = {
        k: v for k, v in dotenv_values(release["env"], interpolate=False).items() if v is not None
    }
    return {
        **os.environ,
        **values,
        "NICANAV_ENV_FILE": release["env"],
        "NICANAV_COMPOSE_FILE": release["compose"],
        "COMPOSE_PROJECT_NAME": release["project"],
    }


def compose(release, *args, **kwargs):
    return command(
        [
            "docker",
            "compose",
            "-p",
            release["project"],
            "-f",
            release["compose"],
            "--env-file",
            release["env"],
            *args,
        ],
        env=environment(release),
        **kwargs,
    )


def sql(release, statement):
    env = environment(release)
    return (
        compose(
            release,
            "exec",
            "-T",
            "postgis",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-U",
            env.get("NICANAV_PG_USER", "nicanav"),
            "-d",
            env.get("NICANAV_PG_DB", "nicanav"),
            "-c",
            statement,
        )
        .decode()
        .strip()
    )


def revision(release):
    return int(sql(release, "SELECT revision FROM release_revision WHERE id=1"))


def require_revision(expected: int, actual: int):
    if expected != actual:
        raise RuntimeError(
            "New corrections arrived after the snapshot. Candidate was not published; refresh again from current data."
        )


def gate_counts(previous: int, candidate: int, max_drop: float = 0.1):
    if candidate < 100 or (previous and candidate < previous * (1 - max_drop)):
        raise RuntimeError(
            f"Candidate coverage failed: {candidate} indexed places, previously {previous}"
        )


def gate_routes(previous, candidate):
    results = candidate.get("results", [])
    if not results or candidate.get("blocking_failures", 0) or any(r.get("error") for r in results):
        raise RuntimeError("Candidate route checks failed or are missing")
    if previous is None:
        return
    current = {r["id"]: r for r in results}
    for old in previous.get("results", []):
        new = current.get(old["id"])
        if new is None:
            raise RuntimeError("Candidate is missing a previous route check")
        for field in ("distance_km", "duration_min"):
            before, after = old.get(field), new.get(field)
            if before and (not after or abs(after / before - 1) > 0.25):
                raise RuntimeError(
                    f"Route {old['id']} changed {field} by more than 25%; review before publication"
                )


@contextmanager
def pause_writes(release):
    state = Path(release["state"])
    marker = state / "writes-paused"
    already_paused = marker.exists()
    marker.touch()
    with (state / "writes.lock").open("rb") as lock:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    if not already_paused:
                        marker.unlink(missing_ok=True)
                    raise RuntimeError(
                        "Existing writes did not drain; promotion canceled"
                    ) from None
                time.sleep(0.1)
        lease = {"keep": already_paused}
        try:
            yield lease
        finally:
            if not lease["keep"]:
                marker.unlink(missing_ok=True)


def gateway(releases, active):
    """Only numeric loopback ports and generated IDs enter nginx configuration."""
    blocks = []
    headers = "gzip off; proxy_buffering off; proxy_max_temp_file_size 0; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; proxy_set_header X-Forwarded-Proto $scheme;"
    for item in releases.values():
        rid, asset = item["id"], item["asset"]
        port = int(item["port"])
        if (
            not re.fullmatch(r"[a-z0-9-]+", rid)
            or not re.fullmatch(r"[a-f0-9]{20}", asset)
            or not 1024 <= port <= 65535
        ):
            raise ValueError("Invalid generation metadata")
        blocks.append(
            f"location /data-releases/{rid}/ {{ proxy_pass http://127.0.0.1:{port}/; {headers} }}"
        )
        blocks.append(
            f"location /releases/{asset}/ {{ proxy_pass http://127.0.0.1:{port}; {headers} }}"
        )
    port = int(releases[active]["port"])
    blocks.append("location /data-releases/ { return 410; }")
    blocks.append(f"location / {{ proxy_pass http://127.0.0.1:{port}; {headers} }}")
    return "\n".join(blocks) + "\n"


def install_gateway(registry, releases, active):
    path = Path(registry["gateway"])
    old = path.read_text() if path.exists() else ""
    atomic(path, gateway(releases, active), 0o644)
    try:
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
    except Exception:
        atomic(path, old, 0o644)
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
        raise


def wait_ready(release, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sql(release, "SELECT 1")
            return
        except RuntimeError:
            time.sleep(2)
    raise RuntimeError("Candidate database did not become ready")


def env_text(values):
    # Single quotes prevent Compose from expanding dollar signs in passwords.
    return (
        "\n".join(
            key + "='" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
            for key, value in values.items()
        )
        + "\n"
    )


def prepare_compose(release, values):
    state = Path(release["state"])
    state.mkdir(parents=True)
    os.chmod(state, 0o755)
    (state / "writes.lock").touch(mode=0o644)
    (state / "writes-paused").touch()
    atomic(
        Path(release["env"]),
        env_text(values),
    )
    override = state / "override.json"
    atomic(
        override, json.dumps({"services": {"api": {"volumes": [f"{state}:/srv/release-state:ro"]}}})
    )
    combined = command(
        [
            "docker",
            "compose",
            "-p",
            release["project"],
            "-f",
            str(Path(release["code"]) / "infra/docker-compose.yml"),
            "-f",
            str(override),
            "--env-file",
            release["env"],
            "config",
            "--format",
            "json",
        ],
        env={**os.environ, **values},
    )
    # The normalized compose includes resolved secrets. This file stays mode 600.
    atomic(Path(release["compose"]), combined.decode().replace("$", "$$"))


def available_ports():
    # Reserve four unused ports for the candidate's loopback services. Docker
    # still detects a race with another process before any publication occurs.
    sockets, ports = [], []
    try:
        for _ in range(4):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        return ports
    finally:
        for sock in sockets:
            sock.close()


def build_candidate(registry, registry_path):
    active = registry["releases"][registry["active"]]
    stamp = datetime.now(UTC).strftime("r%Y%m%dt%H%M%S")
    root = registry_path.parent / stamp
    if command(["git", "-C", str(ROOT), "status", "--porcelain"]).strip():
        raise RuntimeError("Commit code changes before building a release")
    if len(registry["releases"]) >= 3:
        for rid in list(registry["releases"]):
            if rid not in {registry["active"], registry.get("previous")}:
                retire(registry, rid, registry_path)
    if len(registry["releases"]) >= 3:
        raise RuntimeError(
            "Keep the existing generations for at least 24 hours before another build"
        )
    root.mkdir(mode=0o700)
    code, data, state = root / "code", root / "data", root / "state"
    commit = command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip()
    command(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(code), commit])
    previous_size = sum(p.stat().st_size for p in Path(active["data"]).rglob("*") if p.is_file())
    if shutil.disk_usage(root).free < previous_size * 2 + 4 * 1024**3:
        raise RuntimeError("Not enough disk for an isolated candidate and rollback copy")
    shutil.copytree(
        active["data"], data, ignore=shutil.ignore_patterns("backups", "*.tmp", ".pipeline.lock")
    )
    port, pg_port, meili_port, route_port = available_ports()
    item = {
        "id": stamp,
        "port": port,
        "code": str(code),
        "data": str(data),
        "state": str(state),
        "project": f"nicanav-{stamp}",
        "env": str(root / ".env"),
        "compose": str(root / "compose.json"),
        "commit": commit,
    }
    values = {
        k: v for k, v in dotenv_values(active["env"], interpolate=False).items() if v is not None
    }
    values.update(
        {
            "NICANAV_DATA_DIR": str(data),
            "NICANAV_TILES_DIR": str(data / "tiles"),
            "NICANAV_VALHALLA_DIR": str(data / "valhalla"),
            "NICANAV_METADATA_DIR": str(data / "metadata"),
            "NICANAV_HTTP_PORT": str(port),
            "NICANAV_PG_BIND_PORT": str(pg_port),
            "NICANAV_MEILI_PORT": str(meili_port),
            "NICANAV_VALHALLA_PORT": str(route_port),
            "NICANAV_PG_HOST": "127.0.0.1",
            "NICANAV_PG_PORT": str(pg_port),
            "NICANAV_MEILI_URL": f"http://127.0.0.1:{meili_port}",
            "NICANAV_VALHALLA_URL": f"http://127.0.0.1:{route_port}",
            "NICANAV_RELEASE_STATE_MOUNT": "/srv/release-state",
            "NICANAV_RELEASE_ID": stamp,
            "NICANAV_WEB_DIR": str(code / "dist/web"),
            "NICANAV_MANAGED_RELEASE": "1",
            "NICANAV_CANDIDATE": "1",
            "NICANAV_VALHALLA_MEMORY": "3g",
            "NICANAV_VALHALLA_CPUS": "2",
            "NICANAV_MEILI_MEMORY": "1536m",
            "NICANAV_MEILI_CPUS": "1",
            "PLANETILER_THREADS": "2",
            "PLANETILER_XMX": "2g",
            "NICANAV_VALHALLA_THREADS": "2",
        }
    )
    # An explicit database URL from the old stack must not override candidate parts.
    values.pop("NICANAV_DATABASE_URL", None)
    prepare_compose(item, values)
    env = environment(item)
    log = root / "build.log"
    try:
        with log.open("wb") as output:
            command(["npm", "ci", "--ignore-scripts"], cwd=code, env=env, output=output)
            command([sys.executable, "scripts/prepare_deploy.py"], cwd=code, env=env, output=output)
            command(["npm", "run", "build"], cwd=code, env=env, output=output)
            compose(item, "up", "-d", "--build", "postgis", "meilisearch", output=output)
            wait_ready(item)
            backup = root / "snapshot.dump"
            old_env = environment(active)
            with backup.open("wb") as stream:
                compose(
                    active,
                    "exec",
                    "-T",
                    "postgis",
                    "pg_dump",
                    "-Fc",
                    "-U",
                    old_env.get("NICANAV_PG_USER", "nicanav"),
                    "-d",
                    old_env.get("NICANAV_PG_DB", "nicanav"),
                    output=stream,
                )
            with backup.open("rb") as stream:
                # stdin uses a file to avoid holding the database dump in memory.
                args = [
                    "docker",
                    "compose",
                    "-p",
                    item["project"],
                    "-f",
                    item["compose"],
                    "--env-file",
                    item["env"],
                    "exec",
                    "-T",
                    "postgis",
                    "pg_restore",
                    "--exit-on-error",
                    "--clean",
                    "--if-exists",
                    "-U",
                    values.get("NICANAV_PG_USER", "nicanav"),
                    "-d",
                    values.get("NICANAV_PG_DB", "nicanav"),
                ]
                result = subprocess.run(args, stdin=stream, stdout=output, stderr=output, env=env)
                if result.returncode:
                    raise RuntimeError("Candidate database restore failed")
            item["snapshot_revision"] = revision(item)
            compose(item, "up", "-d", "--build", output=output)
            # The legacy pipeline now publishes only inside this isolated stack.
            command(["bash", "pipeline/nightly.sh"], cwd=code, env=env, output=output)
            command(["bash", "pipeline/monthly_overture.sh"], cwd=code, env=env, output=output)
            command(
                ["bash", "scripts/verify_deploy.sh", f"http://127.0.0.1:{port}"],
                cwd=code,
                env=env,
                output=output,
            )
        item["asset"] = json.loads((code / "dist/web/release.json").read_text())["version"]
        previous_count = int(sql(active, "SELECT count(*) FROM poi WHERE status != 'closed'"))
        candidate_count = int(sql(item, "SELECT count(*) FROM poi WHERE status != 'closed'"))
        gate_counts(previous_count, candidate_count)
        qa = data / "qa/golden-nightly.json"
        previous_qa = Path(active["data"]) / "qa/golden-nightly.json"
        gate_routes(
            json.loads(previous_qa.read_text()) if previous_qa.exists() else None,
            json.loads(qa.read_text()),
        )
        item["publication_revision"] = revision(item)
        item["validated_at"] = datetime.now(UTC).isoformat()
        atomic(root / "candidate.json", json.dumps(item, indent=2))
        return item
    except Exception:
        with suppress(RuntimeError):
            compose(item, "stop")
        raise


def promote(registry, item, registry_path, *, rollback=False):
    active = registry["releases"][registry["active"]]
    previous = registry["active"]
    releases = {**registry["releases"], item["id"]: item}
    require_revision(item["publication_revision"], revision(item))
    with pause_writes(active) as lease:
        expected = active["publication_revision"] if rollback else item["snapshot_revision"]
        require_revision(expected, revision(active))
        if rollback:
            require_revision(item["publication_revision"], revision(item))
        install_gateway(registry, releases, item["id"])
        try:
            response = httpx.get(registry["public_url"] + "/api/readyz", timeout=15)
            if (
                response.status_code != 200
                or response.headers.get("x-nicanav-release") != item["id"]
            ):
                raise RuntimeError("Public smoke check did not reach the candidate generation")
            item["promoted_at"] = datetime.now(UTC).isoformat()
            registry.update({"active": item["id"], "previous": previous, "releases": releases})
            atomic(registry_path, json.dumps(registry, indent=2))
            lease["keep"] = True
        except Exception:
            install_gateway(registry, releases, previous)
            raise
    # The old generation remains read-only for in-progress sessions. Both its
    # API and its tile URLs stay mapped to the same snapshot.
    (Path(item["state"]) / "writes-paused").unlink(missing_ok=True)


def wait_for_generation(url, rid):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200 and response.headers.get("x-nicanav-release") == rid:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("Initial HTTPS generation readiness failed")


def initialize(args, registry_path):
    """Adopt an already imported stack, preserving its database volume/project."""
    if registry_path.exists():
        raise ValueError("A registry already exists; initialization will not overwrite it")
    if not args.env_file or not args.public_url or not args.gateway:
        raise ValueError("init requires --env-file, --public-url and --gateway")
    if not args.public_url.startswith("https://"):
        raise ValueError("Use the verified HTTPS app origin")
    for unit in ("nightly", "overture", "expire", "golden", "backup"):
        check = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", f"nicanav-{unit}.timer"], capture_output=True
        )
        if check.returncode == 0:
            raise RuntimeError("Disable the legacy nicanav timers before adoption")
    values = {
        k: v for k, v in dotenv_values(args.env_file, interpolate=False).items() if v is not None
    }
    stamp = datetime.now(UTC).strftime("initial%Y%m%dt%H%M%S")
    root = registry_path.parent / stamp
    root.mkdir(mode=0o700)
    data = Path(values.get("NICANAV_DATA_DIR", ROOT / "data"))
    data = (args.env_file.resolve().parent / data).resolve() if not data.is_absolute() else data
    if not (data / "tiles/base.pmtiles").is_file():
        raise ValueError("Import and verify the initial dataset before adopting the stack")
    item = {
        "id": stamp,
        "port": int(values.get("NICANAV_HTTP_PORT", "8400")),
        "code": str(ROOT),
        "data": str(data),
        "project": args.project,
        "state": str(root / "state"),
        "env": str(root / ".env"),
        "compose": str(root / "compose.json"),
    }
    values.update(
        {
            "NICANAV_RELEASE_ID": stamp,
            "NICANAV_RELEASE_STATE_MOUNT": "/srv/release-state",
            "NICANAV_DATA_DIR": str(data),
            "NICANAV_TILES_DIR": str(data / "tiles"),
            "NICANAV_METADATA_DIR": str(data / "metadata"),
            "NICANAV_VALHALLA_DIR": str(data / "valhalla"),
            "NICANAV_WEB_DIR": str(ROOT / "dist/web"),
            "NICANAV_MANAGED_RELEASE": "1",
            "NICANAV_CANDIDATE": "0",
        }
    )
    prepare_compose(item, values)
    # These commands attach the existing Compose project's named volume. The
    # operator supplies its actual project name; never infer a different one.
    atomic(root / "adoption.json", json.dumps(item, indent=2))
    original_web = {
        name: (ROOT / "dist/web" / name).read_bytes()
        for name in ("index.html", "config.js", "sw.js", "manifest.webmanifest", "release.json")
        if (ROOT / "dist/web" / name).exists()
    }
    previous_gateway = (
        args.gateway.read_text()
        if args.gateway.exists()
        else (
            f"location / {{ proxy_pass http://127.0.0.1:{item['port']}; "
            "proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; "
            "proxy_set_header X-Forwarded-Proto $scheme; }\n"
        )
    )
    try:
        sql(item, (ROOT / "db/migrations/004_release_revision.sql").read_text())
        env = environment(item)
        command([sys.executable, "scripts/prepare_deploy.py"], cwd=ROOT, env=env)
        command(["npm", "ci", "--ignore-scripts"], cwd=ROOT, env=env)
        command(["npm", "run", "build"], cwd=ROOT, env=env)
        compose(item, "up", "-d", "--build", "api", "nginx")
        item["asset"] = json.loads((ROOT / "dist/web/release.json").read_text())["version"]
        item["publication_revision"] = revision(item)
        item["promoted_at"] = datetime.now(UTC).isoformat()
        registry = {
            "active": stamp,
            "previous": None,
            "public_url": args.public_url.rstrip("/"),
            "gateway": str(args.gateway.resolve()),
            "releases": {stamp: item},
        }
        install_gateway(registry, registry["releases"], stamp)
        wait_for_generation(registry["public_url"] + "/api/readyz", stamp)
        # The versioned path proves that the host included the generated gateway.
        wait_for_generation(registry["public_url"] + f"/data-releases/{stamp}/api/readyz", stamp)
        atomic(registry_path, json.dumps(registry, indent=2))
        (Path(item["state"]) / "writes-paused").unlink(missing_ok=True)
        print(f"Adopted {stamp}; use the registry for all subsequent maintenance")

    except Exception:
        for name, contents in original_web.items():
            (ROOT / "dist/web" / name).write_bytes(contents)
        atomic(args.gateway, previous_gateway, 0o644)
        command(
            [
                "docker",
                "compose",
                "-p",
                args.project,
                "-f",
                str(ROOT / "infra/docker-compose.yml"),
                "--env-file",
                str(args.env_file.resolve()),
                "up",
                "-d",
                "--build",
                "api",
                "nginx",
            ]
        )
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
        raise


def retire(registry, rid, registry_path):
    if rid in {registry["active"], registry.get("previous")} or rid not in registry["releases"]:
        raise ValueError("Only an older, retained generation can be retired")
    item = registry["releases"][rid]
    age = datetime.now(UTC) - datetime.fromisoformat(item["promoted_at"])
    if age.total_seconds() < 86400:
        raise ValueError("Keep generations for at least 24 hours for open trips")
    releases = {key: value for key, value in registry["releases"].items() if key != rid}
    install_gateway(registry, releases, registry["active"])
    registry["releases"] = releases
    atomic(registry_path, json.dumps(registry, indent=2))
    compose(item, "stop")
    print(f"Retired {rid}; files and database volume retained for recovery")


def maintain(registry, action):
    item = registry["releases"][registry["active"]]
    if action == "expire":
        sql(
            item,
            "UPDATE closure SET active=false WHERE active AND ends_at < now(); "
            "UPDATE report SET status='expired' WHERE status='pending' AND expires_at < now(); "
            "DELETE FROM search_log WHERE created_at < now() - interval '7 days'; "
            "DELETE FROM route_log WHERE created_at < now() - interval '7 days';",
        )
    elif action == "backup":
        command(["bash", "scripts/backup_db.sh"], cwd=item["code"], env=environment(item))
    elif action == "check":
        command(
            ["bash", "scripts/verify_deploy.sh", registry["public_url"]],
            cwd=item["code"],
            env=environment(item),
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry", type=Path, default=Path("/var/lib/nicanav-releases/registry.json")
    )
    parser.add_argument(
        "action",
        choices=[
            "init",
            "build",
            "promote",
            "refresh",
            "rollback",
            "status",
            "retire",
            "backup",
            "expire",
            "check",
        ],
    )
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--public-url")
    parser.add_argument("--gateway", type=Path)
    parser.add_argument("--project", default="nicanav")
    parser.add_argument("--release")
    args = parser.parse_args(argv)
    registry_path = args.registry.resolve()
    registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with registry_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "init":
            initialize(args, registry_path)
            return 0
        registry = json.loads(registry_path.read_text())
        if not registry["public_url"].startswith("https://"):
            raise ValueError("A verified HTTPS public URL is required")
        if args.action == "status":
            print(
                json.dumps(
                    {key: registry.get(key) for key in ["active", "previous", "public_url"]},
                    indent=2,
                )
            )
            return 0
        if args.action == "retire":
            retire(registry, args.release, registry_path)
            return 0
        if args.action in {"backup", "expire", "check"}:
            maintain(registry, args.action)
            return 0
        if args.action in {"build", "refresh"}:
            item = build_candidate(registry, registry_path)
            print(f"Candidate {item['id']} validated")
        elif args.action == "rollback":
            item = registry["releases"][registry["previous"]]
        else:
            if not args.candidate:
                parser.error("--candidate is required for promote")
            item = json.loads(args.candidate.read_text())
            if not item.get("validated_at"):
                raise ValueError("Candidate has not passed validation")
        if args.action != "build":
            promote(registry, item, registry_path, rollback=args.action == "rollback")
            print(f"Serving {item['id']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Release stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from error
