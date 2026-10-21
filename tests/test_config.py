from pathlib import Path
from contextlib import closing

from weather_bot.config import PROJECT_ROOT, Settings


def test_default_database_stays_at_project_root_from_other_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Path(Settings().database) == PROJECT_ROOT / "bot.sqlite3"


def test_database_environment_default_and_relative_override(monkeypatch):
    monkeypatch.setattr("weather_bot.config.load_dotenv", lambda: None)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert Settings.from_env().database == str(PROJECT_ROOT / "bot.sqlite3")
    monkeypatch.setenv("DATABASE_PATH", "logs/ci/override.sqlite3")
    assert Settings.from_env().database == str(PROJECT_ROOT / "logs/ci/override.sqlite3")


def test_ci_database_is_separate_and_marked(ci_database_path):
    import sqlite3
    path = Path(ci_database_path)
    assert path.parent == PROJECT_ROOT / "logs/ci/databases"
    assert path != Path(Settings().database)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0x43495453


def test_local_tunnel_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr("weather_bot.config.load_dotenv", lambda: None)
    monkeypatch.delenv("LOCAL_TUNNEL_ENABLED", raising=False)
    assert Settings.from_env().local_tunnel_enabled is True
    monkeypatch.setenv("LOCAL_TUNNEL_ENABLED", "false")
    assert Settings.from_env().local_tunnel_enabled is False
