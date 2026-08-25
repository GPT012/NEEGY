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


# Une commande pending non réglée expire pour ne pas bloquer le client (et
# libérer un éventuel créneau déjà retenu par l'ancien flux checkout).
PENDING_ORDER_TTL_HOURS = 24


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    description: str
    price_cents: int
    currency: str
    category: str
    duration_minutes: int | None
    reward_count: int | None = None
    wheel_id: int | None = None


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
    points_amount: int | None = None
    content_pool: str | None = None
    call_duration_minutes: int | None = None
    weight: int = 1


@dataclass(frozen=True)
class WheelInfo:
    id: int
    slug: str
    name: str
    price_cents: int
    product_id: int | None
    prizes: tuple[WheelPrize, ...]


@dataclass(frozen=True)
class RewardAsset:
    id: int
    pool: str
    kind: str
    telegram_file_id: str
    caption: str


@dataclass
class FulfillmentResult:
    warnings: list[str]
    prize_label: str | None = None
    prize_kind: str | None = None
    points_amount: int | None = None
    assets: list[RewardAsset] | None = None
    call_slot: CallSlot | None = None
    shipped_complete: bool = False
    needs_manual_ship: bool = False

    def __post_init__(self) -> None:
        if self.assets is None:
            self.assets = []


@dataclass(frozen=True)
class RewardGrantRow:
    created_at: datetime
    user_id: int
    pool: str
    kind: str
    source: str
    order_id: int | None
    caption: str


# Deux files globales : packs ET lots roue piochent dedans.
# (Les anciens noms de slots sont migrés au démarrage, voir schema.sql.)
VALID_REWARD_POOLS = {
    "photos": "photo",
    "videos": "video",
}

REWARD_POOL_LABELS = {
    "photos": "File photos",
    "videos": "File vidéos",
}

POOL_PHOTOS = "photos"
POOL_VIDEOS = "videos"

# Tous les packs photo prennent N fichiers dans la même file.
_BOOSTER_POOL_BY_COUNT = {3: POOL_PHOTOS, 8: POOL_PHOTOS, 12: POOL_PHOTOS}


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
    is_first_order: bool = False
    folders: tuple[str, ...] = ()
    is_vip: bool = False


@dataclass(frozen=True)
class CustomerSnapshot:
    is_first_order: bool
    paid_count: int
    previous_products: tuple[str, ...]
    folders: tuple[str, ...]
    vip_plan: str | None


@dataclass(frozen=True)
class FolderSummary:
    name: str
    member_count: int


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


