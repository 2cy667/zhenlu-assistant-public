from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import create_app


def test_checksum_verified_and_retransmission_preserves_audio_and_transcript(client, settings):
    sid = client.post("/api/sessions", json={"patient_name": "虚构上传"}).json()["id"]
    content = b"test-audio-bytes"
    checksum = hashlib.sha256(content).hexdigest()
    url = f"/api/sessions/{sid}/audio"
    bad = client.post(url, files={"file": ("sample.wav", content)}, headers={"X-Audio-SHA256": "0" * 64})
    assert bad.status_code == 400 and bad.json()["code"] == "audio_checksum_mismatch"
    assert list((settings.upload_dir / sid).iterdir()) == []
    first = client.post(url, files={"file": ("sample.wav", content)}, headers={"X-Audio-SHA256": checksum})
    assert first.status_code == 201
    audio = first.json()["audio"]
    assert audio["sha256"] == checksum
    assert client.post(f"/api/sessions/{sid}/transcript").status_code == 200
    retry = client.post(url, files={"file": ("sample.wav", content)}, headers={"X-Audio-SHA256": checksum})
    assert retry.status_code == 201 and retry.json()["audio"] == audio
    assert retry.json()["status"] == "transcribed"
    assert len(list((settings.upload_dir / sid).iterdir())) == 1
    corrupt_retry = client.post(url, files={"file": ("sample.wav", b"corrupt")}, headers={"X-Audio-SHA256": checksum})
    assert corrupt_retry.status_code == 400
    assert client.get(url).content == content


def test_upgrade_has_consistent_backup_and_idempotent_schema(settings):
    settings.job_worker_enabled = False
    with TestClient(create_app(settings)) as client:
        sid = client.post("/api/sessions", json={"patient_name": "虚构迁移病例"}).json()["id"]
        client.post(f"/api/sessions/{sid}/transcript/manual", json={"text": "患者：咳嗽。"})
        draft = client.post(f"/api/sessions/{sid}/draft").json()["draft"]
        before = client.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": draft["revision"]}).json()
    path = Path(settings.database_url.removeprefix("sqlite:///"))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE audio_files DROP COLUMN sha256")
        db.execute("DROP TABLE schema_migrations")
        db.execute("""CREATE TABLE old_sessions (
            id VARCHAR(32) PRIMARY KEY, patient_name VARCHAR(80) NOT NULL, patient_ref VARCHAR(120),
            status VARCHAR(32) NOT NULL, error_message TEXT, confirmed_version INTEGER NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)""")
        db.execute("""INSERT INTO old_sessions SELECT id, patient_name, patient_ref, status,
            error_message, confirmed_version, created_at, updated_at FROM consultation_sessions""")
        db.execute("DROP TABLE consultation_sessions")
        db.execute("ALTER TABLE old_sessions RENAME TO consultation_sessions")
        for table in ("consultation_item_actions", "consultation_items", "patients"):
            db.execute(f"DROP TABLE {table}")
        for table in ("advisor_runs", "advisor_states", "transcript_batches"):
            db.execute(f"DROP TABLE {table}")
        db.execute("ALTER TABLE transcripts DROP COLUMN revision")
        db.execute("ALTER TABLE transcripts DROP COLUMN state")
        db.commit()
    with TestClient(create_app(settings)) as client:
        after = client.get(f"/api/sessions/{sid}").json()
        assert after["patient_id"] is None
        assert client.get(f"/api/sessions/{sid}/follow-up-summary").json()["previous_record"] is None
        assert after["transcript"] == before["transcript"]
        assert after["latest_record"] == before["latest_record"]
    backups = list((path.parent / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT patient_name FROM consultation_sessions WHERE id=?", (sid,)).fetchone()[0] == "虚构迁移病例"
        assert "sha256" not in [r[1] for r in backup.execute("PRAGMA table_info(audio_files)")]
        assert "revision" not in [r[1] for r in backup.execute("PRAGMA table_info(transcripts)")]
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/sessions/{sid}").status_code == 200
        with client.app.state.db.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert len(list((path.parent / "backups").glob("*.sqlite3"))) == 1


def test_backup_includes_committed_wal_and_never_overwrites(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE demo (value TEXT)")
        source.execute("INSERT INTO demo VALUES ('committed')")
        source.commit()
        database = Database(f"sqlite:///{path.as_posix()}")
        target = tmp_path / "saved.sqlite3"
        try:
            database.backup(target)
            with sqlite3.connect(target) as backup:
                assert backup.execute("SELECT value FROM demo").fetchone()[0] == "committed"
            with pytest.raises(FileExistsError):
                database.backup(target)
        finally:
            database.dispose()
