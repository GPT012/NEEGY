"""Chargement et validation de la configuration depuis les variables d'environnement.

Aucune valeur sensible ne doit jamais être codée en dur ici : tout provient du
fichier .env (voir .env.example pour la liste des variables attendues).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Levée quand une variable d'environnement requise est manquante ou invalide."""


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Variable d'environnement requise manquante : {name}")
    return value


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

    log_level: str
    log_file_path: str


def load_config() -> Config:
    """Charge et valide la configuration. Lève ConfigError si incohérente."""
    use_webhook = _get_bool("USE_WEBHOOK", default=False)

    webhook_secret_token = os.getenv("WEBHOOK_SECRET_TOKEN") or None
    webhook_url = os.getenv("WEBHOOK_URL") or None
    database_url = os.getenv("DATABASE_URL") or None
    mini_app_url = os.getenv("MINI_APP_URL") or None

    if use_webhook:
        if not webhook_secret_token:
            raise ConfigError(
                "WEBHOOK_SECRET_TOKEN est requis quand USE_WEBHOOK=true "
                "(nécessaire pour valider le header X-Telegram-Bot-Api-Secret-Token)."
            )
        if not webhook_url:
            raise ConfigError("WEBHOOK_URL est requis quand USE_WEBHOOK=true.")
        if not database_url:
            raise ConfigError(
                "DATABASE_URL est requis quand USE_WEBHOOK=true "
                "(nécessaire pour le catalogue et le panier de la Mini App)."
            )
        if not mini_app_url:
            raise ConfigError(
                "MINI_APP_URL est requis quand USE_WEBHOOK=true "
                "(URL publique HTTPS servant la Mini App, ex: https://<domaine>/webapp/)."
            )

    return Config(
        bot_token=_get_required("BOT_TOKEN"),
        use_webhook=use_webhook,
        webhook_secret_token=webhook_secret_token,
        webhook_url=webhook_url,
        webhook_path=os.getenv("WEBHOOK_PATH", "/webhook"),
        webapp_host=os.getenv("WEBAPP_HOST", "0.0.0.0"),
        # Railway (et d'autres PaaS à process persistant) injectent PORT
        # automatiquement ; WEBAPP_PORT reste utilisable en local/autres hébergeurs.
        webapp_port=_get_int("PORT", _get_int("WEBAPP_PORT", 8080)),
        database_url=database_url,
        mini_app_url=mini_app_url,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file_path=os.getenv("LOG_FILE_PATH", "logs/errors.log"),
    )


config = load_config()
