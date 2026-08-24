"""Accès aux données : catalogue, panier, commandes, créneaux d'appel, roue, VIP.

Toutes les requêtes sont paramétrées (asyncpg, placeholders $1, $2, ...),
jamais de f-string / concaténation dans le SQL.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

import asyncpg


class CartError(RuntimeError):
    """Erreur métier liée au panier (ex: panier vide, créneau plus disponible)."""


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    description: str
    price_cents: int
    currency: str
    category: str
    duration_minutes: int | None


@dataclass(frozen=True)
class CartItem:
    product_id: int
    name: str
    price_cents: int
    currency: str
    quantity: int
    category: str
    duration_minutes: int | None
    call_slot_id: int | None
    call_slot_start_at: datetime | None

    @property
    def subtotal_cents(self) -> int:
        return self.price_cents * self.quantity


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    total_cents: int
    original_total_cents: int
    discount_percent: int | None
    currency: str
    items: list[CartItem]


@dataclass(frozen=True)
class CallSlot:
    id: int
    start_at: datetime
    duration_minutes: int
    status: str


@dataclass(frozen=True)
class WheelPrize:
    id: int
    label: str
    description: str
    kind: str
    discount_percent: int | None


@dataclass(frozen=True)
class VipPlan:
    id: int
    name: str
    price_cents: int
    duration_days: int
    description: str


@dataclass(frozen=True)
class VipStatus:
    active: bool
    plan_name: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class OrderRecord:
    id: int
    user_id: int
    total_cents: int
    currency: str
    status: str
    customer_name: str | None = None
    telegram_username: str | None = None


@dataclass(frozen=True)
class ShipTask:
    order_id: int
    user_id: int
    customer_name: str | None
    telegram_username: str | None
    items_label: str


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


async def list_products(pool: asyncpg.Pool, category: str | None = None) -> list[Product]:
    if category is None:
        rows = await pool.fetch(
            """
            SELECT id, name, description, price_cents, currency, category, duration_minutes
            FROM products
            WHERE is_active = TRUE
            ORDER BY category, price_cents
            """
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, name, description, price_cents, currency, category, duration_minutes
            FROM products
            WHERE is_active = TRUE AND category = $1
            ORDER BY price_cents
            """,
            category,
        )
    return [_row_to_product(row) for row in rows]


def _row_to_product(row) -> Product:
    return Product(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        price_cents=row["price_cents"],
        currency=row["currency"],
        category=row["category"],
        duration_minutes=row["duration_minutes"],
    )


# --------------------------------------------------------------------------
# Créneaux d'appel
# --------------------------------------------------------------------------


async def list_available_call_slots(pool: asyncpg.Pool, duration_minutes: int) -> list[CallSlot]:
    rows = await pool.fetch(
        """
        SELECT id, start_at, duration_minutes, status
        FROM call_slots
        WHERE status = 'available' AND duration_minutes = $1 AND start_at > now()
        ORDER BY start_at
        """,
        duration_minutes,
    )
    return [
        CallSlot(
            id=row["id"],
            start_at=row["start_at"],
            duration_minutes=row["duration_minutes"],
            status=row["status"],
        )
        for row in rows
    ]


async def list_upcoming_call_slots(pool: asyncpg.Pool) -> list[CallSlot]:
    """Tous les créneaux à venir (disponibles ou réservés) — usage admin."""
    rows = await pool.fetch(
        """
        SELECT id, start_at, duration_minutes, status
        FROM call_slots
        WHERE start_at > now()
        ORDER BY start_at
        """
    )
    return [
        CallSlot(
            id=row["id"],
            start_at=row["start_at"],
            duration_minutes=row["duration_minutes"],
            status=row["status"],
        )
        for row in rows
    ]


async def create_call_slot(pool: asyncpg.Pool, start_at: datetime, duration_minutes: int) -> int:
    return await pool.fetchval(
        """
        INSERT INTO call_slots (start_at, duration_minutes)
        VALUES ($1, $2)
        RETURNING id
        """,
        start_at,
        duration_minutes,
    )


