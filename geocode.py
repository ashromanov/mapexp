"""Scoped geocoder candidates. Models never supply coordinates or precision."""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from threading import Lock

import httpx

from settings import settings
from storage import connect, haversine_m, now

CITY_NAME = settings.city_name
CITY_LAT = settings.latitude
CITY_LON = settings.longitude
SCOPE_RADIUS_M = 90000
_nominatim_lock = Lock()
_last_nominatim_request = 0.0


def normalize_location(query: str) -> str:
    query = unicodedata.normalize("NFKC", query).replace("ё", "е").replace("Ё", "Е")
    query = re.sub(r'[«»“”"]', "", query)
    for pattern, replacement in [
        (r"(?i)\b(?:трк|трц)\b", "торгово-развлекательный центр"),
        (r"(?i)\bтц\b", "торговый центр"),
        (r"(?i)\bжк\b", "жилой комплекс"),
        (r"(?i)\bбц\b", "бизнес-центр"),
        (r"(?i)\bул(?:\.|\s+)", "улица "),
        (r"(?i)\bпр-?т(?:\.|\s+)", "проспект "),
        (r"(?i)\bш(?:\.|\s+)", "шоссе "),
        (r"(?i)\bпер(?:\.|\s+)", "переулок "),
    ]:
        query = re.sub(pattern, replacement, query)
    query = " ".join(re.sub(r"\s*[,;]\s*", ", ", query).split()).strip(" ,.")[0:240]
    if query and CITY_NAME.casefold() not in query.casefold():
        query = f"{query}, {CITY_NAME}"
    return query


_HOUSE = re.compile(
    r"(?<![\w])\d+[а-яa-z]?(?:\s*/\s*\d+[а-яa-z]?)?"
    r"(?:\s*(?:корпус|корп\.?|к\.?|строение|стр\.?)\s*\d+[а-яa-z]?)?(?![\w])",
    re.IGNORECASE,
)
_STREET_TYPES = {
    "улица",
    "шоссе",
    "проспект",
    "переулок",
    "бульвар",
    "проезд",
    "набережная",
}
_STOP_WORDS = _STREET_TYPES | {
    "ул",
    "ш",
    "пр",
    "просп",
    "пер",
    "г",
    "город",
    "дом",
    "д",
    "около",
    "возле",
    "рядом",
    "остановка",
    "остановке",
    "район",
    "районе",
    "на",
    "в",
    "у",
    "с",
    "со",
    "и",
    "улиц",
    "улице",
    "улицы",
    "районы",
    "районах",
    "районов",
    "перекресток",
    "перекрестке",
    "пересечение",
    "пересечении",
}


def place_tokens(value: str) -> set[str]:
    value = _HOUSE.sub(lambda match: normalize_house(match.group()), value)
    city = set(re.findall(r"[а-яa-z0-9]+", CITY_NAME.casefold().replace("ё", "е")))
    if CITY_NAME.casefold().endswith("ь"):
        base = CITY_NAME.casefold()[:-1]
        city.update({base + "и", base + "ью"})
    return (
        set(re.findall(r"[а-яa-z0-9]+", value.casefold().replace("ё", "е")))
        - _STOP_WORDS
        - city
    )


def house_number(value: str) -> str | None:
    matches = list(_HOUSE.finditer(value))
    return normalize_house(matches[-1].group()) if matches else None


def normalize_house(value: str) -> str:
    value = value.casefold().translate(str.maketrans("abcehkmoptxy", "авсенкмортху"))
    value = re.sub(r"корпус|корп\.?|к\.?", "к", value)
    value = re.sub(r"строение|стр\.?", "стр", value)
    return re.sub(r"\s+", "", value)


def address_matches(query: str, candidate: dict) -> bool:
    """Compare complete house identifiers, street and city, not substring numbers."""
    house = house_number(query)
    if not house or candidate.get("precision") != "address":
        return False
    address = candidate.get("address") or {}
    label = candidate.get("label", "")
    city = address.get("city")
    if city and place_tokens(city) != place_tokens(CITY_NAME):
        return False
    if not city and CITY_NAME.casefold().replace(
        "ё", "е"
    ) not in label.casefold().replace("ё", "е"):
        return False
    actual_house = address.get("house_number")
    if actual_house:
        if normalize_house(actual_house) != house:
            return False
    elif house not in {
        normalize_house(match.group()) for match in _HOUSE.finditer(label)
    }:
        return False
    expected_street = place_tokens(_HOUSE.sub(" ", query))
    street = address.get("street") or label
    types = _STREET_TYPES & set(query.casefold().split())
    actual_types = _STREET_TYPES & set(street.casefold().split())
    return (
        bool(expected_street)
        and expected_street <= place_tokens(street)
        and (not types or not actual_types or types == actual_types)
    )


