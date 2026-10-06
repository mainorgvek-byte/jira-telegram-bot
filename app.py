"""Jira -> Telegram: FastAPI-сервіс, що приймає вебхуки Jira і пересилає їх у Telegram-групу.

Запуск:
    pip install fastapi uvicorn httpx
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="-1001234567890"
    export WEBHOOK_SECRET="довільний-секрет"        # необов'язково
    export NOTIFY_ONLY_STATUS_CHANGES="false"       # true — лише зміни статусу (перетягування карток)
    uvicorn main:app --host 0.0.0.0 --port 8000

URL вебхука в Jira:
    https://your-domain/webhook/jira?secret=довільний-секрет
"""

import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request

# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")  # ID теми, якщо в групі ввімкнено Topics
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # якщо не задано — перевірка вимкнена

# true — реагувати лише на зміну статусу; інші оновлення (опис, виконавець…) ігнорувати
NOTIFY_ONLY_STATUS_CHANGES = os.getenv("NOTIFY_ONLY_STATUS_CHANGES", "false").lower() in {"1", "true", "yes"}

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Назви подій Jira, які вважаємо створенням / оновленням / видаленням
CREATED_EVENTS = {"jira:issue_created", "issue_created"}
UPDATED_EVENTS = {"jira:issue_updated", "issue_updated", "issue_generic"}
DELETED_EVENTS = {"jira:issue_deleted", "issue_deleted"}

# Емодзі за категорією статусу Jira (status.statusCategory.key)
STATUS_EMOJI = {"new": "📋", "indeterminate": "🔵", "done": "✅"}

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # щоб токен бота не потрапляв у логи
logger = logging.getLogger("jira-telegram")


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------
_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def esc(value: Any) -> str:
    """Екранує спецсимволи MarkdownV2, щоб текст з Jira не ламав розмітку."""
    return _MD_SPECIAL.sub(r"\\\1", str(value))


def dig(data: Any, *path: str, default: Any = None) -> Any:
    """Безпечно йде вглиб JSON: dig(d, 'a', 'b') ~ d['a']['b'], але без KeyError/None-помилок."""
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


def format_date(raw: str | None) -> str:
    """'2026-10-15' -> '15.10.2026'; якщо дедлайну немає — 'не вказано'."""
    if not raw:
        return "не вказано"
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return str(raw)


