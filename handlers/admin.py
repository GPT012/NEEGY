"""Commandes réservées à l'administrateur (ADMIN_USER_ID).

Aucun paiement en ligne n'est intégré (v1) : le règlement se fait hors
plateforme, et c'est l'admin qui confirme manuellement la réception du
paiement via /confirm. Toutes les commandes de ce routeur sont filtrées par
identité Telegram (message.from_user.id == config.admin_user_id) ; si
ADMIN_USER_ID n'est pas configuré, aucune commande admin ne se déclenche.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import config, public_inbox_url
from db.inbox_repository import (
    create_chat_agent,
    delete_canned_response,
    list_chat_agents,
    list_canned_responses,
    revoke_chat_agent,
    upsert_canned_response,
)
from db.repository import (
    CartError,
    REWARD_POOL_LABELS,
    VALID_REWARD_POOLS,
    add_reward_asset,
    add_reward_assets_bulk,
    cancel_pending_order,
    clear_product_preview,
    create_call_slot,
    customer_hint_suffix,
    find_user_id_by_username,
    get_call_slot_for_order,
    get_order,
    get_photo_items_label,
    list_client_folders,
    list_grants_for_order,
    list_grants_for_user,
    list_media_products,
    list_orders_to_ship,
    list_pending_orders,
    list_reward_stock,
    list_upcoming_call_slots,
    mark_order_paid,
    mark_order_shipped,
    set_product_preview,
    tag_user,
    untag_user,
)
from handlers.rewards import admin_fulfillment_lines, deliver_order_media
from keyboards.admin import (
    CALLBACK_PAY_PREFIX,
    CALLBACK_PREVIEW_CLEAR_PREFIX,
    CALLBACK_PREVIEW_NO,
    CALLBACK_PREVIEW_OK,
    CALLBACK_PREVIEW_PREFIX,
    CALLBACK_SHIP_PREFIX,
    CALLBACK_STOCK_MENU,
    CALLBACK_STOCK_MORE,
    CALLBACK_STOCK_NO,
    CALLBACK_STOCK_OK,
    CALLBACK_STOCK_POOL_PREFIX,
    CALLBACK_STOCK_RAPID,
    orders_inbox_keyboard,
    product_preview_confirm_keyboard,
    product_preview_waiting_keyboard,
    settings_keyboard,
    shipped_keyboard,
    stock_after_add_keyboard,
    stock_preview_keyboard,
    stock_waiting_keyboard,
)
from keyboards.main_menu import CALLBACK_SETTINGS
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id == config.admin_user_id)
router.callback_query.filter(F.from_user.id == config.admin_user_id)

# Albums Telegram : plusieurs messages partagent media_group_id ; on agrège puis on flush.
_album_buffers: dict[str, list[tuple[str, str, str]]] = {}
_album_tasks: dict[str, asyncio.Task] = {}
_ALBUM_FLUSH_DELAY = 1.2


class StockFSM(StatesGroup):
    waiting_media = State()
    preview = State()
    waiting_product_preview = State()
    confirm_product_preview = State()

GENERIC_ERROR_MESSAGE = "Une erreur est survenue. Merci de réessayer plus tard."

ADDSLOT_USAGE = "Usage : /addslot AAAA-MM-JJ HH:MM DURÉE_MIN (heure en UTC)\nEx: /addslot 2026-08-26 18:00 30"
CONFIRM_USAGE = "Usage : /confirm ID_COMMANDE\nEx: /confirm 42"
STOCK_USAGE = (
    "Envoie une photo ou une vidéo avec la légende /stock POOL\n"
    "ou ouvre le menu : /stock"
)
GRANTS_USAGE = "Usage : /grants ID_COMMANDE ou /grants @username (alias : /rewards)"
FULFILL_USAGE = "Usage : /fulfill ID_COMMANDE\nEx: /fulfill 12"
CANCEL_USAGE = "Usage : /cancel ID_COMMANDE\nEx: /cancel 42"
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
        # Paiement déjà confirmé : on tente quand même de (re)livrer le média.
        fulfillment, send_errors = await deliver_order_media(bot, db_pool, order_id)
        lines = [
            f"Commande #{order_id} déjà {order.status} — relance d'envoi.",
            *admin_fulfillment_lines(fulfillment),
            *(f"⚠ {err}" for err in send_errors),
        ]
        if send_errors:
            lines.append(f"→ Réessaie avec /fulfill {order_id}")
        photo_label = await get_photo_items_label(db_pool, order_id)
        show_ship = bool(photo_label) and not fulfillment.shipped_complete
        return "\n".join(lines), show_ship

    marked, fulfillment = await mark_order_paid(db_pool, order_id)
    if not marked or fulfillment is None:
        return f"Commande #{order_id} déjà traitée entre-temps.", False

    summary_lines = [f"✅ Commande #{order_id} confirmée."]
    # Envoi média avec retries (cœur du parcours cliente).
    fulfillment, send_errors = await deliver_order_media(bot, db_pool, order_id)
    summary_lines.extend(admin_fulfillment_lines(fulfillment))
    summary_lines.extend(f"⚠ {err}" for err in send_errors)
    if send_errors:
        summary_lines.append(
            f"❌ ENVOI INCOMPLET — après /start de la cliente : /fulfill {order_id}"
        )

    call_slot = await get_call_slot_for_order(db_pool, order_id)
    slot_conflict = any("déjà été pris" in w for w in fulfillment.warnings)
    if call_slot is not None and fulfillment.call_slot is None:
        summary_lines.append(f"→ Appel confirmé pour le {call_slot.start_at:%d/%m/%Y %H:%M} UTC.")
        try:
            await bot.send_message(
                order.user_id,
                f"📞 Ton appel du {call_slot.start_at:%d/%m/%Y à %H:%M} UTC est confirmé !",
            )
        except Exception:
            logger.exception("Impossible de notifier le client user_id=%s (appel confirmé)", order.user_id)

    delivered = bool(
        fulfillment.shipped_complete
        or fulfillment.points_amount
        or fulfillment.call_slot
        or (fulfillment.assets and not send_errors)
    )
    if slot_conflict:
        try:
            await bot.send_message(
                order.user_id,
                "✅ Paiement reçu. Le créneau n'était plus libre : on te propose un autre horaire.",
            )
        except Exception:
            logger.exception("Impossible de notifier le client user_id=%s (créneau pris)", order.user_id)
    elif call_slot is None and not delivered and not fulfillment.assets:
        # Pas de média / points / appel : simple accusé.
        try:
            await bot.send_message(order.user_id, f"✅ Ta commande #{order_id} est confirmée, merci !")
        except Exception:
            logger.exception("Impossible de notifier le client user_id=%s (commande confirmée)", order.user_id)

    photo_label = await get_photo_items_label(db_pool, order_id)
    show_ship = bool(photo_label) and not fulfillment.shipped_complete
    if show_ship:
        who = _who(order.customer_name, order.telegram_username, order.user_id)
        summary_lines.append(f"→ À envoyer / relancer : {photo_label} — {who}")
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


@router.message(Command("cancel"))
async def handle_cancel(
    message: Message, command: CommandObject, bot: Bot, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return

    args = (command.args or "").split()
    if len(args) != 1 or not args[0].isdigit():
        await message.answer(CANCEL_USAGE)
        return
    order_id = int(args[0])

    try:
        order = await get_order(db_pool, order_id)
        if order is None:
            await message.answer(f"Commande #{order_id} introuvable.")
            return
        if order.status != "pending":
            await message.answer(f"Commande #{order_id} n'est pas en attente (statut : {order.status}).")
            return
        cancelled = await cancel_pending_order(db_pool, order_id)
        if not cancelled:
            await message.answer(f"Commande #{order_id} n'est plus en attente.")
            return
        try:
            await bot.send_message(
                order.user_id,
                f"Ta commande #{order_id} n'est plus en attente. Tu peux en passer une autre.",
            )
        except Exception:
            logger.exception("Impossible de notifier le client user_id=%s (commande annulée)", order.user_id)
        await message.answer(f"🗑 Commande #{order_id} annulée. Le client peut recommander.")
    except Exception:
        logger.exception("Erreur lors de l'annulation de la commande #%s", order_id)
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
    message: Message, command: CommandObject, bot: Bot, db_pool: asyncpg.Pool | None
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
        fulfillment, send_errors = await deliver_order_media(bot, db_pool, order_id)
        if send_errors:
            await message.answer(
                f"#{order_id} envoi incomplet.\n"
                + "\n".join(f"⚠ {e}" for e in send_errors)
            )
            return
        shipped = fulfillment.shipped_complete or await mark_order_shipped(db_pool, order_id)
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


def _extract_stock_media(message: Message) -> tuple[str, str, str] | None:
    """Accepte photo, vidéo, note vidéo, ou fichier vidéo envoyé comme document."""
    src = message
    if (
        not message.photo
        and not message.video
        and not message.video_note
        and not message.document
        and message.reply_to_message
    ):
        src = message.reply_to_message
    if src.photo:
        shot = src.photo[-1]
        return "photo", shot.file_id, shot.file_unique_id
    if src.video:
        return "video", src.video.file_id, src.video.file_unique_id
    if src.video_note:
        return "video", src.video_note.file_id, src.video_note.file_unique_id
    if src.document:
        mime = (src.document.mime_type or "").lower()
        if mime.startswith("video/"):
            return "video", src.document.file_id, src.document.file_unique_id
        if mime.startswith("image/"):
            return "photo", src.document.file_id, src.document.file_unique_id
    return None


def _pool_title(pool_name: str) -> str:
    return REWARD_POOL_LABELS.get(pool_name, pool_name)


def _expected_kind_label(kind: str) -> str:
    return "une vidéo" if kind == "video" else "une photo"


async def _stock_menu_content(db_pool: asyncpg.Pool) -> tuple[str, object]:
    rows = await list_reward_stock(db_pool)
    counts = {name: unused for name, _total, unused in rows}
    media = await list_media_products(db_pool)
    lines = [
        "⚙️ Paramètres\n",
        "Stock (files partagées) :\n"
        "• File photos → packs Photo + lot photo Rose\n"
        "• File vidéos → packs Vidéo + lots vidéo Rose / Nuit\n"
        "Chaque fichier n'est donné qu'à une seule cliente.\n"
        "Astuce stock : /depot (Drive) ou album / mode rapide.\n",
        "Previews boutique : 1 média par tarif, visible au clic.\n",
    ]
    for name, total, unused in rows:
        lines.append(f"• {_pool_title(name)} — {total} fichier(s), {unused} libre(s)")
    for product in media:
        mark = "définie" if product.has_preview else "absente"
        lines.append(f"• Preview {product.name} — {mark}")
    return "\n".join(lines), settings_keyboard(counts, media)



async def _send_stock_menu(message: Message, db_pool: asyncpg.Pool) -> None:
    text, markup = await _stock_menu_content(db_pool)
    await message.answer(text, reply_markup=markup)


async def _show_stock_menu(message: Message, db_pool: asyncpg.Pool) -> None:
    text, markup = await _stock_menu_content(db_pool)
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=markup)


async def _ask_for_stock_file(
    target: Message, state: FSMContext, pool_name: str, *, edit: bool = False
) -> None:
    kind = VALID_REWARD_POOLS[pool_name]
    data = await state.get_data()
    rapid = bool(data.get("rapid"))
    await state.set_state(StockFSM.waiting_media)
    await state.update_data(mode="stock", pool=pool_name, kind=kind, product_id=None)
    mode_line = (
        "⚡ Mode rapide : albums et fichiers ajoutés sans preview unitaire.\n"
        if rapid
        else "Tu peux envoyer un album (plusieurs photos/vidéos d'un coup).\n"
        "Ou active le mode rapide pour sauter la preview.\n"
    )
    text = (
        f"Envoie {_expected_kind_label(kind)} pour « {_pool_title(pool_name)} ».\n"
        f"{mode_line}"
    )
    markup = stock_waiting_keyboard(rapid=rapid)
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == CALLBACK_STOCK_RAPID)
async def handle_stock_rapid_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    rapid = not bool(data.get("rapid"))
    await state.update_data(rapid=rapid)
    pool_name = data.get("pool")
    await callback.answer("Mode rapide ON" if rapid else "Mode rapide OFF")
    if pool_name in VALID_REWARD_POOLS and callback.message:
        await _ask_for_stock_file(callback.message, state, pool_name, edit=True)


async def _flush_album_stock(
    *,
    album_key: str,
    message: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
) -> None:
    await asyncio.sleep(_ALBUM_FLUSH_DELAY)
    items = _album_buffers.pop(album_key, [])
    _album_tasks.pop(album_key, None)
    data = await state.get_data()
    pool_name = data.get("pool")
    if not items or pool_name not in VALID_REWARD_POOLS:
        return
    try:
        added, skipped = await add_reward_assets_bulk(
            db_pool, pool_name=pool_name, items=items
        )
    except Exception:
        logger.exception("Erreur album stock %s", pool_name)
        await message.answer(GENERIC_ERROR_MESSAGE)
        return
    await state.set_state(StockFSM.waiting_media)
    await message.answer(
        f"✅ Album : {added} ajouté(s) dans « {_pool_title(pool_name)} »"
        + (f", {skipped} doublon(s) ignoré(s)." if skipped else "."),
        reply_markup=stock_after_add_keyboard(),
    )


async def _add_stock_item_now(
    message: Message,
    state: FSMContext,
    db_pool: asyncpg.Pool,
    pool_name: str,
    kind: str,
    file_id: str,
    unique_id: str,
) -> None:
    try:
        asset_id = await add_reward_asset(
            db_pool,
            pool_name=pool_name,
            kind=kind,
            telegram_file_id=file_id,
            file_unique_id=unique_id,
        )
    except CartError as exc:
        await message.answer(str(exc))
        return
    except Exception:
        logger.exception("Erreur ajout stock rapide %s", pool_name)
        await message.answer(GENERIC_ERROR_MESSAGE)
        return
    await state.set_state(StockFSM.waiting_media)
    await state.update_data(file_id=None, unique_id=None)
    await message.answer(
        f"✅ #{asset_id} ajouté dans « {_pool_title(pool_name)} ».",
        reply_markup=stock_after_add_keyboard(),
    )


async def _ask_for_product_preview(
    target: Message, state: FSMContext, product_id: int, product_name: str, *, edit: bool = False
) -> None:
    await state.set_state(StockFSM.waiting_product_preview)
    await state.update_data(mode="preview", product_id=product_id, product_name=product_name)
    text = (
        f"Envoie une photo ou une vidéo pour la preview de « {product_name} ».\n"
        "Les clientes la verront en cliquant sur ce tarif dans la boutique."
    )
    markup = product_preview_waiting_keyboard()
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == CALLBACK_SETTINGS)
async def handle_admin_settings(
    callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    if db_pool is None:
        await callback.answer("Base de données indisponible.", show_alert=True)
        return
    await state.clear()
    try:
        await _show_stock_menu(callback.message, db_pool)
        await callback.answer()
    except Exception:
        logger.exception("Erreur paramètres stock")
        await callback.answer(GENERIC_ERROR_MESSAGE, show_alert=True)


@router.message(Command("slot", "drive_slot"))
async def handle_drive_slot(message: Message, command: CommandObject) -> None:
    from services.drive import (
        audit_slot_by_path,
        clear_drive_service_cache,
        is_drive_configured,
        parse_slot_args,
    )

    if not is_drive_configured():
        await message.answer("Drive non configuré — voir /drive_check")
        return
    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Vérifier un slot Drive (lu en direct, pas de sync) :\n\n"
            "/slot photo 5 1\n"
            "/slot video 10 1\n"
            "ou /slot photos/5/slot_01\n\n"
            "Seul /slot ou /drive_slot apparaît en bleu dans Telegram ; "
            "le reste est à taper après un espace."
        )
        return
    slot_path = parse_slot_args(raw_args) or raw_args.replace(" ", "")
    clear_drive_service_cache()
    try:
        lines = await asyncio.wait_for(
            asyncio.to_thread(audit_slot_by_path, slot_path),
            timeout=20,
        )
        await message.answer("\n".join(lines))
    except asyncio.TimeoutError:
        await message.answer("Timeout — réessaie.")
    except Exception:
        logger.exception("drive_slot")
        await message.answer(GENERIC_ERROR_MESSAGE)


@router.message(Command("drive_check"))
async def handle_drive_check(message: Message) -> None:
    from config import describe_drive_env
    from services.drive import audit_structure, clear_drive_service_cache, is_drive_configured

    if not is_drive_configured():
        diag = "\n".join(describe_drive_env())
        await message.answer(
            "Google Drive pas encore actif.\n\n"
            f"{diag}\n\n"
            "Rappel : partage le dossier Drive avec\n"
            "neegs-965@neegy-506816.iam.gserviceaccount.com (Lecteur)."
        )
        return

    status = await message.answer(
        "Vérification Drive…\n"
        "Si rien ne vient en 20s, le problème est côté Google (API / partage)."
    )
    clear_drive_service_cache()

    async def _run() -> list[str]:
        return await asyncio.to_thread(audit_structure)

    try:
        lines = await asyncio.wait_for(_run(), timeout=20)
        text = "📁 Drive check\n\n" + "\n".join(lines)
        if len(text) > 4000:
            text = text[:3900] + "\n…"
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
    except asyncio.TimeoutError:
        text = (
            "⏱ Timeout 20s — Google ne répond pas.\n\n"
            "Fais ces 2 checks maintenant :\n"
            "1) https://console.cloud.google.com/apis/library/drive.googleapis.com\n"
            "   Projet neegy-506816 → bouton Activer\n"
            "2) Drive → ton dossier → Partager →\n"
            "   neegs-965@neegy-506816.iam.gserviceaccount.com → Lecteur\n\n"
            "Sans ces 2 points, le bot ne peut pas lire le Drive."
        )
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
    except Exception as exc:
        logger.exception("drive_check")
        text = f"❌ Erreur Drive : {exc}"
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)


@router.message(Command("stock"))
async def handle_stock(
    message: Message, command: CommandObject, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return
    pool_name = (command.args or "").split()[0].lower() if command.args else ""
    if not pool_name:
        try:
            await state.clear()
            await _send_stock_menu(message, db_pool)
        except Exception:
            logger.exception("Erreur /stock liste")
            await message.answer(GENERIC_ERROR_MESSAGE)
        return
    if pool_name not in VALID_REWARD_POOLS:
        await message.answer(STOCK_USAGE)
        return
    media = _extract_stock_media(message)
    if media is None:
        await _ask_for_stock_file(message, state, pool_name)
        return
    kind, file_id, unique_id = media
    await state.set_state(StockFSM.preview)
    await state.update_data(pool=pool_name, kind=kind, file_id=file_id, unique_id=unique_id)
    await _send_stock_preview(message, pool_name, kind, file_id)


async def _send_stock_preview(message: Message, pool_name: str, kind: str, file_id: str) -> None:
    caption = f"Aperçu — {_pool_title(pool_name)}"
    markup = stock_preview_keyboard()
    try:
        if kind == "video":
            await message.answer_video(file_id, caption=caption, reply_markup=markup)
        else:
            await message.answer_photo(file_id, caption=caption, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer_document(file_id, caption=caption, reply_markup=markup)


@router.callback_query(F.data.startswith(CALLBACK_STOCK_POOL_PREFIX))
async def handle_stock_pool_pick(callback: CallbackQuery, state: FSMContext) -> None:
    pool_name = (callback.data or "")[len(CALLBACK_STOCK_POOL_PREFIX) :]
    if pool_name not in VALID_REWARD_POOLS:
        await callback.answer("Slot inconnu.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _ask_for_stock_file(callback.message, state, pool_name, edit=True)


@router.callback_query(F.data == CALLBACK_STOCK_MENU)
async def handle_stock_menu(
    callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    await state.clear()
    await callback.answer()
    if db_pool is None or callback.message is None:
        return
    await _show_stock_menu(callback.message, db_pool)


@router.callback_query(F.data == CALLBACK_STOCK_MORE)
async def handle_stock_more(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pool_name = data.get("pool")
    if pool_name not in VALID_REWARD_POOLS:
        await callback.answer("Choisis d'abord un slot.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _ask_for_stock_file(callback.message, state, pool_name, edit=True)


@router.message(StateFilter(StockFSM.waiting_media), F.photo | F.video | F.video_note | F.document)
async def handle_stock_media(
    message: Message, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    data = await state.get_data()
    pool_name = data.get("pool")
    expected = data.get("kind")
    rapid = bool(data.get("rapid"))
    media = _extract_stock_media(message)
    if pool_name not in VALID_REWARD_POOLS or media is None:
        await message.answer("Envoie une photo ou une vidéo (fichier vidéo accepté aussi).")
        return
    kind, file_id, unique_id = media
    if kind != expected:
        await message.answer(
            f"Ce slot attend {_expected_kind_label(str(expected))}. Envoie le bon type de fichier."
        )
        return

    # Album Telegram → ajout en masse après regroupement.
    if message.media_group_id and db_pool is not None:
        album_key = f"{message.chat.id}:{message.media_group_id}"
        _album_buffers.setdefault(album_key, []).append((kind, file_id, unique_id))
        old = _album_tasks.get(album_key)
        if old and not old.done():
            old.cancel()
        _album_tasks[album_key] = asyncio.create_task(
            _flush_album_stock(
                album_key=album_key, message=message, state=state, db_pool=db_pool
            )
        )
        return

    if rapid and db_pool is not None:
        await _add_stock_item_now(
            message, state, db_pool, pool_name, kind, file_id, unique_id
        )
        return

    await state.set_state(StockFSM.preview)
    await state.update_data(file_id=file_id, unique_id=unique_id, kind=kind)
    await _send_stock_preview(message, pool_name, kind, file_id)


@router.callback_query(StateFilter(StockFSM.preview), F.data == CALLBACK_STOCK_OK)
async def handle_stock_confirm(
    callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await callback.answer("Base indisponible.", show_alert=True)
        return
    data = await state.get_data()
    pool_name = data.get("pool")
    kind = data.get("kind")
    file_id = data.get("file_id")
    unique_id = data.get("unique_id")
    if pool_name not in VALID_REWARD_POOLS or not file_id or not unique_id or not kind:
        await callback.answer("Aperçu expiré. Renvoie le fichier.", show_alert=True)
        await state.clear()
        return
    try:
        asset_id = await add_reward_asset(
            db_pool,
            pool_name=pool_name,
            kind=kind,
            telegram_file_id=file_id,
            file_unique_id=unique_id,
        )
    except CartError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("Erreur ajout stock %s", pool_name)
        await callback.answer(GENERIC_ERROR_MESSAGE, show_alert=True)
        return
    await state.set_state(StockFSM.waiting_media)
    await state.update_data(file_id=None, unique_id=None)
    await callback.answer("Ajouté")
    if callback.message:
        await callback.message.answer(
            f"✅ #{asset_id} ajouté dans « {_pool_title(pool_name)} ».",
            reply_markup=stock_after_add_keyboard(),
        )


@router.callback_query(F.data == CALLBACK_STOCK_NO)
async def handle_stock_cancel_preview(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pool_name = data.get("pool")
    await callback.answer("Annulé")
    if pool_name in VALID_REWARD_POOLS and callback.message:
        await _ask_for_stock_file(callback.message, state, pool_name)
    else:
        await state.clear()


@router.callback_query(F.data.startswith(CALLBACK_PREVIEW_PREFIX))
async def handle_preview_product_pick(
    callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    raw = (callback.data or "")[len(CALLBACK_PREVIEW_PREFIX) :]
    if not raw.isdigit() or db_pool is None:
        await callback.answer("Produit inconnu.", show_alert=True)
        return
    product_id = int(raw)
    products = await list_media_products(db_pool)
    product = next((p for p in products if p.id == product_id), None)
    if product is None:
        await callback.answer("Produit introuvable.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _ask_for_product_preview(
            callback.message, state, product.id, product.name, edit=True
        )


@router.message(
    StateFilter(StockFSM.waiting_product_preview),
    F.photo | F.video | F.video_note | F.document,
)
async def handle_product_preview_media(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    product_id = data.get("product_id")
    product_name = data.get("product_name") or "ce tarif"
    media = _extract_stock_media(message)
    if not product_id or media is None:
        await message.answer("Envoie une photo ou une vidéo.")
        return
    kind, file_id, unique_id = media
    await state.set_state(StockFSM.confirm_product_preview)
    await state.update_data(kind=kind, file_id=file_id, unique_id=unique_id)
    caption = f"Preview — {product_name}"
    markup = product_preview_confirm_keyboard(int(product_id))
    try:
        if kind == "video":
            await message.answer_video(file_id, caption=caption, reply_markup=markup)
        else:
            await message.answer_photo(file_id, caption=caption, reply_markup=markup)
    except TelegramBadRequest:
        await message.answer_document(file_id, caption=caption, reply_markup=markup)


@router.callback_query(StateFilter(StockFSM.confirm_product_preview), F.data == CALLBACK_PREVIEW_OK)
async def handle_product_preview_confirm(
    callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await callback.answer("Base indisponible.", show_alert=True)
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    kind = data.get("kind")
    file_id = data.get("file_id")
    unique_id = data.get("unique_id")
    if not product_id or not kind or not file_id or not unique_id:
        await callback.answer("Aperçu expiré.", show_alert=True)
        await state.clear()
        return
    try:
        name = await set_product_preview(
            db_pool,
            product_id=int(product_id),
            kind=kind,
            telegram_file_id=file_id,
            file_unique_id=unique_id,
        )
        _ = name
    except CartError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("Erreur preview produit #%s", product_id)
        await callback.answer(GENERIC_ERROR_MESSAGE, show_alert=True)
        return
    await state.clear()
    await callback.answer("Preview enregistrée")
    if callback.message:
        await _show_stock_menu(callback.message, db_pool)


@router.callback_query(F.data == CALLBACK_PREVIEW_NO)
async def handle_product_preview_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    product_id = data.get("product_id")
    product_name = data.get("product_name") or "ce tarif"
    await callback.answer("Annulé")
    if product_id and callback.message:
        await _ask_for_product_preview(
            callback.message, state, int(product_id), str(product_name)
        )
    else:
        await state.clear()


@router.callback_query(F.data.startswith(CALLBACK_PREVIEW_CLEAR_PREFIX))
async def handle_product_preview_clear(
    callback: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    raw = (callback.data or "")[len(CALLBACK_PREVIEW_CLEAR_PREFIX) :]
    if not raw.isdigit() or db_pool is None:
        await callback.answer("Produit inconnu.", show_alert=True)
        return
    cleared = await clear_product_preview(db_pool, int(raw))
    await state.clear()
    await callback.answer("Preview effacée" if cleared else "Rien à effacer")
    if callback.message:
        await _show_stock_menu(callback.message, db_pool)


@router.message(Command("grants", "rewards"))
async def handle_grants(message: Message, command: CommandObject, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(GRANTS_USAGE)
        return
    try:
        if raw.startswith("@") or (raw and not raw.isdigit()):
            user_id = await find_user_id_by_username(db_pool, raw)
            if user_id is None:
                await message.answer("Aucun client avec ce @.")
                return
            rows = await list_grants_for_user(db_pool, user_id)
            header = f"Lots de {raw} :"
        else:
            order_id = int(raw)
            rows = await list_grants_for_order(db_pool, order_id)
            header = f"Lots de la commande #{order_id} :"
    except Exception:
        logger.exception("Erreur /grants")
        await message.answer(GENERIC_ERROR_MESSAGE)
        return
    if not rows:
        await message.answer("Aucun lot enregistré.")
        return
    lines = [header, ""]
    for row in rows:
        when = row.created_at.strftime("%d/%m %H:%M")
        ref = f" #{row.order_id}" if row.order_id else ""
        lines.append(f"• {when} — {row.pool} ({row.kind}, {row.source}){ref}")
    await message.answer("\n".join(lines))


@router.message(Command("fulfill"))
async def handle_fulfill(
    message: Message, command: CommandObject, bot: Bot, db_pool: asyncpg.Pool | None
) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible pour le moment.")
        return
    args = (command.args or "").split()
    if len(args) != 1 or not args[0].isdigit():
        await message.answer(FULFILL_USAGE)
        return
    order_id = int(args[0])
    try:
        fulfillment, send_errors = await deliver_order_media(bot, db_pool, order_id)
        lines = [f"Relance #{order_id}."]
        lines.extend(admin_fulfillment_lines(fulfillment))
        lines.extend(f"⚠ {err}" for err in send_errors)
        if not send_errors and fulfillment.shipped_complete:
            lines.append("✅ Livré.")
        await message.answer("\n".join(lines))
    except Exception:
        logger.exception("Erreur /fulfill #%s", order_id)
        await message.answer(GENERIC_ERROR_MESSAGE)


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
        failed = "ENVOI INCOMPLET" in text or "file vide" in text or "Stock insuffisant" in text
        if _is_orders_inbox(callback):
            await callback.answer(
                "Échec envoi — détail en message" if failed else "Confirmé",
                show_alert=failed,
            )
            inbox_text, inbox_markup = await _orders_inbox(db_pool)
            if callback.message is not None:
                try:
                    await callback.message.edit_text(inbox_text, reply_markup=inbox_markup)
                except TelegramBadRequest:
                    pass
            if failed or "⚠" in text:
                await bot.send_message(callback.from_user.id, text)
            return
        await _respond_admin_callback(
            callback,
            text,
            shipped_keyboard(order_id) if show_ship else None,
            toast="Échec envoi" if failed else "Confirmé",
        )
    except Exception:
        logger.exception("Erreur callback paiement commande #%s", order_id)
        await callback.answer(GENERIC_ERROR_MESSAGE, show_alert=True)


@router.callback_query(F.from_user.id == config.admin_user_id, F.data.startswith(CALLBACK_SHIP_PREFIX))
async def handle_ship_callback(
    callback: CallbackQuery, bot: Bot, db_pool: asyncpg.Pool | None
) -> None:
    order_id = _parse_order_callback(callback.data, CALLBACK_SHIP_PREFIX)
    if order_id is None:
        await callback.answer("Commande invalide.", show_alert=True)
        return
    if db_pool is None:
        await callback.answer("Base indisponible.", show_alert=True)
        return
    try:
        fulfillment, send_errors = await deliver_order_media(bot, db_pool, order_id)
        if send_errors:
            await callback.answer("Envoi incomplet", show_alert=True)
            await bot.send_message(
                callback.from_user.id,
                f"#{order_id}\n" + "\n".join(f"⚠ {e}" for e in send_errors),
            )
            if _is_orders_inbox(callback):
                inbox_text, inbox_markup = await _orders_inbox(db_pool)
                if callback.message is not None:
                    try:
                        await callback.message.edit_text(inbox_text, reply_markup=inbox_markup)
                    except TelegramBadRequest:
                        pass
            return
        shipped = fulfillment.shipped_complete or await mark_order_shipped(db_pool, order_id)
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


@router.message(Command("agent_add"))
async def handle_agent_add(message: Message, command: CommandObject, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible.")
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Usage : /agent_add Prénom\nExemple : /agent_add Sarah")
        return
    try:
        agent, token = await create_chat_agent(db_pool, name)
    except Exception:
        logger.exception("Erreur /agent_add")
        await message.answer(GENERIC_ERROR_MESSAGE)
        return
    await message.answer(
        f"✅ Chatteur « {escape(agent.name)} » créé.\n\n"
        f"Identifiant : {escape(agent.name)}\n"
        f"Token (à copier une seule fois) :\n<code>{escape(token)}</code>\n\n"
        f"Connexion inbox : {public_inbox_url()}\n"
        "Ne partage pas ce token publiquement."
    )


@router.message(Command("agents"))
async def handle_agents(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible.")
        return
    agents = await list_chat_agents(db_pool)
    if not agents:
        await message.answer("Aucun chatteur. Ajoute-en un avec /agent_add Prénom")
        return
    lines = ["Chatteurs inbox :\n"]
    for agent in agents:
        status = "actif" if agent.is_active else "révoqué"
        lines.append(f"• {escape(agent.name)} — {status}")
    await message.answer("\n".join(lines))


@router.message(Command("agent_revoke"))
async def handle_agent_revoke(message: Message, command: CommandObject, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible.")
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Usage : /agent_revoke Prénom")
        return
    revoked = await revoke_chat_agent(db_pool, name)
    if revoked:
        await message.answer(f"🚫 Accès inbox révoqué pour « {escape(name)} ».")
    else:
        await message.answer(f"Aucun chatteur actif nommé « {escape(name)} ».")


def _parse_canned_args(raw: str) -> tuple[str, str] | None:
    text = (raw or "").strip()
    if not text:
        return None
    if " | " in text:
        shortcut, content = text.split(" | ", 1)
        shortcut, content = shortcut.strip(), content.strip()
        return (shortcut, content) if shortcut and content else None
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


@router.message(Command("canned_add"))
async def handle_canned_add(message: Message, command: CommandObject, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible.")
        return
    parsed = _parse_canned_args(command.args or "")
    if not parsed:
        await message.answer(
            "Usage : /canned_add raccourci Message\n"
            "Exemple : /canned_add relance Hey bb, tu es toujours là ?\n"
            "Ou : /canned_add promo | Offre spéciale ce soir 🔥"
        )
        return
    shortcut, content = parsed
    await upsert_canned_response(db_pool, shortcut, content)
    await message.answer(
        f"✅ Commande /{escape(shortcut.lower())} enregistrée.\n\n"
        f"Les chatteurs peuvent taper /{escape(shortcut.lower())} dans l'inbox."
    )


@router.message(Command("canned_list"))
async def handle_canned_list(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible.")
        return
    items = await list_canned_responses(db_pool)
    if not items:
        await message.answer("Aucune commande. Ajoute-en avec /canned_add")
        return
    lines = ["Commandes inbox disponibles :\n"]
    for item in items:
        preview = item.content.replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:57] + "…"
        lines.append(f"/{escape(item.shortcut)} — {escape(preview)}")
    await message.answer("\n".join(lines))


@router.message(Command("canned_del"))
async def handle_canned_del(message: Message, command: CommandObject, db_pool: asyncpg.Pool | None) -> None:
    if db_pool is None:
        await message.answer("Base de données indisponible.")
        return
    shortcut = (command.args or "").strip()
    if not shortcut:
        await message.answer("Usage : /canned_del raccourci\nExemple : /canned_del relance")
        return
    deleted = await delete_canned_response(db_pool, shortcut)
    if deleted:
        await message.answer(f"🗑 Commande /{escape(shortcut.lower())} supprimée.")
    else:
        await message.answer(f"Aucune commande active /{escape(shortcut.lower())}.")
