"""Read-only HTTP/SSE check for a newly built or deployed service."""

import argparse
import gzip
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("url")
parser.add_argument("--expected-revision")
args = parser.parse_args()
base = args.url.rstrip("/")
with urlopen(base + "/healthz", timeout=15) as response:
    health = json.load(response)
assert health["status"] == "ok"
if args.expected_revision:
    assert health["revision"] == args.expected_revision, health
    with urlopen(base, timeout=15) as response:
        assert ("?v=" + args.expected_revision).encode() in response.read()
with urlopen(
    Request(base + "/api/events", headers={"Accept-Encoding": "gzip"}), timeout=15
) as response:
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Cache-Control"] == "private, no-cache"
    etag = response.headers["ETag"]
    events = json.loads(gzip.decompress(response.read()))
assert isinstance(events, list)
for attempt in range(5):
    try:
        with urlopen(
            Request(
                base + "/api/events",
                headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
            ),
            timeout=15,
        ) as response:
            newer = response.headers["ETag"]
            response.read()
            assert newer != etag, (
                "Server ignored If-None-Match for an unchanged representation"
            )
            etag = newer
    except HTTPError as response:
        assert response.code == 304
        assert response.read() == b""
        break
else:
    raise AssertionError("Feed changed during every conditional check")
with urlopen(base + "/api/stream", timeout=15) as response:
    assert not response.headers.get("Content-Encoding")
    assert response.readline().decode().strip() == "event: events"
    assert response.readline().decode().startswith("data: ")
if events:
    with urlopen(base + "/api/events/" + events[0]["id"], timeout=15) as response:
        detail = json.load(response)
    assert detail["id"] == events[0]["id"] and isinstance(detail["feed"], list)
print(
    json.dumps(
        {
            "status": "ok",
            "events": len(events),
            "revision": health.get("revision"),
            "gzip": True,
            "conditional_get": True,
            "sse": True,
        }
    )
)