async def list_products(pool: asyncpg.Pool, category: str | None = None) -> list[Product]:
    if category is None:
        rows = await pool.fetch(
            """
            SELECT id, name, description, price_cents, currency, category, duration_minutes,
                   reward_count, wheel_id
            FROM products
            WHERE is_active = TRUE
            ORDER BY category, price_cents
            """
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, name, description, price_cents, currency, category, duration_minutes,
                   reward_count, wheel_id
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
        reward_count=row["reward_count"] if "reward_count" in row.keys() else None,
        wheel_id=row["wheel_id"] if "wheel_id" in row.keys() else None,
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

    Un seul article à la fois, quantité 1. Un client avec une commande encore
    pending ne peut rien ajouter. Pour un appel, le créneau n'est pas bloqué
    ici : il reste visible tant que le paiement n'est pas confirmé.
    """
    if quantity <= 0:
        await remove_cart_item(pool, user_id, product_id)
        return
    if quantity > 1:
        raise CartError("Un seul exemplaire à la fois.")

    await expire_stale_pending_orders(pool)

    async with pool.acquire() as connection:
        async with connection.transaction():
            pending_id = await connection.fetchval(
                """
                SELECT id FROM orders
                WHERE user_id = $1 AND status = 'pending'
                LIMIT 1
                """,
                user_id,
            )
            if pending_id is not None:
                raise CartError(
                    "Ta commande précédente attend encore le règlement. "
                    "Une fois confirmée, tu pourras en passer une autre."
                )

            other = await connection.fetchval(
                """
                SELECT product_id FROM cart_items
                WHERE user_id = $1 AND product_id <> $2
                LIMIT 1
                """,
                user_id,
                product_id,
            )
            if other is not None:
                raise CartError("Un seul article à la fois. Retire l'autre d'abord.")

            await connection.execute(
                """
                INSERT INTO cart_items (user_id, product_id, quantity, call_slot_id, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (user_id, product_id)
                DO UPDATE SET quantity = EXCLUDED.quantity,
                    call_slot_id = EXCLUDED.call_slot_id, updated_at = now()
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

    Opération atomique : soit tout réussit (commande + articles créés,
    abonnement VIP mis en attente, panier vidé), soit rien n'est modifié.
    Les créneaux d'appel sont seulement mémorisés : ils restent disponibles
    jusqu'au paiement, pour qu'un checkout impayé ne bloque pas l'agenda.
    """
    await expire_stale_pending_orders(pool)

    async with pool.acquire() as connection:
        async with connection.transaction():
            pending_id = await connection.fetchval(
                """
                SELECT id FROM orders
                WHERE user_id = $1 AND status = 'pending'
                LIMIT 1
                """,
                user_id,
            )
            if pending_id is not None:
                raise CartError(
                    "Ta commande précédente attend encore le règlement. "
                    "Une fois confirmée, tu pourras en passer une autre."
                )

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
            if len(rows) > 1:
                raise CartError("Un seul article à la fois.")

            items: list[CartItem] = []
            for row in rows:
                if row["quantity"] > 1:
                    raise CartError("Un seul exemplaire à la fois.")
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
                        (order_id, product_id, product_name, unit_price_cents,
                         quantity, call_slot_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    order_id,
                    item.product_id,
                    item.name,
                    item.price_cents,
                    item.quantity,
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


async def get_pending_order(pool: asyncpg.Pool, user_id: int) -> OrderResult | None:
    """Commande encore à régler, s'il y en a une. Expire d'abord les trop vieilles."""
    await expire_stale_pending_orders(pool)
    row = await pool.fetchrow(
        """
        SELECT id, total_cents, currency, discount_percent
        FROM orders
        WHERE user_id = $1 AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        user_id,
    )
    if row is None:
        return None

    item_rows = await pool.fetch(
        """
        SELECT
            oi.product_id, oi.product_name AS name, oi.unit_price_cents AS price_cents,
            $2::text AS currency, oi.quantity, p.category, p.duration_minutes,
            oi.call_slot_id, s.start_at AS call_slot_start_at
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        LEFT JOIN call_slots s ON s.id = oi.call_slot_id
        WHERE oi.order_id = $1
        ORDER BY oi.id
        """,
        row["id"],
        row["currency"],
    )
    items = [_row_to_cart_item(item) for item in item_rows]
    original_total_cents = sum(item.subtotal_cents for item in items)
    return OrderResult(
        order_id=row["id"],
        total_cents=row["total_cents"],
        original_total_cents=original_total_cents,
        discount_percent=row["discount_percent"],
        currency=row["currency"],
        items=items,
    )


async def expire_stale_pending_orders(pool: asyncpg.Pool) -> None:
    """Annule les commandes pending trop anciennes et libère leurs créneaux."""
    stale_ids = await pool.fetch(
        """
        SELECT id FROM orders
        WHERE status = 'pending'
          AND created_at < now() - make_interval(hours => $1)
        """,
        PENDING_ORDER_TTL_HOURS,
    )
    for stale in stale_ids:
        await cancel_pending_order(pool, stale["id"])


async def cancel_pending_order(pool: asyncpg.Pool, order_id: int) -> bool:
    """Annule une commande pending : libère créneau et VIP en attente."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            result = await connection.execute(
                "UPDATE orders SET status = 'cancelled' WHERE id = $1 AND status = 'pending'",
                order_id,
            )
            if not result.endswith(" 1"):
                return False
            await connection.execute(
                """
                UPDATE call_slots
                SET status = 'available', order_id = NULL
                WHERE order_id = $1 AND status = 'booked'
                """,
                order_id,
            )
            await connection.execute(
                """
                UPDATE vip_subscriptions
                SET status = 'cancelled'
                WHERE order_id = $1 AND status = 'pending'
                """,
                order_id,
            )
            return True


async def _book_call_slots_for_order(connection: asyncpg.Connection, order_id: int) -> str | None:
    """Passe les créneaux mémorisés en booked. None si ok ou pas d'appel.

    Si le créneau a déjà été pris par un autre paiement, le paiement de cette
    commande reste valable : on renvoie un message pour que l'admin replanifie.
    """
    slot_ids = [
        row["call_slot_id"]
        for row in await connection.fetch(
            """
            SELECT call_slot_id FROM order_items
            WHERE order_id = $1 AND call_slot_id IS NOT NULL
            """,
            order_id,
        )
    ]
    if not slot_ids:
        return None

    for slot_id in slot_ids:
        slot = await connection.fetchrow(
            """
            SELECT id, status, order_id, start_at
            FROM call_slots WHERE id = $1 FOR UPDATE
            """,
            slot_id,
        )
        if slot is None:
            return "Le créneau n'existe plus. Paiement OK — replanifie l'appel."
        if slot["status"] == "booked" and slot["order_id"] == order_id:
            continue
        if slot["status"] != "available":
            when = slot["start_at"].strftime("%d/%m/%Y %H:%M")
            return (
                f"Le créneau du {when} UTC a déjà été pris. "
                "Paiement OK — replanifie l'appel."
            )
        await connection.execute(
            "UPDATE call_slots SET status = 'booked', order_id = $1 WHERE id = $2",
            order_id,
            slot_id,
        )
    return None


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
        SELECT wp.id, wp.label, wp.description, wp.kind, wp.discount_percent, wp.points_amount,
               wp.content_pool, wp.call_duration_minutes, wp.weight
        FROM wheel_spins ws
        JOIN wheel_prizes wp ON wp.id = ws.prize_id
        WHERE ws.user_id = $1
          AND ws.spin_date = (now() AT TIME ZONE 'UTC')::date
          AND ws.is_daily = TRUE
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
        points_amount=row["points_amount"] if "points_amount" in row.keys() else None,
        content_pool=row["content_pool"] if "content_pool" in row.keys() else None,
        call_duration_minutes=row["call_duration_minutes"] if "call_duration_minutes" in row.keys() else None,
        weight=int(row["weight"]) if "weight" in row.keys() and row["weight"] is not None else 1,
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
                WHERE user_id = $1
                  AND spin_date = (now() AT TIME ZONE 'UTC')::date
                  AND is_daily = TRUE
                """,
                user_id,
            )
            if already is not None:
                raise CartError("Tu as déjà tourné la roue aujourd'hui, reviens demain !")

            free_id = await connection.fetchval("SELECT id FROM wheels WHERE slug = 'free'")
            prize_rows = await connection.fetch(
                """
                SELECT id, label, description, kind, discount_percent, points_amount, weight,
                       content_pool, call_duration_minutes
                FROM wheel_prizes
                WHERE is_active = TRUE AND (wheel_id = $1 OR ($1 IS NULL AND wheel_id IS NULL))
                """,
                free_id,
            )
            if not prize_rows:
                raise CartError("Aucun lot disponible pour le moment.")

            weights = [row["weight"] for row in prize_rows]
            chosen = random.choices(prize_rows, weights=weights, k=1)[0]

            await connection.execute(
                """
                INSERT INTO wheel_spins (user_id, spin_date, prize_id, wheel_id, is_daily)
                VALUES ($1, (now() AT TIME ZONE 'UTC')::date, $2, $3, TRUE)
                """,
                user_id,
                chosen["id"],
                free_id,
            )

            if chosen["kind"] == "points" and chosen["points_amount"]:
                await _add_points(
                    connection,
                    user_id,
                    int(chosen["points_amount"]),
                    reason="wheel",
                )

            return _row_to_wheel_prize(chosen)


