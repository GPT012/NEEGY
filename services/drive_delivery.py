"""Attribution et envoi des packs Drive (slots par tarif / cliente)."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile
import asyncpg

from db.repository import mark_order_shipped
from services import drive as drive_client
from utils.logger import get_logger

logger = get_logger(__name__)

# Limite prudente Bot API (upload) — au-delà on alerte l'admin.
_MAX_UPLOAD_BYTES = 48 * 1024 * 1024


async def assign_drive_slot_for_item(
    connection: asyncpg.Connection,
    *,
    user_id: int,
    order_id: int,
    media_kind: str,
    price_cents: int,
) -> tuple[int, str, str | None]:
    """Réserve le prochain slot Drive pour cette commande.

    Retourne (slot_number, slot_path, warning_or_none).
    """
    price_eur = int(price_cents) // 100
    existing = await connection.fetchrow(
        """
        SELECT slot_number, slot_path, drive_folder_id
        FROM drive_slot_deliveries
        WHERE order_id = $1
        """,
        order_id,
    )
    if existing is not None:
        return int(existing["slot_number"]), existing["slot_path"], None

    next_slot = await connection.fetchval(
        """
        SELECT COALESCE(MAX(slot_number), 0) + 1
        FROM drive_slot_deliveries
        WHERE user_id = $1 AND media_kind = $2 AND price_eur = $3
        """,
        user_id,
        media_kind,
        price_eur,
    )
    slot_number = int(next_slot or 1)
    path = drive_client.slot_path(media_kind, price_eur, slot_number)
    await connection.execute(
        """
        INSERT INTO drive_slot_deliveries
            (order_id, user_id, media_kind, price_eur, slot_number, slot_path)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        order_id,
        user_id,
        media_kind,
        price_eur,
        slot_number,
        path,
    )
    return slot_number, path, None


async def get_drive_delivery(pool: asyncpg.Pool, order_id: int):
    return await pool.fetchrow(
        """
        SELECT order_id, user_id, media_kind, price_eur, slot_number, slot_path,
               drive_folder_id, delivered_at
        FROM drive_slot_deliveries
        WHERE order_id = $1
        """,
        order_id,
    )


async def _cached_telegram_id(pool: asyncpg.Pool, drive_file_id: str) -> str | None:
    return await pool.fetchval(
        "SELECT telegram_file_id FROM drive_file_cache WHERE drive_file_id = $1",
        drive_file_id,
    )


async def _store_cache(
    pool: asyncpg.Pool, drive_file_id: str, telegram_file_id: str, kind: str, file_name: str
) -> None:
    await pool.execute(
        """
        INSERT INTO drive_file_cache (drive_file_id, telegram_file_id, kind, file_name, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (drive_file_id) DO UPDATE
        SET telegram_file_id = EXCLUDED.telegram_file_id,
            kind = EXCLUDED.kind,
            file_name = EXCLUDED.file_name,
            updated_at = now()
        """,
        drive_file_id,
        telegram_file_id,
        kind,
        file_name,
    )


def _extract_telegram_file_id(message, kind: str) -> str | None:
    if kind == "video":
        if message.video:
            return message.video.file_id
        if message.document:
            return message.document.file_id
        if message.video_note:
            return message.video_note.file_id
    if message.photo:
        return message.photo[-1].file_id
    if message.document:
        return message.document.file_id
    return None


async def _send_drive_bytes(
    bot: Bot,
    user_id: int,
    *,
    kind: str,
    file_name: str,
    payload: bytes,
) -> str:
    buffered = BufferedInputFile(payload, filename=file_name)
    for attempt in range(5):
        try:
            if kind == "video":
                try:
                    msg = await bot.send_video(
                        user_id,
                        buffered,
                        protect_content=True,
                        supports_streaming=True,
                    )
                except TelegramBadRequest:
                    msg = await bot.send_document(
                        user_id, BufferedInputFile(payload, filename=file_name), protect_content=True
                    )
            else:
                try:
                    msg = await bot.send_photo(
                        user_id, buffered, protect_content=False
                    )
                except TelegramBadRequest:
                    msg = await bot.send_document(
                        user_id,
                        BufferedInputFile(payload, filename=file_name),
                        protect_content=False,
                    )
            file_id = _extract_telegram_file_id(msg, kind)
            if not file_id:
                raise RuntimeError("Telegram n'a pas renvoyé de file_id")
            return file_id
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 0.5)
            if attempt == 4:
                raise
        except TelegramForbiddenError:
            raise
    raise RuntimeError("Envoi Drive abandonné (flood)")


async def _send_cached(
    bot: Bot, user_id: int, *, kind: str, telegram_file_id: str
) -> None:
    if kind == "video":
        try:
            await bot.send_video(
                user_id, telegram_file_id, protect_content=True, supports_streaming=True
            )
        except TelegramBadRequest:
            await bot.send_document(user_id, telegram_file_id, protect_content=True)
        return
    try:
        await bot.send_photo(user_id, telegram_file_id, protect_content=False)
    except TelegramBadRequest:
        await bot.send_document(user_id, telegram_file_id, protect_content=False)


