"""Handlers des callbacks du menu inline (boutique, infos, paramètres)."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from config import config
from keyboards.main_menu import (
    CALLBACK_BACK,
    CALLBACK_INFO,
    CALLBACK_SETTINGS,
    get_main_menu_keyboard,
    user_is_admin,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="menu")

GENERIC_ERROR_ALERT = "Une erreur est survenue. Merci de réessayer plus tard."


def _menu_markup(callback: CallbackQuery):
    return get_main_menu_keyboard(
        mini_app_url=config.mini_app_url,
        is_admin=user_is_admin(callback.from_user),
    )


async def _edit_menu(callback: CallbackQuery, text: str, state: FSMContext | None = None) -> None:
    """Remplace le contenu du message par `text` et réaffiche le menu.

    Un clic sur un bouton menant à l'écran déjà affiché fait échouer editMessageText
    avec "message is not modified" : c'est attendu côté Telegram, on l'absorbe sans
    alerter l'utilisateur ni polluer les logs d'erreurs.
    """
    if state is not None:
        await state.clear()
    try:
        await callback.message.edit_text(text, reply_markup=_menu_markup(callback))
        await callback.answer()
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            await callback.answer()
            return
        if "there is no text" in err or "message to edit" in err:
            try:
                await callback.message.answer(text, reply_markup=_menu_markup(callback))
                await callback.answer()
                return
            except Exception:
                await _safe_answer(callback)
                return
        await _safe_answer(callback)
    except Exception:
        await _safe_answer(callback)


@router.callback_query(F.data == CALLBACK_INFO)
async def handle_info(callback: CallbackQuery, state: FSMContext) -> None:
    await _edit_menu(
        callback,
        "ℹ️ Boutique S94lma : ouvre l'app pour le catalogue, le panier et les roues.",
        state,
    )


@router.callback_query(F.data == CALLBACK_SETTINGS)
async def handle_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await _edit_menu(
        callback,
        "⚙️ Les paramètres (stock photos et vidéos) sont réservés à l'administration.",
        state,
    )


@router.callback_query(F.data == CALLBACK_BACK)
async def handle_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _edit_menu(callback, "👋 Menu principal :", state)


async def _safe_answer(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    logger.exception("Erreur lors du traitement d'un callback pour user_id=%s", user_id)
    try:
        await callback.answer(GENERIC_ERROR_ALERT, show_alert=True)
    except Exception:
        logger.exception("Impossible de répondre au callback avec le message d'erreur générique")
