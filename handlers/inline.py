"""Partage de la boutique dans n'importe quelle conversation via @bot."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from config import config
from keyboards.main_menu import get_shop_open_keyboard, mini_app_deep_link
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="inline")


@router.inline_query()
async def handle_inline_shop(query: InlineQuery, bot: Bot) -> None:
    if not config.mini_app_url:
        await query.answer([], cache_time=1)
        return

    try:
        me = await bot.get_me()
        link = mini_app_deep_link(me.username or "", config.mini_app_short_name)
        await query.answer(
            [
                InlineQueryResultArticle(
                    id="shop",
                    title="Boutique NEEGY",
                    description="Envoyer le lien pour ouvrir la Mini App",
                    input_message_content=InputTextMessageContent(
                        message_text=f"Boutique : {link}"
                    ),
                    reply_markup=get_shop_open_keyboard(config.mini_app_url),
                )
            ],
            cache_time=30,
            is_personal=False,
        )
    except Exception:
        logger.exception("Erreur lors de la requête inline boutique")
        try:
            await query.answer([], cache_time=1)
        except Exception:
            logger.exception("Impossible de répondre à la requête inline")
