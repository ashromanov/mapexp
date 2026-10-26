"""Local HTTP/SSE regression on a disposable copy; never changes the source DB.

Run: .venv/bin/python tools/load_test.py --database data/render-comparison.sqlite3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def serve(port: int) -> None:
    # Instrumentation exists only in this isolated test process.
    import resource
    import threading

    import uvicorn

    import web

    counters = {"revision_checks": 0, "feed_builds": 0}
    lock = threading.Lock()

    def instrument(name, fn):
        def tracked(*args, **kwargs):
            with lock:
                counters[name] += 1
            return fn(*args, **kwargs)

        return tracked

    web.stream_revision = instrument("revision_checks", web.stream_revision)
    web.feed_snapshot = instrument("feed_builds", web.feed_snapshot)

    @web.app.get("/__test_metrics")
    def metrics():
        return {
            **counters,
            "pid": os.getpid(),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }

    uvicorn.run(
        web.app, host="127.0.0.1", port=port, access_log=False, log_level="warning"
    )


async def exercise(port: int, users: int, database: Path, expected_pid: int) -> dict:
    import httpx

    base = f"http://127.0.0.1:{port}"
    state = {"connected": 0, "updated": 0, "heartbeats": set()}
    ready, updated = asyncio.Event(), asyncio.Event()
    semaphore = asyncio.Semaphore(100)
    errors = []

    async with httpx.AsyncClient(base_url=base, timeout=30) as api:
        for _ in range(200):
            try:
                if (await api.get("/healthz")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("Server did not start")
        identity = (await api.get("/__test_metrics")).json()
        assert identity["pid"] == expected_pid, "Port belongs to a different process"
        initial = await api.get("/api/events", headers={"Accept-Encoding": "gzip"})
        assert (
            initial.status_code == 200 and initial.headers["content-encoding"] == "gzip"
        )
        initial_count = len(initial.json())
        etag = initial.headers["etag"]
        event_id = initial.json()[0]["id"]
        before = (await api.get("/__test_metrics")).json()
        limits = httpx.Limits(
            max_connections=users + 10, max_keepalive_connections=users + 10
        )
        async with httpx.AsyncClient(
            base_url=base, timeout=None, limits=limits
        ) as streaming:

            async def subscriber(index):
                held = False
                try:
                    await semaphore.acquire()
                    held = True
                    async with streaming.stream("GET", "/api/stream") as response:
                        assert response.status_code == 200
                        assert "content-encoding" not in response.headers
                        event, data, previous = "", "", None
                        async for line in response.aiter_lines():
                            if line.startswith("event: "):
                                event = line[7:]
                            elif line.startswith("data: "):
                                data = line[6:]
                            elif not line:
                                if event == "events":
                                    if previous is None:
                                        state["connected"] += 1
                                        semaphore.release()
                                        held = False
                                        if state["connected"] == users:
                                            ready.set()
                                    elif data != previous:
                                        state["updated"] += 1
                                        if state["updated"] == users:
                                            updated.set()
                                    previous = data
                                elif event == "heartbeat":
                                    state["heartbeats"].add(index)
                                event, data = "", ""
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, AssertionError, OSError) as error:
                    errors.append(f"{index}: {type(error).__name__}: {error}")
                finally:
                    if held:
                        semaphore.release()

            tasks = [asyncio.create_task(subscriber(i)) for i in range(users)]
            try:
                await asyncio.wait_for(ready.wait(), 60)
                idle_before = (await api.get("/__test_metrics")).json()
                await asyncio.sleep(12)
                idle_after = (await api.get("/__test_metrics")).json()
                idle_checks = (
                    idle_after["revision_checks"] - idle_before["revision_checks"]
                )
                assert 4 <= idle_checks <= 8, idle_checks
                assert len(state["heartbeats"]) == users, len(state["heartbeats"])
                assert idle_after["feed_builds"] == before["feed_builds"]

                async def burst(conditional):
                    gates = asyncio.Semaphore(50)
                    elapsed, transferred = [], []

                    async def one(_):
                        async with gates:
                            headers = {"Accept-Encoding": "gzip"}
                            if conditional:
                                headers["If-None-Match"] = etag
                            start = time.perf_counter()
                            async with api.stream(
                                "GET", "/api/events", headers=headers
                            ) as response:
                                assert response.status_code == (
                                    304 if conditional else 200
                                )
                                size = sum(
                                    [len(chunk) async for chunk in response.aiter_raw()]
                                )
                                transferred.append(size)
                            elapsed.append((time.perf_counter() - start) * 1000)

                    start = time.perf_counter()
                    await asyncio.gather(*(one(i) for i in range(users)))
                    elapsed.sort()
                    return {
                        "requests": users,
                        "seconds": round(time.perf_counter() - start, 3),
                        "p50_ms": round(elapsed[len(elapsed) // 2], 2),
                        "p95_ms": round(
                            elapsed[min(len(elapsed) - 1, int(len(elapsed) * 0.95))], 2
                        ),
                        "wire_body_bytes": sum(transferred),
                    }

                conditional = await burst(True)
                compressed = await burst(False)
                prior = (await api.get("/__test_metrics")).json()
                assert prior["feed_builds"] == before["feed_builds"]
                start = time.perf_counter()
                with sqlite3.connect(database) as conn:
                    conn.execute(
                        "UPDATE events SET title=title || ' · LOAD TEST' WHERE id=?",
                        (event_id,),
                    )
                await asyncio.wait_for(updated.wait(), 15)
                notification_ms = round((time.perf_counter() - start) * 1000, 2)
                changed = await api.get("/api/events", headers={"If-None-Match": etag})
                assert changed.status_code == 200
                assert len(changed.json()) == initial_count
                assert changed.json()[0]["title"].endswith(" · LOAD TEST")
                detail = await api.get("/api/events/" + event_id)
                assert detail.status_code == 200 and detail.json()["feed"]
                after = (await api.get("/__test_metrics")).json()
                assert after["feed_builds"] == before["feed_builds"] + 1
                assert not errors, errors[:5]
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        # A fresh stream gets the latest revision after previous subscribers close.
        async with api.stream("GET", "/api/stream") as reconnect:
            assert reconnect.status_code == 200
            async for line in reconnect.aiter_lines():
                if line.startswith("data: "):
                    break
        return {
            "users": users,
            "incidents": initial_count,
            "connected": state["connected"],
            "heartbeats_received": len(state["heartbeats"]),
            "idle_revision_checks_in_12s": idle_checks,
            "conditional_requests": conditional,
            "compressed_requests": compressed,
            "update_delivered_to_all_ms": notification_ms,
            "updates_received": state["updated"],
            "feed_rebuilds_for_one_write": after["feed_builds"] - before["feed_builds"],
            "json_bytes": len(initial.content),
            "gzip_bytes": int(initial.headers["content-length"]),
            "server_peak_rss_kib": after["peak_rss_kib"],
            "errors": errors,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--port", type=int, default=8022)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
        return
    if args.database is None or not args.database.is_file():
        parser.error("--database must point to an existing populated MapExp database")
    if args.users < 1:
        parser.error("--users must be positive")
    with tempfile.TemporaryDirectory(prefix="mapexp-load-") as folder:
        database = Path(folder) / "load.sqlite3"
        source_uri = args.database.resolve().as_uri() + "?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True) as source,
            sqlite3.connect(database) as target,
        ):
            source.backup(target)
        env = {
            **os.environ,
            "DATABASE_PATH": str(database),
            "MEDIA_DIR": str(Path(folder) / "media"),
        }
        with open(Path(folder) / "server.log", "w+") as log:
            process = subprocess.Popen(
                [sys.executable, __file__, "--serve", "--port", str(args.port)],
                env=env,
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                result = asyncio.run(
                    exercise(args.port, args.users, database, process.pid)
                )
                print(json.dumps(result, indent=2))
            except BaseException:
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
