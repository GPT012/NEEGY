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
ALTER TABLE products DROP CONSTRAINT IF EXISTS products_category_check;
ALTER TABLE products ADD CONSTRAINT products_category_check
    CHECK (category IN ('photo', 'call', 'vip'));

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
    kind             TEXT NOT NULL CHECK (kind IN ('manual', 'discount')),
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
