"""Boutons sous les notifications de commande (admin)."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db.repository import REWARD_POOL_LABELS, VALID_REWARD_POOLS

CALLBACK_PAY_PREFIX = "adm:pay:"
CALLBACK_SHIP_PREFIX = "adm:ship:"
CALLBACK_STOCK_POOL_PREFIX = "adm:stk:p:"
CALLBACK_STOCK_OK = "adm:stk:ok"
CALLBACK_STOCK_NO = "adm:stk:no"
CALLBACK_STOCK_MORE = "adm:stk:more"
CALLBACK_STOCK_MENU = "adm:stk:menu"


def pay_received_keyboard(order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Paiement reçu", callback_data=f"{CALLBACK_PAY_PREFIX}{order_id}")
    return builder.as_markup()


def shipped_keyboard(order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Envoyé", callback_data=f"{CALLBACK_SHIP_PREFIX}{order_id}")
    return builder.as_markup()


def orders_inbox_keyboard(
    pending_ids: list[int], ship_ids: list[int]
) -> InlineKeyboardMarkup | None:
    if not pending_ids and not ship_ids:
        return None
    builder = InlineKeyboardBuilder()
    for order_id in pending_ids:
        builder.button(
            text=f"Paiement reçu #{order_id}",
            callback_data=f"{CALLBACK_PAY_PREFIX}{order_id}",
        )
    for order_id in ship_ids:
        builder.button(
            text=f"Envoyé #{order_id}",
            callback_data=f"{CALLBACK_SHIP_PREFIX}{order_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def stock_pools_keyboard(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for pool_name, kind in VALID_REWARD_POOLS.items():
        label = REWARD_POOL_LABELS.get(pool_name, pool_name)
        n = (counts or {}).get(pool_name, 0)
        kind_mark = "vidéo" if kind == "video" else "photo"
        builder.button(
            text=f"{label} · {n} {kind_mark}",
            callback_data=f"{CALLBACK_STOCK_POOL_PREFIX}{pool_name}",
        )
    builder.adjust(1)
    return builder.as_markup()


def stock_preview_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Ajouter au stock", callback_data=CALLBACK_STOCK_OK)
    builder.button(text="Annuler", callback_data=CALLBACK_STOCK_NO)
    builder.adjust(1)
    return builder.as_markup()


def stock_after_add_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Encore une dans ce slot", callback_data=CALLBACK_STOCK_MORE)
    builder.button(text="Autre slot", callback_data=CALLBACK_STOCK_MENU)
    builder.adjust(1)
    return builder.as_markup()