def points_needed_for_cents(total_cents: int) -> int:
    """1 point = 1 €, arrondi au euro supérieur (29,90 € → 30 points)."""
    if total_cents <= 0:
        return 0
    return (total_cents + 99) // 100


async def get_points_balance(pool: asyncpg.Pool, user_id: int) -> int:
    value = await pool.fetchval("SELECT balance FROM user_points WHERE user_id = $1", user_id)
    return int(value or 0)


async def _add_points(
    connection: asyncpg.Connection,
    user_id: int,
    delta: int,
    *,
    reason: str,
    order_id: int | None = None,
) -> int:
    row = await connection.fetchrow(
        "SELECT balance FROM user_points WHERE user_id = $1 FOR UPDATE",
        user_id,
    )
    current = int(row["balance"]) if row is not None else 0
    new_balance = current + delta
    if new_balance < 0:
        raise CartError("Solde de points insuffisant.")

    if row is None:
        await connection.execute(
            "INSERT INTO user_points (user_id, balance, updated_at) VALUES ($1, $2, now())",
            user_id,
            new_balance,
        )
    else:
        await connection.execute(
            "UPDATE user_points SET balance = $2, updated_at = now() WHERE user_id = $1",
            user_id,
            new_balance,
        )
    await connection.execute(
        """
        INSERT INTO point_ledger (user_id, delta, reason, order_id)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        delta,
        reason,
        order_id,
    )
    return new_balance


async def list_wheels(pool: asyncpg.Pool) -> list[WheelInfo]:
    rows = await pool.fetch(
        """
        SELECT w.id, w.slug, w.name, w.price_cents, p.id AS product_id
        FROM wheels w
        LEFT JOIN products p ON p.wheel_id = w.id AND p.category = 'wheel' AND p.is_active = TRUE
        WHERE w.is_active = TRUE
        ORDER BY w.price_cents, w.id
        """
    )
    wheels: list[WheelInfo] = []
    for row in rows:
        prizes = await pool.fetch(
            """
            SELECT id, label, description, kind, discount_percent, points_amount, weight,
                   content_pool, call_duration_minutes
            FROM wheel_prizes
            WHERE is_active = TRUE AND wheel_id = $1
            ORDER BY id
            """,
            row["id"],
        )
        wheels.append(
            WheelInfo(
                id=row["id"],
                slug=row["slug"],
                name=row["name"],
                price_cents=row["price_cents"],
                product_id=row["product_id"],
                prizes=tuple(_row_to_wheel_prize(prize) for prize in prizes),
            )
        )
    return wheels


def _row_to_asset(row) -> RewardAsset:
    return RewardAsset(
        id=row["id"],
        pool=row["pool"],
        kind=row["kind"],
        telegram_file_id=row["telegram_file_id"],
        caption=row["caption"] or "",
    )


async def add_reward_asset(
    pool: asyncpg.Pool,
    *,
    pool_name: str,
    kind: str,
    telegram_file_id: str,
    file_unique_id: str,
    caption: str = "",
) -> int:
    expected = VALID_REWARD_POOLS.get(pool_name)
    if expected is None:
        raise CartError("Pool inconnu.")
    if kind != expected:
        raise CartError(f"Ce pool attend un fichier {expected}.")
    try:
        return await pool.fetchval(
            """
            INSERT INTO reward_assets (pool, kind, telegram_file_id, file_unique_id, caption)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            pool_name,
            kind,
            telegram_file_id,
            file_unique_id,
            caption,
        )
    except asyncpg.UniqueViolationError as exc:
        raise CartError("Ce fichier est déjà dans ce pool.") from exc


