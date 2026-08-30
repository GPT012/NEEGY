"""Chargement et validation de la configuration depuis les variables d'environnement.

Aucune valeur sensible ne doit jamais être codée en dur ici : tout provient du
fichier .env (voir .env.example pour la liste des variables attendues).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any
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


def _sanitize_drive_folder_id(raw: str | None) -> str | None:
    """Nettoie un ID Drive collé avec du texte d'aide (ex: '… (déjà le défaut)')."""
    if not raw:
        return None
    text = raw.strip().strip('"').strip("'")
    # Coupe tout commentaire / parenthèse / espace après l'ID.
    for sep in (" ", "(", "\n", "\t", "#"):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
    # Les IDs Drive sont alphanumériques + _ -
    cleaned = "".join(c for c in text if c.isalnum() or c in "_-")
    return cleaned or None


def _parse_google_service_account(raw: str | None) -> dict[str, Any] | None:
    """Parse le JSON du compte de service. Jamais bloquant : échec → Drive désactivé."""
    if not raw:
        return None
    text = (
        raw.strip()
        .replace("\ufeff", "")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    # Collage fréquent : corps JSON sans { } autour.
    if text.lstrip().startswith('"type"'):
        body = text.strip().rstrip(",")
        if not body.startswith("{"):
            body = "{" + body
        if not body.endswith("}"):
            body = body + "}"
        text = body
    # JSON entier entre guillemets (rare).
    if text.startswith('"{') and text.endswith('}"'):
        try:
            text = json.loads(text)
            if isinstance(text, str):
                pass  # decoded to inner JSON string — handled below
            else:
                text = raw  # unexpected
        except Exception:
            text = text[1:-1]

    candidates: list[str] = []
    if isinstance(text, str):
        candidates.append(text.strip())
        # Si toujours sans accolades après nettoyage.
        t = text.strip()
        if t.startswith('"type"') or (t.startswith('"') and '"type"' in t[:40]):
            body = t.rstrip(",")
            if not body.startswith("{"):
                body = "{" + body
            if not body.endswith("}"):
                body = body + "}"
            candidates.append(body)

    data: Any = None
    errors: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        try:
            if candidate.lstrip().startswith("{"):
                data = json.loads(candidate)
                break
            sample = "".join(candidate.split())
            if len(sample) > 80 and all(c.isalnum() or c in "+/=_-" for c in sample):
                padded = sample + ("=" * (-len(sample) % 4))
                decoded = base64.b64decode(padded)
                data = json.loads(decoded.decode("utf-8"))
                break
        except Exception as exc:
            errors.append(type(exc).__name__)
            data = None

    if data is None:
        import logging

        logging.getLogger(__name__).error(
            "GOOGLE_SERVICE_ACCOUNT_JSON illisible (%s). "
            "Astuce Mac : base64 -i ~/Downloads/ton-fichier.json | pbcopy",
            ",".join(errors) or "vide",
        )
        return None

    if not isinstance(data, dict) or "client_email" not in data or "private_key" not in data:
        import logging

        logging.getLogger(__name__).error(
            "GOOGLE_SERVICE_ACCOUNT_JSON incomplet (client_email / private_key manquants)."
        )
        return None
    return data


def describe_drive_env() -> list[str]:
    """Diagnostic sans secret pour /drive_check."""
    lines: list[str] = []
    raw = _get_str("GOOGLE_SERVICE_ACCOUNT_JSON")
    folder = _get_str("GOOGLE_DRIVE_FOLDER_ID") or "1uzZ27BUbaAsl6Rz4E4oHWNmTqD57gE2l"
    lines.append(f"Folder ID : ✅ {folder[:12]}…")
    if not raw:
        lines.append("JSON : ❌ variable absente ou vide sur ce service Railway")
        lines.append("Méthode simple (Mac Terminal) :")
        lines.append("base64 -i ~/Downloads/TON_FICHIER.json | pbcopy")
        lines.append("Puis colle dans GOOGLE_SERVICE_ACCOUNT_JSON et redéploie.")
        return lines
    first = raw.lstrip()[:1]
    lines.append(f"JSON : présente ({len(raw)} caractères), commence par {first!r}")
    parsed = _parse_google_service_account(raw)
    if parsed:
        lines.append(f"✅ Compte reconnu : {parsed.get('client_email')}")
    else:
        lines.append("❌ Contenu illisible (JSON cassé au collage)")
        lines.append("Efface la variable, encode en base64 (commande ci-dessus), recolle.")
    return lines


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

    # Inbox chatteurs (bot relais Secretary Mode → /inbox/ web).
    inbox_enabled: bool
    business_bot_token: str | None
    business_webhook_secret: str | None
    business_webhook_path: str
    business_webhook_url: str | None

    # Chat/groupe Telegram où tu déposes photos & vidéos (style Drive).
    # Laisse vide pour n'utiliser que la commande /depot en privé avec le bot.
    stock_deposit_chat_id: int | None

    # Google Drive : dossier NEEGY_STOCK + JSON du compte de service.
    google_drive_folder_id: str | None
    google_service_account_info: dict | None

    paypal_url: str | None
    bank_iban: str | None
    bank_holder: str | None
    crypto_solana: str | None
    crypto_ethereum: str | None
    crypto_bitcoin: str | None

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

    inbox_enabled = _get_bool("INBOX_ENABLED", default=False)
    business_bot_token = _get_str("BUSINESS_BOT_TOKEN")
    business_webhook_secret = _get_str("BUSINESS_WEBHOOK_SECRET")
    business_webhook_path = _get_str("BUSINESS_WEBHOOK_PATH", "/webhooks/business")

    business_webhook_url: str | None = None
    business_webhook_url_raw = _get_str("BUSINESS_WEBHOOK_URL")
    if business_webhook_url_raw:
        business_webhook_url = business_webhook_url_raw.rstrip("/")
        _check_https_url(
            "BUSINESS_WEBHOOK_URL",
            business_webhook_url,
            "https://mon-domaine/webhooks/business",
        )
    elif webhook_url and inbox_enabled:
        base = webhook_url.rstrip("/")
        if base.endswith(config_webhook_path := _get_str("WEBHOOK_PATH", "/webhook")):
            root = base[: -len(config_webhook_path)]
        else:
            root = base.rsplit("/", 1)[0]
        business_webhook_url = f"{root}{business_webhook_path}"

    if inbox_enabled:
        missing = [
            name
            for name, value in (
                ("BUSINESS_BOT_TOKEN", business_bot_token),
                ("BUSINESS_WEBHOOK_SECRET", business_webhook_secret),
                ("DATABASE_URL", database_url),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "INBOX_ENABLED=true exige : "
                + ", ".join(missing)
                + ". Voir .env.example."
            )
        if not use_webhook:
            raise ConfigError(
                "INBOX_ENABLED=true exige USE_WEBHOOK=true "
                "(webhook bot relais + inbox web)."
            )
        if not business_webhook_url:
            raise ConfigError(
                "INBOX_ENABLED=true exige BUSINESS_WEBHOOK_URL ou WEBHOOK_URL "
                "pour enregistrer le webhook du bot relais."
            )

    deposit_chat_raw = _get_str("STOCK_DEPOSIT_CHAT_ID")
    stock_deposit_chat_id = (
        _get_required_int("STOCK_DEPOSIT_CHAT_ID") if deposit_chat_raw else None
    )

    # Dossier Drive fourni par l'admin (NEEGY_STOCK).
    google_drive_folder_id = _sanitize_drive_folder_id(
        _get_str("GOOGLE_DRIVE_FOLDER_ID")
    ) or "1uzZ27BUbaAsl6Rz4E4oHWNmTqD57gE2l"
    google_service_account_info = _parse_google_service_account(
        _get_str("GOOGLE_SERVICE_ACCOUNT_JSON")
    )

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
        inbox_enabled=inbox_enabled,
        business_bot_token=business_bot_token,
        business_webhook_secret=business_webhook_secret,
        business_webhook_path=business_webhook_path,
        business_webhook_url=business_webhook_url,
        stock_deposit_chat_id=stock_deposit_chat_id,
        google_drive_folder_id=google_drive_folder_id,
        google_service_account_info=google_service_account_info,
        paypal_url=paypal_url,
        bank_iban=_get_str("BANK_IBAN") or "FR76 2823 3000 0106 8425 4424 364",
        bank_holder=_get_str("BANK_HOLDER") or "Selma Kouassi",
        crypto_solana=_get_str("CRYPTO_SOLANA")
        or "36Swt8qwJyYx1ufb5rdhFSPyPQGG7uqYQNYggpyrE4f9",
        crypto_ethereum=_get_str("CRYPTO_ETHEREUM")
        or "0xeFc510b0536E940046F8689f34E2Ab270c942B91",
        crypto_bitcoin=_get_str("CRYPTO_BITCOIN")
        or "bc1p4902hhgku70353h55dzhwjn75zzrgjekvrzm3xpmd8nl2zlac2pszfwquf",
        log_level=_get_str("LOG_LEVEL", "INFO"),
        log_file_path=_get_str("LOG_FILE_PATH", "logs/errors.log"),
    )


config = load_config()


def public_inbox_url() -> str:
    """URL publique de l'inbox chatteurs (MINI_APP_URL ou WEBHOOK_URL)."""
    if config.mini_app_url:
        url = config.mini_app_url.rstrip("/")
        if url.endswith("/webapp"):
            return f"{url[: -len('/webapp')]}/inbox/"
        return f"{url}/inbox/"
    if config.webhook_url:
        base = config.webhook_url.rstrip("/")
        path = (config.webhook_path or "/webhook").rstrip("/")
        if path and base.endswith(path):
            root = base[: -len(path)]
        else:
            root = base.rsplit("/", 1)[0]
        return f"{root.rstrip('/')}/inbox/"
    return "/inbox/"
