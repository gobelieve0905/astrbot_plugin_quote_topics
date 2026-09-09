"""Local message index. Chat text remains in AstrBot's conversation database."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Topic:
    cid: str
    owner: str


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise RuntimeError("Unsupported quote topics database version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                scope TEXT NOT NULL, cid TEXT NOT NULL, owner TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(scope, cid)
            );
            CREATE TABLE IF NOT EXISTS messages (
                scope TEXT NOT NULL, mid TEXT NOT NULL, cid TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(scope, mid),
                FOREIGN KEY(scope,cid) REFERENCES topics(scope,cid) ON DELETE CASCADE
            );
            PRAGMA user_version=1;
        """)

    def lookup(self, scope: str, mid: str) -> Topic | None:
        row = self.db.execute(
            "SELECT t.cid,t.owner FROM messages m JOIN topics t "
            "ON t.scope=m.scope AND t.cid=m.cid WHERE m.scope=? AND m.mid=?",
            (scope, mid),
        ).fetchone()
        return Topic(*row) if row else None

    def bind(self, scope: str, mid: str, topic: Topic, kind: str):
        if not mid or kind not in ("input", "reply"):
            raise ValueError("Invalid message binding")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO topics VALUES(?,?,?,?)",
                (scope, topic.cid, topic.owner, time.time()),
            )
            existing = self.lookup(scope, mid)
            if existing and existing != topic:
                raise ValueError("Message already belongs to another topic")
            self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)",
                (scope, mid, topic.cid, kind, time.time()),
            )

    def close(self):
        self.db.close()
