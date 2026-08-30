"""Webhook du bot relais Telegram Business (Secretary Mode)."""

from __future__ import annotations

import hmac
from typing import Any

from aiohttp import web

from config import config
from db.inbox_repository import record_incoming_business_message
from services.business_bot import extract_message_content
from utils.logger import get_logger

logger = get_logger(__name__)

routes = web.RouteTableDef()


def _authorized(request: web.Request) -> bool:
    expected = config.business_webhook_secret or ""
    if not expected:
        return False
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return bool(received) and hmac.compare_digest(received, expected)


def _client_label(user: dict[str, Any]) -> tuple[str, str | None]:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = " ".join(p for p in (first, last) if p) or f"Client {user.get('id')}"
    username = user.get("username")
    return name[:255], str(username) if username else None


async def _handle_business_message(pool, message: dict[str, Any]) -> None:
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    telegram_user_id = user.get("id")
    telegram_chat_id = chat.get("id")
    if telegram_user_id is None or telegram_chat_id is None:
        return

    content = extract_message_content(message)
    if not content:
        return

    connection_id = str(message.get("business_connection_id") or "")
    client_name, client_username = _client_label(user)

    await record_incoming_business_message(
        pool,
        telegram_user_id=int(telegram_user_id),
        telegram_chat_id=int(telegram_chat_id),
        business_connection_id=connection_id,
        client_name=client_name,
        client_username=client_username,
        content=content,
    )


@routes.post("/webhooks/business")
async def handle_business_webhook(request: web.Request) -> web.Response:
    if not config.inbox_enabled:
        return web.Response(status=404)
    if not _authorized(request):
        logger.warning("Webhook bot relais rejeté : secret invalide")
        return web.Response(status=403)

    pool = request.app.get("db_pool")
    if pool is None:
        return web.Response(status=503)

    try:
        update = await request.json()
    except Exception:
        logger.exception("Webhook bot relais : JSON invalide")
        return web.Response(status=400)

    try:
        if "business_message" in update:
            await _handle_business_message(pool, update["business_message"])
        elif "edited_business_message" in update:
            await _handle_business_message(pool, update["edited_business_message"])
        elif "business_connection" in update:
            conn = update["business_connection"]
            if conn.get("is_enabled") and conn.get("can_reply"):
                request.app["business_connection_id"] = conn.get("id") or ""
                logger.info("Connexion Business active : %s", conn.get("id"))
    except Exception:
        logger.exception("Erreur traitement webhook bot relais")
        return web.Response(status=500)

    return web.Response(text="ok")
