"""Validated process settings; secrets stay out of public configuration and repr."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not key.strip().isidentifier():
            raise ValueError(f"Invalid variable name in {path.name}")
        tokens = shlex.split(value, comments=True)
        os.environ.setdefault(key.strip(), " ".join(tokens))


load_env()


@dataclass(frozen=True)
class Settings:
    city_name: str = field(default_factory=lambda: os.getenv("CITY_NAME", "Пермь"))
    timezone: str = field(
        default_factory=lambda: os.getenv("CITY_TIMEZONE", "Europe/Moscow")
    )
    latitude: float = field(
        default_factory=lambda: float(os.getenv("CITY_LAT", "58.0105"))
    )
    longitude: float = field(
        default_factory=lambda: float(os.getenv("CITY_LON", "56.2502"))
    )
    zoom: int = field(default_factory=lambda: int(os.getenv("CITY_ZOOM", "11")))
    database: Path = field(
        default_factory=lambda: ROOT / os.getenv("DATABASE_PATH", "data/mapexp.sqlite3")
    )
    media: Path = field(default_factory=lambda: ROOT / os.getenv("MEDIA_DIR", "media"))
    session: Path = field(
        default_factory=lambda: ROOT / os.getenv("TG_SESSION", "sessions/mapexp")
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""), repr=False
    )
    text_model: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_MODEL", "openai/gpt-5.6-luna-pro")
    )
    decision_model: str = field(
        default_factory=lambda: os.getenv("JEV_MODEL", "typesafe/jev-1.13")
    )
    geocoder_agent: str = field(
        default_factory=lambda: os.getenv("GEOCODER_USER_AGENT", "MapExp/1.0")
    )

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if not self.city_name.strip() or not (
            -90 <= self.latitude <= 90 and -180 <= self.longitude <= 180
        ):
            raise ValueError("Invalid city settings")
        if not 1 <= self.zoom <= 19:
            raise ValueError("CITY_ZOOM must be between 1 and 19")


settings = Settings()
