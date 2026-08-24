"""Handlers des callbacks du menu inline (exemple à 3 boutons)."""

from aiogram import F, Router
from aiogram.types import CallbackQuery

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


@router.callback_query(F.data == CALLBACK_INFO)
async def handle_info(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_text(
            "ℹ️ Ceci est un bot d'exemple construit avec aiogram 3.x.",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
    except Exception:
        await _safe_answer(callback)


@router.callback_query(F.data == CALLBACK_SETTINGS)
async def handle_settings(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_text(
            "⚙️ Aucun paramètre configurable pour le moment.",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
    except Exception:
        await _safe_answer(callback)


@router.callback_query(F.data == CALLBACK_BACK)
async def handle_back(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_text(
            "👋 Menu principal :",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
    except Exception:
        await _safe_answer(callback)


async def _safe_answer(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    logger.exception("Erreur lors du traitement d'un callback pour user_id=%s", user_id)
    try:
        await callback.answer(GENERIC_ERROR_ALERT, show_alert=True)
    except Exception:
        logger.exception("Impossible de répondre au callback avec le message d'erreur générique")
