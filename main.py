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
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from api import webapp_routes
from config import config
from db.pool import close_pool, create_pool, describe_dsn
from handlers import admin, commands, inline, menu
from middlewares.throttling import ThrottlingMiddleware
from utils.logger import get_logger, setup_logging

setup_logging(config.log_level, config.log_file_path)
logger = get_logger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"

BOT_COMMANDS = [
    BotCommand(command="start", description="Démarrer / afficher le menu"),
    BotCommand(command="shop", description="Ouvrir la boutique"),
    BotCommand(command="link", description="Lien boutique à envoyer"),
    BotCommand(command="help", description="Aide"),
]

ADMIN_BOT_COMMANDS = [
    BotCommand(command="orders", description="À envoyer"),
    BotCommand(command="tag", description="Mettre dans un dossier"),
    BotCommand(command="folders", description="Liste des dossiers"),
    BotCommand(command="ship", description="Marquer envoyé"),
    BotCommand(command="confirm", description="Paiement reçu"),
    BotCommand(command="cancel", description="Annuler une commande pending"),
    BotCommand(command="slots", description="Créneaux d'appel"),
    BotCommand(command="addslot", description="Ajouter un créneau"),
    BotCommand(command="stock", description="Remplir photos et vidéos"),
    BotCommand(command="grants", description="Qui a reçu quel lot"),
    BotCommand(command="rewards", description="Alias de /grants"),
    BotCommand(command="fulfill", description="Relancer l'envoi d'un lot"),
    *BOT_COMMANDS,
]


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Toujours présent (même None) : les handlers admin déclarent db_pool en
    # paramètre, l'injection aiogram échouerait si la clé était absente du
    # workflow_data (ex: mode polling local sans base connectée).
    dispatcher["db_pool"] = None

    dispatcher.message.middleware(ThrottlingMiddleware())
    dispatcher.callback_query.middleware(ThrottlingMiddleware())

    dispatcher.include_router(admin.router)
    dispatcher.include_router(commands.router)
    dispatcher.include_router(inline.router)
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

    await bot.set_my_commands(BOT_COMMANDS)
    if config.admin_user_id:
        await bot.set_my_commands(
            ADMIN_BOT_COMMANDS,
            scope=BotCommandScopeChat(chat_id=config.admin_user_id),
        )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Boutique",
            web_app=WebAppInfo(url=config.mini_app_url),
        )
    )
    logger.info("Commandes et bouton menu (Mini App) configurés")


async def _on_app_startup(app: web.Application) -> None:
    """Initialise le pool PostgreSQL sans bloquer le démarrage en cas d'échec.

    Une base injoignable ne doit pas empêcher le bot de répondre : le process
    démarre en mode dégradé (routes Mini App en 503, voir webapp_routes) plutôt
    que de boucler sur des redémarrages, ce qui rend les logs illisibles et
    laisse le webhook Telegram sans destinataire.
    """
    try:
        app["db_pool"] = await create_pool(config.database_url)
    except Exception:
        app["db_pool"] = None
        logger.exception(
            "Base de données injoignable : démarrage en mode dégradé "
            "(la boutique restera indisponible). Paramètres reçus : %s",
            describe_dsn(config.database_url),
        )

    # Propage le pool aux handlers aiogram (commandes admin), qui ne lisent
    # pas l'objet aiohttp `app` mais le workflow_data du dispatcher.
    app["dispatcher"]["db_pool"] = app["db_pool"]


async def _on_app_cleanup(app: web.Application) -> None:
    await close_pool(app.get("db_pool"))


async def _on_shutdown(bot: Bot) -> None:
    # Déclenché via app.on_cleanup (setup_application relie dispatcher.shutdown
    # à aiohttp) : web.run_app gère SIGINT/SIGTERM par défaut (handle_signals=
    # True) et appelle runner.cleanup() à l'arrêt, garantissant l'exécution de
    # cette fonction à chaque redéploiement Railway.
    #
    # Important : on NE supprime PAS le webhook ici volontairement. Lors d'un
    # rolling deploy (Railway démarre le nouveau conteneur avant d'arrêter
    # l'ancien), l'ancien conteneur peut s'arrêter *après* que le nouveau ait
    # déjà réenregistré le webhook avec succès. Un delete_webhook() ici
    # effacerait alors le webhook fraîchement configuré par le nouveau
    # conteneur (race condition). Le prochain démarrage réenregistre de toute
    # façon l'URL via set_webhook, donc rien à nettoyer explicitement ici.
    await _safe_close_session(bot)
    logger.info("Bot arrêté proprement (webhook laissé en place)")


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
    """Healthcheck non protégé, utilisé par Railway pour vérifier que le service est vivant.

    Renvoie toujours 200 tant que le process répond, avec le détail de l'état de
    la base : pratique pour diagnostiquer un démarrage en mode dégradé sans
    fouiller les logs.
    """
    pool = request.app.get("db_pool")
    database_ok = False
    if pool is not None:
        try:
            async with pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
            database_ok = True
        except Exception:
            logger.exception("Healthcheck : la base de données ne répond pas")

    return web.json_response({"status": "ok", "database": "ok" if database_ok else "indisponible"})


async def handle_webapp_index(request: web.Request) -> web.FileResponse:
    """Sert explicitement index.html sur /webapp/.

    aiohttp ne sert pas automatiquement index.html pour un accès de
    dossier via add_static (403 par défaut) : il faut cette route dédiée,
    enregistrée avant la route statique générique.
    """
    return web.FileResponse(WEBAPP_DIR / "index.html")


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

    # Pool PostgreSQL (catalogue, panier, commandes) et accès au bot pour
    # l'envoi du récapitulatif de commande — utilisés par api/webapp_routes.py.
    app["bot"] = bot
    app["bot_token"] = config.bot_token
    app["admin_user_id"] = config.admin_user_id
    app["paypal_url"] = config.paypal_url
    app["bank_iban"] = config.bank_iban
    app["bank_holder"] = config.bank_holder
    app["crypto_solana"] = config.crypto_solana
    app["crypto_ethereum"] = config.crypto_ethereum
    app["crypto_bitcoin"] = config.crypto_bitcoin
    app["dispatcher"] = dispatcher
    app.on_startup.append(_on_app_startup)
    app.on_cleanup.append(_on_app_cleanup)

    app.router.add_get("/health", handle_health)
    app.add_routes(webapp_routes.routes)
    # Route exacte sur "/webapp/" enregistrée avant add_static : voir
    # handle_webapp_index pour l'explication (index.html non servi par défaut).
    app.router.add_get("/webapp/", handle_webapp_index)
    app.router.add_get("/webapp", lambda request: web.HTTPFound("/webapp/"))
    app.router.add_static("/webapp/", path=WEBAPP_DIR, show_index=False)

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
