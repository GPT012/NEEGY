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
    ("Pack Découverte", "Une sélection de 3 photos.", 1000, "EUR", 3),
    ("Pack Complet", "Une sélection de 8 photos, plusieurs styles.", 2000, "EUR", 8),
    ("Pack Exclusif", "La sélection la plus complète, contenu inédit.", 3000, "EUR", 12),
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

# slug, name, price_cents
_SEED_WHEELS = [
    ("free", "Quotidienne", 0),
    ("rose", "Rose", 500),
    ("nuit", "Nuit", 2000),
]

# wheel_slug, label, description, kind, weight, points_amount, content_pool, call_duration
_SEED_WHEEL_PRIZES = [
    ("free", "2", "C'est tombé.", "points", 5, 2, None, None),
    ("free", "4", "Un peu plus loin.", "points", 4, 4, None, None),
    ("free", "9", "Le rare.", "points", 1, 9, None, None),
    ("rose", "3", "C'est tombé.", "points", 3, 3, None, None),
    ("rose", "5", "Un peu plus loin.", "points", 1, 5, None, None),
    ("rose", "9", "Le rare.", "points", 1, 9, None, None),
    ("rose", "photo", "Une image pour toi.", "photo", 4, None, "wheel5_photo", None),
    ("rose", "vidéo", "Rien qu'à tes yeux.", "video", 1, None, "wheel5_video", None),
    ("nuit", "15", "C'est tombé.", "points", 3, 15, None, None),
    ("nuit", "20", "Un peu plus loin.", "points", 1, 20, None, None),
    ("nuit", "36", "Le rare.", "points", 1, 36, None, None),
    ("nuit", "vidéo", "Rien qu'à tes yeux.", "video", 3, None, "wheel20_video", None),
    ("nuit", "cam", "Elle te réserve un appel.", "call", 2, None, None, 15),
]

_SEED_WHEEL_PRODUCTS = [
    ("rose", "Tour Rose", "Un tour de la roue à 5 €. Le lot part dans Telegram dès le paiement."),
    ("nuit", "Tour Nuit", "Un tour de la roue à 20 €. Le lot part dans Telegram dès le paiement."),
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
    await _seed_wheels(pool)
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
        await _sync_photo_reward_counts(connection)
        return
    for name, description, price_cents, currency, reward_count in _SEED_PHOTOS:
        await connection.execute(
            """
            INSERT INTO products (name, description, price_cents, currency, category, reward_count)
            VALUES ($1, $2, $3, $4, 'photo', $5)
            """,
            name,
            description,
            price_cents,
            currency,
            reward_count,
        )
    logger.info("Catalogue 'photo' initialisé avec %d article(s)", len(_SEED_PHOTOS))


async def _sync_photo_reward_counts(connection: asyncpg.Connection) -> None:
    for name, _description, _price, _currency, reward_count in _SEED_PHOTOS:
        await connection.execute(
            """
            UPDATE products
            SET reward_count = $2
            WHERE category = 'photo' AND name = $1 AND is_active = TRUE
              AND (reward_count IS NULL OR reward_count <> $2)
            """,
            name,
            reward_count,
        )


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


async def _seed_wheels(pool: asyncpg.Pool) -> None:
    """Crée les 3 roues, leurs lots, et les produits « tour payant »."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            slug_to_id: dict[str, int] = {}
            for slug, name, price_cents in _SEED_WHEELS:
                wheel_id = await connection.fetchval(
                    "SELECT id FROM wheels WHERE slug = $1", slug
                )
                if wheel_id is None:
                    wheel_id = await connection.fetchval(
                        """
                        INSERT INTO wheels (slug, name, price_cents)
                        VALUES ($1, $2, $3)
                        RETURNING id
                        """,
                        slug,
                        name,
                        price_cents,
                    )
                else:
                    await connection.execute(
                        "UPDATE wheels SET name = $2, price_cents = $3, is_active = TRUE WHERE id = $1",
                        wheel_id,
                        name,
                        price_cents,
                    )
                slug_to_id[slug] = int(wheel_id)

            free_id = slug_to_id["free"]
            await connection.execute(
                "UPDATE wheel_prizes SET wheel_id = $1 WHERE wheel_id IS NULL",
                free_id,
            )
            await connection.execute(
                """
                UPDATE wheel_spins SET wheel_id = $1, is_daily = TRUE
                WHERE wheel_id IS NULL AND order_id IS NULL
                """,
                free_id,
            )
            await connection.execute(
                "UPDATE wheel_prizes SET is_active = FALSE WHERE kind IN ('manual', 'discount')"
            )

            for slug, label, description, kind, weight, points_amount, content_pool, call_duration in _SEED_WHEEL_PRIZES:
                wheel_id = slug_to_id[slug]
                existing = await connection.fetchval(
                    """
                    SELECT id FROM wheel_prizes
                    WHERE wheel_id = $1 AND kind = $2
                      AND COALESCE(points_amount, 0) = COALESCE($3, 0)
                      AND COALESCE(content_pool, '') = COALESCE($4, '')
                    """,
                    wheel_id,
                    kind,
                    points_amount,
                    content_pool,
                )
                if existing:
                    await connection.execute(
                        """
                        UPDATE wheel_prizes
                        SET label = $1, description = $2, weight = $3, is_active = TRUE,
                            discount_percent = NULL, content_pool = $4,
                            call_duration_minutes = $5
                        WHERE id = $6
                        """,
                        label,
                        description,
                        weight,
                        content_pool,
                        call_duration,
                        existing,
                    )
                else:
                    await connection.execute(
                        """
                        INSERT INTO wheel_prizes
                            (label, description, kind, discount_percent, weight, points_amount,
                             wheel_id, content_pool, call_duration_minutes)
                        VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8)
                        """,
                        label,
                        description,
                        kind,
                        weight,
                        points_amount,
                        wheel_id,
                        content_pool,
                        call_duration,
                    )

            for slug, name, description in _SEED_WHEEL_PRODUCTS:
                wheel_id = slug_to_id[slug]
                price_cents = next(p for s, _n, p in _SEED_WHEELS if s == slug)
                existing = await connection.fetchval(
                    "SELECT id FROM products WHERE category = 'wheel' AND wheel_id = $1",
                    wheel_id,
                )
                if existing:
                    await connection.execute(
                        """
                        UPDATE products
                        SET name = $1, description = $2, price_cents = $3, is_active = TRUE
                        WHERE id = $4
                        """,
                        name,
                        description,
                        price_cents,
                        existing,
                    )
                else:
                    await connection.execute(
                        """
                        INSERT INTO products
                            (name, description, price_cents, currency, category, wheel_id)
                        VALUES ($1, $2, $3, 'EUR', 'wheel', $4)
                        """,
                        name,
                        description,
                        price_cents,
                        wheel_id,
                    )
        logger.info("Roues et lots synchronisés")
