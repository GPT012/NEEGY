"""Boutons sous les notifications de commande (admin)."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

CALLBACK_PAY_PREFIX = "adm:pay:"
CALLBACK_SHIP_PREFIX = "adm:ship:"


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
