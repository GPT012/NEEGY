"""Couche données pour l'inbox chatteurs (conversations Telegram Business)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

import asyncpg


@dataclass(frozen=True)
class ChatAgent:
    id: int
    name: str
    is_active: bool
    created_at: datetime


@dataclass(frozen=True)
class ChatConversation:
    id: int
    telegram_user_id: int
    telegram_chat_id: int
    business_connection_id: str
    client_name: str
    client_username: str | None
    status: str
    last_message_at: datetime
    last_preview: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    id: int
    conversation_id: int
    direction: str
    content: str
    agent_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class CannedResponse:
    shortcut: str
    content: str


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_agent_token() -> str:
    return secrets.token_urlsafe(32)


async def create_chat_agent(pool: asyncpg.Pool, name: str) -> tuple[ChatAgent, str]:
    token = generate_agent_token()
    token_hash = hash_agent_token(token)
    row = await pool.fetchrow(
        """
        INSERT INTO chat_agents (name, token_hash)
        VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE
            SET token_hash = EXCLUDED.token_hash,
                is_active = TRUE
        RETURNING id, name, is_active, created_at
        """,
        name.strip(),
        token_hash,
    )
    assert row is not None
    agent = ChatAgent(
        id=int(row["id"]),
        name=row["name"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )
    return agent, token


async def revoke_chat_agent(pool: asyncpg.Pool, name: str) -> bool:
    result = await pool.execute(
        """
        UPDATE chat_agents SET is_active = FALSE
        WHERE lower(name) = lower($1) AND is_active = TRUE
        """,
        name.strip(),
    )
    return result.endswith(" 1")


async def list_chat_agents(pool: asyncpg.Pool) -> list[ChatAgent]:
    rows = await pool.fetch(
        """
        SELECT id, name, is_active, created_at
        FROM chat_agents
        ORDER BY name
        """
    )
    return [
        ChatAgent(
            id=int(row["id"]),
            name=row["name"],
            is_active=row["is_active"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


async def verify_chat_agent(pool: asyncpg.Pool, name: str, token: str) -> ChatAgent | None:
    token_hash = hash_agent_token(token)
    row = await pool.fetchrow(
        """
        SELECT id, name, is_active, created_at
        FROM chat_agents
        WHERE lower(name) = lower($1) AND token_hash = $2 AND is_active = TRUE
        """,
        name.strip(),
        token_hash,
    )
    if row is None:
        return None
    return ChatAgent(
        id=int(row["id"]),
        name=row["name"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


async def get_agent_by_token(pool: asyncpg.Pool, token: str) -> ChatAgent | None:
    token_hash = hash_agent_token(token)
    row = await pool.fetchrow(
        """
        SELECT id, name, is_active, created_at
        FROM chat_agents
        WHERE token_hash = $1 AND is_active = TRUE
        """,
        token_hash,
    )
    if row is None:
        return None
    return ChatAgent(
        id=int(row["id"]),
        name=row["name"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


async def record_business_event(
    pool: asyncpg.Pool,
    *,
    telegram_user_id: int,
    telegram_chat_id: int,
    business_connection_id: str,
    client_name: str,
    client_username: str | None,
    direction: str,
    content: str,
    telegram_message_id: int | None = None,
) -> int:
    """Enregistre un message business (entrant client OU sortant compte perso).

    Déduplique via telegram_message_id pour éviter le double affichage quand
    Telegram renvoie en écho un message déjà envoyé par le bot relais.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            conv_id = await connection.fetchval(
                """
                INSERT INTO chat_conversations (
                    telegram_user_id, telegram_chat_id, business_connection_id,
                    client_name, client_username, status, last_message_at
                )
                VALUES ($1, $2, $3, $4, $5, 'open', now())
                ON CONFLICT (telegram_user_id) DO UPDATE SET
                    telegram_chat_id = EXCLUDED.telegram_chat_id,
                    business_connection_id = CASE
                        WHEN EXCLUDED.business_connection_id <> '' THEN EXCLUDED.business_connection_id
                        ELSE chat_conversations.business_connection_id
                    END,
                    client_name = EXCLUDED.client_name,
                    client_username = COALESCE(EXCLUDED.client_username, chat_conversations.client_username),
                    status = 'open',
                    last_message_at = now()
                RETURNING id
                """,
                telegram_user_id,
                telegram_chat_id,
                business_connection_id,
                client_name,
                client_username,
            )
            await connection.execute(
                """
                INSERT INTO chat_messages
                    (conversation_id, direction, content, telegram_message_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                conv_id,
                direction,
                content,
                telegram_message_id,
            )
            return int(conv_id)


async def list_chat_conversations(
    pool: asyncpg.Pool, *, limit: int = 50
) -> list[ChatConversation]:
    rows = await pool.fetch(
        """
        SELECT
            c.id, c.telegram_user_id, c.telegram_chat_id, c.business_connection_id,
            c.client_name, c.client_username, c.status, c.last_message_at,
            (
                SELECT m.content FROM chat_messages m
                WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC
                LIMIT 1
            ) AS last_preview
        FROM chat_conversations c
        ORDER BY c.last_message_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        ChatConversation(
            id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            telegram_chat_id=int(row["telegram_chat_id"]),
            business_connection_id=row["business_connection_id"] or "",
            client_name=row["client_name"],
            client_username=row["client_username"],
            status=row["status"],
            last_message_at=row["last_message_at"],
            last_preview=row["last_preview"],
        )
        for row in rows
    ]


