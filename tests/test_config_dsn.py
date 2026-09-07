"""Settings: composing the database URL from parts.

The regression these guard: a generated password routinely contains characters
that a URL cannot carry raw. `openssl rand -base64 32` emits `/` and `+`, and a
`/` truncates a postgresql:// URL at the database name — silently, producing a
connection to the wrong database or none at all. Compose additionally
interpolates `$` inside an env file, so a base64 secret can arrive truncated
before anything else gets a look at it.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import pytest

from common.config import Settings


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


class TestComposition:
    def test_defaults(self):
        assert settings().database_url == "postgresql://nicanav:nicanav@postgis:5432/nicanav"

    def test_parts_are_used(self):
        url = settings(
            pg_host="db.internal", pg_port=6543, pg_db="otro", pg_user="mapa", pg_password="x"
        ).database_url
        parsed = urlparse(url)
        assert (parsed.hostname, parsed.port, parsed.path) == ("db.internal", 6543, "/otro")
        assert parsed.username == "mapa"

    @pytest.mark.parametrize(
        "password",
        [
            "abc/def",            # base64 emits this, and it truncates the URL
            "abc+def",
            "abc=def==",
            "p@ssw0rd",           # an @ starts a new authority section
            "with?query#frag",
            "sp ace",
            "back\\slash",
            "acentuada-ñ",
            "$dollar$sign",
        ],
    )
    def test_awkward_passwords_survive_a_round_trip(self, password: str):
        parsed = urlparse(settings(pg_password=password).database_url)
        assert unquote(parsed.password or "") == password
        # The host and database must still be what we asked for — that is what a
        # raw `/` or `@` in the password would have quietly broken.
        assert parsed.hostname == "postgis"
        assert parsed.path == "/nicanav"

    def test_password_is_encoded_exactly_once(self):
        # Double-encoding is the other half of this bug: it authenticates
        # against nothing and the error says "password authentication failed".
        parsed = urlparse(settings(pg_password="a/b").database_url)
        assert parsed.password == "a%2Fb"
        assert unquote(parsed.password) == "a/b"

    def test_username_is_encoded_too(self):
        parsed = urlparse(settings(pg_user="user@host").database_url)
        assert unquote(parsed.username or "") == "user@host"


class TestOverride:
    def test_an_explicit_url_wins(self):
        url = "postgresql://someone:else@elsewhere:5432/other"
        assert settings(database_url=url, pg_password="ignored").database_url == url

    def test_environment_variable_is_read(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("NICANAV_PG_PASSWORD", "from-the-environment")
        parsed = urlparse(Settings(_env_file=None).database_url)
        assert unquote(parsed.password or "") == "from-the-environment"