async def list_reward_stock(pool: asyncpg.Pool) -> list[tuple[str, int, int]]:
    """(pool, total_actifs, fichiers_jamais_attribués_à_personne) — le 3e sert d'indicateur."""
    rows = await pool.fetch(
        """
        SELECT
            p.pool,
            COUNT(a.id)::int AS total,
            COUNT(a.id) FILTER (
                WHERE NOT EXISTS (SELECT 1 FROM reward_grants g WHERE g.asset_id = a.id)
            )::int AS unused_ever
        FROM unnest($1::text[]) AS p(pool)
        LEFT JOIN reward_assets a ON a.pool = p.pool AND a.is_active = TRUE
        GROUP BY p.pool
        ORDER BY p.pool
        """,
        list(VALID_REWARD_POOLS.keys()),
    )
    return [(row["pool"], row["total"], row["unused_ever"]) for row in rows]


async def list_assets_granted_for_order(pool: asyncpg.Pool, order_id: int) -> list[RewardAsset]:
    rows = await pool.fetch(
        """
        SELECT a.id, a.pool, a.kind, a.telegram_file_id, a.caption
        FROM reward_grants g
        JOIN reward_assets a ON a.id = g.asset_id
        WHERE g.order_id = $1
        ORDER BY g.id
        """,
        order_id,
    )
    return [_row_to_asset(row) for row in rows]


async def list_grants_for_order(pool: asyncpg.Pool, order_id: int) -> list[RewardGrantRow]:
    rows = await pool.fetch(
        """
        SELECT g.created_at, g.user_id, a.pool, a.kind, g.source, g.order_id, a.caption
        FROM reward_grants g
        JOIN reward_assets a ON a.id = g.asset_id
        WHERE g.order_id = $1
        ORDER BY g.id
        """,
        order_id,
    )
    return [_row_to_grant(row) for row in rows]


