"""Routes HTTP de la Mini App : catalogue, panier, checkout, roue, VIP.

Toutes les routes qui touchent au panier/à la roue/au VIP exigent un
`initData` Telegram valide (header `X-Telegram-Init-Data`), revalidé à chaque
requête - voir api/telegram_auth.py. Aucune confiance n'est faite à un
user_id envoyé tel quel par le client.
"""

from __future__ import annotations

import json
from html import escape

from aiogram import Bot
from aiohttp import web
import asyncpg

from api.telegram_auth import InvalidInitData, validate_init_data
from db.repository import (
    activate_vip_for_order,
    CartError,
    CartItem,
    Product,
    VipStatus,
    WheelPrize,
    create_order_from_cart,
    customer_note_lines,
    get_cart,
    get_customer_snapshot,
    get_order,
    get_photo_items_label,
    get_today_spin,
    get_vip_status,
    get_call_slot_for_order,
    get_points_balance,
    list_active_vip_plans,
    list_available_call_slots,
    list_products,
    remove_cart_item,
    pay_order_with_points,
    points_needed_for_cents,
    spin_wheel,
    upsert_cart_item,
)
from keyboards.admin import pay_received_keyboard
from utils.logger import get_logger

logger = get_logger(__name__)

routes = web.RouteTableDef()

INIT_DATA_HEADER = "X-Telegram-Init-Data"
GENERIC_ERROR_BODY = {"error": "Une erreur est survenue. Merci de réessayer plus tard."}
UNAVAILABLE_BODY = {"error": "La boutique est momentanément indisponible. Réessaie dans quelques minutes."}
VALID_CATEGORIES = {"photo", "call", "vip"}


def _get_pool(request: web.Request) -> asyncpg.Pool:
    """Retourne le pool, ou lève 503 si l'app tourne en mode dégradé (base injoignable)."""
    pool = request.app.get("db_pool")
    if pool is None:
        raise web.HTTPServiceUnavailable(
            text=json.dumps(UNAVAILABLE_BODY),
            content_type="application/json",
        )
    return pool


def _authenticated_user(request: web.Request) -> dict:
    """Valide le header X-Telegram-Init-Data et retourne le dict user Telegram.

    Lève web.HTTPUnauthorized si absent ou invalide.
    """
    bot_token: str = request.app["bot_token"]
    init_data = request.headers.get(INIT_DATA_HEADER, "")
    try:
        user = validate_init_data(init_data, bot_token)
    except InvalidInitData:
        logger.warning("Requête Mini App rejetée : initData invalide")
        raise web.HTTPUnauthorized(text="Unauthorized")
    return user


def _authenticate(request: web.Request) -> int:
    """Valide le header X-Telegram-Init-Data et retourne le user_id Telegram."""
    return int(_authenticated_user(request)["id"])


def _serialize_product(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "price_cents": product.price_cents,
        "currency": product.currency,
        "category": product.category,
        "duration_minutes": product.duration_minutes,
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
                "category": item.category,
                "duration_minutes": item.duration_minutes,
                "call_slot_id": item.call_slot_id,
                "call_slot_start_at": item.call_slot_start_at.isoformat() if item.call_slot_start_at else None,
                "subtotal_cents": item.subtotal_cents,
            }
            for item in items
        ],
        "total_cents": sum(item.subtotal_cents for item in items),
    }


def _serialize_wheel_prize(prize: WheelPrize) -> dict:
    return {
        "label": prize.label,
        "description": prize.description,
        "kind": prize.kind,
        "discount_percent": prize.discount_percent,
        "points_amount": prize.points_amount,
    }


def _serialize_vip_status(status: VipStatus) -> dict:
    return {
        "active": status.active,
        "plan_name": status.plan_name,
        "expires_at": status.expires_at.isoformat() if status.expires_at else None,
    }


