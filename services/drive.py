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

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
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


def find_slot_folder(parent_id: str, slot_number: int) -> str | None:
    """Accepte slot_01, slot_1, Slot_01…"""
    wanted = {slot_folder_name(slot_number).lower(), f"slot_{slot_number}"}
    for child in _list_children(parent_id, folders_only=True):
        name = (child.get("name") or "").strip()
        low = name.lower()
        if low in wanted:
            return child["id"]
        match = _SLOT_RE.match(name)
        if match and int(match.group(1)) == slot_number:
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
    if not is_drive_configured():
        return None
    root_id = find_child_folder(config.google_drive_folder_id, media_root_name(media_kind))
    if root_id is None:
        return None
    price_id = find_child_folder(root_id, str(price_eur))
    if price_id is None:
        return None
    return find_slot_folder(price_id, slot_number)


def infer_kind(file_name: str, mime_type: str, fallback: str) -> str:
    name = (file_name or "").lower()
    if any(name.endswith(ext) for ext in _VIDEO_EXTS):
        return "video"
    if any(name.endswith(ext) for ext in _IMAGE_EXTS):
        return "photo"
    mime = (mime_type or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "photo"
    return fallback


def _collect_slot_files(
    folder_id: str,
    media_kind: str,
    *,
    depth: int = 0,
    max_depth: int = 2,
) -> list[DriveFile]:
    """Fichiers dans le slot (+ sous-dossiers sur 2 niveaux si besoin)."""
    items: list[DriveFile] = []
    for raw in _list_children(folder_id, folders_only=False):
        mime = raw.get("mimeType") or ""
        if mime == _FOLDER_MIME:
            if depth < max_depth:
                items.extend(
                    _collect_slot_files(
                        raw["id"], media_kind, depth=depth + 1, max_depth=max_depth
                    )
                )
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


def describe_slot_folder(folder_id: str, media_kind: str, path_label: str) -> list[str]:
    """Explique ce que Drive voit quand le slot semble vide."""
    lines = [f"Contenu brut de {path_label} :"]
    try:
        children = _list_children(folder_id, folders_only=False)
    except Exception as exc:
        return [f"Impossible de lister : {exc}"]
    if not children:
        lines.append("→ vide (0 élément). Ajoute des fichiers DANS slot_01, pas à côté.")
        return lines
    for raw in children:
        name = raw.get("name") or "?"
        mime = raw.get("mimeType") or "?"
        if mime == _FOLDER_MIME:
            sub_files = _collect_slot_files(raw["id"], media_kind, depth=0, max_depth=1)
            lines.append(
                f"📁 {name}/ — {len(sub_files)} fichier(s) photo/vidéo valide(s) dedans"
            )
            continue
        kind = infer_kind(name, mime, media_kind)
        if kind == media_kind:
            lines.append(f"✅ {name}")
        else:
            lines.append(f"⚠ ignoré : {name} ({mime})")
    valid = _collect_slot_files(folder_id, media_kind)
    lines.append(f"→ {len(valid)} fichier(s) envoyable(s) au total.")
    return lines


def list_slot_files(folder_id: str, *, media_kind: str) -> list[DriveFile]:
    return _collect_slot_files(folder_id, media_kind)


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


def parse_slot_args(raw: str) -> str | None:
    """Convertit vers photos/5/slot_01 depuis plusieurs formats admin."""
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("\\", "/")
    if "/" in text and " " not in text:
        parts = [p.strip() for p in text.split("/") if p.strip()]
        if len(parts) == 3:
            return "/".join(parts)
    parts = text.split()
    if len(parts) == 3:
        kind = parts[0].lower()
        if kind.startswith("photo"):
            media = "photo"
        elif kind.startswith("vid"):
            media = "video"
        else:
            return None
        try:
            price = int(parts[1])
            slot_token = parts[2].lower().replace("slot_", "").replace("slot", "")
            slot_n = int(slot_token) if slot_token else int(parts[2])
        except ValueError:
            return None
        return slot_path(media, price, slot_n)
    return None


def audit_slot_by_path(slot_path: str) -> list[str]:
    """Diagnostic d'un slot précis, ex: photos/5/slot_01."""
    parts = [p.strip() for p in slot_path.replace("\\", "/").split("/") if p.strip()]
    if len(parts) != 3:
        return [
            "Format : photos/5/slot_01 ou videos/10/slot_02",
            "Ex: /drive_slot photos/5/slot_01",
        ]
    root_name = parts[0]
    if root_name not in ("photos", "videos"):
        return ["Le 1er segment doit être photos ou videos."]
    media_kind = "photo" if root_name == "photos" else "video"
    slot_match = _SLOT_RE.match(parts[2])
    if not slot_match:
        return [f"❌ Nom invalide : {parts[2]} — utilise slot_01, slot_02…"]
    folder_id = resolve_slot_folder_id(
        media_kind, int(parts[1]), int(slot_match.group(1))
    )
    if folder_id is None:
        return [f"❌ Dossier introuvable : {slot_path}"]
    lines = [f"📂 {slot_path}"]
    files = list_slot_files(folder_id, media_kind=media_kind)
    if files:
        for f in files:
            lines.append(f"  ✅ {f.name}")
        lines.append(f"→ {len(files)} fichier(s) prêt(s) à envoyer.")
    else:
        lines.extend(describe_slot_folder(folder_id, media_kind, slot_path))
    return lines
