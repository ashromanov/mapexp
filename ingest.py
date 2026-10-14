"""One durable Telegram worker: live updates, backfill, evidence and maintenance."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import signal
import time
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError, RPCError

from llm import LLMBlocked, ProviderFailure
from pipeline import process_post
from settings import ROOT, load_env, settings
from storage import (
    attach_comment,
    connect,
    discard_non_source_posts,
    ensure_source,
    event_source_posts_for_comments,
    get_post,
    init_db,
    now,
    pending_post_ids,
    purge_expired_events,
    save_media,
    save_post,
    schedule_retry,
    set_media_path,
    worker_status,
)

log = logging.getLogger("mapexp.ingest")


def require_env(name: str, cast=str):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing {name}")
    return cast(value)


def load_sources(path: Path) -> list[dict]:
    items = json.loads(path.read_text())
    if not isinstance(items, list) or not items:
        raise ValueError("sources.json must contain at least one public channel")
    keys = set()
    usernames = set()
    for item in items:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) and item[k].strip()
            for k in ["key", "name", "username", "timezone"]
        ):
            raise ValueError("Each source needs key, name, username and timezone")
        item["username"] = item["username"].removeprefix("@")
        if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", item["username"]):
            raise ValueError("Source must be a public Telegram username")
        ZoneInfo(item["timezone"])
        if item["key"] in keys or item["username"].lower() in usernames:
            raise ValueError("Duplicate source")
        keys.add(item["key"])
        usernames.add(item["username"].lower())
    return items


def message_link(username: str | None, message_id: int) -> str | None:
    return f"https://t.me/{username}/{message_id}" if username else None


def is_source_message(message, entity) -> bool:
    return message.chat_id == utils.get_peer_id(entity)


def queue_priority(posted_at: str, *, live: bool) -> tuple[int, float]:
    stamp = datetime.fromisoformat(posted_at).timestamp()
    return (0 if live else 1, -stamp)


def media_items(message) -> list[dict]:
    if not message.media:
        return []
    file = getattr(message, "file", None)
    mime = getattr(file, "mime_type", None)
    if message.photo:
        kind, mime = "photo", "image/jpeg"
    elif mime and mime.startswith("video/"):
        kind = "video"
    elif mime and mime.startswith("audio/"):
        kind = "audio"
    else:
        kind = "document"
    file_id = str(getattr(file, "id", None) or message.id)
    return [{"kind": kind, "mime_type": mime, "telegram_file_id": file_id}]


async def download_media(client: TelegramClient, message, post_id: int) -> None:
    if not message or not message.media:
        return
    with connect() as conn:
        active = conn.execute(
            "SELECT 1 FROM event_posts ep JOIN events e ON e.id=ep.event_id WHERE ep.post_id=? AND e.status='active' AND e.expires_at>?",
            (post_id, now()),
        ).fetchone()
        missing = conn.execute(
            "SELECT telegram_file_id FROM media WHERE post_id=? AND local_path IS NULL",
            (post_id,),
        ).fetchall()
    if not active or not missing:
        return
    item = media_items(message)[0]
    if item["telegram_file_id"] not in {row[0] for row in missing}:
        return
    settings.media.mkdir(parents=True, exist_ok=True)
    file_base = settings.media / f"{post_id}_{item['telegram_file_id']}"
    output = await client.download_media(message, file=str(file_base))
    if output:
        relative = Path(output).resolve().relative_to(settings.media.resolve())
        set_media_path(post_id, item["telegram_file_id"], relative.as_posix())


def store_message(
    message,
    source: dict,
    *,
    parent_post_id: int | None = None,
    comment: bool = False,
    link: str | None = None,
) -> tuple[int, bool]:
    items = media_items(message)
    post_id, changed = save_post(
        {
            "source_id": ensure_source(source),
            "chat_id": message.chat_id,
            "message_id": message.id,
            "parent_post_id": parent_post_id,
            "posted_at": message.date.astimezone(UTC).isoformat(),
            "edited_at": message.edit_date.astimezone(UTC).isoformat()
            if message.edit_date
            else None,
            "text": message.raw_text or "",
            "link": link if comment else message_link(source["username"], message.id),
            "raw_json": {
                "grouped_id": getattr(message, "grouped_id", None),
                "has_media": bool(items),
                "media_ids": [x["telegram_file_id"] for x in items],
            },
        }
    )
    for item in items:
        save_media(post_id, item)
    if comment and parent_post_id:
        attach_comment(parent_post_id, post_id)
    return post_id, changed


async def sync_today(
    client, sources, entities, enqueue, recent_hours: int | None = None
) -> None:
    for source in sources:
        tz = ZoneInfo(source["timezone"])
        local = datetime.now(tz)
        start = (
            datetime.now(UTC) - timedelta(hours=recent_hours)
            if recent_hours
            else local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
                UTC
            )
        )
        messages = []
        entity = entities[source["key"]]
        async for message in client.iter_messages(entity):
            if message.date < start:
                break
            if is_source_message(message, entity):
                messages.append(message)
        for message in reversed(messages):
            post_id, changed = store_message(message, source)
            if changed:
                enqueue(post_id)
        log.info("backfill source=%s posts=%s", source["key"], len(messages))
    worker_status("state", "watching")
    worker_status("backfill_completed_at", now())
    log.info("backfill complete")


async def sync_comments(client, sources, entities) -> None:
    source_by_username = {x["username"]: x for x in sources}
    for parent in event_source_posts_for_comments():
        source = source_by_username.get(parent["username"])
        if not source:
            continue
        try:
            async for comment in client.iter_messages(
                entities[source["key"]], reply_to=parent["message_id"], limit=100
            ):
                # Telegram comment links refer to the channel POST, with the discussion message as ?comment=.
                link = f"https://t.me/{source['username']}/{parent['message_id']}?comment={comment.id}"
                post_id, _ = store_message(
                    comment,
                    source,
                    parent_post_id=parent["id"],
                    comment=True,
                    link=link,
                )
                await download_media(client, comment, post_id)
        except FloodWaitError:
            raise
        except RPCError as exc:
            log.warning(
                "comments unavailable source=%s error=%s",
                source["key"],
                type(exc).__name__,
            )


async def retry_media(client, sources, entities) -> None:
    source_by_key = {x["key"]: x for x in sources}
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.id,p.message_id,p.chat_id,p.parent_post_id,s.source_key FROM posts p JOIN sources s ON s.id=p.source_id JOIN media m ON m.post_id=p.id JOIN event_posts ep ON ep.post_id=p.id JOIN events e ON e.id=ep.event_id WHERE m.local_path IS NULL AND e.status='active' AND e.expires_at>? LIMIT 20",
            (now(),),
        ).fetchall()
    for row in rows:
        source = source_by_key.get(row["source_key"])
        if not source or row["parent_post_id"]:
            continue  # Comments retry during their own sync.
        message = await client.get_messages(
            entities[source["key"]], ids=row["message_id"]
        )
        await download_media(client, message, row["id"])


async def poll_sources(client, sources, entities, enqueue, cursors) -> None:
    """Public channel history works even when this account does not subscribe."""
    active_posts = event_source_posts_for_comments()
    for source in sources:
        entity = entities[source["key"]]
        if source["key"] not in cursors:
            latest = await client.get_messages(entity, limit=1)
            messages = latest
        else:
            messages = [
                m
                async for m in client.iter_messages(
                    entity, min_id=cursors[source["key"]], reverse=True
                )
            ]
        for message in messages:
            if is_source_message(message, entity):
                post_id, changed = store_message(message, source)
                if changed:
                    enqueue(post_id, live=True)
                cursors[source["key"]] = max(cursors.get(source["key"], 0), message.id)
        ids = [
            p["message_id"] for p in active_posts if p["username"] == source["username"]
        ]
        if ids:
            for message in await client.get_messages(entity, ids=ids):
                if message and is_source_message(message, entity):
                    post_id, changed = store_message(message, source)
                    if changed:
                        enqueue(post_id, live=True)
        cursors.setdefault(source["key"], 0)
    worker_status("last_polled_at", now())


async def backfill_with_retry(
    client, sources, entities, enqueue, recent_hours, stop
) -> None:
    attempts = 0
    while not stop.is_set():
        try:
            await sync_today(client, sources, entities, enqueue, recent_hours)
            return
        except FloodWaitError as exc:
            delay = exc.seconds
        except (
            RPCError,
            ConnectionError,
            OSError,
            asyncio.TimeoutError,
        ) as exc:
            attempts += 1
            delay = min(300, 5 * 2 ** min(attempts, 6))
            worker_status("last_error", exc)
        log.warning("backfill interrupted; retrying in %s seconds", delay)
        await asyncio.sleep(delay)


async def run(args) -> None:
    init_db()
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    lock_path = settings.database.with_suffix(".ingest.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another ingest worker already owns this database"
            ) from exc
        await _run(args)


async def _run(args) -> None:
    sources = load_sources(Path(args.sources))
    client = TelegramClient(
        str(settings.session), require_env("TG_API_ID", int), require_env("TG_API_HASH")
    )
    tasks = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    worker_status("state", "connecting")
    worker_status("started_at", now())
    worker_status("backfill_completed_at", None)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Existing Telegram session is not authorized; interactive login is required"
            )
        entities = {
            source["key"]: await client.get_entity(source["username"])
            for source in sources
        }
        source_by_key = {x["key"]: x for x in sources}
        chats = {
            utils.get_peer_id(entity): source_by_key[key]
            for key, entity in entities.items()
        }
        discard_non_source_posts(
            {key: utils.get_peer_id(entity) for key, entity in entities.items()}
        )
        queue = asyncio.PriorityQueue()
        queued = set()
        sequence = count()
        paused_until = 0.0

        def enqueue(post_id: int, *, live: bool = False) -> None:
            if post_id in queued:
                return
            post = get_post(post_id)
            if not post or post["parent_post_id"]:
                return
            queued.add(post_id)
            queue.put_nowait(
                (*queue_priority(post["posted_at"], live=live), next(sequence), post_id)
            )

        async def worker() -> None:
            nonlocal paused_until
            while not stop.is_set():
                if time.monotonic() < paused_until:
                    await asyncio.sleep(min(5, paused_until - time.monotonic()))
                    continue
                _, _, _, post_id = await queue.get()
                post = get_post(post_id)
                revision = post["revision"] if post else None
                try:
                    event_id = await asyncio.to_thread(process_post, post_id)
                    worker_status("last_success", now())
                    worker_status("provider_blocked", False)
                    worker_status("last_error", None)
                    if event_id:
                        current = get_post(post_id)
                        source = (
                            source_by_key.get(current["source_key"])
                            if current
                            else None
                        )
                        if source:
                            try:
                                message = await client.get_messages(
                                    entities[source["key"]], ids=current["message_id"]
                                )
                                await download_media(client, message, post_id)
                            except (RPCError, OSError) as exc:
                                log.warning(
                                    "media deferred post=%s error=%s",
                                    post_id,
                                    type(exc).__name__,
                                )
                    log.info("processed post=%s event=%s", post_id, event_id or "none")
                except LLMBlocked as exc:
                    schedule_retry(post_id, exc, blocked=True)
                    paused_until = time.monotonic() + max(300, exc.retry_after)
                    worker_status("provider_blocked", True)
                    worker_status("last_error", exc)
                    log.error("provider circuit paused: %s", exc)
                except Exception as exc:
                    schedule_retry(post_id, exc)
                    worker_status("last_error", exc)
                    log.warning(
                        "post=%s deferred error=%s%s",
                        post_id,
                        type(exc).__name__,
                        f" reason={exc}" if isinstance(exc, ProviderFailure) else "",
                    )
                finally:
                    queued.discard(post_id)
                    queue.task_done()
                    current = get_post(post_id)
                    if (
                        current
                        and current["revision"] != revision
                        and current["llm_status"] == "pending"
                    ):
                        enqueue(post_id, live=True)

        async def heartbeat() -> None:
            while not stop.is_set():
                worker_status("heartbeat", now())
                worker_status(
                    "state", "watching" if client.is_connected() else "disconnected"
                )
                for post_id in pending_post_ids():
                    enqueue(post_id)
                await asyncio.sleep(15)

        async def maintenance() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(purge_expired_events)
                    await retry_media(client, sources, entities)
                    await sync_comments(client, sources, entities)
                except FloodWaitError as exc:
                    log.warning("Telegram rate limit: waiting %s seconds", exc.seconds)
                    await asyncio.sleep(exc.seconds)
                except Exception as exc:
                    log.warning("maintenance deferred error=%s", type(exc).__name__)
                await asyncio.sleep(120)

        async def polling() -> None:
            cursors = {}
            while not stop.is_set():
                try:
                    await poll_sources(client, sources, entities, enqueue, cursors)
                except FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds)
                except (
                    RPCError,
                    ConnectionError,
                    OSError,
                    asyncio.TimeoutError,
                ) as exc:
                    worker_status("last_error", exc)
                    log.warning("source polling deferred error=%s", type(exc).__name__)
                await asyncio.sleep(30)

        async def receive(event) -> None:
            source = chats.get(event.chat_id)
            if source and is_source_message(event.message, entities[source["key"]]):
                post_id, changed = store_message(event.message, source)
                if changed:
                    enqueue(post_id, live=True)

        client.add_event_handler(
            receive, events.NewMessage(chats=list(entities.values()))
        )
        client.add_event_handler(
            receive, events.MessageEdited(chats=list(entities.values()))
        )
        tasks.append(asyncio.create_task(worker(), name="decisions"))
        for post_id in pending_post_ids():
            enqueue(post_id)

        backfill = (
            asyncio.create_task(
                backfill_with_retry(
                    client, sources, entities, enqueue, args.recent_hours, stop
                )
            )
            if args.today or args.recent_hours
            else None
        )
        if backfill:
            tasks.append(backfill)
        if not args.watch:
            if backfill:
                await backfill
            if args.comments:
                await sync_comments(client, sources, entities)
            await queue.join()
            return
        supervised = [
            tasks[0],
            asyncio.create_task(heartbeat()),
            asyncio.create_task(maintenance()),
            asyncio.create_task(polling()),
        ]
        tasks.extend(supervised[1:])
        tasks.append(asyncio.create_task(client.run_until_disconnected()))
        tasks.append(asyncio.create_task(stop.wait()))
        log.info("watching %s public source channels", len(sources))
        watched = set([*supervised, *tasks[-2:]])
        if backfill:
            watched.add(backfill)
        while watched:
            done, watched = await asyncio.wait(
                watched, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if not task.cancelled() and task.exception():
                    raise task.exception()
                if task is not backfill:
                    return
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()
        worker_status("state", "stopped")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default=str(ROOT / "sources.json"))
    parser.add_argument(
        "--telegram-env",
        type=Path,
        help="Optional existing Telegram credential file; values are never printed",
    )
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--recent-hours", type=int)
    parser.add_argument("--comments", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if args.recent_hours is not None and args.recent_hours <= 0:
        parser.error("--recent-hours must be positive")
    if not any([args.today, args.recent_hours, args.comments, args.watch, args.retry]):
        parser.error("Choose --watch, --recent-hours, --today, --comments or --retry")
    if args.telegram_env:
        if not args.telegram_env.is_file():
            parser.error("--telegram-env file does not exist")
        load_env(args.telegram_env)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
