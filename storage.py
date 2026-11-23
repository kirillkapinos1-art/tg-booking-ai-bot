"""SQLite persistence layer for bookings, statuses, CSV export, and AI dialog history."""

from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

VALID_BOOKING_STATUSES = ("new", "confirmed", "cancelled")


@dataclass(frozen=True, slots=True)
class BookingRecord:
    """Represents a single customer service booking stored in SQLite."""

    id: int
    user_id: int
    username: str
    service_id: str
    service_name: str
    client_name: str
    contact: str
    preferred_time: str
    comment: str
    status: str
    created_at: str


class BookingStore:
    """Thread-safe SQLite repository for bookings and per-user AI conversation context."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        parent = Path(self.db_path).resolve().parent
        os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    service_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    preferred_time TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (user_id, id DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dialog_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dialog_user ON dialog_messages (user_id, id DESC)"
            )

    @staticmethod
    def _row_to_booking(row: sqlite3.Row) -> BookingRecord:
        return BookingRecord(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            service_id=str(row["service_id"]),
            service_name=str(row["service_name"]),
            client_name=str(row["client_name"]),
            contact=str(row["contact"]),
            preferred_time=str(row["preferred_time"]),
            comment=str(row["comment"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
        )

    def create_booking(
        self,
        *,
        user_id: int,
        username: str = "",
        service_id: str,
        service_name: str,
        client_name: str,
        contact: str,
        preferred_time: str,
        comment: str = "",
    ) -> BookingRecord:
        """Insert a new booking and return the created record."""
        clean_name = client_name.strip()
        clean_contact = contact.strip()
        clean_time = preferred_time.strip()
        if not clean_name or not clean_contact or not clean_time:
            raise ValueError("Client name, contact, and preferred time are required.")

        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO bookings (
                    user_id, username, service_id, service_name,
                    client_name, contact, preferred_time, comment, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')
                """,
                (
                    int(user_id),
                    username.strip(),
                    service_id.strip(),
                    service_name.strip(),
                    clean_name,
                    clean_contact,
                    clean_time,
                    comment.strip(),
                ),
            )
            booking_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_booking(row)

    def get_booking(self, booking_id: int) -> BookingRecord | None:
        """Fetch a booking by primary key ID."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (int(booking_id),)
            ).fetchone()
        return self._row_to_booking(row) if row else None

    def list_bookings(
        self, *, user_id: int | None = None, limit: int = 20
    ) -> list[BookingRecord]:
        """List recent bookings, optionally filtered to a single Telegram user."""
        safe_limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as conn:
            if user_id is not None:
                rows = conn.execute(
                    "SELECT * FROM bookings WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                    (int(user_id), safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bookings ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [self._row_to_booking(r) for r in rows]

    def update_status(self, booking_id: int, status: str) -> BookingRecord | None:
        """Update booking status ('new', 'confirmed', 'cancelled') and return updated row."""
        clean_status = status.strip().lower()
        if clean_status not in VALID_BOOKING_STATUSES:
            raise ValueError(f"Unsupported booking status: {status!r}")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE bookings SET status = ? WHERE id = ?",
                (clean_status, int(booking_id)),
            )
            row = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (int(booking_id),)
            ).fetchone()
        return self._row_to_booking(row) if row else None

    def export_csv(self, output_path: str | Path) -> Path:
        """Export all bookings ordered chronologically to a UTF-8 CSV file."""
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM bookings ORDER BY id ASC").fetchall()

        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "id",
                    "user_id",
                    "username",
                    "service_id",
                    "service_name",
                    "client_name",
                    "contact",
                    "preferred_time",
                    "comment",
                    "status",
                    "created_at",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["id"],
                        row["user_id"],
                        row["username"],
                        row["service_id"],
                        row["service_name"],
                        row["client_name"],
                        row["contact"],
                        row["preferred_time"],
                        row["comment"],
                        row["status"],
                        row["created_at"],
                    ]
                )
        return target

    def add_dialog_turn(self, user_id: int, user_text: str, model_reply: str) -> None:
        """Persist one user/model exchange for AI assistant context."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO dialog_messages (user_id, role, text) VALUES (?, 'user', ?)",
                (int(user_id), user_text),
            )
            conn.execute(
                "INSERT INTO dialog_messages (user_id, role, text) VALUES (?, 'model', ?)",
                (int(user_id), model_reply),
            )

    def get_dialog_history(
        self, user_id: int, *, limit: int = 12
    ) -> list[dict[str, object]]:
        """Return recent Gemini-formatted dialog turns in chronological order."""
        safe_limit = max(2, min(int(limit), 40))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT role, text FROM dialog_messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(user_id), safe_limit),
            ).fetchall()
        turns: list[dict[str, object]] = [
            {"role": str(r["role"]), "parts": [{"text": str(r["text"])}]}
            for r in reversed(rows)
        ]
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        return turns

    def clear_dialog_history(self, user_id: int) -> None:
        """Delete AI dialog history for a specific user."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM dialog_messages WHERE user_id = ?", (int(user_id),)
            )