async def list_grants_for_user(pool: asyncpg.Pool, user_id: int, limit: int = 30) -> list[RewardGrantRow]:
    rows = await pool.fetch(
        """
        SELECT g.created_at, g.user_id, a.pool, a.kind, g.source, g.order_id, a.caption
        FROM reward_grants g
        JOIN reward_assets a ON a.id = g.asset_id
        WHERE g.user_id = $1
        ORDER BY g.id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [_row_to_grant(row) for row in rows]


def _row_to_grant(row) -> RewardGrantRow:
    return RewardGrantRow(
        created_at=row["created_at"],
        user_id=row["user_id"],
        pool=row["pool"],
        kind=row["kind"],
        source=row["source"],
        order_id=row["order_id"],
        caption=row["caption"] or "",
    )


async def _pick_unused_assets(
    connection: asyncpg.Connection,
    user_id: int,
    pool_name: str,
    count: int,
) -> list[RewardAsset]:
    if count <= 0:
        return []
    rows = await connection.fetch(
        """
        SELECT a.id, a.pool, a.kind, a.telegram_file_id, a.caption
        FROM reward_assets a
        WHERE a.pool = $1 AND a.is_active = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM reward_grants g
              WHERE g.asset_id = a.id AND g.user_id = $2
          )
        ORDER BY random()
        LIMIT $3
        """,
        pool_name,
        user_id,
        count,
    )
    return [_row_to_asset(row) for row in rows]


async def _grant_assets(
    connection: asyncpg.Connection,
    user_id: int,
    order_id: int,
    pool_name: str,
    count: int,
    source: str,
) -> tuple[list[RewardAsset], str | None]:
    assets = await _pick_unused_assets(connection, user_id, pool_name, count)
    for asset in assets:
        await connection.execute(
            """
            INSERT INTO reward_grants (user_id, asset_id, order_id, source)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, asset_id) DO NOTHING
            """,
            user_id,
            asset.id,
            order_id,
            source,
        )
    warning = None
    if len(assets) < count:
        warning = (
            f"Stock insuffisant : pool {pool_name}, commande #{order_id} "
            f"({len(assets)}/{count})."
        )
    return assets, warning


async def _book_prize_call_slot(
    connection: asyncpg.Connection,
    order_id: int,
    duration_minutes: int,
) -> tuple[CallSlot | None, str | None]:
    slot = await connection.fetchrow(
        """
        SELECT id, start_at, duration_minutes, status
        FROM call_slots
        WHERE status = 'available' AND duration_minutes = $1 AND start_at > now()
        ORDER BY start_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        duration_minutes,
    )
    if slot is None:
        return None, (
            f"Aucun créneau {duration_minutes} min disponible pour la commande #{order_id}. "
            "Ajoute un /addslot."
        )
    await connection.execute(
        "UPDATE call_slots SET status = 'booked', order_id = $1 WHERE id = $2",
        order_id,
        slot["id"],
    )
    await connection.execute(
        """
        UPDATE order_items SET call_slot_id = $2
        WHERE id = (
            SELECT oi.id FROM order_items oi
            WHERE oi.order_id = $1 AND oi.call_slot_id IS NULL
            ORDER BY oi.id LIMIT 1
        )
        """,
        order_id,
        slot["id"],
    )
    return (
        CallSlot(
            id=slot["id"],
            start_at=slot["start_at"],
            duration_minutes=slot["duration_minutes"],
            status="booked",
        ),
        None,
    )