def place_match_score(query: str, candidate: dict) -> float:
    if address_matches(query, candidate):
        return 1.0
    tokens = place_tokens(_HOUSE.sub(" ", query))
    if not tokens:
        return 0.0
    matched = len(tokens & place_tokens(candidate.get("label", ""))) / len(tokens)
    return matched * (0.8 if house_number(query) else 1.0)


def _sufficient_match(query: str, results: list[dict]) -> bool:
    if house_number(query):
        return any(address_matches(query, item) for item in results)
    return any(
        place_match_score(query, item) >= 0.8
        for item in results
        if item.get("precision") not in {"city", "unknown"}
    )


def _query_variants(query: str) -> tuple[str, ...]:
    plain = re.sub(
        rf"(?:,\s*)?{re.escape(CITY_NAME)}$", "", query, flags=re.IGNORECASE
    ).strip(" ,")
    return tuple(dict.fromkeys(x for x in [query, plain] if x))


def _request_json(url: str, params: dict) -> dict | list | None:
    try:
        response = httpx.get(
            url,
            params=params,
            headers={"User-Agent": settings.geocoder_agent},
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, (dict, list)) else None
    except (httpx.HTTPError, ValueError):
        return None


def _in_scope(latitude: float, longitude: float) -> bool:
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
        and haversine_m(CITY_LAT, CITY_LON, latitude, longitude) <= SCOPE_RADIUS_M
    )


def _precision(
    category: str | None, item_type: str | None, has_house_number: bool = False
) -> str:
    category, item_type = (category or "").lower(), (item_type or "").lower()
    if has_house_number:
        return "address"
    if item_type in {"cemetery", "grave_yard"}:
        return "poi"
    if category == "waterway" or item_type in {
        "water",
        "wood",
        "forest",
        "river",
        "stream",
        "reservoir",
    }:
        return "area"
    if category in {"highway", "street"} or item_type in {
        "street",
        "road",
        "residential",
        "tertiary",
        "primary",
    }:
        return "street"
    if item_type in {"city", "town", "village", "municipality"}:
        return "city"
    if item_type in {"district", "suburb", "neighbourhood", "locality", "hamlet"}:
        return "district"
    if category in {
        "amenity",
        "shop",
        "tourism",
        "historic",
        "leisure",
        "building",
        "railway",
        "office",
    }:
        return "poi"
    return "unknown"


def _candidate(
    label, latitude, longitude, precision, source, **metadata
) -> dict | None:
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (ValueError, TypeError):
        return None
    if not _in_scope(latitude, longitude):
        return None
    bounds = metadata.pop("bounds", None)
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        try:
            west, north, east, south = map(float, bounds)
            if (
                all(math.isfinite(v) for v in (west, north, east, south))
                and -180 <= west <= east <= 180
                and -90 <= south <= north <= 90
            ):
                metadata["radius_m"] = math.ceil(
                    max(
                        haversine_m(latitude, longitude, y, x)
                        for y in (south, north)
                        for x in (west, east)
                    )
                )
                if metadata["radius_m"] > 50000:
                    return None
        except (ValueError, TypeError):
            pass
    return {
        "label": str(label)[:400],
        "latitude": latitude,
        "longitude": longitude,
        "precision": precision,
        "source": source,
        **metadata,
    }


def _photon(query: str) -> list[dict]:
    raw = _request_json(
        "https://photon.komoot.io/api/",
        {"q": query, "limit": 5, "lat": CITY_LAT, "lon": CITY_LON},
    )
    if not isinstance(raw, dict):
        return []
    results = []
    for feature in raw.get("features", []):
        if not isinstance(feature, dict):
            continue
        p = feature.get("properties") or {}
        xy = (feature.get("geometry") or {}).get("coordinates", [])
        if len(xy) < 2:
            continue
        label = ", ".join(
            str(p[k]) for k in ["name", "housenumber", "street", "city"] if p.get(k)
        )
        item = _candidate(
            label or query,
            xy[1],
            xy[0],
            _precision(
                p.get("osm_key"), p.get("osm_value"), bool(p.get("housenumber"))
            ),
            "Photon/OpenStreetMap",
            bounds=p.get("extent"),
            feature_type=f"{p.get('osm_key', '')}/{p.get('osm_value', '')}",
            address={
                "house_number": p.get("housenumber"),
                "street": p.get("street"),
                "city": p.get("city"),
            },
        )
        if item:
            results.append(item)
    return results