@routes.get("/api/products")
async def get_products(request: web.Request) -> web.Response:
    pool = _get_pool(request)
    category = request.query.get("category")
    if category is not None and category not in VALID_CATEGORIES:
        return web.json_response({"error": "Catégorie invalide"}, status=400)

    try:
        products = await list_products(pool, category=category)
        return web.json_response([_serialize_product(p) for p in products])
    except Exception:
        logger.exception("Erreur lors de la récupération du catalogue")
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.get("/api/call-slots")
async def get_call_slots(request: web.Request) -> web.Response:
    pool = _get_pool(request)
    duration_raw = request.query.get("duration")
    try:
        duration_minutes = int(duration_raw)
        if duration_minutes <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return web.json_response({"error": "Paramètre duration (entier positif) requis"}, status=400)

    try:
        slots = await list_available_call_slots(pool, duration_minutes)
        return web.json_response(
            [
                {"id": s.id, "start_at": s.start_at.isoformat(), "duration_minutes": s.duration_minutes}
                for s in slots
            ]
        )
    except Exception:
        logger.exception("Erreur lors de la récupération des créneaux d'appel")
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.get("/api/cart")
async def get_user_cart(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool = _get_pool(request)
    try:
        items = await get_cart(pool, user_id)
        return web.json_response(_serialize_cart(items))
    except Exception:
        logger.exception("Erreur lors de la récupération du panier pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.post("/api/cart")
async def update_cart_item(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool = _get_pool(request)

    try:
        body = await request.json()
        product_id = int(body["product_id"])
        quantity = int(body["quantity"])
        call_slot_id_raw = body.get("call_slot_id")
        call_slot_id = int(call_slot_id_raw) if call_slot_id_raw is not None else None
    except (KeyError, TypeError, ValueError):
        return web.json_response(
            {"error": "product_id et quantity (entiers) requis, call_slot_id optionnel"}, status=400
        )

    try:
        await upsert_cart_item(pool, user_id, product_id, quantity, call_slot_id=call_slot_id)
        items = await get_cart(pool, user_id)
        return web.json_response(_serialize_cart(items))
    except asyncpg.ForeignKeyViolationError:
        return web.json_response({"error": "Produit ou créneau introuvable"}, status=404)
    except Exception:
        logger.exception("Erreur lors de la mise à jour du panier pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.delete("/api/cart/{product_id}")
async def delete_cart_item(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool = _get_pool(request)

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


@routes.get("/api/wheel/status")
async def get_wheel_status(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool = _get_pool(request)
    try:
        prize = await get_today_spin(pool, user_id)
        balance = await get_points_balance(pool, user_id)
        return web.json_response(
            {
                "can_spin": prize is None,
                "prize": _serialize_wheel_prize(prize) if prize else None,
                "points_balance": balance,
                "points_rate": "1 point = 1 €",
            }
        )
    except Exception:
        logger.exception("Erreur lors de la lecture du statut de la roue pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


@routes.post("/api/wheel/spin")
async def post_wheel_spin(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool = _get_pool(request)
    bot: Bot = request.app["bot"]
    admin_user_id = request.app.get("admin_user_id")

    try:
        prize = await spin_wheel(pool, user_id)
        balance = await get_points_balance(pool, user_id)
    except CartError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Erreur lors du tirage de la roue pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)

    if prize.kind == "manual" and admin_user_id:
        try:
            await bot.send_message(
                admin_user_id,
                f"🎡 User {user_id} a gagné à la roue : {prize.label}\n"
                f"{prize.description}\nMerci de lui envoyer le contenu manuellement.",
            )
        except Exception:
            logger.exception("Impossible de notifier l'admin du gain à la roue (user_id=%s)", user_id)

    return web.json_response({**_serialize_wheel_prize(prize), "points_balance": balance})


@routes.get("/api/vip/status")
async def get_vip_status_route(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    pool = _get_pool(request)
    try:
        status = await get_vip_status(pool, user_id)
        plans = await list_active_vip_plans(pool)
        return web.json_response(
            {
                **_serialize_vip_status(status),
                "plans": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "price_cents": p.price_cents,
                        "duration_days": p.duration_days,
                        "description": p.description,
                    }
                    for p in plans
                ],
            }
        )
    except Exception:
        logger.exception("Erreur lors de la récupération du statut VIP pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)


def _clip(value: object, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_len]


def _who_label(name: str | None, username: str | None, user_id: int) -> str:
    if username:
        return f"@{escape(username)}"
    if name:
        return escape(name)
    return f"user {user_id}"


def _format_item_line(item: CartItem) -> str:
    label = f"• {item.name} x{item.quantity} — {item.subtotal_cents / 100:.2f} {item.currency}"
    if item.category == "call" and item.call_slot_start_at:
        label += f" (créneau : {item.call_slot_start_at:%d/%m/%Y %H:%M} UTC)"
    return label


def _payment_payload(request: web.Request) -> dict:
    """Coordonnées affichées après commande. Aucune API de paiement."""
    return {
        "paypal_url": request.app.get("paypal_url"),
        "bank_iban": request.app.get("bank_iban"),
        "bank_holder": request.app.get("bank_holder"),
        "reference": None,
    }


def _payment_message_lines(payment: dict) -> list[str]:
    lines = ["\nPaiement — PayPal ou virement :"]
    if payment.get("paypal_url"):
        lines.append(f"\nPayPal :\n{payment['paypal_url']}")
    holder = payment.get("bank_holder")
    iban = payment.get("bank_iban")
    if holder or iban:
        lines.append("\nVirement :")
        if holder:
            lines.append(f"Nom : {holder}")
        if iban:
            lines.append(f"IBAN : {iban}")
    if payment.get("reference"):
        lines.append(f"Libellé : {payment['reference']}")
    if not payment.get("paypal_url") and not iban:
        lines.append("\nRéponds à ce message pour convenir du règlement.")
    return lines


@routes.post("/api/checkout")
async def checkout(request: web.Request) -> web.Response:
    user = _authenticated_user(request)
    user_id = int(user["id"])
    pool = _get_pool(request)
    bot: Bot = request.app["bot"]
    admin_user_id = request.app.get("admin_user_id")
    customer_name = _clip(user.get("first_name"), 64)
    telegram_username = _clip(user.get("username"), 32)

    try:
        result = await create_order_from_cart(
            pool,
            user_id,
            customer_name=customer_name,
            telegram_username=telegram_username,
        )
    except CartError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Erreur lors du checkout pour user_id=%s", user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)

    summary_lines = [f"✅ Commande #{result.order_id} enregistrée !\n"]
    for item in result.items:
        summary_lines.append(_format_item_line(item))
    if result.discount_percent:
        summary_lines.append(f"\nRéduction roue appliquée : -{result.discount_percent}%")
    summary_lines.append(f"\nTotal : {result.total_cents / 100:.2f} {result.currency}")
    payment = _payment_payload(request)
    payment["reference"] = f"NEEGY-{result.order_id}"
    summary_lines.extend(_payment_message_lines(payment))

    try:
        await bot.send_message(user_id, "\n".join(summary_lines))
    except Exception:
        # La commande est déjà enregistrée en base : une erreur d'envoi du
        # message ne doit pas faire échouer le checkout côté client.
        logger.exception("Impossible d'envoyer le récapitulatif de commande pour user_id=%s", user_id)

    if admin_user_id:
        who = _who_label(customer_name, telegram_username, user_id)
        admin_lines = [f"🛒 #{result.order_id} — {who}"]
        try:
            snapshot = await get_customer_snapshot(pool, user_id, current_order_id=result.order_id)
            notes = customer_note_lines(snapshot)
            if notes:
                admin_lines.append("")
                admin_lines.extend(escape(note) for note in notes)
        except Exception:
            logger.exception("Impossible de charger le profil cliente pour user_id=%s", user_id)
        admin_lines.append("")
        for item in result.items:
            admin_lines.append(_format_item_line(item))
        if result.discount_percent:
            admin_lines.append(f"\nRéduction roue : -{result.discount_percent}%")
        admin_lines.append(f"\nTotal : {result.total_cents / 100:.2f} {result.currency}")
        try:
            await bot.send_message(
                admin_user_id,
                "\n".join(admin_lines),
                reply_markup=pay_received_keyboard(result.order_id),
            )
        except Exception:
            logger.exception("Impossible de notifier l'admin de la commande #%s", result.order_id)

    return web.json_response(
        {
            "order_id": result.order_id,
            "total_cents": result.total_cents,
            "original_total_cents": result.original_total_cents,
            "discount_percent": result.discount_percent,
            "currency": result.currency,
            "payment": payment,
            "points_balance": await get_points_balance(pool, user_id),
            "points_needed": points_needed_for_cents(result.total_cents),
        }
    )


@routes.post("/api/orders/{order_id}/pay-points")
async def pay_with_points(request: web.Request) -> web.Response:
    user = _authenticated_user(request)
    user_id = int(user["id"])
    pool = _get_pool(request)
    bot: Bot = request.app["bot"]
    admin_user_id = request.app.get("admin_user_id")

    try:
        order_id = int(request.match_info["order_id"])
    except ValueError:
        return web.json_response({"error": "Commande invalide"}, status=400)

    try:
        points_spent, balance = await pay_order_with_points(pool, user_id, order_id)
    except CartError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Erreur paiement points commande #%s user_id=%s", request.match_info.get("order_id"), user_id)
        return web.json_response(GENERIC_ERROR_BODY, status=500)

    order = await get_order(pool, order_id)
    try:
        vip_status = await activate_vip_for_order(pool, order_id)
        if vip_status is not None:
            await bot.send_message(
                user_id,
                f"🎉 Ton abonnement VIP « {vip_status.plan_name} » est activé jusqu'au "
                f"{vip_status.expires_at:%d/%m/%Y} !",
            )
        call_slot = await get_call_slot_for_order(pool, order_id)
        if call_slot is not None:
            await bot.send_message(
                user_id,
                f"📞 Ton appel du {call_slot.start_at:%d/%m/%Y à %H:%M} UTC est confirmé !",
            )
        if vip_status is None and call_slot is None:
            await bot.send_message(
                user_id,
                f"✅ Commande #{order_id} payée avec {points_spent} points. Merci !",
            )
    except Exception:
        logger.exception("Impossible de notifier le client user_id=%s après paiement points", user_id)

    if admin_user_id and order is not None:
        who = _who_label(order.customer_name, order.telegram_username, user_id)
        extra = ""
        try:
            photo_label = await get_photo_items_label(pool, order_id)
            if photo_label:
                extra = f"\nÀ envoyer : {photo_label} — {who}"
        except Exception:
            logger.exception("Impossible de lire les photos de la commande #%s", order_id)
        try:
            await bot.send_message(
                admin_user_id,
                f"✅ #{order_id} payée en points ({points_spent} pts) — {who}{extra}",
            )
        except Exception:
            logger.exception("Impossible de notifier l'admin du paiement points #%s", order_id)

    return web.json_response(
        {
            "order_id": order_id,
            "paid_with_points": True,
            "points_spent": points_spent,
            "points_balance": balance,
        }
    )
