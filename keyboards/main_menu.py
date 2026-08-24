"""Claviers inline du menu principal."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

CALLBACK_INFO = "menu:info"
CALLBACK_SETTINGS = "menu:settings"
CALLBACK_BACK = "menu:back"


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Construit le clavier inline du menu principal (exemple à 3 boutons)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="ℹ️ Informations", callback_data=CALLBACK_INFO)
    builder.button(text="⚙️ Paramètres", callback_data=CALLBACK_SETTINGS)
    builder.button(text="⬅️ Retour", callback_data=CALLBACK_BACK)
    builder.adjust(2, 1)
    return builder.as_markup()
