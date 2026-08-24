"""Gestion du pool de connexions PostgreSQL (asyncpg).

Le schéma est appliqué au démarrage de façon idempotente (CREATE TABLE IF NOT
EXISTS), sans outil de migration dédié : suffisant pour ce stade du projet.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

from utils.logger import get_logger

logger = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_CONNECT_ATTEMPTS = 5
_CONNECT_RETRY_DELAY_SECONDS = 3

_FATAL_CONNECT_ERRORS = (
    asyncpg.InvalidPasswordError,
    asyncpg.InvalidAuthorizationSpecificationError,
    asyncpg.InvalidCatalogNameError,
    asyncpg.exceptions.ClientConfigurationError,
)

_SEED_PRODUCTS = [
    ("Audit rapide", "Analyse de ton besoin en 30 minutes, en visio.", 4900, "EUR"),
    ("Accompagnement mensuel", "Suivi et conseils tout au long du mois.", 19900, "EUR"),
    ("Formation express", "Session de formation individuelle de 2 heures.", 9900, "EUR"),
]


def describe_dsn(database_url: str) -> str:
    """Décrit une chaîne de connexion sans jamais exposer le mot de passe.

    Sert au diagnostic des erreurs de connexion : permet de vérifier dans les
    logs quels paramètres l'application reçoit réellement (utile quand la
    variable d'environnement est mal renseignée par l'hébergeur).
    """
    try:
        parsed = urlparse(database_url)
    except ValueError:
        return "chaîne de connexion illisible"

    password_info = (
        f"{len(parsed.password)} caractères" if parsed.password else "ABSENT"
    )
    return (
        f"scheme={parsed.scheme or 'ABSENT'} "
        f"user={parsed.username or 'ABSENT'} "
        f"host={parsed.hostname or 'ABSENT'} "
        f"port={parsed.port or 'défaut'} "
        f"base={parsed.path.lstrip('/') or 'ABSENT'} "
        f"mot_de_passe={password_info}"
    )


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Crée le pool de connexions et applique le schéma + le seed initial.

    Réessaie quelques fois : au démarrage d'un déploiement, la base peut ne pas
    encore accepter les connexions.
    """
    logger.info("Connexion PostgreSQL avec %s", describe_dsn(database_url))

    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
            break
        except _FATAL_CONNECT_ERRORS:
            # Identifiants ou nom de base erronés : réessayer n'y changera rien.
            raise
        except (OSError, asyncpg.PostgresError) as exc:
            if attempt == _CONNECT_ATTEMPTS:
                raise
            logger.warning(
                "Connexion PostgreSQL échouée (tentative %d/%d) : %s — nouvel essai dans %ds",
                attempt,
                _CONNECT_ATTEMPTS,
                exc,
                _CONNECT_RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)

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
