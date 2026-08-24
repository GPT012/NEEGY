"""Accès aux données : catalogue, panier, commandes.

Toutes les requêtes sont paramétrées (asyncpg, placeholders $1, $2, ...),
jamais de f-string / concaténation dans le SQL.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


class CartError(RuntimeError):
    """Erreur métier liée au panier (ex: panier vide au checkout)."""


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    description: str
    price_cents: int
    currency: str


@dataclass(frozen=True)
class CartItem:
    product_id: int
    name: str
    price_cents: int
    currency: str
    quantity: int

    @property
    def subtotal_cents(self) -> int:
        return self.price_cents * self.quantity


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    total_cents: int
    currency: str
    items: list[CartItem]


async def list_products(pool: asyncpg.Pool) -> list[Product]:
    rows = await pool.fetch(
        """
        SELECT id, name, description, price_cents, currency
        FROM products
        WHERE is_active = TRUE
        ORDER BY id
        """
    )
    return [
        Product(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            price_cents=row["price_cents"],
            currency=row["currency"],
        )
        for row in rows
    ]


async def get_cart(pool: asyncpg.Pool, user_id: int) -> list[CartItem]:
    rows = await pool.fetch(
        """
        SELECT p.id AS product_id, p.name, p.price_cents, p.currency, c.quantity
        FROM cart_items c
        JOIN products p ON p.id = c.product_id
        WHERE c.user_id = $1
        ORDER BY c.id
        """,
        user_id,
    )
    return [
        CartItem(
            product_id=row["product_id"],
            name=row["name"],
            price_cents=row["price_cents"],
            currency=row["currency"],
            quantity=row["quantity"],
        )
        for row in rows
    ]


async def upsert_cart_item(pool: asyncpg.Pool, user_id: int, product_id: int, quantity: int) -> None:
    """Définit la quantité d'un produit dans le panier (supprime si quantity <= 0)."""
    if quantity <= 0:
        await remove_cart_item(pool, user_id, product_id)
        return

    await pool.execute(
        """
        INSERT INTO cart_items (user_id, product_id, quantity, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (user_id, product_id)
        DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = now()
        """,
        user_id,
        product_id,
        quantity,
    )


async def remove_cart_item(pool: asyncpg.Pool, user_id: int, product_id: int) -> None:
    await pool.execute(
        "DELETE FROM cart_items WHERE user_id = $1 AND product_id = $2",
        user_id,
        product_id,
    )


async def create_order_from_cart(pool: asyncpg.Pool, user_id: int) -> OrderResult:
    """Transforme le panier courant en commande, puis vide le panier.

    Opération atomique : soit tout réussit (commande + articles créés, panier
    vidé), soit rien n'est modifié en cas d'erreur.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch(
                """
                SELECT p.id AS product_id, p.name, p.price_cents, p.currency, c.quantity
                FROM cart_items c
                JOIN products p ON p.id = c.product_id
                WHERE c.user_id = $1
                FOR UPDATE OF c
                """,
                user_id,
            )
            if not rows:
                raise CartError("Le panier est vide.")

            items = [
                CartItem(
                    product_id=row["product_id"],
                    name=row["name"],
                    price_cents=row["price_cents"],
                    currency=row["currency"],
                    quantity=row["quantity"],
                )
                for row in rows
            ]
            total_cents = sum(item.subtotal_cents for item in items)
            currency = items[0].currency

            order_id = await connection.fetchval(
                """
                INSERT INTO orders (user_id, total_cents, currency, status)
                VALUES ($1, $2, $3, 'pending')
                RETURNING id
                """,
                user_id,
                total_cents,
                currency,
            )

            for item in items:
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

            await connection.execute("DELETE FROM cart_items WHERE user_id = $1", user_id)

            return OrderResult(
                order_id=order_id,
                total_cents=total_cents,
                currency=currency,
                items=items,
            )
