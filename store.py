"""SQLite persistence so missions (and their undo data) survive restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from macos import STATE_DIR


class MissionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_DIR / "missions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS missions ("
            "id TEXT PRIMARY KEY, created_at REAL NOT NULL, data TEXT NOT NULL)"
        )
        self._db.commit()

    def save(self, mission: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO missions (id, created_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (mission["id"], mission["created_at"], json.dumps(mission)),
            )
            self._db.commit()

    def get(self, mission_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM missions WHERE id = ?", (mission_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM missions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM missions ORDER BY created_at DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, mission_ids: list[str]) -> int:
        if not mission_ids:
            return 0
        with self._lock:
            deleted = self._db.executemany("DELETE FROM missions WHERE id = ?", [(i,) for i in mission_ids]).rowcount
            self._db.commit()
        return deleted


class RoutineStore:
    """Approved plans that replay on a schedule; same database file as missions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_DIR / "missions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS routines ("
            "id TEXT PRIMARY KEY, created_at REAL NOT NULL, data TEXT NOT NULL)"
        )
        self._db.commit()

    def save(self, routine: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO routines (id, created_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (routine["id"], routine["created_at"], json.dumps(routine)),
            )
            self._db.commit()

    def get(self, routine_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM routines WHERE id = ?", (routine_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM routines ORDER BY created_at").fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, routine_id: str) -> bool:
        with self._lock:
            deleted = self._db.execute("DELETE FROM routines WHERE id = ?", (routine_id,)).rowcount
            self._db.commit()
        return bool(deleted)