async def get_chat_conversation(pool: asyncpg.Pool, conversation_id: int) -> ChatConversation | None:
    row = await pool.fetchrow(
        """
        SELECT
            c.id, c.telegram_user_id, c.telegram_chat_id, c.business_connection_id,
            c.client_name, c.client_username, c.status, c.last_message_at,
            NULL::text AS last_preview
        FROM chat_conversations c
        WHERE c.id = $1
        """,
        conversation_id,
    )
    if row is None:
        return None
    return ChatConversation(
        id=int(row["id"]),
        telegram_user_id=int(row["telegram_user_id"]),
        telegram_chat_id=int(row["telegram_chat_id"]),
        business_connection_id=row["business_connection_id"] or "",
        client_name=row["client_name"],
        client_username=row["client_username"],
        status=row["status"],
        last_message_at=row["last_message_at"],
    )


async def list_chat_messages(
    pool: asyncpg.Pool, conversation_id: int, *, since_id: int = 0
) -> list[ChatMessage]:
    rows = await pool.fetch(
        """
        SELECT m.id, m.conversation_id, m.direction, m.content, m.created_at,
               a.name AS agent_name
        FROM chat_messages m
        LEFT JOIN chat_agents a ON a.id = m.agent_id
        WHERE m.conversation_id = $1 AND m.id > $2
        ORDER BY m.created_at ASC
        """,
        conversation_id,
        since_id,
    )
    return [
        ChatMessage(
            id=int(row["id"]),
            conversation_id=int(row["conversation_id"]),
            direction=row["direction"],
            content=row["content"],
            agent_name=row["agent_name"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


async def record_outgoing_message(
    pool: asyncpg.Pool,
    *,
    conversation_id: int,
    agent_id: int,
    content: str,
    telegram_message_id: int | None = None,
) -> None:
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO chat_messages
                    (conversation_id, direction, content, agent_id, telegram_message_id)
                VALUES ($1, 'out', $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                conversation_id,
                content,
                agent_id,
                telegram_message_id,
            )
            await connection.execute(
                """
                UPDATE chat_conversations
                SET last_message_at = now(), status = 'open'
                WHERE id = $1
                """,
                conversation_id,
            )


async def list_canned_responses(pool: asyncpg.Pool) -> list[CannedResponse]:
    rows = await pool.fetch(
        """
        SELECT shortcut, content FROM chat_canned_responses
        WHERE is_active = TRUE
        ORDER BY shortcut
        """
    )
    return [CannedResponse(shortcut=row["shortcut"], content=row["content"]) for row in rows]
