"""API HTTP de l'inbox chatteurs (/inbox/)."""

from __future__ import annotations

import json

from aiohttp import web
import asyncpg

from config import config
from db.inbox_repository import (
    delete_canned_response,
    get_agent_by_token,
    get_chat_conversation,
    list_canned_responses,
    list_chat_conversations,
    list_chat_messages,
    record_outgoing_message,
    upsert_canned_response,
    verify_chat_agent,
    ChatAgent,
)
from keyboards.main_menu import mini_app_deep_link
from services.business_bot import BusinessBotError, send_business_reply
from services.inbox_canned import CannedContext, expand_canned_content, resolve_outgoing_message
from utils.logger import get_logger

logger = get_logger(__name__)

routes = web.RouteTableDef()

_AUTH_HEADER = "Authorization"
_BEARER_PREFIX = "Bearer "


def _get_pool(request: web.Request) -> asyncpg.Pool:
    pool = request.app.get("db_pool")
    if pool is None:
        raise web.HTTPServiceUnavailable(text="Base de données indisponible")
    return pool


def _json(data: object, *, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        status=status,
    )


def _extract_bearer(request: web.Request) -> str | None:
    header = request.headers.get(_AUTH_HEADER, "")
    if header.startswith(_BEARER_PREFIX):
        token = header[len(_BEARER_PREFIX) :].strip()
        return token or None
    return None


async def _authenticate(request: web.Request) -> ChatAgent:
    token = _extract_bearer(request)
    if not token:
        raise web.HTTPUnauthorized(text="Token requis")
    pool = _get_pool(request)
    agent = await get_agent_by_token(pool, token)
    if agent is None:
        raise web.HTTPUnauthorized(text="Token invalide")
    return agent


def _shop_link(request: web.Request) -> str:
    bot_username = request.app.get("shop_bot_username")
    if bot_username:
        return mini_app_deep_link(bot_username, config.mini_app_short_name)
    if config.mini_app_url:
        return config.mini_app_url
    return "https://t.me/"


def _canned_context(request: web.Request, agent_name: str | None = None) -> CannedContext:
    return CannedContext(shop_link=_shop_link(request), agent_name=agent_name)


def _expand_canned(content: str, request: web.Request, agent_name: str | None = None) -> str:
    return expand_canned_content(content, _canned_context(request, agent_name))


@routes.post("/api/inbox/login")
async def inbox_login(request: web.Request) -> web.Response:
    pool = _get_pool(request)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "JSON invalide"}, status=400)

    name = str(body.get("name") or "").strip()
    token = str(body.get("token") or "").strip()
    if not name or not token:
        return _json({"error": "Nom et token requis"}, status=400)

    agent = await verify_chat_agent(pool, name, token)
    if agent is None:
        return _json({"error": "Identifiants invalides"}, status=401)

    return _json({"agent": {"id": agent.id, "name": agent.name}, "token": token})


@routes.get("/api/inbox/conversations")
async def inbox_conversations(request: web.Request) -> web.Response:
    await _authenticate(request)
    pool = _get_pool(request)
    conversations = await list_chat_conversations(pool)
    payload = [
        {
            "id": c.id,
            "client_name": c.client_name,
            "client_username": c.client_username,
            "telegram_user_id": c.telegram_user_id,
            "status": c.status,
            "last_message_at": c.last_message_at,
            "last_preview": c.last_preview,
        }
        for c in conversations
    ]
    return _json({"conversations": payload})


@routes.get("/api/inbox/conversations/{conversation_id:\\d+}/messages")
async def inbox_messages(request: web.Request) -> web.Response:
    await _authenticate(request)
    pool = _get_pool(request)
    conversation_id = int(request.match_info["conversation_id"])
    since_id = int(request.query.get("since_id", "0"))

    conversation = await get_chat_conversation(pool, conversation_id)
    if conversation is None:
        return _json({"error": "Conversation introuvable"}, status=404)

    messages = await list_chat_messages(pool, conversation_id, since_id=since_id)
    return _json(
        {
            "conversation": {
                "id": conversation.id,
                "client_name": conversation.client_name,
                "client_username": conversation.client_username,
            },
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "content": m.content,
                    "agent_name": m.agent_name,
                    "created_at": m.created_at,
                }
                for m in messages
            ],
        }
    )


@routes.post("/api/inbox/conversations/{conversation_id:\\d+}/reply")
async def inbox_reply(request: web.Request) -> web.Response:
    agent = await _authenticate(request)
    pool = _get_pool(request)
    conversation_id = int(request.match_info["conversation_id"])

    try:
        body = await request.json()
    except Exception:
        return _json({"error": "JSON invalide"}, status=400)

    content = str(body.get("content") or "").strip()
    if not content:
        return _json({"error": "Message vide"}, status=400)
    if len(content) > 4000:
        return _json({"error": "Message trop long"}, status=400)

    resolved, resolve_error = await resolve_outgoing_message(
        pool,
        content,
        _canned_context(request, agent.name),
    )
    if resolve_error == "help":
        items = await list_canned_responses(pool)
        shortcuts = [item.shortcut for item in items]
        return _json(
            {
                "error": "Utilise /help dans l'interface pour voir les commandes.",
                "shortcuts": shortcuts,
            },
            status=400,
        )
    if resolve_error:
        return _json({"error": resolve_error}, status=400)
    content = resolved or content
    if len(content) > 4000:
        return _json({"error": "Message trop long après expansion"}, status=400)

    conversation = await get_chat_conversation(pool, conversation_id)
    if conversation is None:
        return _json({"error": "Conversation introuvable"}, status=404)

    connection_id = conversation.business_connection_id
    if not connection_id:
        connection_id = request.app.get("business_connection_id") or ""

    try:
        sent_message_id = await send_business_reply(
            chat_id=conversation.telegram_chat_id,
            text=content,
            business_connection_id=connection_id,
        )
    except BusinessBotError as exc:
        logger.exception("Échec envoi inbox conv=%s", conversation_id)
        return _json({"error": str(exc)}, status=502)

    await record_outgoing_message(
        pool,
        conversation_id=conversation_id,
        agent_id=agent.id,
        content=content,
        telegram_message_id=sent_message_id,
    )
    return _json({"ok": True})


@routes.get("/api/inbox/canned")
async def inbox_canned(request: web.Request) -> web.Response:
    await _authenticate(request)
    pool = _get_pool(request)
    items = await list_canned_responses(pool)
    return _json(
        {
            "items": [
                {
                    "shortcut": item.shortcut,
                    "content": _expand_canned(item.content, request, agent_name=None),
                    "template": item.content,
                }
                for item in items
            ]
        }
    )