async def _fulfill_paid_wheel(
    connection: asyncpg.Connection,
    user_id: int,
    order_id: int,
    wheel_id: int,
    result: FulfillmentResult,
) -> None:
    existing = await connection.fetchrow(
        """
        SELECT wp.kind, wp.label, wp.points_amount, wp.content_pool, wp.call_duration_minutes
        FROM wheel_spins ws
        JOIN wheel_prizes wp ON wp.id = ws.prize_id
        WHERE ws.order_id = $1
        """,
        order_id,
    )

    if existing is None:
        prize_rows = await connection.fetch(
            """
            SELECT id, label, description, kind, discount_percent, points_amount, weight,
                   content_pool, call_duration_minutes
            FROM wheel_prizes
            WHERE is_active = TRUE AND wheel_id = $1
            """,
            wheel_id,
        )
        if not prize_rows:
            result.warnings.append(f"Aucun lot configuré pour la roue (commande #{order_id}).")
            return
        chosen = random.choices(prize_rows, weights=[row["weight"] for row in prize_rows], k=1)[0]
        await connection.execute(
            """
            INSERT INTO wheel_spins (user_id, spin_date, prize_id, wheel_id, order_id, is_daily)
            VALUES ($1, (now() AT TIME ZONE 'UTC')::date, $2, $3, $4, FALSE)
            """,
            user_id,
            chosen["id"],
            wheel_id,
            order_id,
        )
        if chosen["kind"] == "points" and chosen["points_amount"]:
            await _add_points(
                connection,
                user_id,
                int(chosen["points_amount"]),
                reason="wheel",
                order_id=order_id,
            )
            result.points_amount = int(chosen["points_amount"])
        result.prize_label = chosen["label"]
        result.prize_kind = chosen["kind"]
        kind = chosen["kind"]
        content_pool = chosen["content_pool"]
        call_duration = chosen["call_duration_minutes"]
    else:
        result.prize_label = existing["label"]
        result.prize_kind = existing["kind"]
        kind = existing["kind"]
        content_pool = existing["content_pool"]
        call_duration = existing["call_duration_minutes"]
        if kind == "points" and existing["points_amount"]:
            result.points_amount = int(existing["points_amount"])

    if kind in ("photo", "video") and content_pool:
        already = await connection.fetchval(
            "SELECT COUNT(*) FROM reward_grants WHERE order_id = $1 AND source = 'wheel'",
            order_id,
        )
        if int(already or 0) > 0:
            granted_rows = await connection.fetch(
                """
                SELECT a.id, a.pool, a.kind, a.telegram_file_id, a.caption
                FROM reward_grants g
                JOIN reward_assets a ON a.id = g.asset_id
                WHERE g.order_id = $1 AND g.source = 'wheel'
                ORDER BY g.id
                """,
                order_id,
            )
            result.assets.extend(_row_to_asset(row) for row in granted_rows)
            return
        assets, warning = await _grant_assets(
            connection, user_id, order_id, content_pool, 1, "wheel"
        )
        result.assets.extend(assets)
        if warning:
            result.warnings.append(warning)
            result.needs_manual_ship = True
        return

    if kind == "call":
        existing_slot = await connection.fetchrow(
            "SELECT id FROM call_slots WHERE order_id = $1",
            order_id,
        )
        if existing_slot is not None:
            return
        duration = int(call_duration or 15)
        slot, warning = await _book_prize_call_slot(connection, order_id, duration)
        result.call_slot = slot
        if warning:
            result.warnings.append(warning)


async def fulfill_paid_order(connection: asyncpg.Connection, order_id: int) -> FulfillmentResult:
    """Tire et attribue les lots d'une commande déjà marquée paid (même transaction)."""
    result = FulfillmentResult(warnings=[])
    order = await connection.fetchrow(
        "SELECT user_id FROM orders WHERE id = $1",
        order_id,
    )
    if order is None:
        result.warnings.append(f"Commande #{order_id} introuvable.")
        return result
    user_id = int(order["user_id"])

    items = await connection.fetch(
        """
        SELECT oi.id, p.category, p.reward_count, p.wheel_id, p.price_cents
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = $1
        """,
        order_id,
    )
    for item in items:
        if item["category"] == "wheel" and item["wheel_id"] is not None:
            await _fulfill_paid_wheel(
                connection, user_id, order_id, int(item["wheel_id"]), result
            )
        elif item["category"] == "photo":
            count = int(item["reward_count"] or 0)
            pool_name = _BOOSTER_POOL_BY_COUNT.get(count)
            if not pool_name or count <= 0:
                result.needs_manual_ship = True
                result.warnings.append(
                    f"Pack photo sans stock automatisé (commande #{order_id})."
                )
                continue
            already = await connection.fetchval(
                "SELECT COUNT(*) FROM reward_grants WHERE order_id = $1 AND source = 'booster'",
                order_id,
            )
            needed = count - int(already or 0)
            if needed <= 0:
                result.shipped_complete = True
                continue
            assets, warning = await _grant_assets(
                connection, user_id, order_id, pool_name, needed, "booster"
            )
            result.assets.extend(assets)
            if warning:
                result.warnings.append(warning)
                result.needs_manual_ship = True
            granted = int(already or 0) + len(assets)
            if granted >= count:
                await connection.execute(
                    """
                    UPDATE orders SET shipped_at = now()
                    WHERE id = $1 AND shipped_at IS NULL
                    """,
                    order_id,
                )
                result.shipped_complete = True
            else:
                result.needs_manual_ship = True
    return result


