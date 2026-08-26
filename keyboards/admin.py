"""Boutons sous les notifications de commande (admin)."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db.repository import Product, REWARD_POOL_LABELS, VALID_REWARD_POOLS
from keyboards.main_menu import CALLBACK_BACK

CALLBACK_PAY_PREFIX = "adm:pay:"
CALLBACK_SHIP_PREFIX = "adm:ship:"
CALLBACK_STOCK_POOL_PREFIX = "adm:stk:p:"
CALLBACK_STOCK_OK = "adm:stk:ok"
CALLBACK_STOCK_NO = "adm:stk:no"
CALLBACK_STOCK_MORE = "adm:stk:more"
CALLBACK_STOCK_MENU = "adm:stk:menu"
CALLBACK_STOCK_RAPID = "adm:stk:rapid"
CALLBACK_PREVIEW_PREFIX = "adm:prv:p:"
CALLBACK_PREVIEW_OK = "adm:prv:ok"
CALLBACK_PREVIEW_NO = "adm:prv:no"
CALLBACK_PREVIEW_CLEAR_PREFIX = "adm:prv:c:"


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


def settings_keyboard(
    stock_counts: dict[str, int] | None = None,
    media_products: list[Product] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for pool_name, kind in VALID_REWARD_POOLS.items():
        label = REWARD_POOL_LABELS.get(pool_name, pool_name)
        n = (stock_counts or {}).get(pool_name, 0)
        kind_mark = "vidéo" if kind == "video" else "photo"
        builder.button(
            text=f"Stock · {label} · {n}",
            callback_data=f"{CALLBACK_STOCK_POOL_PREFIX}{pool_name}",
        )
    for product in media_products or []:
        mark = "✓" if product.has_preview else "—"
        builder.button(
            text=f"Preview · {product.name} {mark}",
            callback_data=f"{CALLBACK_PREVIEW_PREFIX}{product.id}",
        )
    builder.button(text="⬅️ Retour au menu", callback_data=CALLBACK_BACK)
    builder.adjust(1)
    return builder.as_markup()


def stock_pools_keyboard(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    return settings_keyboard(stock_counts=counts)


def stock_waiting_keyboard(*, rapid: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=("⚡ Mode rapide ON" if rapid else "⚡ Mode rapide OFF"),
        callback_data=CALLBACK_STOCK_RAPID,
    )
    builder.button(text="⬅️ Paramètres", callback_data=CALLBACK_STOCK_MENU)
    builder.button(text="⬅️ Retour au menu", callback_data=CALLBACK_BACK)
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
    builder.button(text="⬅️ Retour au menu", callback_data=CALLBACK_BACK)
    builder.adjust(1)
    return builder.as_markup()


def product_preview_confirm_keyboard(product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Utiliser comme preview", callback_data=CALLBACK_PREVIEW_OK)
    builder.button(text="Annuler", callback_data=CALLBACK_PREVIEW_NO)
    builder.button(
        text="Effacer la preview",
        callback_data=f"{CALLBACK_PREVIEW_CLEAR_PREFIX}{product_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def product_preview_waiting_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Paramètres", callback_data=CALLBACK_STOCK_MENU)
    builder.button(text="⬅️ Retour au menu", callback_data=CALLBACK_BACK)
    builder.adjust(1)
    return builder.as_markup()
