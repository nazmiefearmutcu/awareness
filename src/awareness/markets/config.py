"""Settings for the markets layer (``AW_MARKETS_*`` environment variables)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "BRK-B",
    "SPY", "QQQ", "BTC", "ETH", "SOL",
]
DEFAULT_HOLIDAY_COUNTRIES = [
    "US", "TR", "GB", "DE", "FR", "IT", "ES", "NL", "CH", "JP", "CN", "HK", "KR", "IN",
    "AU", "NZ", "CA", "BR", "MX", "ZA",
]
DEFAULT_EXCHANGES = [
    "XNYS", "XNAS", "XIST", "XLON", "XETR", "XPAR", "XTKS", "XHKG", "XSHG", "XTSE",
    "XASX", "XSWX",
]

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
BOT_UA = "AwarenessMarkets/0.3 (+https://github.com/nazmiefearmutcu/awareness; market research)"
SEC_UA = "showme-awareness/0.3 (local market research terminal)"


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class MarketsSettings(BaseSettings):
    """Runtime configuration. Every field maps to ``AW_MARKETS_<NAME>``."""

    model_config = SettingsConfigDict(env_prefix="AW_MARKETS_", extra="ignore")

    data_dir: Path | None = None
    config_dir: Path = PACKAGE_DATA_DIR
    host: str = "127.0.0.1"
    port: int = 8086
    autostart_runner: bool = True
    sentiment_backend: Literal["auto", "finbert", "lexicon"] = "auto"
    finbert_model: str = "ProsusAI/finbert"
    fulltext: bool = True
    fulltext_concurrency: int = 4
    max_item_age_days: int = 7
    news_retention_days: int = 120
    cluster_window_hours: int = 36
    watchlist: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    holiday_countries: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_HOLIDAY_COUNTRIES)
    )
    exchanges: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_EXCHANGES))
    econ_days_back: int = 7
    econ_days_ahead: int = 21
    corp_days_back: int = 30
    corp_days_ahead: int = 21
    http_timeout_s: float = 20.0
    browser_user_agent: str = BROWSER_UA
    bot_user_agent: str = BOT_UA
    sec_user_agent: str = SEC_UA
    finnhub_api_key: str | None = None
    api_key: str | None = None

    _split_lists = field_validator("watchlist", "holiday_countries", "exchanges", mode="before")(
        _split_csv
    )

    @field_validator("watchlist", "holiday_countries", "exchanges")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            norm = item.strip().upper()
            if norm and norm not in seen:
                seen.append(norm)
        return seen

    @property
    def resolved_data_dir(self) -> Path:
        """``AW_MARKETS_DATA_DIR`` or ``$AW_DATA_DIR/markets`` or ``<repo>/data/markets``."""
        if self.data_dir is not None:
            return Path(self.data_dir)
        base = os.environ.get("AW_DATA_DIR")
        root = Path(base) if base else REPO_ROOT / "data"
        return root / "markets"

    @property
    def store_path(self) -> Path:
        return self.resolved_data_dir / "markets.sqlite"

    @property
    def ref_dir(self) -> Path:
        return self.resolved_data_dir / "ref"

    def config_file(self, name: str) -> Path:
        """Resolve a static config file, preferring ``config_dir`` then package data."""
        candidate = Path(self.config_dir) / name
        if candidate.exists():
            return candidate
        return PACKAGE_DATA_DIR / name


@lru_cache(maxsize=1)
def get_markets_settings() -> MarketsSettings:
    return MarketsSettings()


def reset_markets_settings_cache() -> None:
    get_markets_settings.cache_clear()
