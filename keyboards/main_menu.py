"""Claviers inline du menu principal et de partage de la boutique."""

from aiogram.types import InlineKeyboardMarkup, WebAppInfo, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config

CALLBACK_INFO = "menu:info"
CALLBACK_SETTINGS = "menu:settings"
CALLBACK_BACK = "menu:back"


def user_is_admin(user: User | None) -> bool:
    return bool(config.admin_user_id and user and user.id == config.admin_user_id)


def mini_app_deep_link(bot_username: str, short_name: str | None = None) -> str:
    """Lien t.me qui ouvre la Mini App depuis n'importe quelle conversation."""
    username = bot_username.lstrip("@")
    if short_name:
        return f"https://t.me/{username}/{short_name}"
    return f"https://t.me/{username}?startapp"


def get_shop_open_keyboard(mini_app_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Ouvrir la boutique", web_app=WebAppInfo(url=mini_app_url))
    return builder.as_markup()


def get_link_keyboard(mini_app_url: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if mini_app_url:
        builder.button(text="Ouvrir la boutique", web_app=WebAppInfo(url=mini_app_url))
    builder.button(text="Envoyer dans une conversation", switch_inline_query="")
    builder.adjust(1)
    return builder.as_markup()


def get_main_menu_keyboard(
    mini_app_url: str | None = None, *, is_admin: bool = False
) -> InlineKeyboardMarkup:
    """Construit le clavier inline du menu principal.

    Si `mini_app_url` est fourni, un bouton ouvrant la Mini App (boutique)
    est ajouté en première ligne. Les paramètres (stock photos / vidéos)
    n'apparaissent que pour l'administrateur.
    """
    builder = InlineKeyboardBuilder()

    if mini_app_url:
        builder.button(text="🛍️ Ouvrir la boutique", web_app=WebAppInfo(url=mini_app_url))

    builder.button(text="ℹ️ Informations", callback_data=CALLBACK_INFO)
    if is_admin:
        builder.button(text="⚙️ Paramètres", callback_data=CALLBACK_SETTINGS)
    builder.button(text="⬅️ Retour", callback_data=CALLBACK_BACK)

    if mini_app_url and is_admin:
        builder.adjust(1, 2, 1)
    elif mini_app_url:
        builder.adjust(1, 1, 1)
    elif is_admin:
        builder.adjust(2, 1)
    else:
        builder.adjust(1, 1)
    return builder.as_markup()
