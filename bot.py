"""Telegram booking and AI consultation bot powered by aiogram 3, SQLite, and Gemini."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

from ai_consultant import GeminiConsultant, KnowledgeBase
from storage import BookingRecord, BookingStore

BASE_DIR = Path(__file__).resolve().parent


class BookingStates(StatesGroup):
    """Finite-state machine steps for collecting a client booking."""

    choosing_service = State()
    entering_name = State()
    entering_contact = State()
    entering_time = State()
    entering_comment = State()
    confirming = State()


STATUS_LABELS = {
    "new": "🟡 Новая",
    "confirmed": "🟢 Подтверждена",
    "cancelled": "🔴 Отменена",
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent bottom navigation keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Записаться / Заявка"), KeyboardButton(text="📋 Услуги и цены")],
            [KeyboardButton(text="🤖 Вопрос ИИ"), KeyboardButton(text="🗂 Мои заявки")],
        ],
        resize_keyboard=True,
    )


def services_inline_keyboard(kb: KnowledgeBase) -> InlineKeyboardMarkup:
    """Build inline buttons for picking a service in the booking flow."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{svc.name} ({svc.price})" if svc.price else svc.name,
                callback_data=f"book_svc:{svc.id}",
            )
        ]
        for svc in kb.services
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_booking_keyboard() -> InlineKeyboardMarkup:
    """Build confirm/cancel inline controls for the booking summary step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="book_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel"),
            ]
        ]
    )


def admin_booking_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Build inline moderation buttons sent to the manager on each new booking."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"admin_status:{booking_id}:confirmed"
                ),
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"admin_status:{booking_id}:cancelled"
                ),
            ]
        ]
    )


def format_booking_card(record: BookingRecord) -> str:
    """Render an HTML-safe booking summary card."""
    status_label = STATUS_LABELS.get(record.status, record.status)
    uname = f" (@{html.escape(record.username)})" if record.username else ""
    comment_line = (
        f"\n💬 <b>Комментарий:</b> {html.escape(record.comment)}"
        if record.comment
        else ""
    )
    return (
        f"<b>Заявка #{record.id}</b> · {status_label}\n"
        f"🛠 <b>Услуга:</b> {html.escape(record.service_name)}\n"
        f"👤 <b>Клиент:</b> {html.escape(record.client_name)}{uname}\n"
        f"📞 <b>Контакт:</b> {html.escape(record.contact)}\n"
        f"🕒 <b>Удобное время:</b> {html.escape(record.preferred_time)}"
        f"{comment_line}"
    )


def _is_admin(user_id: int, admin_chat_id: int | None) -> bool:
    return admin_chat_id is not None and user_id == admin_chat_id


def create_dispatcher(
    *,
    store: BookingStore,
    knowledge: KnowledgeBase,
    consultant: GeminiConsultant | None,
    admin_chat_id: int | None,
) -> Dispatcher:
    """Wire all message and callback handlers onto an aiogram Dispatcher."""
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            f"Здравствуйте! Вы в боте <b>{html.escape(knowledge.business_name)}</b>.\n\n"
            "Здесь можно посмотреть услуги, записаться на удобное время или задать вопрос AI-консультанту.",
            reply_markup=main_menu_keyboard(),
        )

    @dp.message(Command("reset"))
    async def cmd_reset(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user:
            store.clear_dialog_history(message.from_user.id)
        await message.answer("Контекст диалога и текущая форма сброшены.", reply_markup=main_menu_keyboard())

    @dp.message(F.text == "📋 Услуги и цены")
    async def show_services(message: Message) -> None:
        await message.answer(knowledge.format_catalog_text(), reply_markup=main_menu_keyboard())

    @dp.message(F.text == "🗂 Мои заявки")
    async def show_my_bookings(message: Message) -> None:
        if not message.from_user:
            return
        items = store.list_bookings(user_id=message.from_user.id, limit=5)
        if not items:
            await message.answer("У вас пока нет заявок. Нажмите «📝 Записаться / Заявка».")
            return
        text = "\n\n".join(format_booking_card(item) for item in items)
        await message.answer(text)

    @dp.message(F.text == "🤖 Вопрос ИИ")
    async def prompt_ai(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Напишите любой вопрос по услугам, срокам или бюджету — я отвечу на основе каталога студии."
        )

    @dp.message(Command("book"))
    @dp.message(F.text == "📝 Записаться / Заявка")
    async def start_booking(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not knowledge.services:
            await message.answer("Каталог услуг пока пуст.")
            return
        await state.set_state(BookingStates.choosing_service)
        await message.answer(
            "Выберите услугу из списка:",
            reply_markup=services_inline_keyboard(knowledge),
        )

    @dp.callback_query(F.data == "book_cancel")
    async def cancel_booking(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if callback.message:
            await callback.message.edit_text("Оформление заявки отменено.")
        await callback.answer()

    @dp.callback_query(BookingStates.choosing_service, F.data.startswith("book_svc:"))
    async def select_service(callback: CallbackQuery, state: FSMContext) -> None:
        service_id = (callback.data or "").split(":", 1)[1]
        svc = knowledge.get_service(service_id)
        if svc is None:
            await callback.answer("Услуга не найдена", show_alert=True)
            return
        await state.update_data(service_id=svc.id, service_name=svc.name)
        await state.set_state(BookingStates.entering_name)
        if callback.message:
            await callback.message.edit_text(
                f"Выбрана услуга: <b>{html.escape(svc.name)}</b>\n\nКак к вам обращаться? Напишите имя:"
            )
        await callback.answer()

    @dp.message(BookingStates.entering_name, F.text)
    async def enter_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if len(name) < 2:
            await message.answer("Введите корректное имя (от 2 символов):")
            return
        await state.update_data(client_name=name)
        await state.set_state(BookingStates.entering_contact)
        await message.answer("Укажите контакт для связи (Telegram @username, телефон или email):")

    @dp.message(BookingStates.entering_contact, F.text)
    async def enter_contact(message: Message, state: FSMContext) -> None:
        contact = (message.text or "").strip()
        if len(contact) < 3:
            await message.answer("Укажите корректный контакт (телефон, @username или email):")
            return
        await state.update_data(contact=contact)
        await state.set_state(BookingStates.entering_time)
        await message.answer("Укажите удобную дату/время (например: «завтра после 15:00»):")

    @dp.message(BookingStates.entering_time, F.text)
    async def enter_time(message: Message, state: FSMContext) -> None:
        preferred = (message.text or "").strip()
        if not preferred:
            await message.answer("Напишите удобное время или «в любое время»:")
            return
        await state.update_data(preferred_time=preferred)
        await state.set_state(BookingStates.entering_comment)
        await message.answer("Добавьте краткое описание задачи (или отправьте «-» чтобы пропустить):")

    @dp.message(BookingStates.entering_comment, F.text)
    async def enter_comment(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        comment = "" if raw == "-" else raw
        await state.update_data(comment=comment)
        data = await state.get_data()
        await state.set_state(BookingStates.confirming)

        preview = (
            "<b>Проверьте данные заявки:</b>\n"
            f"🛠 <b>Услуга:</b> {html.escape(data['service_name'])}\n"
            f"👤 <b>Имя:</b> {html.escape(data['client_name'])}\n"
            f"📞 <b>Контакт:</b> {html.escape(data['contact'])}\n"
            f"🕒 <b>Время:</b> {html.escape(data['preferred_time'])}\n"
            f"💬 <b>Комментарий:</b> {html.escape(comment or '—')}"
        )
        await message.answer(preview, reply_markup=confirm_booking_keyboard())

    @dp.callback_query(BookingStates.confirming, F.data == "book_confirm")
    async def confirm_booking(
        callback: CallbackQuery, state: FSMContext, bot: Bot
    ) -> None:
        data = await state.get_data()
        user = callback.from_user
        record = store.create_booking(
            user_id=user.id,
            username=user.username or "",
            service_id=data["service_id"],
            service_name=data["service_name"],
            client_name=data["client_name"],
            contact=data["contact"],
            preferred_time=data["preferred_time"],
            comment=data.get("comment", ""),
        )
        await state.clear()
        if callback.message:
            await callback.message.edit_text(
                f"✅ Заявка #{record.id} принята! Менеджер свяжется с вами в ближайшее время."
            )
        if admin_chat_id is not None:
            try:
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text="📥 <b>Новая заявка!</b>\n\n" + format_booking_card(record),
                    reply_markup=admin_booking_keyboard(record.id),
                )
            except Exception as exc:
                logging.warning("Failed to notify admin %s: %s", admin_chat_id, exc)
        await callback.answer("Заявка сохранена")

    @dp.callback_query(F.data.startswith("admin_status:"))
    async def admin_update_status(callback: CallbackQuery, bot: Bot) -> None:
        if not _is_admin(callback.from_user.id, admin_chat_id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Некорректные данные", show_alert=True)
            return
        booking_id = int(parts[1])
        new_status = parts[2]
        updated = store.update_status(booking_id, new_status)
        if updated is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if callback.message:
            await callback.message.edit_text(format_booking_card(updated))
        try:
            await bot.send_message(
                chat_id=updated.user_id,
                text=f"Статус вашей заявки #{updated.id} обновлён:\n\n{format_booking_card(updated)}",
            )
        except Exception as exc:
            logging.warning("Failed to notify user %s: %s", updated.user_id, exc)
        await callback.answer("Статус обновлён")

    @dp.message(Command("bookings"))
    async def admin_list_bookings(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, admin_chat_id):
            await message.answer("Команда доступна только администратору.")
            return
        items = store.list_bookings(limit=10)
        if not items:
            await message.answer("База заявок пуста.")
            return
        await message.answer("\n\n".join(format_booking_card(item) for item in items))

    @dp.message(Command("export"))
    async def admin_export_csv(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, admin_chat_id):
            await message.answer("Команда доступна только администратору.")
            return
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = store.export_csv(Path(tmp_dir) / "bookings_export.csv")
            await message.answer_document(FSInputFile(csv_path))

    @dp.message(F.text)
    async def handle_ai_question(message: Message, bot: Bot) -> None:
        if not message.from_user or not message.text:
            return
        if consultant is None:
            await message.answer(
                "AI-консультант не настроен (не задан GEMINI_API_KEY). "
                "Используйте кнопку «📝 Записаться / Заявка»."
            )
            return

        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        history = store.get_dialog_history(message.from_user.id, limit=10)
        try:
            reply = await consultant.answer(message.text, history=history)
        except Exception as exc:
            logging.exception("AI consultation error: %s", exc)
            await message.answer(
                "Не удалось получить ответ от ИИ. Попробуйте позже или оформите заявку кнопкой ниже."
            )
            return

        store.add_dialog_turn(message.from_user.id, message.text.strip(), reply)
        for idx in range(0, len(reply), 4000):
            await message.answer(reply[idx : idx + 4000])

    return dp


async def healthcheck(_request: web.Request) -> web.Response:
    """Railway / load-balancer health endpoint."""
    return web.Response(text="OK")


def main() -> None:
    """Load configuration and run the bot in webhook or polling mode."""
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set.")

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    admin_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    admin_chat_id = int(admin_raw) if admin_raw else None

    db_path = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "bookings.db")))
    kb_path = Path(os.getenv("KNOWLEDGE_PATH", str(BASE_DIR / "knowledge.example.json")))

    knowledge = KnowledgeBase.from_file(kb_path)
    store = BookingStore(db_path)
    consultant = (
        GeminiConsultant(api_key=gemini_key, model=gemini_model, knowledge=knowledge)
        if gemini_key
        else None
    )

    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dispatcher(
        store=store,
        knowledge=knowledge,
        consultant=consultant,
        admin_chat_id=admin_chat_id,
    )

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    webhook_base = os.getenv("WEBHOOK_URL", "").strip() or (
        f"https://{railway_domain}" if railway_domain else ""
    )
    if webhook_base:
        webhook_path = "/webhook"
        webhook_secret = (
            os.getenv("WEBHOOK_SECRET", "").strip()
            or hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:32]
        )
        port = int(os.getenv("PORT", "8080"))

        async def on_startup(active_bot: Bot) -> None:
            target = f"{webhook_base.rstrip('/')}{webhook_path}"
            await active_bot.set_webhook(target, secret_token=webhook_secret)

        dp.startup.register(on_startup)
        app = web.Application()
        app.router.add_get("/", healthcheck)
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=webhook_secret
        ).register(app, path=webhook_path)
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=port)
    else:
        asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
