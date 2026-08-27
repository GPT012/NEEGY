"""Dépôt style Drive : drop photos/vidéos → files stock automatiques."""

from __future__ import annotations

import asyncio

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, TelegramObject

from config import config
from db.repository import (
    POOL_PHOTOS,
    POOL_VIDEOS,
    add_reward_assets_bulk,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = Router(name="deposit")

_album_buffers: dict[str, list[tuple[str, str, str]]] = {}
_album_tasks: dict[str, asyncio.Task] = {}
_ALBUM_FLUSH_DELAY = 1.2


class DepotFSM(StatesGroup):
    receiving = State()


class DepositChatFilter(Filter):
    async def __call__(self, event: TelegramObject) -> bool:
        if config.stock_deposit_chat_id is None:
            return False
        chat = getattr(event, "chat", None)
        return chat is not None and chat.id == config.stock_deposit_chat_id


def _extract_media(message: Message) -> tuple[str, str, str] | None:
    """Retourne (kind, file_id, unique_id) ou None."""
    if message.photo:
        photo = message.photo[-1]
        return "photo", photo.file_id, photo.file_unique_id
    if message.video:
        return "video", message.video.file_id, message.video.file_unique_id
    if message.video_note:
        return "video", message.video_note.file_id, message.video_note.file_unique_id
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("video/"):
            return "video", message.document.file_id, message.document.file_unique_id
        if mime.startswith("image/"):
            return "photo", message.document.file_id, message.document.file_unique_id
    return None


def _pool_for_kind(kind: str) -> str:
    return POOL_VIDEOS if kind == "video" else POOL_PHOTOS


async def _flush_album(
    *,
    album_key: str,
    message: Message,
    db_pool: asyncpg.Pool,
) -> None:
    await asyncio.sleep(_ALBUM_FLUSH_DELAY)
    items = _album_buffers.pop(album_key, [])
    _album_tasks.pop(album_key, None)
    if not items:
        return
    by_pool: dict[str, list[tuple[str, str, str]]] = {}
    for kind, file_id, unique_id in items:
        by_pool.setdefault(_pool_for_kind(kind), []).append((kind, file_id, unique_id))
    parts: list[str] = []
    try:
        for pool_name, pool_items in by_pool.items():
            added, skipped = await add_reward_assets_bulk(
                db_pool, pool_name=pool_name, items=pool_items
            )
            label = "photos" if pool_name == POOL_PHOTOS else "vidéos"
            parts.append(f"{added} {label}" + (f" ({skipped} doublons)" if skipped else ""))
    except Exception:
        logger.exception("Erreur dépôt album")
        await message.answer("Erreur lors de l'ajout de l'album.")
        return
    await message.answer("📁 Album rangé : " + ", ".join(parts) + ".")


async def _ingest_media(message: Message, db_pool: asyncpg.Pool) -> None:
    media = _extract_media(message)
    if media is None:
        return
    kind, file_id, unique_id = media

    if message.media_group_id:
        album_key = f"{message.chat.id}:{message.media_group_id}"
        _album_buffers.setdefault(album_key, []).append((kind, file_id, unique_id))
        old = _album_tasks.get(album_key)
        if old and not old.done():
            old.cancel()
        _album_tasks[album_key] = asyncio.create_task(
            _flush_album(album_key=album_key, message=message, db_pool=db_pool)
        )
        return

    pool_name = _pool_for_kind(kind)
    try:
        added, skipped = await add_reward_assets_bulk(
            db_pool, pool_name=pool_name, items=[(kind, file_id, unique_id)]
        )
    except Exception:
        logger.exception("Erreur dépôt fichier")
        await message.answer("Erreur lors de l'ajout.")
        return
    if skipped and not added:
        await message.answer("Déjà en stock (doublon).")
        return
    label = "photo" if kind == "photo" else "vidéo"
    await message.answer(f"✅ {label.capitalize()} ajoutée à la file {label}s.")


def _is_admin(message: Message) -> bool:
    return (
        message.from_user is not None
        and config.admin_user_id is not None
        and message.from_user.id == config.admin_user_id
    )


@router.message(Command("depot"))
async def handle_depot_open(message: Message, state: FSMContext) -> None:
    if not _is_admin(message):
        return
    await state.set_state(DepotFSM.receiving)
    chat_hint = ""
    if config.stock_deposit_chat_id:
        chat_hint = (
            f"\n\nTu as aussi un chat Drive (ID {config.stock_deposit_chat_id}) : "
            "tout média posté là-bas est rangé automatiquement."
        )
    await message.answer(
        "📁 Dépôt ouvert.\n"
        "Envoie des photos et vidéos (albums OK) — elles partent dans les bonnes files.\n"
        "Tape /depot_stop pour fermer."
        f"{chat_hint}"
    )


@router.message(Command("depot_stop"))
async def handle_depot_stop(message: Message, state: FSMContext) -> None:
    if not _is_admin(message):
        return
    await state.clear()
    await message.answer("📁 Dépôt fermé.")


@router.message(StateFilter(DepotFSM.receiving), F.photo | F.video | F.video_note | F.document)
async def handle_depot_private(
    message: Message, state: FSMContext, db_pool: asyncpg.Pool | None
) -> None:
    if not _is_admin(message):
        return
    if db_pool is None:
        await message.answer("Base indisponible.")
        return
    await _ingest_media(message, db_pool)


@router.message(
    DepositChatFilter(),
    F.photo | F.video | F.video_note | F.document,
)
async def handle_depot_chat(message: Message, db_pool: asyncpg.Pool | None) -> None:
    if not _is_admin(message):
        return
    if db_pool is None:
        return
    await _ingest_media(message, db_pool)
