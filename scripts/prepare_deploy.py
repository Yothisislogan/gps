#!/usr/bin/env python3
"""Render public configuration and nginx credentials from the deployment environment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.config import get_settings  # noqa: E402
from scripts.render_web_config import main as render_config  # noqa: E402


def write_admin_auth(user: str, password: str, destination: Path) -> None:
    """Use nginx's supported APR1 hash; the password never enters argv or logs."""
    if not user or not user.isascii() or any(c in user for c in ":\r\n"):
        raise ValueError("NICANAV_ADMIN_USER must be ASCII without colons or newlines")
    if not password or any(c in password for c in "\r\n"):
        raise ValueError("NICANAV_ADMIN_PASSWORD must be set and contain no newlines")
    result = subprocess.run(
        ["openssl", "passwd", "-apr1", "-stdin"],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    hashed = result.stdout.strip()
    if not hashed.startswith("$apr1$"):
        raise ValueError("openssl did not produce an APR1 password hash")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # nginx's unprivileged worker must read this hash through the read-only mount.
    fd, name = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(f"{user}:{hashed}\n")
        os.chmod(name, 0o644)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def main() -> int:
    settings = get_settings()
    try:
        write_admin_auth(
            settings.admin_user,
            settings.admin_password,
            REPO_ROOT / "infra" / "secrets" / "admin.htpasswd",
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Cannot prepare admin authentication: {exc}", file=sys.stderr)
        return 1
    return render_config([])


if __name__ == "__main__":
    raise SystemExit(main())
