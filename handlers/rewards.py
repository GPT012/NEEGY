"""Envoi Telegram des lots (photos / vidéos protégées) après paiement."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from db.repository import FulfillmentResult, RewardAsset, mark_grants_delivered
from utils.logger import get_logger

logger = get_logger(__name__)


async def _send_asset(bot: Bot, user_id: int, asset: RewardAsset) -> None:
    caption = asset.caption or None
    for attempt in range(4):
        try:
            if asset.kind == "video":
                try:
                    await bot.send_video(
                        user_id,
                        asset.telegram_file_id,
                        caption=caption,
                        protect_content=True,
                        supports_streaming=True,
                    )
                    return
                except TelegramBadRequest:
                    await bot.send_document(
                        user_id,
                        asset.telegram_file_id,
                        caption=caption,
                        protect_content=True,
                    )
                    return
            try:
                await bot.send_photo(
                    user_id,
                    asset.telegram_file_id,
                    caption=caption,
                    protect_content=False,
                )
            except TelegramBadRequest:
                await bot.send_document(
                    user_id,
                    asset.telegram_file_id,
                    caption=caption,
                    protect_content=False,
                )
            return
        except TelegramRetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 1) or 1) + 0.5
            logger.warning(
                "Flood control Telegram (tentative %s) user=%s asset=%s — pause %.1fs",
                attempt + 1,
                user_id,
                asset.id,
                wait,
            )
            await asyncio.sleep(wait)
    raise RuntimeError(f"Envoi asset #{asset.id} abandonné après flood control")


async def deliver_fulfillment(
    bot: Bot,
    user_id: int,
    fulfillment: FulfillmentResult,
    *,
    order_id: int | None = None,
    db_pool=None,
) -> list[str]:
    """Envoie les médias et messages cliente. Retourne les erreurs d'envoi pour l'admin."""
    send_errors: list[str] = []
    if fulfillment.points_amount:
        try:
            await bot.send_message(
                user_id,
                f"La roue a parlé. +{fulfillment.points_amount} points.",
            )
        except Exception:
            logger.exception("Impossible d'annoncer les points à user_id=%s", user_id)
            send_errors.append("message points non délivré")

    if fulfillment.prize_kind in ("photo", "video") and not fulfillment.assets:
        try:
            await bot.send_message(
                user_id,
                "Ton lot arrive. Encore un instant.",
            )
        except Exception:
            logger.exception("Impossible de prévenir user_id=%s (stock vide)", user_id)

    delivered_ids: list[int] = []
    for asset in fulfillment.assets or []:
        try:
            await _send_asset(bot, user_id, asset)
            delivered_ids.append(asset.id)
            # Petite pause entre fichiers pour rester sous le flood control.
            await asyncio.sleep(0.05)
        except TelegramBadRequest:
            logger.exception("Fichier Telegram refusé pour user_id=%s asset=%s", user_id, asset.id)
            send_errors.append(f"fichier #{asset.id} refusé")
        except Exception:
            logger.exception("Impossible d'envoyer l'asset #%s à user_id=%s", asset.id, user_id)
            send_errors.append(f"fichier #{asset.id} non envoyé")

    if db_pool is not None and order_id is not None and delivered_ids:
        try:
            await mark_grants_delivered(db_pool, order_id, delivered_ids)
        except Exception:
            logger.exception("Impossible de marquer delivered_at commande #%s", order_id)

    if fulfillment.call_slot is not None:
        slot = fulfillment.call_slot
        try:
            await bot.send_message(
                user_id,
                f"📞 Ton appel du {slot.start_at:%d/%m/%Y à %H:%M} UTC est confirmé !",
            )
        except Exception:
            logger.exception("Impossible d'annoncer l'appel gagné à user_id=%s", user_id)

    return send_errors


def admin_fulfillment_lines(fulfillment: FulfillmentResult) -> list[str]:
    lines: list[str] = []
    if fulfillment.prize_label:
        extra = f" (+{fulfillment.points_amount} pts)" if fulfillment.points_amount else ""
        kind = f" [{fulfillment.prize_kind}]" if fulfillment.prize_kind else ""
        lines.append(f"→ Lot : {fulfillment.prize_label}{kind}{extra}")
    if fulfillment.assets:
        photos = sum(1 for a in fulfillment.assets if a.kind == "photo")
        videos = sum(1 for a in fulfillment.assets if a.kind == "video")
        parts = []
        if photos:
            parts.append(f"{photos} photo(s)")
        if videos:
            parts.append(f"{videos} vidéo(s)")
        lines.append(f"→ Envoyé : {', '.join(parts) or f'{len(fulfillment.assets)} fichier(s)'}.")
    elif fulfillment.prize_kind in ("photo", "video"):
        lines.append("→ Aucun fichier envoyé (file vide). Remplis le stock puis /fulfill.")
    if fulfillment.shipped_complete:
        lines.append("→ Média livré automatiquement.")
    for warning in fulfillment.warnings:
        lines.append(f"⚠ {warning}")
    return lines
