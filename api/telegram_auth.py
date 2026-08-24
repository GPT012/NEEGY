"""Validation cryptographique du `initData` envoyé par une Telegram Mini App.

Algorithme officiel Telegram (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app) :
1. Extraire le champ `hash` du initData, le retirer des données à vérifier.
2. Construire une chaîne "clé=valeur" triée par clé, jointe par des \n.
3. Calculer secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token).
4. Calculer computed_hash = HMAC_SHA256(key=secret_key, msg=data_check_string).
5. Comparer computed_hash à hash en temps constant (hmac.compare_digest).

Ne jamais faire confiance à un user_id envoyé par le client sans cette
vérification : n'importe qui pourrait sinon usurper l'identité d'un autre
utilisateur en fabriquant sa propre requête.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidInitData(Exception):
    """Levée quand le initData est absent, mal formé, expiré ou falsifié."""


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict:
    """Valide le initData et retourne le dict `user` décodé (id, first_name, ...).

    Lève InvalidInitData si la signature est invalide, absente, ou trop ancienne.
    """
    if not init_data:
        raise InvalidInitData("initData manquant")

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise InvalidInitData("initData mal formé") from exc

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("hash manquant dans initData")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InvalidInitData("signature invalide")

    auth_date_raw = parsed.get("auth_date", "0")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise InvalidInitData("auth_date invalide") from exc

    if max_age_seconds and (time.time() - auth_date) > max_age_seconds:
        raise InvalidInitData("initData expiré")

    user_raw = parsed.get("user")
    if not user_raw:
        raise InvalidInitData("champ user manquant dans initData")

    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError) as exc:
        raise InvalidInitData("champ user mal formé") from exc

    if "id" not in user:
        raise InvalidInitData("user.id manquant")

    return user
