"""Raccourcis /commandes pour les chatteurs inbox."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from config import config
from db.inbox_repository import get_canned_by_shortcut


@dataclass(frozen=True)
class CannedContext:
    shop_link: str
    agent_name: str | None = None


def expand_canned_content(content: str, ctx: CannedContext) -> str:
    mapping = {
        "{shop_link}": ctx.shop_link,
        "{paypal_url}": config.paypal_url or "",
        "{bank_iban}": config.bank_iban or "",
        "{bank_holder}": config.bank_holder or "",
        "{crypto_solana}": config.crypto_solana or "",
        "{crypto_ethereum}": config.crypto_ethereum or "",
        "{crypto_bitcoin}": config.crypto_bitcoin or "",
        "{agent_name}": ctx.agent_name or "",
    }
    for key, value in mapping.items():
        content = content.replace(key, value)
    return content


async def resolve_outgoing_message(
    pool: asyncpg.Pool,
    raw: str,
    ctx: CannedContext,
) -> tuple[str | None, str | None]:
    """Résout /shortcut en message final.

    Retourne (texte, None) ou (None, erreur).
    """
    text = raw.strip()
    if not text.startswith("/"):
        return text, None

    parts = text.split(maxsplit=1)
    shortcut = parts[0][1:].lower()
    extra = parts[1] if len(parts) > 1 else ""

    if not shortcut:
        return text, None
    if shortcut == "help":
        return None, "help"

    canned = await get_canned_by_shortcut(pool, shortcut)
    if canned is None:
        return None, f"Commande inconnue : /{shortcut}"

    message = expand_canned_content(canned.content, ctx)
    if extra:
        message = f"{message}\n{extra}"
    return message, None
