"""Point d'entrée du bot Telegram.

Deux modes de fonctionnement, pilotés par la variable d'environnement
USE_WEBHOOK :
- USE_WEBHOOK=false (par défaut) : polling, pratique en développement local.
- USE_WEBHOOK=true : serveur web aiohttp exposant un endpoint webhook,
  protégé par la vérification du header X-Telegram-Bot-Api-Secret-Token.

Ce fichier lance un process persistant (aiohttp `web.run_app` / polling en
boucle), adapté à un hébergeur à process long-lived comme Railway (voir
Procfile à la racine : `web: python main.py`). En production sur Railway,
utiliser USE_WEBHOOK=true : le port d'écoute est piloté par la variable PORT
injectée automatiquement par Railway (voir config.py).
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import config
from handlers import commands, menu
from middlewares.throttling import ThrottlingMiddleware
from utils.logger import get_logger, setup_logging

setup_logging(config.log_level, config.log_file_path)
logger = get_logger(__name__)


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()

    dispatcher.message.middleware(ThrottlingMiddleware())
    dispatcher.callback_query.middleware(ThrottlingMiddleware())

    dispatcher.include_router(commands.router)
    dispatcher.include_router(menu.router)

    return dispatcher


def create_bot() -> Bot:
    return Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def run_polling() -> None:
    """Démarre le bot en mode polling (développement local)."""
    bot = create_bot()
    dispatcher = create_dispatcher()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Démarrage en mode polling")
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


async def _on_startup(bot: Bot) -> None:
    await bot.set_webhook(
        url=config.webhook_url,
        secret_token=config.webhook_secret_token,
        drop_pending_updates=True,
    )
    logger.info("Webhook configuré sur %s", config.webhook_url)


async def _on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()
    await bot.session.close()
    logger.info("Webhook supprimé, bot arrêté proprement")


def run_webhook() -> None:
    """Démarre le bot en mode webhook via un serveur aiohttp.

    La vérification du header X-Telegram-Bot-Api-Secret-Token est assurée
    nativement par aiogram (SimpleRequestHandler avec secret_token=...),
    qui rejette toute requête avec un header absent ou invalide (401)
    avant même d'atteindre le dispatcher.
    """
    bot = create_bot()
    dispatcher = create_dispatcher()

    dispatcher.startup.register(_on_startup)
    dispatcher.shutdown.register(_on_shutdown)

    app = web.Application()

    webhook_handler = SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=config.webhook_secret_token,
    )
    webhook_handler.register(app, path=config.webhook_path)

    setup_application(app, dispatcher, bot=bot)

    logger.info(
        "Démarrage en mode webhook sur %s:%s%s",
        config.webapp_host,
        config.webapp_port,
        config.webhook_path,
    )
    web.run_app(app, host=config.webapp_host, port=config.webapp_port)


def main() -> None:
    if config.use_webhook:
        run_webhook()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
