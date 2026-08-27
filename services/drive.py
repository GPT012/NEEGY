"""Client Google Drive (lecture seule) pour le stock NEEGY_STOCK."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import lru_cache

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SLOT_RE = re.compile(r"^slot_(\d+)$", re.IGNORECASE)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int


def is_drive_configured() -> bool:
    return bool(config.google_drive_folder_id and config.google_service_account_info)


@lru_cache(maxsize=1)
def _drive_service():
    if not is_drive_configured():
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.error("Paquets Google absents — ajoute google-api-python-client et google-auth")
        return None

    info = config.google_service_account_info
    assert info is not None
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def clear_drive_service_cache() -> None:
    _drive_service.cache_clear()


def _list_children(parent_id: str, *, folders_only: bool | None = None) -> list[dict]:
    service = _drive_service()
    if service is None:
        return []
    parts = [f"'{parent_id}' in parents", "trashed = false"]
    if folders_only is True:
        parts.append(f"mimeType = '{_FOLDER_MIME}'")
    elif folders_only is False:
        parts.append(f"mimeType != '{_FOLDER_MIME}'")
    query = " and ".join(parts)
    files: list[dict] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def find_child_folder(parent_id: str, name: str) -> str | None:
    target = name.strip().lower()
    for child in _list_children(parent_id, folders_only=True):
        if child.get("name", "").strip().lower() == target:
            return child["id"]
    return None


def resolve_path(folder_names: list[str]) -> str | None:
    """Résout une liste de noms depuis la racine NEEGY_STOCK."""
    if not is_drive_configured():
        return None
    current = config.google_drive_folder_id
    assert current
    for name in folder_names:
        found = find_child_folder(current, name)
        if found is None:
            return None
        current = found
    return current


def media_root_name(media_kind: str) -> str:
    return "photos" if media_kind == "photo" else "videos"


def slot_folder_name(slot_number: int) -> str:
    return f"slot_{slot_number:02d}"


def slot_path(media_kind: str, price_eur: int, slot_number: int) -> str:
    return f"{media_root_name(media_kind)}/{price_eur}/{slot_folder_name(slot_number)}"


def resolve_slot_folder_id(media_kind: str, price_eur: int, slot_number: int) -> str | None:
    return resolve_path(
        [media_root_name(media_kind), str(price_eur), slot_folder_name(slot_number)]
    )


def infer_kind(file_name: str, mime_type: str, fallback: str) -> str:
    mime = (mime_type or "").lower()
    name = (file_name or "").lower()
    if mime.startswith("video/") or any(name.endswith(ext) for ext in _VIDEO_EXTS):
        return "video"
    if mime.startswith("image/") or any(name.endswith(ext) for ext in _IMAGE_EXTS):
        return "photo"
    return fallback


def list_slot_files(folder_id: str, *, media_kind: str) -> list[DriveFile]:
    items: list[DriveFile] = []
    for raw in _list_children(folder_id, folders_only=False):
        mime = raw.get("mimeType") or ""
        if mime == _FOLDER_MIME:
            continue
        if mime.startswith("application/vnd.google-apps."):
            # Docs Google natifs non envoyables tels quels.
            continue
        name = raw.get("name") or "fichier"
        kind = infer_kind(name, mime, media_kind)
        if kind != media_kind:
            # Ignore un jpg dans un slot vidéo, etc.
            continue
        items.append(
            DriveFile(
                id=raw["id"],
                name=name,
                mime_type=mime,
                size=int(raw.get("size") or 0),
            )
        )
    items.sort(key=lambda f: f.name.lower())
    return items


def download_file(file_id: str) -> bytes:
    service = _drive_service()
    if service is None:
        raise RuntimeError("Drive non configuré")
    from googleapiclient.http import MediaIoBaseDownload

    buffer = io.BytesIO()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    downloader = MediaIoBaseDownload(buffer, request, chunksize=1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buffer.getvalue()


def audit_structure() -> list[str]:
    """Retourne des lignes de diagnostic pour /drive_check."""
    lines: list[str] = []
    if not config.google_drive_folder_id:
        return ["❌ GOOGLE_DRIVE_FOLDER_ID manquant"]
    if not config.google_service_account_info:
        return [
            "❌ GOOGLE_SERVICE_ACCOUNT_JSON manquant",
            "Crée un compte de service Google Cloud, active Drive API,",
            "puis partage NEEGY_STOCK avec l'email du compte (Lecteur).",
        ]
    try:
        service = _drive_service()
        if service is None:
            return ["❌ Impossible d'initialiser le client Drive"]
        root = service.files().get(
            fileId=config.google_drive_folder_id,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        lines.append(f"✅ Racine : {root.get('name')} ({root.get('id')})")
    except Exception as exc:
        logger.exception("Audit Drive racine")
        return [
            f"❌ Accès racine refusé : {exc}",
            "Vérifie que le dossier est partagé avec l'email du service account.",
        ]

    expected = {
        "photos": (5, 10, 20),
        "videos": (10, 20, 30),
    }
    for root_name, prices in expected.items():
        root_id = find_child_folder(config.google_drive_folder_id, root_name)
        if root_id is None:
            lines.append(f"❌ Dossier manquant : {root_name}/")
            continue
        lines.append(f"✅ {root_name}/")
        for price in prices:
            price_id = find_child_folder(root_id, str(price))
            if price_id is None:
                lines.append(f"  ❌ {root_name}/{price}/ manquant")
                continue
            slots = []
            for child in _list_children(price_id, folders_only=True):
                match = _SLOT_RE.match((child.get("name") or "").strip())
                if match:
                    slots.append(int(match.group(1)))
            slots.sort()
            if 1 not in slots:
                lines.append(f"  ❌ {root_name}/{price}/slot_01 manquant")
                continue
            slot01 = resolve_path([root_name, str(price), "slot_01"])
            kind = "photo" if root_name == "photos" else "video"
            files = list_slot_files(slot01, media_kind=kind) if slot01 else []
            if not files:
                lines.append(f"  ⚠ {root_name}/{price}/slot_01 vide (0 fichier {kind})")
            else:
                lines.append(
                    f"  ✅ {root_name}/{price}/ — slots {slots[0]:02d}…{slots[-1]:02d} "
                    f"(slot_01 : {len(files)} fichier(s))"
                )
    return lines