async def fulfill_remaining_for_order(pool: asyncpg.Pool, order_id: int) -> FulfillmentResult:
    """Relance l'attribution (stock vide au premier essai, ou /fulfill)."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            order = await connection.fetchrow(
                "SELECT status FROM orders WHERE id = $1 FOR UPDATE",
                order_id,
            )
            if order is None:
                result = FulfillmentResult(warnings=[f"Commande #{order_id} introuvable."])
                return result
            if order["status"] != "paid":
                result = FulfillmentResult(
                    warnings=[f"Commande #{order_id} n'est pas payée."]
                )
                return result
            return await fulfill_paid_order(connection, order_id)


async def pay_order_with_points(pool: asyncpg.Pool, user_id: int, order_id: int) -> tuple[int, int, FulfillmentResult]:
    """Paie une commande pending intégralement en points.

    Retourne (points_spent, new_balance, fulfillment).
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            order = await connection.fetchrow(
                """
                SELECT id, user_id, total_cents, status
                FROM orders WHERE id = $1 FOR UPDATE
                """,
                order_id,
            )
            if order is None:
                raise CartError("Commande introuvable.")
            if int(order["user_id"]) != user_id:
                raise CartError("Commande introuvable.")
            if order["status"] != "pending":
                raise CartError("Cette commande n'est plus à payer.")

            needed = points_needed_for_cents(int(order["total_cents"]))
            if needed <= 0:
                raise CartError("Rien à payer en points.")

            new_balance = await _add_points(
                connection,
                user_id,
                -needed,
                reason="order",
                order_id=order_id,
            )
            result = await connection.execute(
                """
                UPDATE orders
                SET status = 'paid', points_spent = $2
                WHERE id = $1 AND status = 'pending'
                """,
                order_id,
                needed,
            )
            if not result.endswith(" 1"):
                raise CartError("Cette commande n'est plus à payer.")
            slot_warning = await _book_call_slots_for_order(connection, order_id)
            fulfillment = await fulfill_paid_order(connection, order_id)
            if slot_warning:
                fulfillment.warnings.append(slot_warning)
            return needed, new_balance, fulfillment


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


async def find_user_id_by_username(pool: asyncpg.Pool, username: str) -> int | None:
    cleaned = username.lstrip("@").strip()
    if not cleaned:
        return None
    return await pool.fetchval(
        """
        SELECT user_id FROM orders
        WHERE lower(telegram_username) = lower($1)
        ORDER BY id DESC
        LIMIT 1
        """,
        cleaned,
    )


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


