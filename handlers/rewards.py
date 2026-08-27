"""Envoi Telegram des lots (photos / vidéos protégées) après paiement."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from db.repository import (
    FulfillmentResult,
    RewardAsset,
    fulfill_remaining_for_order,
    get_order,
    list_assets_granted_for_order,
    list_undelivered_assets_for_order,
    mark_grants_delivered,
    mark_order_shipped_if_delivered,
)
from utils.logger import get_logger

logger = get_logger(__name__)


async def _send_asset(bot: Bot, user_id: int, asset: RewardAsset) -> None:
    caption = asset.caption or None
    for attempt in range(5):
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
        except TelegramForbiddenError:
            raise
    raise RuntimeError(f"Envoi asset #{asset.id} abandonné après flood control")


def _friendly_send_error(exc: BaseException, asset_id: int | None = None) -> str:
    label = f"fichier #{asset_id}" if asset_id is not None else "message"
    if isinstance(exc, TelegramForbiddenError):
        return (
            f"{label} : la cliente n'a pas démarré le bot (ou l'a bloqué). "
            "Demande-lui d'ouvrir @S94lmabot et /start, puis /fulfill ID."
        )
    text = str(exc).lower()
    if "chat not found" in text:
        return f"{label} : chat introuvable — la cliente doit /start le bot."
    return f"{label} non envoyé"


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
        except Exception as exc:
            logger.exception("Impossible d'annoncer les points à user_id=%s", user_id)
            send_errors.append(_friendly_send_error(exc))

    assets = list(fulfillment.assets or [])
    if fulfillment.prize_kind in ("photo", "video") and not assets:
        try:
            await bot.send_message(
                user_id,
                "✅ Paiement reçu. Ton contenu arrive très bientôt.",
            )
        except Exception as exc:
            logger.exception("Impossible de prévenir user_id=%s (stock vide)", user_id)
            send_errors.append(_friendly_send_error(exc))
        send_errors.append(
            "Stock vide ou aucun fichier attribué — remplis /depot puis /fulfill ID."
        )

    if assets:
        try:
            await bot.send_message(user_id, "📦 Voici ton contenu :")
        except TelegramForbiddenError as exc:
            logger.exception("Cliente inaccessible user_id=%s", user_id)
            send_errors.append(_friendly_send_error(exc))
            return send_errors
        except Exception:
            logger.exception("Impossible d'annoncer le contenu à user_id=%s", user_id)

    delivered_ids: list[int] = []
    for asset in assets:
        try:
            await _send_asset(bot, user_id, asset)
            delivered_ids.append(asset.id)
            await asyncio.sleep(0.08)
        except TelegramForbiddenError as exc:
            logger.exception("Cliente inaccessible user_id=%s asset=%s", user_id, asset.id)
            send_errors.append(_friendly_send_error(exc, asset.id))
            break
        except TelegramBadRequest as exc:
            logger.exception("Fichier Telegram refusé pour user_id=%s asset=%s", user_id, asset.id)
            send_errors.append(_friendly_send_error(exc, asset.id))
        except Exception as exc:
            logger.exception("Impossible d'envoyer l'asset #%s à user_id=%s", asset.id, user_id)
            send_errors.append(_friendly_send_error(exc, asset.id))

    if db_pool is not None and order_id is not None and delivered_ids:
        try:
            await mark_grants_delivered(db_pool, order_id, delivered_ids)
        except Exception:
            logger.exception("Impossible de marquer delivered_at commande #%s", order_id)
        try:
            if await mark_order_shipped_if_delivered(db_pool, order_id):
                fulfillment.shipped_complete = True
        except Exception:
            logger.exception("Impossible de marquer shipped commande #%s", order_id)

    if fulfillment.call_slot is not None:
        slot = fulfillment.call_slot
        try:
            await bot.send_message(
                user_id,
                f"📞 Ton appel du {slot.start_at:%d/%m/%Y à %H:%M} UTC est confirmé !",
            )
        except Exception as exc:
            logger.exception("Impossible d'annoncer l'appel gagné à user_id=%s", user_id)
            send_errors.append(_friendly_send_error(exc))

    return send_errors


async def deliver_order_media(
    bot: Bot,
    db_pool,
    order_id: int,
    *,
    retries: int = 3,
) -> tuple[FulfillmentResult, list[str]]:
    """Attribue le stock manquant si besoin, puis envoie (avec retries) les fichiers non livrés."""
    from services.drive import is_drive_configured
    from services.drive_delivery import deliver_drive_for_order, get_drive_delivery

    order = await get_order(db_pool, order_id)
    if order is None:
        empty = FulfillmentResult(warnings=[f"Commande #{order_id} introuvable."])
        return empty, ["commande introuvable"]

    fulfillment = await fulfill_remaining_for_order(db_pool, order_id)
    send_errors: list[str] = []

    # Packs boutique via Drive (prioritaire si configuré).
    if is_drive_configured():
        drive_row = await get_drive_delivery(db_pool, order_id)
        if drive_row is not None:
            fulfillment.drive_slot_path = drive_row["slot_path"]
            fulfillment.drive_slot_number = int(drive_row["slot_number"])
            fulfillment.prize_kind = drive_row["media_kind"]
            fulfillment.prize_label = (
                f"{'Photo' if drive_row['media_kind'] == 'photo' else 'Vidéo'} "
                f"· {drive_row['slot_path']}"
            )
            for attempt in range(max(1, retries)):
                drive_errors, drive_ok = await deliver_drive_for_order(bot, db_pool, order_id)
                if drive_ok:
                    fulfillment.shipped_complete = True
                    return fulfillment, []
                send_errors = list(drive_errors)
                if any("pas de /start" in e or "bloqué" in e for e in send_errors):
                    break
                if attempt + 1 < retries:
                    await asyncio.sleep(1.2 * (attempt + 1))
            return fulfillment, send_errors

    for attempt in range(max(1, retries)):
        undelivered = await list_undelivered_assets_for_order(db_pool, order_id)
        if undelivered:
            fulfillment.assets = undelivered
            send_errors = await deliver_fulfillment(
                bot, order.user_id, fulfillment, order_id=order_id, db_pool=db_pool
            )
            still = await list_undelivered_assets_for_order(db_pool, order_id)
            if not still:
                break
            if any("démarré le bot" in e or "bloqué" in e for e in send_errors):
                break
            if attempt + 1 < retries:
                await asyncio.sleep(1.2 * (attempt + 1))
            continue

        # Plus rien à envoyer : déjà livré, ou stock vide.
        already = await list_assets_granted_for_order(db_pool, order_id)
        if already:
            if await mark_order_shipped_if_delivered(db_pool, order_id):
                fulfillment.shipped_complete = True
            fulfillment.assets = []
            send_errors = []
            break

        if fulfillment.prize_kind in ("photo", "video"):
            fulfillment.assets = []
            send_errors = await deliver_fulfillment(
                bot, order.user_id, fulfillment, order_id=order_id, db_pool=db_pool
            )
        break

    return fulfillment, send_errors


def admin_fulfillment_lines(fulfillment: FulfillmentResult) -> list[str]:
    lines: list[str] = []
    if fulfillment.prize_label:
        extra = f" (+{fulfillment.points_amount} pts)" if fulfillment.points_amount else ""
        kind = f" [{fulfillment.prize_kind}]" if fulfillment.prize_kind else ""
        lines.append(f"→ Lot : {fulfillment.prize_label}{kind}{extra}")
    if fulfillment.drive_slot_path:
        lines.append(f"→ Drive : {fulfillment.drive_slot_path}")
    if fulfillment.assets:
        photos = sum(1 for a in fulfillment.assets if a.kind == "photo")
        videos = sum(1 for a in fulfillment.assets if a.kind == "video")
        parts = []
        if photos:
            parts.append(f"{photos} photo(s)")
        if videos:
            parts.append(f"{videos} vidéo(s)")
        lines.append(f"→ Fichier(s) à envoyer : {', '.join(parts) or f'{len(fulfillment.assets)}'}.")
    elif fulfillment.prize_kind in ("photo", "video") and not fulfillment.drive_slot_path:
        lines.append("→ Aucun fichier (file vide). Remplis /depot ou configure Drive.")
    if fulfillment.shipped_complete:
        lines.append("→ Média livré automatiquement.")
    for warning in fulfillment.warnings:
        lines.append(f"⚠ {warning}")
    return lines
