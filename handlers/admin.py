"""Commandes réservées à l'administrateur (ADMIN_USER_ID).

Aucun paiement en ligne n'est intégré (v1) : le règlement se fait hors
plateforme, et c'est l'admin qui confirme manuellement la réception du
paiement via /confirm. Toutes les commandes de ce routeur sont filtrées par
identité Telegram (message.from_user.id == config.admin_user_id) ; si
ADMIN_USER_ID n'est pas configuré, aucune commande admin ne se déclenche.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import config
from db.repository import (
    activate_vip_for_order,
    create_call_slot,
    get_call_slot_for_order,
    get_order,
    get_photo_items_label,
    list_orders_to_ship,
    list_upcoming_call_slots,
    mark_order_paid,
    mark_order_shipped,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id == config.admin_user_id)

GENERIC_ERROR_MESSAGE = "Une erreur est survenue. Merci de réessayer plus tard."

ADDSLOT_USAGE = "Usage : /addslot AAAA-MM-JJ HH:MM DURÉE_MIN (heure en UTC)\nEx: /addslot 2026-08-26 18:00 30"
CONFIRM_USAGE = "Usage : /confirm ID_COMMANDE\nEx: /confirm 42"
SHIP_USAGE = "Usage : /ship ID_COMMANDE\nEx: /ship 42"


def _who(name: str | None, username: str | None, user_id: int) -> str:
    if username:
        return f"@{escape(username)}"
    if name:
        return escape(name)
    return f"user {user_id}"


@router.message(Command("addslot"))
async def handle_addslot(message: Message, command: CommandObject, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    args = (command.args or "").split()
    if len(args) != 3:
        await message.answer(ADDSLOT_USAGE)
        return

    date_str, time_str, duration_str = args
    try:
        start_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        duration_minutes = int(duration_str)
        if duration_minutes <= 0:
            raise ValueError("duration must be positive")
    except ValueError:
        await message.answer(f"Format invalide.\n\n{ADDSLOT_USAGE}")
        return

    try:
        slot_id = await create_call_slot(db_pool, start_at, duration_minutes)
        await message.answer(
            f"✅ Créneau #{slot_id} ajouté : {start_at.strftime('%d/%m/%Y %H:%M UTC')} ({duration_minutes} min)."
        )
    except Exception:
        logger.exception("Erreur lors de la création d'un créneau d'appel")
        await message.answer(GENERIC_ERROR_MESSAGE)


@router.message(Command("slots"))
async def handle_slots(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    try:
        slots = await list_upcoming_call_slots(db_pool)
    except Exception:
        logger.exception("Erreur lors de la récupération des créneaux d'appel")
        await message.answer(GENERIC_ERROR_MESSAGE)
        return

    if not slots:
        await message.answer("Aucun créneau à venir. Ajoute-en avec /addslot.")
        return

    lines = ["📅 Créneaux à venir :\n"]
    for slot in slots:
        icon = "🔒" if slot.status == "booked" else "🟢"
        lines.append(
            f"{icon} #{slot.id} — {slot.start_at.strftime('%d/%m/%Y %H:%M UTC')} "
            f"({slot.duration_minutes} min) — {slot.status}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("confirm"))
async def handle_confirm(
    message: Message, command: CommandObject, bot: Bot, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    args = (command.args or "").split()
    if len(args) != 1 or not args[0].isdigit():
        await message.answer(CONFIRM_USAGE)
        return
    order_id = int(args[0])

    try:
        order = await get_order(db_pool, order_id)
        if order is None:
            await message.answer(f"Commande #{order_id} introuvable.")
            return
        if order.status != "pending":
            await message.answer(f"Commande #{order_id} déjà traitée (statut : {order.status}).")
            return

        marked = await mark_order_paid(db_pool, order_id)
        if not marked:
            await message.answer(f"Commande #{order_id} déjà traitée entre-temps.")
            return

        summary_lines = [f"✅ Commande #{order_id} confirmée."]

        vip_status = await activate_vip_for_order(db_pool, order_id)
        if vip_status is not None:
            summary_lines.append(f"→ VIP « {vip_status.plan_name} » activé jusqu'au {vip_status.expires_at:%d/%m/%Y}.")
            try:
                await bot.send_message(
                    order.user_id,
                    f"🎉 Ton abonnement VIP « {vip_status.plan_name} » est activé jusqu'au "
                    f"{vip_status.expires_at:%d/%m/%Y} !",
                )
            except Exception:
                logger.exception("Impossible de notifier le client user_id=%s (VIP activé)", order.user_id)

        call_slot = await get_call_slot_for_order(db_pool, order_id)
        if call_slot is not None:
            summary_lines.append(f"→ Appel confirmé pour le {call_slot.start_at:%d/%m/%Y %H:%M} UTC.")
            try:
                await bot.send_message(
                    order.user_id,
                    f"📞 Ton appel du {call_slot.start_at:%d/%m/%Y à %H:%M} UTC est confirmé !",
                )
            except Exception:
                logger.exception("Impossible de notifier le client user_id=%s (appel confirmé)", order.user_id)

        if vip_status is None and call_slot is None:
            try:
                await bot.send_message(order.user_id, f"✅ Ta commande #{order_id} est confirmée, merci !")
            except Exception:
                logger.exception("Impossible de notifier le client user_id=%s (commande confirmée)", order.user_id)

        photo_label = await get_photo_items_label(db_pool, order_id)
        if photo_label:
            who = _who(order.customer_name, order.telegram_username, order.user_id)
            summary_lines.append(f"→ À envoyer : {photo_label} — {who}")
            summary_lines.append(f"/ship {order_id} quand c'est parti.")

        await message.answer("\n".join(summary_lines))
    except Exception:
        logger.exception("Erreur lors de la confirmation de la commande #%s", order_id)
        await message.answer(GENERIC_ERROR_MESSAGE)


@router.message(Command("orders"))
async def handle_orders(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    try:
        tasks = await list_orders_to_ship(db_pool)
    except Exception:
        logger.exception("Erreur lors de la récupération des commandes à envoyer")
        await message.answer(GENERIC_ERROR_MESSAGE)
        return

    if not tasks:
        await message.answer("Rien à envoyer.")
        return

    lines = ["À envoyer :\n"]
    for task in tasks:
        who = _who(task.customer_name, task.telegram_username, task.user_id)
        lines.append(f"#{task.order_id}  {task.items_label}  →  {who}")
    await message.answer("\n".join(lines))


@router.message(Command("ship"))
async def handle_ship(
    message: Message, command: CommandObject, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    args = (command.args or "").split()
    if len(args) != 1 or not args[0].isdigit():
        await message.answer(SHIP_USAGE)
        return
    order_id = int(args[0])

    try:
        shipped = await mark_order_shipped(db_pool, order_id)
    except Exception:
        logger.exception("Erreur lors du marquage envoyé de la commande #%s", order_id)
        await message.answer(GENERIC_ERROR_MESSAGE)
        return

    if shipped:
        await message.answer(f"#{order_id} envoyée.")
    else:
        await message.answer(f"#{order_id} introuvable, pas encore payée, ou déjà envoyée.")
