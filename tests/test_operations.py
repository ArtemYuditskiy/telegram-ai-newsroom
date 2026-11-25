"""Verify health endpoints and online SQLite snapshots."""

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_ai_newsroom.cli.main import app
from telegram_ai_newsroom.web.app import create_app
from telegram_ai_newsroom.web.auth import HASHER


def test_health_and_request_correlation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "newsroom.db"
    caplog.set_level(logging.INFO, logger="uvicorn.error.newsroom")
    with TestClient(create_app(path, HASHER.hash("integration-password"))) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert not path.exists()
        path.touch()
        assert client.get("/health/ready").json()["database"] == "unavailable"
        result = CliRunner().invoke(app, ["db", "upgrade", "--db", str(path)])
        assert result.exit_code == 0, result.output
        assert client.get("/health/ready").status_code == 200
        response = client.get("/login?private=secret-fixture")
        payload = json.loads(caplog.records[-1].message)
        assert payload["request_id"] == response.headers["x-request-id"]
        assert payload["route"] == "/login" and payload["status"] == 200
        assert "secret-fixture" not in caplog.text


def test_backup_contains_committed_wal_and_refuses_overwrite(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "live.db"
    snapshot = tmp_path / "snapshot.db"
    restored = tmp_path / "restored.db"
    assert runner.invoke(app, ["db", "upgrade", "--db", str(source)]).exit_code == 0
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO feed_sources (url, hosts) VALUES ('https://example.org', '[]')")
        conn.commit()
        assert source.with_suffix(".db-wal").exists()
        assert runner.invoke(app, ["db", "backup", str(source), str(snapshot)]).exit_code == 0
    assert runner.invoke(app, ["db", "backup", str(source), str(snapshot)]).exit_code != 0
    assert runner.invoke(app, ["db", "backup", str(snapshot), str(restored)]).exit_code == 0
    status = runner.invoke(app, ["db", "status", "--db", str(restored)])
    assert status.exit_code == 0, status.output
    report = json.loads(status.output)
    assert report["feeds"][0]["url"] == "https://example.org"
    assert report["publications"] == {}
    assert report["reported_cost_usd"] == "0"
    missing = runner.invoke(app, ["db", "status", "--db", str(tmp_path / "missing")])
    assert missing.exit_code == 1 and json.loads(missing.output)["database"] == "unavailable"
    assert (
        runner.invoke(app, ["db", "backup", str(restored), str(tmp_path / "no" / "file")]).exit_code
        == 1
    )