async def deliver_drive_for_order(bot: Bot, pool: asyncpg.Pool, order_id: int) -> tuple[list[str], bool]:
    """Envoie le pack Drive de la commande. Retourne (erreurs, succès_complet)."""
    if not drive_client.is_drive_configured():
        return [], False

    row = await get_drive_delivery(pool, order_id)
    if row is None:
        return [], False
    if row["delivered_at"] is not None:
        return [], True

    user_id = int(row["user_id"])
    media_kind = row["media_kind"]
    path = row["slot_path"]
    folder_id = row["drive_folder_id"]
    errors: list[str] = []

    if not folder_id:
        try:
            folder_id = await asyncio.to_thread(
                drive_client.resolve_slot_folder_id,
                media_kind,
                int(row["price_eur"]),
                int(row["slot_number"]),
            )
        except Exception as exc:
            logger.exception("Resolve Drive %s", path)
            return [f"Drive inaccessible ({path}) : {exc}"], False
        if folder_id:
            await pool.execute(
                "UPDATE drive_slot_deliveries SET drive_folder_id = $2 WHERE order_id = $1",
                order_id,
                folder_id,
            )

    if not folder_id:
        return [f"Dossier Drive introuvable : {path}"], False

    try:
        files = await asyncio.to_thread(
            drive_client.list_slot_files, folder_id, media_kind=media_kind
        )
    except Exception as exc:
        logger.exception("List Drive %s", path)
        return [f"Impossible de lister {path} : {exc}"], False

    if not files:
        detail = await asyncio.to_thread(
            drive_client.describe_slot_folder, folder_id, media_kind, path
        )
        msg = f"Slot Drive vide : {path}"
        return [msg, *detail], False

    try:
        await bot.send_message(user_id, f"📦 Voici ton contenu ({path}) :")
    except TelegramForbiddenError as exc:
        return [
            "Cliente inaccessible (pas de /start ou bot bloqué). "
            f"Après /start → /fulfill {order_id}"
        ], False
    except Exception:
        logger.exception("Annonce Drive user=%s", user_id)

    sent = 0
    for item in files:
        # Déjà livré ?
        prior = await pool.fetchval(
            """
            SELECT delivered_at FROM drive_order_files
            WHERE order_id = $1 AND drive_file_id = $2
            """,
            order_id,
            item.id,
        )
        if prior is not None:
            sent += 1
            continue

        await pool.execute(
            """
            INSERT INTO drive_order_files (order_id, drive_file_id, kind, file_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (order_id, drive_file_id) DO NOTHING
            """,
            order_id,
            item.id,
            media_kind,
            item.name,
        )

        try:
            cached = await _cached_telegram_id(pool, item.id)
            if cached:
                await _send_cached(bot, user_id, kind=media_kind, telegram_file_id=cached)
                telegram_file_id = cached
            else:
                if item.size and item.size > _MAX_UPLOAD_BYTES:
                    errors.append(
                        f"{item.name} trop lourd ({item.size // (1024 * 1024)} Mo) — max ~48 Mo"
                    )
                    continue
                payload = await asyncio.to_thread(drive_client.download_file, item.id)
                if len(payload) > _MAX_UPLOAD_BYTES:
                    errors.append(f"{item.name} trop lourd après téléchargement")
                    continue
                telegram_file_id = await _send_drive_bytes(
                    bot, user_id, kind=media_kind, file_name=item.name, payload=payload
                )
                await _store_cache(pool, item.id, telegram_file_id, media_kind, item.name)

            await pool.execute(
                """
                UPDATE drive_order_files
                SET telegram_file_id = $3, delivered_at = now()
                WHERE order_id = $1 AND drive_file_id = $2
                """,
                order_id,
                item.id,
                telegram_file_id,
            )
            sent += 1
            await asyncio.sleep(0.1)
        except TelegramForbiddenError:
            errors.append(
                "Cliente inaccessible (pas de /start). Après /start → /fulfill "
                f"{order_id}"
            )
            break
        except Exception as exc:
            logger.exception("Envoi Drive %s → user %s", item.id, user_id)
            errors.append(f"{item.name} non envoyé : {exc}")

    if sent >= len(files) and not errors:
        await pool.execute(
            "UPDATE drive_slot_deliveries SET delivered_at = now() WHERE order_id = $1",
            order_id,
        )
        await mark_order_shipped(pool, order_id)
        return [], True

    if sent > 0 and errors:
        errors.insert(0, f"Envoi partiel Drive {path} : {sent}/{len(files)}")
    elif sent == 0 and not errors:
        errors.append(f"Aucun fichier envoyé pour {path}")
    return errors, False
