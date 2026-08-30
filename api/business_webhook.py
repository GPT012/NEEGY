"""Webhook du bot relais Telegram Business (Secretary Mode)."""

from __future__ import annotations

import hmac
from html import escape
from typing import Any

from aiohttp import web

from config import config, public_inbox_url
from db.inbox_repository import (
    create_chat_agent,
    list_chat_agents,
    record_incoming_business_message,
    revoke_chat_agent,
)
from services.business_bot import extract_message_content, send_bot_message
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


def _relay_command_args(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return command, args


async def _handle_relay_private_message(
    pool,
    *,
    message: dict[str, Any],
    chat_id: int,
) -> None:
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    user = message.get("from") or {}
    user_id = user.get("id")
    if config.admin_user_id is None or user_id != config.admin_user_id:
        await send_bot_message(
            chat_id=chat_id,
            text="Commande réservée à l'administrateur NEEGY.",
        )
        return

    command, args = _relay_command_args(text)

    if command == "/start":
        await send_bot_message(
            chat_id=chat_id,
            text=(
                "Bot relais NEEGY actif ✅\n\n"
                "Les clientes t'écrivent sur ton compte perso/pro (Secretary Mode).\n"
                "Leurs messages apparaissent ici :\n"
                f"{public_inbox_url()}\n\n"
                "Gestion chatteurs (admin) :\n"
                "/agent_add Prénom — créer un accès\n"
                "/agents — lister\n"
                "/agent_revoke Prénom — révoquer"
            ),
        )
        return

    if command == "/agent_add":
        if not args:
            await send_bot_message(
                chat_id=chat_id,
                text="Usage : /agent_add Prénom\nExemple : /agent_add TMS",
            )
            return
        try:
            agent, token = await create_chat_agent(pool, args)
        except Exception:
            logger.exception("Erreur /agent_add (bot relais)")
            await send_bot_message(chat_id=chat_id, text="Erreur lors de la création du chatteur.")
            return
        await send_bot_message(
            chat_id=chat_id,
            text=(
                f"✅ Chatteur « {escape(agent.name)} » créé.\n\n"
                f"Identifiant : {escape(agent.name)}\n"
                f"Token (à copier une seule fois) :\n<code>{escape(token)}</code>\n\n"
                f"Connexion inbox : {public_inbox_url()}\n"
                "Ne partage pas ce token publiquement."
            ),
            parse_mode="HTML",
        )
        return

    if command == "/agents":
        agents = await list_chat_agents(pool)
        if not agents:
            await send_bot_message(
                chat_id=chat_id,
                text="Aucun chatteur. Ajoute-en un avec /agent_add Prénom",
            )
            return
        lines = ["Chatteurs inbox :\n"]
        for agent in agents:
            status = "actif" if agent.is_active else "révoqué"
            lines.append(f"• {agent.name} — {status}")
        await send_bot_message(chat_id=chat_id, text="\n".join(lines))
        return

    if command == "/agent_revoke":
        if not args:
            await send_bot_message(
                chat_id=chat_id,
                text="Usage : /agent_revoke Prénom",
            )
            return
        revoked = await revoke_chat_agent(pool, args)
        if revoked:
            await send_bot_message(chat_id=chat_id, text=f"Chatteur « {args} » révoqué.")
        else:
            await send_bot_message(
                chat_id=chat_id,
                text=f"Aucun chatteur actif « {args} ».",
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
        if "message" in update:
            message = update["message"]
            chat = message.get("chat") or {}
            if chat.get("type") == "private":
                chat_id = chat.get("id")
                if chat_id is not None:
                    await _handle_relay_private_message(
                        pool,
                        message=message,
                        chat_id=int(chat_id),
                    )
        elif "business_message" in update:
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
