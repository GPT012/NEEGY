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
