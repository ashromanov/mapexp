"""Generate offline incidents for the CI load exercise; never calls providers."""

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--count", type=int, default=1000)
args = parser.parse_args()
os.environ["DATABASE_PATH"] = str(args.database.resolve())

import storage

storage.init_db()
source = storage.ensure_source(
    {
        "key": "ci-demo",
        "name": "CI synthetic data",
        "username": "synthetic_demo",
        "timezone": "Asia/Yekaterinburg",
    }
)
for index in range(args.count):
    post, _ = storage.save_post(
        {
            "source_id": source,
            "chat_id": 1,
            "message_id": index + 1,
            "posted_at": datetime.now(UTC).isoformat(),
            "text": f"Synthetic incident {index + 1}",
            "link": None,
            "raw_json": {},
        }
    )
    located = index % 5 != 0
    storage.apply_decision(
        post,
        {
            "action": "create",
            "kind": "traffic",
            "severity": "info",
            "title": f"CI incident {index + 1}",
            "description": "Synthetic regression fixture.",
            "location_precision": "district" if located else "unknown",
            "latitude": 58.0 + (index % 20) * 0.003 if located else None,
            "longitude": 56.2 + (index % 25) * 0.003 if located else None,
            "radius_m": 1200 if located else None,
            "display_minutes": 10080,
        },
    )
print("Seeded", args.count, "synthetic incidents")
