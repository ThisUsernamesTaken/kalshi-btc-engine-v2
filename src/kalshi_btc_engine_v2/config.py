from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "default.json"


@dataclass(frozen=True, slots=True)
class KalshiConfig:
    series_ticker: str
    rest_base_url: str
    ws_url: str
    key_id: str | None
    private_key_path: Path | None


@dataclass(frozen=True, slots=True)
class SpotConfig:
    max_quote_age_ms: int
    min_venues: int
    venues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    live_enabled: bool
    data_dir: Path
    kalshi: KalshiConfig
    spot: SpotConfig


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    if not value:
        return None
    return Path(value).expanduser()


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    environment = os.getenv("ENGINE_V2_ENV", raw["environment"]).strip().lower()
    live_enabled = _env_bool("ENGINE_V2_LIVE", bool(raw["live_enabled"]))
    data_dir = Path(os.getenv("ENGINE_V2_DATA_DIR", raw["data_dir"])).expanduser()

    kalshi_raw = raw["kalshi"]
    rest_key = "rest_base_url_demo" if environment == "demo" else "rest_base_url_prod"
    ws_key = "ws_url_demo" if environment == "demo" else "ws_url_prod"
    kalshi = KalshiConfig(
        series_ticker=os.getenv("ENGINE_V2_KALSHI_SERIES", kalshi_raw["series_ticker"]),
        rest_base_url=kalshi_raw[rest_key],
        ws_url=kalshi_raw[ws_key],
        key_id=os.getenv("ENGINE_V2_KALSHI_KEY_ID") or None,
        private_key_path=_env_path("ENGINE_V2_KALSHI_PRIVATE_KEY_PATH"),
    )

    spot_raw = raw["spot"]
    spot = SpotConfig(
        max_quote_age_ms=int(spot_raw["max_quote_age_ms"]),
        min_venues=int(spot_raw["min_venues"]),
        venues=tuple(spot_raw["venues"]),
    )

    return Settings(
        environment=environment,
        live_enabled=live_enabled,
        data_dir=data_dir,
        kalshi=kalshi,
        spot=spot,
    )
