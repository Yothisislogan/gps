"""Check generated credentials using the same hash format nginx consumes."""

from __future__ import annotations

import subprocess

import pytest

from scripts.prepare_deploy import write_admin_auth


def test_generated_password_hash_verifies_and_can_be_rotated(tmp_path):
    destination = tmp_path / "secrets" / "admin.htpasswd"
    for password in ("first-test-password", "replacement-test-password"):
        write_admin_auth("moderator", password, destination)
        record = destination.read_text().strip()
        user, hashed = record.split(":", 1)
        assert user == "moderator"
        assert password not in record
        salt = hashed.split("$")[2]
        verified = subprocess.run(
            ["openssl", "passwd", "-apr1", "-salt", salt, "-stdin"],
            input=password + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        assert verified.stdout.strip() == hashed


@pytest.mark.parametrize(
    "user,password", [("admin", ""), ("admin:other", "test"), ("admin", "a\nb")]
)
def test_invalid_credentials_do_not_replace_the_working_file(tmp_path, user, password):
    destination = tmp_path / "admin.htpasswd"
    destination.write_text("existing hash")
    with pytest.raises(ValueError):
        write_admin_auth(user, password, destination)
    assert destination.read_text() == "existing hash"
