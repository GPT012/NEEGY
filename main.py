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
import hmac

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


async def _safe_close_session(bot: Bot) -> None:
    """Ferme la session HTTP du bot sans jamais lever d'exception au shutdown.

    Filet de sécurité valable pour le polling comme pour le webhook : évite
    les warnings de session non fermée à chaque redéploiement (Railway envoie
    SIGTERM), même si la fermeture "normale" (gérée par aiogram/aiohttp) a
    déjà eu lieu ou a échoué.
    """
    try:
        await bot.session.close()
    except Exception:
        logger.exception("Erreur lors de la fermeture de la session du bot")


async def run_polling() -> None:
    """Démarre le bot en mode polling (développement local)."""
    bot = create_bot()
    dispatcher = create_dispatcher()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Démarrage en mode polling")
        # handle_signals=True : aiogram intercepte SIGINT/SIGTERM et déclenche
        # un arrêt propre. close_bot_session=True : bot.session.close() est
        # appelé automatiquement à la fin du polling. (valeurs par défaut,
        # explicitées ici pour rendre le comportement d'arrêt visible.)
        await dispatcher.start_polling(bot, handle_signals=True, close_bot_session=True)
    finally:
        # Filet de sécurité : couvre aussi le cas où une exception survient
        # avant même que start_polling ne soit atteint (ex: delete_webhook échoue).
        await _safe_close_session(bot)


async def _on_startup(bot: Bot) -> None:
    await bot.set_webhook(
        url=config.webhook_url,
        secret_token=config.webhook_secret_token,
        drop_pending_updates=True,
    )
    logger.info("Webhook configuré sur %s", config.webhook_url)


async def _on_shutdown(bot: Bot) -> None:
    # Déclenché via app.on_cleanup (setup_application relie dispatcher.shutdown
    # à aiohttp) : web.run_app gère SIGINT/SIGTERM par défaut (handle_signals=
    # True) et appelle runner.cleanup() à l'arrêt, garantissant l'exécution de
    # cette fonction à chaque redéploiement Railway.
    await bot.delete_webhook()
    await _safe_close_session(bot)
    logger.info("Webhook supprimé, bot arrêté proprement")


@web.middleware
async def webhook_secret_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Vérifie explicitement le secret du webhook, en temps constant.

    Défense en profondeur : aiogram valide déjà ce header en interne
    (SimpleRequestHandler + secrets.compare_digest), mais on le revalide ici
    explicitement, avant même d'atteindre le dispatcher. La route /health
    n'est pas concernée (chemin différent de WEBHOOK_PATH).
    """
    if request.path == config.webhook_path:
        expected_token = config.webhook_secret_token or ""
        received_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected_token or not hmac.compare_digest(received_token, expected_token):
            logger.warning("Webhook rejeté : secret token absent ou invalide")
            return web.Response(status=403)
    return await handler(request)


async def handle_health(request: web.Request) -> web.Response:
    """Healthcheck non protégé, utilisé par Railway pour vérifier que le service est vivant."""
    return web.json_response({"status": "ok"})


def run_webhook() -> None:
    """Démarre le bot en mode webhook via un serveur aiohttp.

    Le header X-Telegram-Bot-Api-Secret-Token est vérifié à deux niveaux :
    - explicitement ici via webhook_secret_middleware (hmac.compare_digest) ;
    - nativement par aiogram (SimpleRequestHandler avec secret_token=...),
      qui rejette aussi toute requête invalide (401).
    """
    bot = create_bot()
    dispatcher = create_dispatcher()

    dispatcher.startup.register(_on_startup)
    dispatcher.shutdown.register(_on_shutdown)

    app = web.Application(middlewares=[webhook_secret_middleware])

    app.router.add_get("/health", handle_health)

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
