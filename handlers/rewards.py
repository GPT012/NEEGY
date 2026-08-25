"""Envoi Telegram des lots (photos / vidéos protégées) après paiement."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from db.repository import FulfillmentResult
from utils.logger import get_logger

logger = get_logger(__name__)


async def deliver_fulfillment(bot: Bot, user_id: int, fulfillment: FulfillmentResult) -> list[str]:
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

    for asset in fulfillment.assets or []:
        caption = asset.caption or None
        try:
            if asset.kind == "video":
                await bot.send_video(
                    user_id,
                    asset.telegram_file_id,
                    caption=caption,
                    protect_content=True,
                    supports_streaming=True,
                )
            else:
                await bot.send_photo(
                    user_id,
                    asset.telegram_file_id,
                    caption=caption,
                    protect_content=False,
                )
        except TelegramBadRequest:
            logger.exception("Fichier Telegram refusé pour user_id=%s asset=%s", user_id, asset.id)
            send_errors.append(f"fichier #{asset.id} refusé")
        except Exception:
            logger.exception("Impossible d'envoyer l'asset #%s à user_id=%s", asset.id, user_id)
            send_errors.append(f"fichier #{asset.id} non envoyé")

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
        lines.append(f"→ Lot : {fulfillment.prize_label}{extra}")
    if fulfillment.assets:
        lines.append(f"→ {len(fulfillment.assets)} fichier(s) envoyé(s).")
    if fulfillment.shipped_complete:
        lines.append("→ Pack photo livré automatiquement.")
    for warning in fulfillment.warnings:
        lines.append(f"⚠ {warning}")
    return lines
