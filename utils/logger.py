"""Configuration du logging applicatif.

Deux sorties :
- console : niveau configurable (LOG_LEVEL), utile en développement.
- fichier séparé : uniquement WARNING/ERROR, pour le suivi en production.

Règle de sécurité : ne jamais logger de tokens, mots de passe, secrets ou
contenu brut de messages privés. Les handlers ne doivent logger que des
identifiants non sensibles (ex: user_id) et le type/message d'erreur.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_CONFIGURED = False


def setup_logging(log_level: str, log_file_path: str) -> None:
    """Configure le logging root une seule fois (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = os.path.dirname(log_file_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level.upper())
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
