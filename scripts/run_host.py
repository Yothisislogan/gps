#!/usr/bin/env python3
"""Run a pipeline command with the host equivalents of the Compose settings."""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def host_environment(env_file: Path):
    env_file = env_file.resolve()
    values = {k: v for k, v in dotenv_values(env_file, interpolate=False).items() if v is not None}
    env = {**values, **os.environ, "NICANAV_ENV_FILE": str(env_file)}
    for key in (
        "NICANAV_DATA_DIR",
        "NICANAV_TILES_DIR",
        "NICANAV_VALHALLA_DIR",
        "NICANAV_METADATA_DIR",
    ):
        if env.get(key):
            path = Path(env[key])
            env[key] = (
                str((env_file.parent / path).resolve()) if not path.is_absolute() else str(path)
            )
    env.setdefault("NICANAV_PG_HOST", "127.0.0.1")
    env.setdefault("NICANAV_PG_PORT", env.get("NICANAV_PG_BIND_PORT", "5432"))
    env.setdefault("NICANAV_MEILI_URL", "http://127.0.0.1:" + env.get("NICANAV_MEILI_PORT", "7700"))
    env.setdefault(
        "NICANAV_VALHALLA_URL", "http://127.0.0.1:" + env.get("NICANAV_VALHALLA_PORT", "8002")
    )
    return env


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Pass the command to run")
    env_file = Path(os.environ.get("NICANAV_ENV_FILE", ROOT / "infra/.env"))
    raise SystemExit(
        subprocess.run(sys.argv[1:], cwd=ROOT, env=host_environment(env_file)).returncode
    )