async def mark_order_paid(pool: asyncpg.Pool, order_id: int) -> tuple[bool, FulfillmentResult | None]:
    """Marque la commande comme payée, réserve le créneau et envoie les lots auto.

    Retourne (ok, fulfillment). False si introuvable ou déjà traitée.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            result = await connection.execute(
                "UPDATE orders SET status = 'paid' WHERE id = $1 AND status = 'pending'",
                order_id,
            )
            if not result.endswith(" 1"):
                return False, None
            warning = await _book_call_slots_for_order(connection, order_id)
            fulfillment = await fulfill_paid_order(connection, order_id)
            if warning:
                fulfillment.warnings.append(warning)
            return True, fulfillment


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
    return await _enrich_ship_tasks(pool, _rows_to_ship_tasks(rows))


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
    return await _enrich_ship_tasks(pool, _rows_to_ship_tasks(rows))


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


async def _enrich_ship_tasks(pool: asyncpg.Pool, tasks: list[ShipTask]) -> list[ShipTask]:
    if not tasks:
        return tasks
    user_ids = list({task.user_id for task in tasks})
    count_rows = await pool.fetch(
        """
        SELECT user_id, COUNT(*)::int AS order_count
        FROM orders
        WHERE user_id = ANY($1::bigint[])
        GROUP BY user_id
        """,
        user_ids,
    )
    counts = {row["user_id"]: row["order_count"] for row in count_rows}

    folder_map: dict[int, list[str]] = {user_id: [] for user_id in user_ids}
    folder_rows = await pool.fetch(
        """
        SELECT m.user_id, f.name
        FROM client_folder_members m
        JOIN client_folders f ON f.id = m.folder_id
        WHERE m.user_id = ANY($1::bigint[])
        ORDER BY f.name
        """,
        user_ids,
    )
    for row in folder_rows:
        folder_map.setdefault(row["user_id"], []).append(row["name"])

    vip_rows = await pool.fetch(
        """
        SELECT DISTINCT vs.user_id
        FROM vip_subscriptions vs
        WHERE vs.user_id = ANY($1::bigint[])
          AND vs.status = 'active'
          AND vs.expires_at > now()
        """,
        user_ids,
    )
    vip_users = {row["user_id"] for row in vip_rows}

    return [
        ShipTask(
            order_id=task.order_id,
            user_id=task.user_id,
            customer_name=task.customer_name,
            telegram_username=task.telegram_username,
            items_label=task.items_label,
            is_first_order=counts.get(task.user_id, 1) <= 1,
            folders=tuple(folder_map.get(task.user_id, [])),
            is_vip=task.user_id in vip_users,
        )
        for task in tasks
    ]


async def get_customer_snapshot(
    pool: asyncpg.Pool, user_id: int, current_order_id: int | None = None
) -> CustomerSnapshot:
    total = await pool.fetchval("SELECT COUNT(*)::int FROM orders WHERE user_id = $1", user_id) or 0
    paid_count = await pool.fetchval(
        """
        SELECT COUNT(*)::int FROM orders
        WHERE user_id = $1 AND status = 'paid' AND id IS DISTINCT FROM $2
        """,
        user_id,
        current_order_id,
    ) or 0
    product_rows = await pool.fetch(
        """
        SELECT DISTINCT oi.product_name
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.user_id = $1 AND o.status = 'paid' AND o.id IS DISTINCT FROM $2
        ORDER BY oi.product_name
        """,
        user_id,
        current_order_id,
    )
    folder_rows = await pool.fetch(
        """
        SELECT f.name
        FROM client_folder_members m
        JOIN client_folders f ON f.id = m.folder_id
        WHERE m.user_id = $1
        ORDER BY f.name
        """,
        user_id,
    )
    vip = await get_vip_status(pool, user_id)
    return CustomerSnapshot(
        is_first_order=total <= 1,
        paid_count=paid_count,
        previous_products=tuple(row["product_name"] for row in product_rows),
        folders=tuple(row["name"] for row in folder_rows),
        vip_plan=vip.plan_name if vip.active else None,
    )


def customer_note_lines(snapshot: CustomerSnapshot) -> list[str]:
    lines: list[str] = []
    if snapshot.is_first_order:
        lines.append("Première commande")
    else:
        label = "commande payée" if snapshot.paid_count == 1 else "commandes payées"
        lines.append(f"Déjà {snapshot.paid_count} {label}")
        if snapshot.previous_products:
            lines.append("Déjà : " + ", ".join(snapshot.previous_products[:6]))
    if snapshot.folders:
        lines.append("Dossiers : " + ", ".join(snapshot.folders))
    if snapshot.vip_plan:
        lines.append(f"VIP : {snapshot.vip_plan}")
    return lines


def customer_hint_suffix(task: ShipTask) -> str:
    bits: list[str] = []
    if task.is_first_order:
        bits.append("1re")
    bits.extend(task.folders)
    if task.is_vip:
        bits.append("VIP")
    if not bits:
        return ""
    return " · " + ", ".join(bits)


async def tag_user(pool: asyncpg.Pool, user_id: int, folder_name: str) -> str:
    """Ajoute la cliente au dossier (créé si besoin). Retourne le nom du dossier."""
    row = await pool.fetchrow(
        "SELECT id, name FROM client_folders WHERE lower(name) = lower($1)",
        folder_name,
    )
    if row is None:
        folder_id = await pool.fetchval(
            "INSERT INTO client_folders (name) VALUES ($1) RETURNING id",
            folder_name,
        )
        name = folder_name
    else:
        folder_id = row["id"]
        name = row["name"]
    await pool.execute(
        """
        INSERT INTO client_folder_members (folder_id, user_id)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        folder_id,
        user_id,
    )
    return name


async def untag_user(pool: asyncpg.Pool, user_id: int, folder_name: str) -> bool:
    result = await pool.execute(
        """
        DELETE FROM client_folder_members m
        USING client_folders f
        WHERE m.folder_id = f.id AND m.user_id = $1 AND lower(f.name) = lower($2)
        """,
        user_id,
        folder_name,
    )
    return result.endswith(" 1")


async def list_client_folders(pool: asyncpg.Pool) -> list[FolderSummary]:
    rows = await pool.fetch(
        """
        SELECT f.name, COUNT(m.user_id)::int AS member_count
        FROM client_folders f
        LEFT JOIN client_folder_members m ON m.folder_id = f.id
        GROUP BY f.id, f.name
        ORDER BY f.name
        """
    )
    return [FolderSummary(name=row["name"], member_count=row["member_count"]) for row in rows]


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
