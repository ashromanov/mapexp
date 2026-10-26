"""Recheck only geographic decisions, preserving event prose, evidence and expiry."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import geocode
import llm
import storage


def remap_event(
    event_id: str, path: Path, *, apply: bool = False, extract_missing: bool = False
) -> dict:
    with storage.connect(path) as conn:
        event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        decision_row = conn.execute(
            "SELECT * FROM decisions WHERE event_id=? ORDER BY id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if not event or not decision_row:
            return {"event_id": event_id, "status": "no_audit"}
        event = dict(event)
        evidence_rows = conn.execute(
            "SELECT ep.accepted_text,p.posted_at,s.name FROM event_posts ep JOIN posts p ON p.id=ep.post_id JOIN sources s ON s.id=p.source_id WHERE ep.event_id=? AND ep.role<>'comment' ORDER BY p.posted_at",
            (event_id,),
        ).fetchall()
        texts = [row[0] for row in evidence_rows]
    decision = json.loads(decision_row["result_json"])
    audit = decision.setdefault("audit", {})
    previous_policy = audit.get("location_resolution", {}).get("policy")
    query = decision.get("location_query") or event.get("location_query")
    evidence = audit.get("location_evidence")
    kind = decision.get("location_kind", "unknown")
    if extract_missing and (not query or not evidence) and evidence_rows:
        generated, generation_audit = llm.draft(
            {
                "source_name": evidence_rows[-1]["name"],
                "posted_at": evidence_rows[-1]["posted_at"],
                "text": "\n\n".join(texts),
            },
            None,
            {"kind": event["kind"], "severity": event["severity"]},
        )
        query, evidence, kind = (
            generated.location_query,
            generated.location_evidence,
            generated.location_kind,
        )
        audit.setdefault("calls", []).append(generation_audit)
        audit["location_evidence"] = evidence
        decision.update(location_query=query, location_kind=kind)
    normalize = lambda value: " ".join(
        (value or "").casefold().replace("ё", "е").split()
    )
    if (
        not query
        or kind in {"unknown", "city"}
        or not evidence
        or len(normalize(evidence)) < 3
        or not any(normalize(evidence) in normalize(text) for text in texts)
    ):
        return {"event_id": event_id, "status": "no_place_evidence"}
    locations = audit.get("location_candidates") or []
    # Old audits are a usable cache. Fetch again only for missing/partial matches.
    sufficient = (
        any(geocode.address_matches(query, item) for item in locations)
        if geocode.house_number(query)
        else any(geocode.place_match_score(query, item) >= 0.6 for item in locations)
    )
    answers = next(
        (
            call["answers"]
            for call in reversed(audit.get("calls", []))
            if isinstance(call.get("answers"), dict)
            and "location_grounded" in call["answers"]
        ),
        {},
    )
    if not sufficient or not answers:
        locations = [
            item
            for item in geocode.geocode(query, path)
            if item.get("precision") not in {"city", "unknown"}
        ]
        if locations:
            new_calls = []
            draft = {
                "location_query": query,
                "location_kind": kind,
                "location_evidence": evidence,
                "title": event["title"],
                "description": event["description"],
            }
            answers = llm.jev(
                {
                    "city": geocode.CITY_NAME,
                    "source_text": "\n\n".join(texts),
                    "draft": draft,
                    "locations": locations,
                },
                llm.location_questions(locations),
                new_calls,
            )
            audit.setdefault("calls", []).extend(new_calls)
    resolution = {}
    index, confidence = llm.select_location(
        answers, locations, query, evidence, resolution
    )
    result = {"event_id": event_id, "status": "unmapped", "resolution": resolution}
    if index is None:
        return result
    model = llm.EventDecision.model_validate(decision).model_copy(
        update={
            "action": "append",
            "event_id": event_id,
            "location_query": query,
            "location_label": event["location_label"],
            "reset_location": False,
        }
    )
    model = llm.apply_geocoded_location(model, locations, index, confidence)
    if model.latitude is None:
        result["resolution"]["reason"] = (
            "Область неопределённости превышает поддерживаемый размер"
        )
        return result
    audit.update(
        location_candidates=locations,
        location_resolution=resolution,
        remapped_from_decision=decision_row["id"],
    )
    model = model.model_copy(update={"audit": audit})
    result.update(
        status="ready",
        precision=model.location_precision,
        latitude=model.latitude,
        longitude=model.longitude,
        radius_m=model.radius_m,
    )
    mapped_fields = {
        "latitude": model.latitude,
        "longitude": model.longitude,
        "location_precision": model.location_precision,
        "radius_m": model.radius_m,
        "location_label": model.location_label,
    }
    if previous_policy == "recall-v1" and all(
        event[key] == value for key, value in mapped_fields.items()
    ):
        result["status"] = "unchanged"
        return result
    if apply:
        with storage.connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE events SET location_label=?,location_precision=?,latitude=?,longitude=?,radius_m=?,location_provider=?,location_confidence=?,location_query=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (
                    model.location_label,
                    model.location_precision,
                    model.latitude,
                    model.longitude,
                    model.radius_m,
                    model.location_source,
                    model.location_confidence,
                    query,
                    storage.now(),
                    event_id,
                    event["revision"],
                ),
            )
            if not cursor.rowcount:
                result["status"] = "changed_during_remap"
                return result
            conn.execute(
                "INSERT INTO decisions(post_id,event_id,action,result_json,created_at) VALUES (?,?,'remap',?,?)",
                (
                    decision_row["post_id"],
                    event_id,
                    json.dumps(model.model_dump(), ensure_ascii=False),
                    storage.now(),
                ),
            )
        result["status"] = "mapped"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--event", help="Recheck one event ID")
    parser.add_argument(
        "--extract-missing",
        action="store_true",
        help="Retry extraction for events without a place clue, using accepted source text",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Save geographic changes after an online backup",
    )
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("Database does not exist")
    if args.apply:
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = args.database.with_name(
            args.database.stem + f".before-remap-{suffix}.sqlite3"
        )
        with (
            sqlite3.connect(args.database) as source,
            sqlite3.connect(backup) as target,
        ):
            source.backup(target)
        print(json.dumps({"backup": str(backup)}), flush=True)
    with storage.connect(args.database) as conn:
        ids = (
            [args.event]
            if args.event
            else [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM events WHERE latitude IS NULL AND location_precision<>'city' ORDER BY last_confirmed_at DESC"
                    if args.extract_missing
                    else "SELECT id FROM events WHERE latitude IS NULL AND location_query IS NOT NULL ORDER BY last_confirmed_at DESC"
                )
            ]
        )
    counts = {}
    for event_id in ids:
        try:
            result = remap_event(
                event_id,
                args.database,
                apply=args.apply,
                extract_missing=args.extract_missing,
            )
        except llm.LLMBlocked:
            raise
        except llm.ProviderFailure as error:
            result = {
                "event_id": event_id,
                "status": "deferred",
                "error": type(error).__name__,
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    print(json.dumps({"summary": counts}), flush=True)


if __name__ == "__main__":
    main()
