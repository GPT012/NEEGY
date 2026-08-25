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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from config import config
from db.repository import (
    activate_vip_for_order,
    create_call_slot,
    customer_hint_suffix,
    get_call_slot_for_order,
    get_order,
    get_photo_items_label,
    list_client_folders,
    list_orders_to_ship,
    list_pending_orders,
    list_upcoming_call_slots,
    mark_order_paid,
    mark_order_shipped,
    tag_user,
    untag_user,
)
from keyboards.admin import (
    CALLBACK_PAY_PREFIX,
    CALLBACK_SHIP_PREFIX,
    orders_inbox_keyboard,
    shipped_keyboard,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id == config.admin_user_id)

GENERIC_ERROR_MESSAGE = "Une erreur est survenue. Merci de réessayer plus tard."

ADDSLOT_USAGE = "Usage : /addslot AAAA-MM-JJ HH:MM DURÉE_MIN (heure en UTC)\nEx: /addslot 2026-08-26 18:00 30"
CONFIRM_USAGE = "Usage : /confirm ID_COMMANDE\nEx: /confirm 42"
SHIP_USAGE = "Usage : /ship ID_COMMANDE\nEx: /ship 42"
TAG_USAGE = "Usage : /tag ID_COMMANDE dossier\nEx: /tag 12 proches"
UNTAG_USAGE = "Usage : /untag ID_COMMANDE dossier\nEx: /untag 12 proches"


def _normalize_folder_name(raw: str) -> str | None:
    name = " ".join(raw.split())
    if not name or len(name) > 32:
        return None
    return name


def _who(name: str | None, username: str | None, user_id: int) -> str:
    if username:
        return f"@{escape(username)}"
    if name:
        return escape(name)
    return f"user {user_id}"


def _is_orders_inbox(callback: CallbackQuery) -> bool:
    text = callback.message.text if callback.message else ""
    if not text:
        return False
    return text.startswith(("À encaisser", "À envoyer", "Rien en attente"))


def _task_line(task) -> str:
    who = _who(task.customer_name, task.telegram_username, task.user_id)
    suffix = escape(customer_hint_suffix(task))
    return f"#{task.order_id}  {escape(task.items_label)}  →  {who}{suffix}"


async def _orders_inbox(db_pool: asyncpg.Pool) -> tuple[str, object]:
    pending = await list_pending_orders(db_pool)
    to_ship = await list_orders_to_ship(db_pool)
    if not pending and not to_ship:
        return "Rien en attente.", None

    lines: list[str] = []
    if pending:
        lines.append("À encaisser :\n")
        lines.extend(_task_line(task) for task in pending)
    if to_ship:
        if lines:
            lines.append("")
        lines.append("À envoyer :\n")
        lines.extend(_task_line(task) for task in to_ship)

    markup = orders_inbox_keyboard(
        [task.order_id for task in pending],
        [task.order_id for task in to_ship],
    )
    return "\n".join(lines), markup


def _parse_order_callback(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :]
    if not raw.isdigit():
        return None
    return int(raw)


async def _confirm_payment(bot: Bot, db_pool: asyncpg.Pool, order_id: int) -> tuple[str, bool]:
    """Enregistre le paiement. Retourne (texte admin, afficher le bouton Envoyé)."""
    order = await get_order(db_pool, order_id)
    if order is None:
        return f"Commande #{order_id} introuvable.", False
    if order.status != "pending":
        return f"Commande #{order_id} déjà traitée (statut : {order.status}).", False

    marked = await mark_order_paid(db_pool, order_id)
    if not marked:
        return f"Commande #{order_id} déjà traitée entre-temps.", False

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
        return "\n".join(summary_lines), True

    return "\n".join(summary_lines), False


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
        text, show_ship = await _confirm_payment(bot, db_pool, order_id)
        await message.answer(
            text,
            reply_markup=shipped_keyboard(order_id) if show_ship else None,
        )
    except Exception:
        logger.exception("Erreur lors de la confirmation de la commande #%s", order_id)
        await message.answer(GENERIC_ERROR_MESSAGE)


