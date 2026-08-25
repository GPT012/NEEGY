-- Schéma de base pour le catalogue de services, le panier et les commandes.
-- Exécuté au démarrage de l'application (idempotent : CREATE ... IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS products (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    currency    TEXT NOT NULL DEFAULT 'EUR',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cart_items (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_cart_items_user_id ON cart_items (user_id);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    currency    TEXT NOT NULL DEFAULT 'EUR',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders (user_id);

CREATE TABLE IF NOT EXISTS order_items (
    id             SERIAL PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id     INTEGER NOT NULL REFERENCES products(id),
    product_name   TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    quantity       INTEGER NOT NULL CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id);

-- Migrations idempotentes (catalogue à paliers, appels à distance, roue, VIP).
-- Ajoutées après le schéma initial : appliquées à chaque démarrage, sans effet
-- si déjà en place (ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS).

ALTER TABLE products ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'photo';
ALTER TABLE products ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NULL;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount_percent INTEGER NULL
    CHECK (discount_percent IS NULL OR (discount_percent > 0 AND discount_percent <= 100));

CREATE TABLE IF NOT EXISTS call_slots (
    id               SERIAL PRIMARY KEY,
    start_at         TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    status           TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'booked')),
    order_id         INTEGER NULL REFERENCES orders(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_call_slots_available
    ON call_slots (duration_minutes, start_at) WHERE status = 'available';

ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS call_slot_id INTEGER NULL
    REFERENCES call_slots(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS vip_plans (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    price_cents   INTEGER NOT NULL CHECK (price_cents >= 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    description   TEXT NOT NULL DEFAULT '',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

-- Relie un produit de catégorie 'vip' au plan d'abonnement qu'il représente
-- (durée, prix "officiel" pour l'activation) : le produit reste l'article
-- affiché/acheté via le panier existant, le plan porte les règles métier.
ALTER TABLE products ADD COLUMN IF NOT EXISTS vip_plan_id INTEGER NULL
    REFERENCES vip_plans(id);

CREATE TABLE IF NOT EXISTS vip_subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    plan_id     INTEGER NOT NULL REFERENCES vip_plans(id),
    order_id    INTEGER NULL REFERENCES orders(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'expired', 'cancelled')),
    started_at  TIMESTAMPTZ NULL,
    expires_at  TIMESTAMPTZ NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vip_subscriptions_user_id ON vip_subscriptions (user_id);

CREATE TABLE IF NOT EXISTS wheel_prizes (
    id               SERIAL PRIMARY KEY,
    label            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL CHECK (kind IN ('manual', 'discount', 'points', 'photo', 'video', 'call')),
    discount_percent INTEGER NULL CHECK (discount_percent IS NULL OR (discount_percent > 0 AND discount_percent <= 100)),
    weight           INTEGER NOT NULL DEFAULT 1 CHECK (weight > 0),
    is_active        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS wheel_spins (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    spin_date    DATE NOT NULL,
    prize_id     INTEGER NOT NULL REFERENCES wheel_prizes(id),
    spun_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    redeemed_at  TIMESTAMPTZ NULL,
    UNIQUE (user_id, spin_date)
);

CREATE INDEX IF NOT EXISTS idx_wheel_spins_user_id ON wheel_spins (user_id);

-- Qui a commandé, et si le colis photo est parti.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name TEXT NULL;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS telegram_username TEXT NULL;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ NULL;

-- Dossiers clients (étiquettes créées par l'admin, ex: proches, VIP photos).
CREATE TABLE IF NOT EXISTS client_folders (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_client_folders_name_lower
    ON client_folders (lower(name));

CREATE TABLE IF NOT EXISTS client_folder_members (
    folder_id INTEGER NOT NULL REFERENCES client_folders(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL,
    PRIMARY KEY (folder_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_client_folder_members_user_id
    ON client_folder_members (user_id);

-- Points de la roue : 1 point = 1 €, utilisables en boutique.
ALTER TABLE wheel_prizes ADD COLUMN IF NOT EXISTS points_amount INTEGER NULL
    CHECK (points_amount IS NULL OR points_amount > 0);

CREATE TABLE IF NOT EXISTS user_points (
    user_id    BIGINT PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS point_ledger (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    order_id   INTEGER NULL REFERENCES orders(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_point_ledger_user_id ON point_ledger (user_id);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS points_spent INTEGER NULL
    CHECK (points_spent IS NULL OR points_spent > 0);

-- Le créneau choisi est mémorisé sur la commande ; il n'est marqué booked
-- qu'au paiement effectif (évite qu'un checkout impayé bloque l'agenda).
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS call_slot_id INTEGER NULL
    REFERENCES call_slots(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_orders_user_pending
    ON orders (user_id) WHERE status = 'pending';

-- Roues : quotidienne gratuite + deux payantes. Les lots média sont des
-- assets Telegram (file_id) attribués une seule fois par cliente.
CREATE TABLE IF NOT EXISTS wheels (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0 CHECK (price_cents >= 0),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

ALTER TABLE products DROP CONSTRAINT IF EXISTS products_category_check;
ALTER TABLE products ADD CONSTRAINT products_category_check
    CHECK (category IN ('photo', 'call', 'vip', 'wheel'));
ALTER TABLE products ADD COLUMN IF NOT EXISTS reward_count INTEGER NULL
    CHECK (reward_count IS NULL OR reward_count > 0);
ALTER TABLE products ADD COLUMN IF NOT EXISTS wheel_id INTEGER NULL
    REFERENCES wheels(id);

ALTER TABLE wheel_prizes ADD COLUMN IF NOT EXISTS wheel_id INTEGER NULL
    REFERENCES wheels(id);
ALTER TABLE wheel_prizes ADD COLUMN IF NOT EXISTS content_pool TEXT NULL;
ALTER TABLE wheel_prizes ADD COLUMN IF NOT EXISTS call_duration_minutes INTEGER NULL;
ALTER TABLE wheel_prizes DROP CONSTRAINT IF EXISTS wheel_prizes_kind_check;
ALTER TABLE wheel_prizes ADD CONSTRAINT wheel_prizes_kind_check
    CHECK (kind IN ('manual', 'discount', 'points', 'photo', 'video', 'call'));

ALTER TABLE wheel_spins ADD COLUMN IF NOT EXISTS wheel_id INTEGER NULL
    REFERENCES wheels(id);
ALTER TABLE wheel_spins ADD COLUMN IF NOT EXISTS order_id INTEGER NULL
    REFERENCES orders(id) ON DELETE SET NULL;
ALTER TABLE wheel_spins ADD COLUMN IF NOT EXISTS is_daily BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE wheel_spins DROP CONSTRAINT IF EXISTS wheel_spins_user_id_spin_date_key;
DROP INDEX IF EXISTS wheel_spins_user_id_spin_date_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_wheel_spins_daily
    ON wheel_spins (user_id, spin_date) WHERE is_daily;

CREATE TABLE IF NOT EXISTS reward_assets (
    id                SERIAL PRIMARY KEY,
    pool              TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('photo', 'video')),
    telegram_file_id  TEXT NOT NULL,
    file_unique_id    TEXT NOT NULL,
    caption           TEXT NOT NULL DEFAULT '',
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pool, file_unique_id)
);

CREATE INDEX IF NOT EXISTS idx_reward_assets_pool
    ON reward_assets (pool) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS reward_grants (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    asset_id   INTEGER NOT NULL REFERENCES reward_assets(id),
    order_id   INTEGER NULL REFERENCES orders(id) ON DELETE SET NULL,
    source     TEXT NOT NULL CHECK (source IN ('wheel', 'booster')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_reward_grants_order_id ON reward_grants (order_id);
CREATE INDEX IF NOT EXISTS idx_reward_grants_user_id ON reward_grants (user_id);