# --------------------------------------------------------------------------
# Panier
# --------------------------------------------------------------------------


async def get_cart(pool: asyncpg.Pool, user_id: int) -> list[CartItem]:
    rows = await pool.fetch(
        """
        SELECT
            p.id AS product_id, p.name, p.price_cents, p.currency, p.category,
            p.duration_minutes, c.quantity, c.call_slot_id, s.start_at AS call_slot_start_at
        FROM cart_items c
        JOIN products p ON p.id = c.product_id
        LEFT JOIN call_slots s ON s.id = c.call_slot_id
        WHERE c.user_id = $1
        ORDER BY c.id
        """,
        user_id,
    )
    return [_row_to_cart_item(row) for row in rows]


def _row_to_cart_item(row) -> CartItem:
    return CartItem(
        product_id=row["product_id"],
        name=row["name"],
        price_cents=row["price_cents"],
        currency=row["currency"],
        quantity=row["quantity"],
        category=row["category"],
        duration_minutes=row["duration_minutes"],
        call_slot_id=row["call_slot_id"],
        call_slot_start_at=row["call_slot_start_at"],
    )


async def upsert_cart_item(
    pool: asyncpg.Pool,
    user_id: int,
    product_id: int,
    quantity: int,
    call_slot_id: int | None = None,
) -> None:
    """Définit la quantité (et le créneau éventuel) d'un produit dans le panier.

    Supprime l'article si quantity <= 0. Pour un appel, call_slot_id doit être
    fourni : la disponibilité réelle n'est vérifiée qu'au checkout (transaction
    atomique), ceci ne fait que refléter le choix du client dans le panier.
    """
    if quantity <= 0:
        await remove_cart_item(pool, user_id, product_id)
        return

    await pool.execute(
        """
        INSERT INTO cart_items (user_id, product_id, quantity, call_slot_id, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (user_id, product_id)
        DO UPDATE SET quantity = EXCLUDED.quantity, call_slot_id = EXCLUDED.call_slot_id, updated_at = now()
        """,
        user_id,
        product_id,
        quantity,
        call_slot_id,
    )


async def remove_cart_item(pool: asyncpg.Pool, user_id: int, product_id: int) -> None:
    await pool.execute(
        "DELETE FROM cart_items WHERE user_id = $1 AND product_id = $2",
        user_id,
        product_id,
    )


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------


