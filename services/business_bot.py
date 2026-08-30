"""Bot relais Telegram Business (Secretary Mode) — envoi via business_connection_id."""

from __future__ import annotations

from typing import Any

import aiohttp

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://api.telegram.org"


class BusinessBotError(RuntimeError):
    pass


async def _api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = config.business_bot_token
    if not token:
        raise BusinessBotError("BUSINESS_BOT_TOKEN non configuré")
    url = f"{API_BASE}/bot{token}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.json(content_type=None)
            if not body.get("ok"):
                logger.error("Telegram Business API %s → %s", method, body)
                description = body.get("description", "erreur Telegram")
                raise BusinessBotError(str(description))
            return body


async def register_business_webhook() -> None:
    if not config.inbox_enabled or not config.business_webhook_url:
        return
    secret = config.business_webhook_secret or ""
    await _api(
        "setWebhook",
        {
            "url": config.business_webhook_url,
            "secret_token": secret,
            "allowed_updates": [
                "message",
                "business_connection",
                "business_message",
                "edited_business_message",
            ],
        },
    )
    logger.info("Webhook bot relais enregistré sur %s", config.business_webhook_url)


async def send_bot_message(
    *,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
) -> None:
    """Message direct au bot (ex. réponse à /start), sans business_connection_id."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    await _api("sendMessage", payload)


async def send_business_reply(
    *,
    chat_id: int,
    text: str,
    business_connection_id: str,
) -> int | None:
    if not business_connection_id:
        raise BusinessBotError("business_connection_id manquant pour cette conversation")
    body = await _api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "business_connection_id": business_connection_id,
        },
    )
    result = body.get("result") or {}
    message_id = result.get("message_id")
    return int(message_id) if message_id is not None else None


def extract_message_content(message: dict[str, Any]) -> str:
    if message.get("text"):
        return str(message["text"])
    if message.get("caption"):
        return str(message["caption"])
    if message.get("photo"):
        return "📷 Photo"
    if message.get("video"):
        return "🎬 Vidéo"
    if message.get("voice"):
        return "🎤 Message vocal"
    if message.get("document"):
        doc = message["document"]
        name = doc.get("file_name", "document")
        return f"📎 Fichier : {name}"
    if message.get("sticker"):
        emoji = message.get("sticker", {}).get("emoji") or ""
        return f"Sticker {emoji}".strip() or "Sticker"
    return ""
