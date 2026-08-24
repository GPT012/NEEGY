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

_LEGACY_PRODUCT_NAMES = ("Audit rapide", "Accompagnement mensuel", "Formation express")

_SEED_PHOTOS = [
    ("Pack Découverte", "Une sélection de 3 photos.", 1000, "EUR"),
    ("Pack Complet", "Une sélection de 8 photos, plusieurs styles.", 2000, "EUR"),
    ("Pack Exclusif", "La sélection la plus complète, contenu inédit.", 3000, "EUR"),
]

_SEED_CALLS = [
    ("Appel 15 minutes", "Appel vidéo/audio en tête-à-tête, 15 minutes.", 7000, "EUR", 15),
    ("Appel 30 minutes", "Appel vidéo/audio en tête-à-tête, 30 minutes.", 12000, "EUR", 30),
]

# name, description, price_cents, duration_days
_SEED_VIP_PLAN = (
    "VIP Mensuel",
    "Accès à une catégorie de contenu exclusif et un tour de roue bonus chaque jour.",
    2990,
    30,
)

# label, description, kind ('manual' ou 'discount'), discount_percent, weight
_SEED_WHEEL_PRIZES = [
    ("Photo surprise", "Une photo gratuite choisie par mes soins, envoyée dans la journée.", "manual", None, 3),
    ("Message vocal", "Un petit message vocal personnalisé, juste pour toi.", "manual", None, 2),
    ("-10% sur ta prochaine commande", "Réduction appliquée automatiquement au prochain checkout.", "discount", 10, 3),
    ("-15% sur ta prochaine commande", "Réduction appliquée automatiquement au prochain checkout.", "discount", 15, 1),
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
    await _seed_catalog(pool)
    await _seed_wheel_prizes(pool)
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


async def _seed_catalog(pool: asyncpg.Pool) -> None:
    """Désactive les anciens produits de démonstration et amorce le vrai catalogue.

    Idempotent : chaque catégorie n'est (ré)amorcée que si elle ne contient
    encore aucun produit actif, de sorte que les commandes/paniers existants
    (référençant les anciens produits par ID) restent valides — on désactive
    au lieu de supprimer.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            await _retire_legacy_products(connection)
            await _seed_photo_category(connection)
            await _seed_call_category(connection)
            await _seed_vip_category(connection)


async def _retire_legacy_products(connection: asyncpg.Connection) -> None:
    result = await connection.execute(
        "UPDATE products SET is_active = FALSE WHERE name = ANY($1::text[]) AND is_active = TRUE",
        list(_LEGACY_PRODUCT_NAMES),
    )
    if result != "UPDATE 0":
        logger.info("Anciens produits de démonstration désactivés (%s)", result)


async def _seed_photo_category(connection: asyncpg.Connection) -> None:
    count = await connection.fetchval(
        "SELECT COUNT(*) FROM products WHERE category = 'photo' AND is_active = TRUE"
    )
    if count:
        return
    for name, description, price_cents, currency in _SEED_PHOTOS:
        await connection.execute(
            """
            INSERT INTO products (name, description, price_cents, currency, category)
            VALUES ($1, $2, $3, $4, 'photo')
            """,
            name,
            description,
            price_cents,
            currency,
        )
    logger.info("Catalogue 'photo' initialisé avec %d article(s)", len(_SEED_PHOTOS))


async def _seed_call_category(connection: asyncpg.Connection) -> None:
    count = await connection.fetchval(
        "SELECT COUNT(*) FROM products WHERE category = 'call' AND is_active = TRUE"
    )
    if count:
        return
    for name, description, price_cents, currency, duration_minutes in _SEED_CALLS:
        await connection.execute(
            """
            INSERT INTO products (name, description, price_cents, currency, category, duration_minutes)
            VALUES ($1, $2, $3, $4, 'call', $5)
            """,
            name,
            description,
            price_cents,
            currency,
            duration_minutes,
        )
    logger.info("Catalogue 'call' initialisé avec %d article(s)", len(_SEED_CALLS))


async def _seed_vip_category(connection: asyncpg.Connection) -> None:
    count = await connection.fetchval(
        "SELECT COUNT(*) FROM products WHERE category = 'vip' AND is_active = TRUE"
    )
    if count:
        return
    name, description, price_cents, duration_days = _SEED_VIP_PLAN
    plan_id = await connection.fetchval(
        """
        INSERT INTO vip_plans (name, price_cents, duration_days, description)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        name,
        price_cents,
        duration_days,
        description,
    )
    await connection.execute(
        """
        INSERT INTO products (name, description, price_cents, currency, category, vip_plan_id)
        VALUES ($1, $2, $3, 'EUR', 'vip', $4)
        """,
        name,
        description,
        price_cents,
        plan_id,
    )
    logger.info("Formule VIP initialisée : %s (%s cents / %s jours)", name, price_cents, duration_days)


async def _seed_wheel_prizes(pool: asyncpg.Pool) -> None:
    """Ajoute les lots de démonstration de la roue si la table est vide."""
    async with pool.acquire() as connection:
        count = await connection.fetchval("SELECT COUNT(*) FROM wheel_prizes")
        if count:
            return
        for label, description, kind, discount_percent, weight in _SEED_WHEEL_PRIZES:
            await connection.execute(
                """
                INSERT INTO wheel_prizes (label, description, kind, discount_percent, weight)
                VALUES ($1, $2, $3, $4, $5)
                """,
                label,
                description,
                kind,
                discount_percent,
                weight,
            )
        logger.info("Lots de la roue initialisés (%d)", len(_SEED_WHEEL_PRIZES))