async def create_order_from_cart(
    pool: asyncpg.Pool,
    user_id: int,
    *,
    customer_name: str | None = None,
    telegram_username: str | None = None,
) -> OrderResult:
    """Transforme le panier courant en commande, puis vide le panier.

    Opération atomique : soit tout réussit (commande + articles créés, créneaux
    d'appel réservés, abonnement VIP mis en attente, panier vidé), soit rien
    n'est modifié en cas d'erreur (créneau pris entre-temps, panier vide...).
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch(
                """
                SELECT
                    p.id AS product_id, p.name, p.price_cents, p.currency, p.category,
                    p.duration_minutes, p.vip_plan_id, c.quantity, c.call_slot_id,
                    s.start_at AS call_slot_start_at
                FROM cart_items c
                JOIN products p ON p.id = c.product_id
                LEFT JOIN call_slots s ON s.id = c.call_slot_id
                WHERE c.user_id = $1
                FOR UPDATE OF c
                """,
                user_id,
            )
            if not rows:
                raise CartError("Le panier est vide.")

            items: list[CartItem] = []
            for row in rows:
                category = row["category"]

                if category == "call":
                    slot_id = row["call_slot_id"]
                    if slot_id is None:
                        raise CartError(
                            f"Aucun créneau choisi pour « {row['name']} ». "
                            "Sélectionne un horaire avant de commander."
                        )
                    slot_row = await connection.fetchrow(
                        "SELECT status FROM call_slots WHERE id = $1 FOR UPDATE",
                        slot_id,
                    )
                    if slot_row is None or slot_row["status"] != "available":
                        raise CartError(
                            f"Le créneau choisi pour « {row['name']} » vient d'être pris. "
                            "Merci d'en choisir un autre."
                        )

                items.append(_row_to_cart_item(row))

            original_total_cents = sum(item.subtotal_cents for item in items)
            currency = items[0].currency

            discount_percent = await _consume_pending_discount(connection, user_id)
            total_cents = original_total_cents
            if discount_percent:
                total_cents = original_total_cents - (original_total_cents * discount_percent) // 100

            order_id = await connection.fetchval(
                """
                INSERT INTO orders (
                    user_id, total_cents, currency, status, discount_percent,
                    customer_name, telegram_username
                )
                VALUES ($1, $2, $3, 'pending', $4, $5, $6)
                RETURNING id
                """,
                user_id,
                total_cents,
                currency,
                discount_percent,
                customer_name,
                telegram_username,
            )

            for row, item in zip(rows, items):
                await connection.execute(
                    """
                    INSERT INTO order_items
                        (order_id, product_id, product_name, unit_price_cents, quantity)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    order_id,
                    item.product_id,
                    item.name,
                    item.price_cents,
                    item.quantity,
                )

                if item.category == "call" and item.call_slot_id is not None:
                    await connection.execute(
                        "UPDATE call_slots SET status = 'booked', order_id = $1 WHERE id = $2",
                        order_id,
                        item.call_slot_id,
                    )

                if item.category == "vip" and row["vip_plan_id"] is not None:
                    await connection.execute(
                        """
                        INSERT INTO vip_subscriptions (user_id, plan_id, order_id, status)
                        VALUES ($1, $2, $3, 'pending')
                        """,
                        user_id,
                        row["vip_plan_id"],
                        order_id,
                    )

            await connection.execute("DELETE FROM cart_items WHERE user_id = $1", user_id)

            return OrderResult(
                order_id=order_id,
                total_cents=total_cents,
                original_total_cents=original_total_cents,
                discount_percent=discount_percent,
                currency=currency,
                items=items,
            )


async def _consume_pending_discount(connection: asyncpg.Connection, user_id: int) -> int | None:
    """Applique et marque comme utilisée la réduction de roue la plus récente, si elle existe."""
    row = await connection.fetchrow(
        """
        SELECT ws.id AS spin_id, wp.discount_percent
        FROM wheel_spins ws
        JOIN wheel_prizes wp ON wp.id = ws.prize_id
        WHERE ws.user_id = $1 AND ws.redeemed_at IS NULL AND wp.kind = 'discount'
        ORDER BY ws.spun_at DESC
        LIMIT 1
        FOR UPDATE OF ws
        """,
        user_id,
    )
    if row is None:
        return None

    await connection.execute(
        "UPDATE wheel_spins SET redeemed_at = now() WHERE id = $1",
        row["spin_id"],
    )
    return row["discount_percent"]


# --------------------------------------------------------------------------
# Roue quotidienne
# --------------------------------------------------------------------------


async def get_today_spin(pool: asyncpg.Pool, user_id: int) -> WheelPrize | None:
    """Retourne le lot déjà gagné aujourd'hui par l'utilisateur, ou None s'il peut encore tourner."""
    row = await pool.fetchrow(
        """
        SELECT wp.id, wp.label, wp.description, wp.kind, wp.discount_percent
        FROM wheel_spins ws
        JOIN wheel_prizes wp ON wp.id = ws.prize_id
        WHERE ws.user_id = $1 AND ws.spin_date = (now() AT TIME ZONE 'UTC')::date
        """,
        user_id,
    )
    if row is None:
        return None
    return _row_to_wheel_prize(row)


def _row_to_wheel_prize(row) -> WheelPrize:
    return WheelPrize(
        id=row["id"],
        label=row["label"],
        description=row["description"],
        kind=row["kind"],
        discount_percent=row["discount_percent"],
    )


