"""Routes HTTP de la Mini App : catalogue, panier, checkout.

Toutes les routes qui touchent au panier exigent un `initData` Telegram
valide (header `X-Telegram-Init-Data`), revalidé à chaque requête - voir
api/telegram_auth.py. Aucune confiance n'est faite à un user_id envoyé tel
quel par le client.
"""

from __future__ import annotations

from aiogram import Bot
from aiohttp import web
import asyncpg

from api.telegram_auth import InvalidInitData, validate_init_data
from db.repository import CartError, CartItem, Product, create_order_from_cart, get_cart, list_products, remove_cart_item, upsert_cart_item
from utils.logger import get_logger

logger = get_logger(__name__)

routes = web.RouteTableDef()

INIT_DATA_HEADER = "X-Telegram-Init-Data"
GENERIC_ERROR_BODY = {"error": "Une erreur est survenue. Merci de réessayer plus tard."}


def _authenticate(request: web.Request) -> int:
    """Valide le header X-Telegram-Init-Data et retourne le user_id Telegram.

    Lève web.HTTPUnauthorized si absent ou invalide.
    """
    bot_token: str = request.app["bot_token"]
    init_data = request.headers.get(INIT_DATA_HEADER, "")
    try:
        user = validate_init_data(init_data, bot_token)
    except InvalidInitData:
        logger.warning("Requête Mini App rejetée : initData invalide")
        raise web.HTTPUnauthorized(text="Unauthorized")
    return int(user["id"])


def _serialize_product(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "price_cents": product.price_cents,
        "currency": product.currency,
    }


def _serialize_cart(items: list[CartItem]) -> dict:
    return {
        "items": [
            {
                "product_id": item.product_id,
                "name": item.name,
                "price_cents": item.price_cents,
                "currency": item.currency,
                "quantity": item.quantity,
                "subtotal_cents": item.subtotal_cents,
            }
            for item in items
        ],
        "total_cents": sum(item.subtotal_cents for item in items),
    }


@routes.get("/api/products")
async def get_products(request: web.Request) -> web.Response:
    pool: asyncpg.Pool = request.app["db_pool"]
    try:
        products = await list_products(pool)
        return web.json_response([_serialize_product(p) for p in products])
    except Exception:
        logger.exception("Erreur lors de la récupération du catalogue")
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.get("/api/cart")
async def get_user_cart(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool: asyncpg.Pool = request.app["db_pool"]
    try:
        items = await get_cart(pool, user_id)
        return web.json_response(_serialize_cart(items))
    except Exception:
        logger.exception("Erreur lors de la récupération du panier pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.post("/api/cart")
async def update_cart_item(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool: asyncpg.Pool = request.app["db_pool"]

    try:
        body = await request.json()
        product_id = int(body["product_id"])
        quantity = int(body["quantity"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"error": "product_id et quantity (entiers) requis"}, status=400)

    try:
        await upsert_cart_item(pool, user_id, product_id, quantity)
        items = await get_cart(pool, user_id)
        return web.json_response(_serialize_cart(items))
    except asyncpg.ForeignKeyViolationError:
        return web.json_response({"error": "Produit introuvable"}, status=404)
    except Exception:
        logger.exception("Erreur lors de la mise à jour du panier pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.delete("/api/cart/{product_id}")
async def delete_cart_item(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool: asyncpg.Pool = request.app["db_pool"]

    try:
        product_id = int(request.match_info["product_id"])
    except ValueError:
        return web.json_response({"error": "product_id invalide"}, status=400)

    try:
        await remove_cart_item(pool, user_id, product_id)
        items = await get_cart(pool, user_id)
        return web.json_response(_serialize_cart(items))
    except Exception:
        logger.exception("Erreur lors de la suppression d'un article pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.post("/api/checkout")
async def checkout(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool: asyncpg.Pool = request.app["db_pool"]
    bot: Bot = request.app["bot"]

    try:
        result = await create_order_from_cart(pool, user_id)
    except CartError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Erreur lors du checkout pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)

    summary_lines = [f"✅ Commande #{result.order_id} confirmée !\n"]
    for item in result.items:
        summary_lines.append(
            f"• {item.name} x{item.quantity} — {item.subtotal_cents / 100:.2f} {item.currency}"
        )
    summary_lines.append(f"\nTotal : {result.total_cents / 100:.2f} {result.currency}")
    summary_lines.append("\nNous revenons vers toi rapidement pour la suite.")

    try:
        await bot.send_message(user_id, "\n".join(summary_lines))
    except Exception:
        # La commande est déjà enregistrée en base : une erreur d'envoi du
        # message ne doit pas faire échouer le checkout côté client.
        logger.exception("Impossible d'envoyer le récapitulatif de commande pour user_id=%s", user_id)

    return web.json_response(
        {
            "order_id": result.order_id,
            "total_cents": result.total_cents,
            "currency": result.currency,
        }
    )
