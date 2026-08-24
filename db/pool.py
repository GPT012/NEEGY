"""Gestion du pool de connexions PostgreSQL (asyncpg).

Le schéma est appliqué au démarrage de façon idempotente (CREATE TABLE IF NOT
EXISTS), sans outil de migration dédié : suffisant pour ce stade du projet.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

from utils.logger import get_logger

logger = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_SEED_PRODUCTS = [
    ("Audit rapide", "Analyse de ton besoin en 30 minutes, en visio.", 4900, "EUR"),
    ("Accompagnement mensuel", "Suivi et conseils tout au long du mois.", 19900, "EUR"),
    ("Formation express", "Session de formation individuelle de 2 heures.", 9900, "EUR"),
]


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Crée le pool de connexions et applique le schéma + le seed initial."""
    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
    await _apply_schema(pool)
    await _seed_products(pool)
    logger.info("Pool PostgreSQL initialisé")
    return pool


async def close_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        return
    try:
        await pool.close()
    except Exception:
        logger.exception("Erreur lors de la fermeture du pool PostgreSQL")


async def _apply_schema(pool: asyncpg.Pool) -> None:
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as connection:
        await connection.execute(schema_sql)


async def _seed_products(pool: asyncpg.Pool) -> None:
    """Ajoute quelques services d'exemple si le catalogue est vide."""
    async with pool.acquire() as connection:
        count = await connection.fetchval("SELECT COUNT(*) FROM products")
        if count:
            return
        for name, description, price_cents, currency in _SEED_PRODUCTS:
            await connection.execute(
                """
                INSERT INTO products (name, description, price_cents, currency)
                VALUES ($1, $2, $3, $4)
                """,
                name,
                description,
                price_cents,
                currency,
            )
        logger.info("Catalogue de services initialisé avec %d exemples", len(_SEED_PRODUCTS))