async def spin_wheel(pool: asyncpg.Pool, user_id: int) -> WheelPrize:
    """Tire un lot pondéré et l'enregistre pour aujourd'hui.

    Lève CartError si l'utilisateur a déjà tourné aujourd'hui (protégé aussi par
    la contrainte UNIQUE (user_id, spin_date), revérifiée explicitement ici pour
    renvoyer un message clair plutôt qu'une erreur d'intégrité brute).
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            already = await connection.fetchrow(
                """
                SELECT 1 FROM wheel_spins
                WHERE user_id = $1 AND spin_date = (now() AT TIME ZONE 'UTC')::date
                """,
                user_id,
            )
            if already is not None:
                raise CartError("Tu as déjà tourné la roue aujourd'hui, reviens demain !")

            prize_rows = await connection.fetch(
                """
                SELECT id, label, description, kind, discount_percent, weight
                FROM wheel_prizes
                WHERE is_active = TRUE
                """
            )
            if not prize_rows:
                raise CartError("Aucun lot disponible pour le moment.")

            weights = [row["weight"] for row in prize_rows]
            chosen = random.choices(prize_rows, weights=weights, k=1)[0]

            await connection.execute(
                """
                INSERT INTO wheel_spins (user_id, spin_date, prize_id)
                VALUES ($1, (now() AT TIME ZONE 'UTC')::date, $2)
                """,
                user_id,
                chosen["id"],
            )

            return _row_to_wheel_prize(chosen)


# --------------------------------------------------------------------------
# VIP
# --------------------------------------------------------------------------


async def list_active_vip_plans(pool: asyncpg.Pool) -> list[VipPlan]:
    rows = await pool.fetch(
        """
        SELECT id, name, price_cents, duration_days, description
        FROM vip_plans
        WHERE is_active = TRUE
        ORDER BY price_cents
        """
    )
    return [
        VipPlan(
            id=row["id"],
            name=row["name"],
            price_cents=row["price_cents"],
            duration_days=row["duration_days"],
            description=row["description"],
        )
        for row in rows
    ]


async def get_vip_status(pool: asyncpg.Pool, user_id: int) -> VipStatus:
    row = await pool.fetchrow(
        """
        SELECT vp.name, vs.expires_at
        FROM vip_subscriptions vs
        JOIN vip_plans vp ON vp.id = vs.plan_id
        WHERE vs.user_id = $1 AND vs.status = 'active' AND vs.expires_at > now()
        ORDER BY vs.expires_at DESC
        LIMIT 1
        """,
        user_id,
    )
    if row is None:
        return VipStatus(active=False, plan_name=None, expires_at=None)
    return VipStatus(active=True, plan_name=row["name"], expires_at=row["expires_at"])


async def activate_vip_for_order(pool: asyncpg.Pool, order_id: int) -> VipStatus | None:
    """Active l'abonnement VIP en attente lié à cette commande (commande admin /confirm).

    Retourne le nouveau statut si un abonnement a été activé, None si la
    commande ne contenait pas d'article VIP.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT vs.id, vs.user_id, vp.name, vp.duration_days
                FROM vip_subscriptions vs
                JOIN vip_plans vp ON vp.id = vs.plan_id
                WHERE vs.order_id = $1 AND vs.status = 'pending'
                FOR UPDATE OF vs
                """,
                order_id,
            )
            if row is None:
                return None

            expires_at = await connection.fetchval(
                """
                UPDATE vip_subscriptions
                SET status = 'active', started_at = now(),
                    expires_at = now() + (make_interval(days => $2))
                WHERE id = $1
                RETURNING expires_at
                """,
                row["id"],
                row["duration_days"],
            )
            return VipStatus(active=True, plan_name=row["name"], expires_at=expires_at)


# --------------------------------------------------------------------------
# Administration (confirmation manuelle des commandes, aucun paiement en ligne)
# --------------------------------------------------------------------------


async def get_order(pool: asyncpg.Pool, order_id: int) -> OrderRecord | None:
    row = await pool.fetchrow(
        """
        SELECT id, user_id, total_cents, currency, status, customer_name, telegram_username
        FROM orders WHERE id = $1
        """,
        order_id,
    )
    if row is None:
        return None
    return OrderRecord(
        id=row["id"],
        user_id=row["user_id"],
        total_cents=row["total_cents"],
        currency=row["currency"],
        status=row["status"],
        customer_name=row["customer_name"],
        telegram_username=row["telegram_username"],
    )


async def mark_order_paid(pool: asyncpg.Pool, order_id: int) -> bool:
    """Marque la commande comme payée. Retourne False si introuvable ou déjà traitée."""
    result = await pool.execute(
        "UPDATE orders SET status = 'paid' WHERE id = $1 AND status = 'pending'",
        order_id,
    )
    return result.endswith(" 1")


def _rows_to_ship_tasks(rows) -> list[ShipTask]:
    return [
        ShipTask(
            order_id=row["id"],
            user_id=row["user_id"],
            customer_name=row["customer_name"],
            telegram_username=row["telegram_username"],
            items_label=row["items_label"],
        )
        for row in rows
    ]


async def list_pending_orders(pool: asyncpg.Pool) -> list[ShipTask]:
    """Commandes en attente de confirmation de paiement."""
    rows = await pool.fetch(
        """
        SELECT
            o.id,
            o.user_id,
            o.customer_name,
            o.telegram_username,
            string_agg(
                oi.product_name || CASE WHEN oi.quantity > 1 THEN ' x' || oi.quantity ELSE '' END,
                ', '
                ORDER BY oi.id
            ) AS items_label
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.status = 'pending'
        GROUP BY o.id, o.user_id, o.customer_name, o.telegram_username
        ORDER BY o.id
        """
    )
    return _rows_to_ship_tasks(rows)


async def list_orders_to_ship(pool: asyncpg.Pool) -> list[ShipTask]:
    """Commandes payées dont les photos n'ont pas encore été envoyées."""
    rows = await pool.fetch(
        """
        SELECT
            o.id,
            o.user_id,
            o.customer_name,
            o.telegram_username,
            string_agg(
                oi.product_name || CASE WHEN oi.quantity > 1 THEN ' x' || oi.quantity ELSE '' END,
                ', '
                ORDER BY oi.id
            ) AS items_label
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.status = 'paid'
          AND o.shipped_at IS NULL
          AND p.category = 'photo'
        GROUP BY o.id, o.user_id, o.customer_name, o.telegram_username
        ORDER BY o.id
        """
    )
    return _rows_to_ship_tasks(rows)


async def get_photo_items_label(pool: asyncpg.Pool, order_id: int) -> str | None:
    label = await pool.fetchval(
        """
        SELECT string_agg(
            oi.product_name || CASE WHEN oi.quantity > 1 THEN ' x' || oi.quantity ELSE '' END,
            ', '
            ORDER BY oi.id
        )
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = $1 AND p.category = 'photo'
        """,
        order_id,
    )
    return label


async def mark_order_shipped(pool: asyncpg.Pool, order_id: int) -> bool:
    """Marque le colis comme envoyé. Uniquement si la commande est payée et pas déjà partie."""
    result = await pool.execute(
        """
        UPDATE orders
        SET shipped_at = now()
        WHERE id = $1 AND status = 'paid' AND shipped_at IS NULL
        """,
        order_id,
    )
    return result.endswith(" 1")


async def get_call_slot_for_order(pool: asyncpg.Pool, order_id: int) -> CallSlot | None:
    row = await pool.fetchrow(
        "SELECT id, start_at, duration_minutes, status FROM call_slots WHERE order_id = $1",
        order_id,
    )
    if row is None:
        return None
    return CallSlot(
        id=row["id"],
        start_at=row["start_at"],
        duration_minutes=row["duration_minutes"],
        status=row["status"],
    )
