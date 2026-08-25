"""Chargement et validation de la configuration depuis les variables d'environnement.

Aucune valeur sensible ne doit jamais être codée en dur ici : tout provient du
fichier .env (voir .env.example pour la liste des variables attendues).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Levée quand une variable d'environnement requise est manquante ou invalide."""


def _get_str(name: str, default: str | None = None) -> str | None:
    """Retourne la valeur nettoyée des espaces/retours à la ligne parasites."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_required(name: str) -> str:
    value = _get_str(name)
    if not value:
        raise ConfigError(f"Variable d'environnement requise manquante : {name}")
    return value


def _check_https_url(name: str, value: str, example: str) -> None:
    """Vérifie que la valeur est bien une URL HTTPS avec un domaine renseigné."""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(
            f"{name} doit être une URL HTTPS complète (ex: {example}), "
            f"reçu : {value!r}"
        )


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Variable d'environnement invalide (entier attendu) : {name}") from exc


def _get_required_int(name: str) -> int:
    value = _get_str(name)
    if not value:
        raise ConfigError(f"Variable d'environnement requise manquante : {name}")
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(
            f"{name} doit être un entier (ID Telegram numérique), reçu : {value!r}"
        ) from exc


@dataclass(frozen=True)
class Config:
    bot_token: str
    use_webhook: bool

    webhook_secret_token: str | None
    webhook_url: str | None
    webhook_path: str
    webapp_host: str
    webapp_port: int

    database_url: str | None
    mini_app_url: str | None
    mini_app_short_name: str | None

    admin_user_id: int | None

    paypal_url: str | None
    bank_iban: str | None
    bank_holder: str | None

    log_level: str
    log_file_path: str


def load_config() -> Config:
    """Charge et valide la configuration. Lève ConfigError si incohérente."""
    use_webhook = _get_bool("USE_WEBHOOK", default=False)

    webhook_secret_token = _get_str("WEBHOOK_SECRET_TOKEN")
    webhook_url = _get_str("WEBHOOK_URL")
    database_url = _get_str("DATABASE_URL")
    mini_app_url = _get_str("MINI_APP_URL")

    if use_webhook:
        if not webhook_secret_token:
            raise ConfigError(
                "WEBHOOK_SECRET_TOKEN est requis quand USE_WEBHOOK=true "
                "(nécessaire pour valider le header X-Telegram-Bot-Api-Secret-Token)."
            )
        if not webhook_url:
            raise ConfigError("WEBHOOK_URL est requis quand USE_WEBHOOK=true.")
        _check_https_url("WEBHOOK_URL", webhook_url, "https://mon-domaine/webhook")
        if not database_url:
            raise ConfigError(
                "DATABASE_URL est requis quand USE_WEBHOOK=true "
                "(nécessaire pour le catalogue et le panier de la Mini App)."
            )
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ConfigError(
                "DATABASE_URL doit être une chaîne de connexion PostgreSQL commençant "
                "par postgresql:// (copie la valeur du service Postgres). "
                f"Reçu (tronqué) : {database_url[:40]!r}"
            )
        if not mini_app_url:
            raise ConfigError(
                "MINI_APP_URL est requis quand USE_WEBHOOK=true "
                "(URL publique HTTPS servant la Mini App, ex: https://<domaine>/webapp/)."
            )
        _check_https_url("MINI_APP_URL", mini_app_url, "https://mon-domaine/webapp/")
        if not _get_str("ADMIN_USER_ID"):
            raise ConfigError(
                "ADMIN_USER_ID est requis quand USE_WEBHOOK=true "
                "(ID Telegram numérique recevant les notifications de commande "
                "et autorisé à utiliser les commandes admin ; envoie /start à "
                "@userinfobot pour connaître le tien)."
            )

    admin_user_id_raw = _get_str("ADMIN_USER_ID")
    admin_user_id = _get_required_int("ADMIN_USER_ID") if admin_user_id_raw else None

    paypal_url = _get_str("PAYPAL_URL") or "https://www.paypal.me/Carlabdrrr"
    _check_https_url("PAYPAL_URL", paypal_url, "https://paypal.me/toncompte")

    return Config(
        bot_token=_get_required("BOT_TOKEN"),
        use_webhook=use_webhook,
        webhook_secret_token=webhook_secret_token,
        webhook_url=webhook_url,
        webhook_path=_get_str("WEBHOOK_PATH", "/webhook"),
        webapp_host=_get_str("WEBAPP_HOST", "0.0.0.0"),
        # Railway (et d'autres PaaS à process persistant) injectent PORT
        # automatiquement ; WEBAPP_PORT reste utilisable en local/autres hébergeurs.
        webapp_port=_get_int("PORT", _get_int("WEBAPP_PORT", 8080)),
        database_url=database_url,
        mini_app_url=mini_app_url,
        mini_app_short_name=_get_str("MINI_APP_SHORT_NAME"),
        admin_user_id=admin_user_id,
        paypal_url=paypal_url,
        bank_iban=_get_str("BANK_IBAN") or "FR76 2823 3000 0106 8425 4424 364",
        bank_holder=_get_str("BANK_HOLDER") or "Selma Kouassi",
        log_level=_get_str("LOG_LEVEL", "INFO"),
        log_file_path=_get_str("LOG_FILE_PATH", "logs/errors.log"),
    )


config = load_config()
