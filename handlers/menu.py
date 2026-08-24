"""Handlers des callbacks du menu inline (exemple à 3 boutons)."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from config import config
from keyboards.main_menu import (
    CALLBACK_BACK,
    CALLBACK_INFO,
    CALLBACK_SETTINGS,
    get_main_menu_keyboard,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="menu")

GENERIC_ERROR_ALERT = "Une erreur est survenue. Merci de réessayer plus tard."


async def _edit_menu(callback: CallbackQuery, text: str) -> None:
    """Remplace le contenu du message par `text` et réaffiche le menu.

    Un clic sur un bouton menant à l'écran déjà affiché fait échouer editMessageText
    avec "message is not modified" : c'est attendu côté Telegram, on l'absorbe sans
    alerter l'utilisateur ni polluer les logs d'erreurs.
    """
    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_main_menu_keyboard(mini_app_url=config.mini_app_url),
        )
        await callback.answer()
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            await _safe_answer(callback)
            return
        await callback.answer()
    except Exception:
        await _safe_answer(callback)


@router.callback_query(F.data == CALLBACK_INFO)
async def handle_info(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "ℹ️ Ceci est un bot d'exemple construit avec aiogram 3.x.")


@router.callback_query(F.data == CALLBACK_SETTINGS)
async def handle_settings(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "⚙️ Aucun paramètre configurable pour le moment.")


@router.callback_query(F.data == CALLBACK_BACK)
async def handle_back(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "👋 Menu principal :")


async def _safe_answer(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    logger.exception("Erreur lors du traitement d'un callback pour user_id=%s", user_id)
    try:
        await callback.answer(GENERIC_ERROR_ALERT, show_alert=True)
    except Exception:
        logger.exception("Impossible de répondre au callback avec le message d'erreur générique")
