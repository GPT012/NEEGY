"""Client Google Drive (lecture seule) pour le stock NEEGY_STOCK."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SLOT_RE = re.compile(r"^slot_(\d+)$", re.IGNORECASE)
_HTTP_TIMEOUT = 12

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
_DRIVE_API = "https://www.googleapis.com/drive/v3"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int


def is_drive_configured() -> bool:
    return bool(config.google_drive_folder_id and config.google_service_account_info)


@lru_cache(maxsize=1)
def _authorized_session():
    """Session HTTP avec timeout — sans googleapiclient (évite les blocages)."""
    if not is_drive_configured():
        return None
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError:
        logger.error("google-auth / requests manquants")
        return None

    info = config.google_service_account_info
    assert info is not None
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return AuthorizedSession(creds)


def clear_drive_service_cache() -> None:
    _authorized_session.cache_clear()


def _drive_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    session = _authorized_session()
    if session is None:
        raise RuntimeError("Drive non configuré")
    url = f"{_DRIVE_API}{path}"
    response = session.get(url, params=params or {}, timeout=_HTTP_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def _list_children(parent_id: str, *, folders_only: bool | None = None) -> list[dict]:
    parts = [f"'{parent_id}' in parents", "trashed = false"]
    if folders_only is True:
        parts.append(f"mimeType = '{_FOLDER_MIME}'")
    elif folders_only is False:
        parts.append(f"mimeType != '{_FOLDER_MIME}'")
    query = " and ".join(parts)
    files: list[dict] = []
    page_token = None
    while True:
        params: dict[str, Any] = {
            "q": query,
            "spaces": "drive",
            "fields": "nextPageToken, files(id, name, mimeType, size)",
            "pageSize": 100,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _drive_get("/files", params)
        files.extend(payload.get("files") or [])
        page_token = payload.get("nextPageToken")
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
            continue
        name = raw.get("name") or "fichier"
        kind = infer_kind(name, mime, media_kind)
        if kind != media_kind:
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
    session = _authorized_session()
    if session is None:
        raise RuntimeError("Drive non configuré")
    url = f"{_DRIVE_API}/files/{file_id}"
    response = session.get(
        url,
        params={"alt": "media", "supportsAllDrives": "true"},
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Téléchargement HTTP {response.status_code}: {response.text[:200]}")
    return response.content


def audit_structure() -> list[str]:
    """Diagnostic rapide avec timeouts HTTP stricts."""
    email = (config.google_service_account_info or {}).get("client_email", "?")
    if not config.google_drive_folder_id:
        return ["❌ GOOGLE_DRIVE_FOLDER_ID manquant"]
    if not config.google_service_account_info:
        return ["❌ GOOGLE_SERVICE_ACCOUNT_JSON manquant"]

    lines = [f"Compte : {email}"]
    folder_id = config.google_drive_folder_id
    assert folder_id

    try:
        root = _drive_get(
            f"/files/{folder_id}",
            {"fields": "id,name", "supportsAllDrives": "true"},
        )
        lines.append(f"✅ Racine : {root.get('name')}")
    except Exception as exc:
        logger.exception("Audit Drive racine")
        msg = str(exc)
        hint = (
            "Partage le dossier avec le service account (Lecteur) "
            "et active Google Drive API sur neegy-506816."
        )
        if "404" in msg:
            hint = "Dossier introuvable ou non partagé avec neegs-965@…."
        elif "403" in msg or "accessNotConfigured" in msg:
            hint = "Active Google Drive API : console.cloud.google.com/apis/library/drive.googleapis.com"
        return [f"❌ Accès Drive refusé ({exc})", hint]

    try:
        top = _list_children(folder_id, folders_only=True)
    except Exception as exc:
        return lines + [f"❌ Listage racine impossible : {exc}"]

    by_name = {(c.get("name") or "").strip().lower(): c for c in top}
    lines.append(
        "Dossiers racine : " + (", ".join(sorted(by_name)) if by_name else "(vide)")
    )

    expected = {
        "photos": (5, 10, 20),
        "videos": (10, 20, 30),
    }
    for root_name, prices in expected.items():
        node = by_name.get(root_name)
        if node is None:
            lines.append(f"❌ Manquant : {root_name}/")
            continue
        lines.append(f"✅ {root_name}/")
        try:
            price_folders = {
                (c.get("name") or "").strip(): c
                for c in _list_children(node["id"], folders_only=True)
            }
        except Exception as exc:
            lines.append(f"  ❌ lecture {root_name}/ : {exc}")
            continue
        kind = "photo" if root_name == "photos" else "video"
        for price in prices:
            price_node = price_folders.get(str(price))
            if price_node is None:
                lines.append(f"  ❌ {root_name}/{price}/ manquant")
                continue
            try:
                slot_children = _list_children(price_node["id"], folders_only=True)
            except Exception as exc:
                lines.append(f"  ❌ {root_name}/{price}/ : {exc}")
                continue
            slots: list[int] = []
            slot01_id = None
            for child in slot_children:
                match = _SLOT_RE.match((child.get("name") or "").strip())
                if match:
                    n = int(match.group(1))
                    slots.append(n)
                    if n == 1:
                        slot01_id = child["id"]
            slots.sort()
            if not slot01_id:
                lines.append(f"  ❌ {root_name}/{price}/slot_01 manquant")
                continue
            try:
                files = list_slot_files(slot01_id, media_kind=kind)
            except Exception as exc:
                lines.append(f"  ❌ slot_01 {root_name}/{price} : {exc}")
                continue
            if not files:
                lines.append(f"  ⚠ {root_name}/{price}/slot_01 vide")
            else:
                last = slots[-1] if slots else 1
                lines.append(
                    f"  ✅ {root_name}/{price}/ slots 01…{last:02d} "
                    f"(slot_01 : {len(files)} fichier(s))"
                )
    return lines
