"""Middleware de rate limiting simple, par user_id, en mémoire.

Implémentation volontairement basique (adaptée à un seul process) : un
dictionnaire user_id -> timestamp de la dernière requête acceptée. Toute
requête arrivant avant l'expiration de la fenêtre est silencieusement
ignorée (pas d'exception, pas de spam de réponses "trop de requêtes").

L'admin n'est pas limité : les albums Telegram (stock en volume) arrivent
en rafale et seraient sinon tronqués.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_RATE_LIMIT_SECONDS = 1.0


class ThrottlingMiddleware(BaseMiddleware):
    """Limite le nombre de requêtes traitées par utilisateur (in-memory)."""

    def __init__(self, rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS) -> None:
        self._rate_limit_seconds = rate_limit_seconds
        self._last_seen: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from aiogram.types import InlineQuery, Message

        if isinstance(event, InlineQuery):
            return await handler(event, data)

        # Albums : plusieurs messages en <1s — ne pas jeter.
        if isinstance(event, Message) and event.media_group_id:
            return await handler(event, data)

        user_id = self._extract_user_id(event)
        if user_id is not None and config.admin_user_id and user_id == config.admin_user_id:
            return await handler(event, data)

        if user_id is not None:
            now = time.monotonic()
            last_call = self._last_seen.get(user_id)

            if last_call is not None and (now - last_call) < self._rate_limit_seconds:
                logger.info("Requête ignorée (rate limit) pour user_id=%s", user_id)
                return None

            self._last_seen[user_id] = now

        return await handler(event, data)

    @staticmethod
    def _extract_user_id(event: TelegramObject) -> int | None:
        if isinstance(event, Update):
            inner = event.message or event.callback_query or event.edited_message
            if inner is not None and inner.from_user is not None:
                return inner.from_user.id
            return None

        from_user = getattr(event, "from_user", None)
        if from_user is not None:
            return from_user.id
        return None
