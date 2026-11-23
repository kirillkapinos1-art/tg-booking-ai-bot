"""Unit tests for BookingStore, KnowledgeBase, and GeminiConsultant."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from typing import Any

import pytest

from ai_consultant import GeminiConsultant, KnowledgeBase
from storage import BookingStore


def test_booking_store_crud_status_and_csv_export(tmp_path: Path) -> None:
    store = BookingStore(tmp_path / "bookings.db")

    rec = store.create_booking(
        user_id=1001,
        username="client_user",
        service_id="tg_bot",
        service_name="Telegram-бот под ключ",
        client_name="Алексей",
        contact="@alex_dev",
        preferred_time="Завтра 16:00",
        comment="Нужен бот записи + админка",
    )
    assert rec.id == 1
    assert rec.status == "new"

    fetched = store.get_booking(rec.id)
    assert fetched is not None
    assert fetched.client_name == "Алексей"

    updated = store.update_status(rec.id, "confirmed")
    assert updated is not None
    assert updated.status == "confirmed"

    with pytest.raises(ValueError):
        store.update_status(rec.id, "invalid_status")

    store.create_booking(
        user_id=1002,
        service_id="autocad_drawing",
        service_name="2D/3D чертёж в AutoCAD",
        client_name="Мария",
        contact="+380001112233",
        preferred_time="Пт 12:00",
    )
    assert len(store.list_bookings(user_id=1001)) == 1
    assert len(store.list_bookings()) == 2

    csv_file = store.export_csv(tmp_path / "export" / "bookings.csv")
    assert csv_file.exists()
    with csv_file.open("r", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 3
    assert rows[1][5] == "Алексей"
    assert rows[1][9] == "confirmed"


def test_dialog_history_trimming_and_clear(tmp_path: Path) -> None:
    store = BookingStore(tmp_path / "bookings.db")
    store.add_dialog_turn(55, "Сколько стоит бот?", "От $50.")
    store.add_dialog_turn(55, "А сроки?", "2–5 дней.")

    history = store.get_dialog_history(55, limit=10)
    assert [turn["role"] for turn in history] == ["user", "model", "user", "model"]
    assert history[0]["parts"] == [{"text": "Сколько стоит бот?"}]

    store.clear_dialog_history(55)
    assert store.get_dialog_history(55) == []


def test_knowledge_base_and_gemini_consultant(tmp_path: Path) -> None:
    kb_file = tmp_path / "kb.json"
    kb_file.write_text(
        json.dumps(
            {
                "business_name": "Test Studio",
                "description": "Bots & 3D",
                "working_hours": "10-18",
                "services": [
                    {
                        "id": "bot",
                        "name": "AI Bot",
                        "price": "$50",
                        "duration": "3 days",
                        "summary": "Custom Telegram bot",
                    }
                ],
                "faq": [{"q": "Сроки?", "a": "От 3 дней."}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    kb = KnowledgeBase.from_file(kb_file)
    assert kb.get_service("bot") is not None
    assert kb.get_service("missing") is None
    assert "AI Bot" in kb.format_catalog_text()
    assert "Test Studio" in kb.build_system_prompt()

    captured: dict[str, Any] = {}

    async def fake_transport(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        return {
            "candidates": [
                {"content": {"parts": [{"text": "Стоимость AI Bot — от $50, срок 3 дня."}]}}
            ]
        }

    consultant = GeminiConsultant(
        api_key="test_key",
        model="gemini-2.5-flash",
        knowledge=kb,
        transport=fake_transport,
    )
    reply = asyncio.run(consultant.answer("Сколько стоит бот?"))
    assert "$50" in reply
    assert "gemini-2.5-flash:generateContent" in captured["url"]
    assert "Test Studio" in captured["payload"]["system_instruction"]["parts"][0]["text"]
