import pytest

from discord_mcp.config import Config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for k in [
        "DISCORD_HEADLESS",
        "DISCORD_READ_ONLY",
        "DISCORD_POOL_SIZE",
        "XDG_CONFIG_HOME",
    ]:
        monkeypatch.delenv(k, raising=False)


class TestConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # no .env
        config = Config.load()
        assert config.headless is True
        assert config.read_only is True
        assert config.pool_size == 4
        assert config.keyring_service == "discord-mcp"
        assert config.keyring_user == "session-key"

    def test_session_path_respects_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        config = Config.load()
        assert config.session_path == tmp_path / "discord-mcp" / "session.enc"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
            ("yes", True),
            ("no", False),
        ],
    )
    def test_bool_parsing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, raw: str, expected: bool
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DISCORD_HEADLESS", raw)
        assert Config.load().headless is expected

    def test_pool_size_clamped_to_minimum(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DISCORD_POOL_SIZE", "0")
        assert Config.load().pool_size == 1

    def test_invalid_int_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DISCORD_POOL_SIZE", "not-a-number")
        assert Config.load().pool_size == 4