@router.message(Command("orders"))
async def handle_orders(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    try:
        text, markup = await _orders_inbox(db_pool)
        await message.answer(text, reply_markup=markup)
    except Exception:
        logger.exception("Erreur lors de la récupération des commandes")
        await message.answer(GENERIC_ERROR_MESSAGE)


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


@router.message(Command("tag"))
async def handle_tag(
    message: Message, command: CommandObject, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return
    args = (command.args or "").split(maxsplit=1)
    if len(args) != 2 or not args[0].isdigit():
        await message.answer(TAG_USAGE)
        return
    folder = _normalize_folder_name(args[1])
    if folder is None:
        await message.answer("Nom de dossier vide ou trop long (max 32 caractères).")
        return
    order_id = int(args[0])
    try:
        order = await get_order(db_pool, order_id)
        if order is None:
            await message.answer(f"Commande #{order_id} introuvable.")
            return
        name = await tag_user(db_pool, order.user_id, folder)
        who = _who(order.customer_name, order.telegram_username, order.user_id)
        await message.answer(f"{who} → dossier « {escape(name)} ».")
    except Exception:
        logger.exception("Erreur /tag commande #%s", order_id)
        await message.answer(GENERIC_ERROR_MESSAGE)


@router.message(Command("untag"))
async def handle_untag(
    message: Message, command: CommandObject, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return
    args = (command.args or "").split(maxsplit=1)
    if len(args) != 2 or not args[0].isdigit():
        await message.answer(UNTAG_USAGE)
        return
    folder = _normalize_folder_name(args[1])
    if folder is None:
        await message.answer("Nom de dossier vide ou trop long (max 32 caractères).")
        return
    order_id = int(args[0])
    try:
        order = await get_order(db_pool, order_id)
        if order is None:
            await message.answer(f"Commande #{order_id} introuvable.")
            return
        removed = await untag_user(db_pool, order.user_id, folder)
        who = _who(order.customer_name, order.telegram_username, order.user_id)
        if removed:
            await message.answer(f"{who} retirée de « {escape(folder)} ».")
        else:
            await message.answer(f"{who} n'était pas dans « {escape(folder)} ».")
    except Exception:
        logger.exception("Erreur /untag commande #%s", order_id)
        await message.answer(GENERIC_ERROR_MESSAGE)


@router.message(Command("folders"))
async def handle_folders(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return
    try:
        folders = await list_client_folders(db_pool)
    except Exception:
        logger.exception("Erreur /folders")
        await message.answer(GENERIC_ERROR_MESSAGE)
        return
    if not folders:
        await message.answer("Aucun dossier. Ajoute quelqu'un avec /tag 12 proches.")
        return
    lines = ["Dossiers :\n"]
    for folder in folders:
        lines.append(f"• {escape(folder.name)} ({folder.member_count})")
    await message.answer("\n".join(lines))


async def _respond_admin_callback(
    callback: CallbackQuery, text: str, markup=None, toast: str | None = None
) -> None:
    if toast:
        await callback.answer(toast)
    else:
        await callback.answer()
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await callback.message.answer(text, reply_markup=markup)


async def _refresh_orders_inbox(callback: CallbackQuery, db_pool: asyncpg.Pool, toast: str) -> None:
    text, markup = await _orders_inbox(db_pool)
    await _respond_admin_callback(callback, text, markup, toast=toast)


@router.callback_query(F.from_user.id == config.admin_user_id, F.data.startswith(CALLBACK_PAY_PREFIX))
async def handle_pay_callback(
    callback: CallbackQuery, bot: Bot, db_pool: asyncpg.Pool | None
) -> None:
    order_id = _parse_order_callback(callback.data, CALLBACK_PAY_PREFIX)
    if order_id is None:
        await callback.answer("Commande invalide.", show_alert=True)
        return
    if db_pool is None:
        await callback.answer("Base indisponible.", show_alert=True)
        return
    try:
        text, show_ship = await _confirm_payment(bot, db_pool, order_id)
        if _is_orders_inbox(callback):
            toast = "Confirmé" if "confirmée" in text else text[:180]
            await _refresh_orders_inbox(callback, db_pool, toast=toast)
            return
        await _respond_admin_callback(
            callback,
            text,
            shipped_keyboard(order_id) if show_ship else None,
            toast="Confirmé",
        )
    except Exception:
        logger.exception("Erreur callback paiement commande #%s", order_id)
        await callback.answer(GENERIC_ERROR_MESSAGE, show_alert=True)


@router.callback_query(F.from_user.id == config.admin_user_id, F.data.startswith(CALLBACK_SHIP_PREFIX))
async def handle_ship_callback(callback: CallbackQuery, db_pool: asyncpg.Pool | None) -> None:
    order_id = _parse_order_callback(callback.data, CALLBACK_SHIP_PREFIX)
    if order_id is None:
        await callback.answer("Commande invalide.", show_alert=True)
        return
    if db_pool is None:
        await callback.answer("Base indisponible.", show_alert=True)
        return
    try:
        shipped = await mark_order_shipped(db_pool, order_id)
    except Exception:
        logger.exception("Erreur callback envoi commande #%s", order_id)
        await callback.answer(GENERIC_ERROR_MESSAGE, show_alert=True)
        return
    if shipped and _is_orders_inbox(callback):
        await _refresh_orders_inbox(callback, db_pool, toast="Envoyée")
        return
    if shipped:
        await _respond_admin_callback(callback, f"#{order_id} envoyée.", toast="Envoyée")
    else:
        await callback.answer("Déjà envoyée, ou pas encore payée.", show_alert=True)