def _nominatim(query: str) -> list[dict]:
    global _last_nominatim_request
    # shortcut: one shared public-endpoint rate limit; use a private geocoder before adding ingestion workers.
    if (
        "replace" in settings.geocoder_agent.lower()
        or settings.geocoder_agent == "MapExp/1.0"
    ):
        return []
    dy = SCOPE_RADIUS_M / 111000
    dx = dy / math.cos(math.radians(CITY_LAT))
    with _nominatim_lock:
        delay = 15.1 - (time.monotonic() - _last_nominatim_request)
        if delay > 0:
            time.sleep(delay)
        raw = _request_json(
            "https://nominatim.openstreetmap.org/search",
            {
                "q": query,
                "format": "jsonv2",
                "limit": 5,
                "addressdetails": 1,
                "countrycodes": "ru",
                "viewbox": f"{CITY_LON - dx},{CITY_LAT + dy},{CITY_LON + dx},{CITY_LAT - dy}",
                "bounded": 1,
            },
        )
        _last_nominatim_request = time.monotonic()
    if not isinstance(raw, list):
        return []
    results = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        address = p.get("address") or {}
        item = _candidate(
            p.get("display_name", query),
            p.get("lat"),
            p.get("lon"),
            _precision(
                p.get("category", p.get("class")),
                p.get("type"),
                bool(address.get("house_number")),
            ),
            "Nominatim/OpenStreetMap",
            address={
                "house_number": address.get("house_number"),
                "street": address.get("road") or address.get("pedestrian"),
                "city": address.get("city")
                or address.get("town")
                or address.get("village"),
            },
            feature_type=f"{p.get('category', p.get('class', ''))}/{p.get('type', '')}",
            bounds=[
                p["boundingbox"][2],
                p["boundingbox"][1],
                p["boundingbox"][3],
                p["boundingbox"][0],
            ]
            if isinstance(p.get("boundingbox"), list) and len(p["boundingbox"]) == 4
            else None,
        )
        if item:
            results.append(item)
    return results


def _open_meteo(query: str) -> list[dict]:
    raw = _request_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {
            "name": query.removesuffix(f", {CITY_NAME}"),
            "count": 5,
            "language": "ru",
            "format": "json",
        },
    )
    if not isinstance(raw, dict):
        return []
    results = []
    for p in raw.get("results", []):
        if not isinstance(p, dict):
            continue
        item = _candidate(
            ", ".join(str(p[k]) for k in ["name", "admin1", "country"] if p.get(k)),
            p.get("latitude"),
            p.get("longitude"),
            "city",
            "Open-Meteo/GeoNames",
        )
        if item:
            results.append(item)
    return results


def _dedupe(candidates: list[dict]) -> list[dict]:
    unique = {}
    ranks = {
        "unknown": 0,
        "city": 1,
        "district": 2,
        "area": 2,
        "street": 3,
        "poi": 4,
        "address": 5,
    }
    for candidate in candidates:
        key = (
            candidate["label"].casefold(),
            round(candidate["latitude"], 5),
            round(candidate["longitude"], 5),
        )
        if key not in unique or ranks.get(candidate["precision"], 0) > ranks.get(
            unique[key]["precision"], 0
        ):
            unique[key] = candidate
    grouped: list[dict] = []
    for candidate in unique.values():
        same = next(
            (
                item
                for item in grouped
                if candidate["precision"] in {"street", "district", "area"}
                and item["precision"] == candidate["precision"]
                and item["label"].casefold() == candidate["label"].casefold()
                and (
                    candidate["precision"] != "area"
                    or item.get("feature_type") == candidate.get("feature_type")
                )
                and haversine_m(
                    item["latitude"],
                    item["longitude"],
                    candidate["latitude"],
                    candidate["longitude"],
                )
                <= 15000
            ),
            None,
        )
        if same is None:
            grouped.append(dict(candidate))
        else:
            same["radius_m"] = max(
                same.get("radius_m", 0),
                math.ceil(
                    haversine_m(
                        same["latitude"],
                        same["longitude"],
                        candidate["latitude"],
                        candidate["longitude"],
                    )
                    + candidate.get("radius_m", 0)
                ),
            )
    return [item for item in grouped if item.get("radius_m", 0) <= 50000][:8]


def geocode(query: str, path=None) -> list[dict]:
    query = normalize_location(query)
    if not query:
        return []
    path = path or settings.database
    key = f"v8:{CITY_LAT}:{CITY_LON}:{query}"
    with connect(path) as conn:
        cached = conn.execute(
            "SELECT result_json,created_at FROM geocode_cache WHERE query=?", (key,)
        ).fetchone()
    if cached:
        try:
            items = json.loads(cached["result_json"])
            age = datetime.now(UTC) - datetime.fromisoformat(cached["created_at"])
            lifetime = timedelta(days=7) if items else timedelta(minutes=2)
            if age < lifetime:
                return items
        except (ValueError, TypeError):
            pass
    results = []
    for provider in [_photon, _nominatim, _open_meteo]:
        for variant in _query_variants(query):
            results.extend(provider(variant))
            if _sufficient_match(query, results):
                break
        if _sufficient_match(query, results):
            break
    # Sort before limiting so a good fallback cannot be displaced by weak first-provider results.
    results.sort(key=lambda item: place_match_score(query, item), reverse=True)
    results = _dedupe(results)
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocode_cache(query,result_json,created_at) VALUES (?,?,?)",
            (key, json.dumps(results, ensure_ascii=False), now()),
        )
    return results
