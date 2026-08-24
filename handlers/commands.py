"""Handlers des commandes de base : /start et /help."""

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from keyboards.main_menu import get_main_menu_keyboard
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="commands")

GENERIC_ERROR_MESSAGE = "Une erreur est survenue. Merci de réessayer plus tard."

WELCOME_MESSAGE = (
    "👋 Bienvenue !\n\n"
    "Je suis votre bot. Utilisez le menu ci-dessous pour commencer, "
    "ou tapez /help pour la liste des commandes."
)

HELP_MESSAGE = (
    "📖 Commandes disponibles :\n\n"
    "/start — Afficher le message de bienvenue et le menu\n"
    "/help — Afficher ce message d'aide"
)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    try:
        await message.answer(WELCOME_MESSAGE, reply_markup=get_main_menu_keyboard())
    except Exception:
        logger.exception("Erreur dans handle_start pour user_id=%s", message.from_user.id if message.from_user else None)
        await _safe_reply(message)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    try:
        await message.answer(HELP_MESSAGE)
    except Exception:
        logger.exception("Erreur dans handle_help pour user_id=%s", message.from_user.id if message.from_user else None)
        await _safe_reply(message)


async def _safe_reply(message: Message) -> None:
    try:
        await message.answer(GENERIC_ERROR_MESSAGE)
    except Exception:
        logger.exception("Impossible d'envoyer le message d'erreur générique à l'utilisateur")
