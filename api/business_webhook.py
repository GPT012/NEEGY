"""Webhook du bot relais Telegram Business (Secretary Mode)."""

from __future__ import annotations

import hmac
from html import escape
from typing import Any

from aiohttp import web

from config import config, public_inbox_url
from db.inbox_repository import (
    create_chat_agent,
    delete_canned_response,
    list_chat_agents,
    list_canned_responses,
    record_business_event,
    revoke_chat_agent,
    upsert_canned_response,
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
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    telegram_chat_id = chat.get("id")
    if telegram_chat_id is None:
        return

    content = extract_message_content(message)
    if not content:
        return

    # Dans un chat privé Business, l'interlocutrice est toujours `chat`.
    # Si l'émetteur diffère du chat, c'est le compte perso qui a écrit (sortant).
    from_id = from_user.get("id")
    is_outgoing = from_id is not None and from_id != telegram_chat_id
    direction = "out" if is_outgoing else "in"

    connection_id = str(message.get("business_connection_id") or "")
    client_name, client_username = _client_label(chat)

    await record_business_event(
        pool,
        telegram_user_id=int(telegram_chat_id),
        telegram_chat_id=int(telegram_chat_id),
        business_connection_id=connection_id,
        client_name=client_name,
        client_username=client_username,
        direction=direction,
        content=content,
        telegram_message_id=message.get("message_id"),
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
                "/agent_revoke Prénom — révoquer\n\n"
                "Commandes chatteurs :\n"
                "/canned_add raccourci Message\n"
                "/canned_list — lister\n"
                "/canned_del raccourci"
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
        return

    if command == "/canned_add":
        if " | " in args:
            shortcut, content = [p.strip() for p in args.split(" | ", 1)]
        else:
            parts = args.split(maxsplit=1)
            shortcut = parts[0].strip() if parts else ""
            content = parts[1].strip() if len(parts) > 1 else ""
        if not shortcut or not content:
            await send_bot_message(
                chat_id=chat_id,
                text=(
                    "Usage : /canned_add raccourci Message\n"
                    "Ex : /canned_add relance Hey bb, tu es là ?"
                ),
            )
            return
        await upsert_canned_response(pool, shortcut, content)
        await send_bot_message(
            chat_id=chat_id,
            text=f"✅ Commande /{shortcut.lower()} enregistrée pour l'inbox.",
        )
        return

    if command == "/canned_list":
        items = await list_canned_responses(pool)
        if not items:
            await send_bot_message(chat_id=chat_id, text="Aucune commande inbox.")
            return
        lines = ["Commandes inbox :\n"]
        for item in items:
            preview = item.content.replace("\n", " ")[:60]
            lines.append(f"/{item.shortcut} — {preview}")
        await send_bot_message(chat_id=chat_id, text="\n".join(lines))
        return

    if command == "/canned_del":
        if not args:
            await send_bot_message(chat_id=chat_id, text="Usage : /canned_del raccourci")
            return
        deleted = await delete_canned_response(pool, args)
        if deleted:
            await send_bot_message(chat_id=chat_id, text=f"Commande /{args.lower()} supprimée.")
        else:
            await send_bot_message(chat_id=chat_id, text=f"Aucune commande /{args.lower()}.")


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