def shorten(text: str, limit: int = 80) -> str:
    """Обрізає надто довгі значення (наприклад, опис) у списку змін."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def issue_url(issue: dict[str, Any]) -> str | None:
    """Будує посилання на задачу з REST-посилання `self` (…/rest/api/2/issue/10000)."""
    key = issue.get("key")
    self_url = issue.get("self") or ""
    if key and "/rest/api" in self_url:
        return f"{self_url.split('/rest/api')[0]}/browse/{key}"
    return None


def key_link(key: str, url: str | None) -> str:
    """Ключ задачі як клікабельне посилання (MarkdownV2) або просто текст."""
    if not url:
        return esc(key)
    safe_url = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{esc(key)}]({safe_url})"


def status_emoji(fields: dict[str, Any]) -> str:
    """Емодзі статусу: 📋 Зробити, 🔵 В процесі, ✅ Виконано (за категорією статусу)."""
    return STATUS_EMOJI.get(dig(fields, "status", "statusCategory", "key", default=""), "📌")


def find_status_change(payload: dict[str, Any]) -> tuple[str | None, str | None] | None:
    """Шукає в changelog зміну статусу. Повертає (був, став) або None, якщо статус не змінювався."""
    for item in dig(payload, "changelog", "items", default=[]):
        if isinstance(item, dict) and item.get("field") == "status":
            return item.get("fromString"), item.get("toString")
    return None


def format_changes(payload: dict[str, Any], skip: tuple[str, ...] = ()) -> list[str]:
    """Перелік змінених полів з changelog: «поле: було ➜ стало»."""
    lines = []
    for item in dig(payload, "changelog", "items", default=[]):
        if not isinstance(item, dict) or item.get("field") in skip:
            continue
        field = item.get("field") or "поле"
        old = shorten(item.get("fromString") or "—")
        new = shorten(item.get("toString") or "—")
        lines.append(f"• {esc(field)}: {esc(old)} ➜ *{esc(new)}*")
    return lines


# ---------------------------------------------------------------------------
# Формування повідомлень
# ---------------------------------------------------------------------------
def build_message(event: str, payload: dict[str, Any]) -> str | None:
    """Повертає текст сповіщення або None, якщо подію треба ігнорувати."""
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}

    raw_key = issue.get("key") or "—"
    summary = esc(fields.get("summary") or "Без назви")
    status_name = dig(fields, "status", "name")  # актуальний статус: issue.fields.status.name
    deadline = esc(format_date(fields.get("duedate")))
    actor = esc(dig(payload, "user", "displayName", default="Невідомо"))
    key = key_link(raw_key, issue_url(issue))
    emoji = status_emoji(fields)

    # 1. Створення задачі — з початковим статусом
    if event in CREATED_EVENTS:
        author = (
            dig(fields, "creator", "displayName")
            or dig(fields, "reporter", "displayName")
            or dig(payload, "user", "displayName", default="Невідомо")
        )
        return "\n".join([
            "🆕 *Нову задачу створено*",
            "",
            f"🔑 *Ключ:* {key}",
            f"📝 *Назва:* {summary}",
            f"{emoji} *Початковий статус:* {esc(status_name or 'Створено')}",
            f"👤 *Автор:* {esc(author)}",
            f"📅 *Дедлайн:* {deadline}",
        ])

    # 2. Оновлення задачі
    if event in UPDATED_EVENTS:
        status_change = find_status_change(payload)

        # 2а. Змінився статус (картку перетягнули в іншу колонку)
        if status_change:
            old_status, new_status = status_change
            lines = [
                "🔄 *Статус задачі змінено*",
                "",
                f"🔑 *Ключ:* {key}",
                f"📝 *Назва:* {summary}",
                f"{emoji} *Новий статус:* {esc(status_name or new_status or '—')}",
                f"↩️ *Був:* {esc(old_status or '—')}",
                f"👤 *Хто змінив:* {actor}",
                f"📅 *Дедлайн:* {deadline}",
            ]
            other = format_changes(payload, skip=("status",))  # інші зміни в тому ж оновленні
            if other:
                lines += ["", "🔄 *Також змінено:*", *other]
            return "\n".join(lines)

        # 2б. Статус не змінювався (редагування полів, коментар тощо)
        if NOTIFY_ONLY_STATUS_CHANGES:
            return None
        lines = [
            "✏️ *Задачу оновлено*",
            "",
            f"🔑 *Ключ:* {key}",
            f"📝 *Назва:* {summary}",
            f"{emoji} *Поточний статус:* {esc(status_name or '—')}",
            f"👤 *Хто змінив:* {actor}",
            f"📅 *Дедлайн:* {deadline}",
        ]
        changes = format_changes(payload)
        if changes:
            lines += ["", "🔄 *Зміни:*", *changes]
        elif payload.get("comment"):
            lines += ["", "💬 Додано або змінено коментар"]
        return "\n".join(lines)

    # 3. Видалення задачі (посилання не робимо — задачі вже не існує)
    if event in DELETED_EVENTS:
        return "\n".join([
            "🗑 *Задачу видалено*",
            "",
            f"🔑 *Ключ:* {esc(raw_key)}",
            f"📝 *Назва:* {summary}",
            f"👤 *Хто видалив:* {actor}",
        ])

    return None  # інші події (коментарі, спринти тощо) ігноруємо


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
async def send_telegram(client: httpx.AsyncClient, text: str) -> None:
    """Надсилає повідомлення в Telegram; при помилці кидає HTTP 502."""
    body: dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "link_preview_options": {"is_disabled": True},
    }
    if TELEGRAM_THREAD_ID:
        body["message_thread_id"] = int(TELEGRAM_THREAD_ID)

    try:
        response = await client.post(TELEGRAM_URL, json=body)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Не логуємо сам exc — у ньому є URL з токеном бота
        logger.error("Telegram відхилив повідомлення: %s %s",
                     exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Telegram rejected the message")
    except httpx.HTTPError as exc:
        logger.error("Помилка з'єднання з Telegram: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Telegram is unreachable")


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Один спільний асинхронний клієнт на весь час роботи сервісу
    async with httpx.AsyncClient(timeout=10.0) as client:
        app.state.http = client
        yield


app = FastAPI(title="Jira → Telegram", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/jira")
async def jira_webhook(request: Request, secret: str | None = Query(default=None)) -> dict[str, str]:
    # Перевірка секрету (Jira не вміє додавати кастомні заголовки, тому — через query-параметр)
    if WEBHOOK_SECRET:
        if not secret or not hmac.compare_digest(secret.encode(), WEBHOOK_SECRET.encode()):
            raise HTTPException(status_code=403, detail="Forbidden")

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    event = payload.get("webhookEvent") or ""
    text = build_message(event, payload)
    if text is None:
        logger.info("Подію %r проігноровано", event)
        return {"status": "ignored", "event": event}

    await send_telegram(request.app.state.http, text)
    logger.info("Сповіщення про %r надіслано", event)
    return {"status": "sent", "event": event}
