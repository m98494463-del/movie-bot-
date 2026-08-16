"""
Telegram Movie Recommendation Bot — Ultimate Edition
=====================================================

A production-ready single-file Telegram bot for movie discovery via TMDb.
Features: random picks, search (paginated), trending (movie/tv), top-rated,
now playing, upcoming, airing today, on the air, genres, collections,
TV shows, anime, favorites, watchlist, ratings, search history, inline mode,
AI recommendations, daily jobs, admin broadcast, person search, reviews,
watch providers, IMDb links, keywords, certifications, backdrops, share,
user stats, data export, advanced discover, and user settings.

--------------------------------------------------------------
SETUP
--------------------------------------------------------------
1) Install dependencies:
     pip install "python-telegram-bot[job-queue]==21.*" python-dotenv requests

2) Create a ".env" file next to this script with:
     BOT_TOKEN=your_telegram_bot_token
     TMDB_API_KEY=your_tmdb_api_key
     ADMIN_ID=123456789          # optional, your numeric Telegram ID
     GEMINI_API_KEY=your_key     # optional, for AI recommendations

3) Run:
     python movie_bot.py
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import random
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import requests
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# =========================================================================
# 1. CONFIG
# =========================================================================

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "movie_bot.db"
LOG_PATH = LOGS_DIR / "bot.log"

__version__ = "3.0.0"


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration, validated once at startup."""

    bot_token: str
    tmdb_api_key: str
    admin_id: Optional[int]
    gemini_api_key: Optional[str]

    @staticmethod
    def load() -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        tmdb_api_key = os.getenv("TMDB_API_KEY", "").strip()
        admin_id_raw = os.getenv("ADMIN_ID", "").strip()
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None

        missing = []
        if not bot_token:
            missing.append("BOT_TOKEN")
        if not tmdb_api_key:
            missing.append("TMDB_API_KEY")
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                f"Create a .env file (see the header of this script)."
            )

        admin_id: Optional[int] = None
        if admin_id_raw:
            try:
                admin_id = int(admin_id_raw)
            except ValueError:
                raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.")

        return Config(
            bot_token=bot_token,
            tmdb_api_key=tmdb_api_key,
            admin_id=admin_id,
            gemini_api_key=gemini_api_key,
        )


# =========================================================================
# 2. LOGGING
# =========================================================================

def setup_logging() -> logging.Logger:
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("movie_bot")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


log = setup_logging()


# =========================================================================
# 3. DATABASE
# =========================================================================

class Database:
    """Repository over SQLite for users, favorites, watchlist, ratings,
    search history, and user settings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username    TEXT,
                    joined_at   TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    movie_id    INTEGER NOT NULL,
                    title       TEXT NOT NULL,
                    added_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, movie_id),
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    movie_id    INTEGER NOT NULL,
                    title       TEXT NOT NULL,
                    added_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, movie_id),
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ratings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    movie_id    INTEGER NOT NULL,
                    rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
                    rated_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, movie_id),
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    query       TEXT NOT NULL,
                    searched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    telegram_id          INTEGER PRIMARY KEY,
                    language             TEXT DEFAULT 'fa',
                    daily_recommendation INTEGER DEFAULT 0,
                    adult_filter         INTEGER DEFAULT 1,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                )
                """
            )
            # user_settings already exists on the live deployment without
            # this column — CREATE TABLE IF NOT EXISTS above won't add it
            # to an existing table, so migrate it in explicitly.
            try:
                conn.execute(
                    "ALTER TABLE user_settings ADD COLUMN adult_filter INTEGER DEFAULT 1"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

    def upsert_user(self, telegram_id: int, username: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username)
                VALUES (?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
                """,
                (telegram_id, username or ""),
            )
            conn.execute(
                "INSERT OR IGNORE INTO user_settings (telegram_id) VALUES (?)",
                (telegram_id,),
            )

    # --- Favorites ---
    def add_favorite(self, telegram_id: int, movie_id: int, title: str) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO favorites (telegram_id, movie_id, title) VALUES (?, ?, ?)",
                    (telegram_id, movie_id, title),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_favorite(self, telegram_id: int, movie_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM favorites WHERE telegram_id = ? AND movie_id = ?",
                (telegram_id, movie_id),
            )
            return cur.rowcount > 0

    def is_favorite(self, telegram_id: int, movie_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM favorites WHERE telegram_id = ? AND movie_id = ?",
                (telegram_id, movie_id),
            ).fetchone()
            return row is not None

    def list_favorites(self, telegram_id: int, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT movie_id, title FROM favorites WHERE telegram_id = ? ORDER BY added_at DESC LIMIT ? OFFSET ?",
                (telegram_id, limit, offset),
            ).fetchall()

    def count_favorites(self, telegram_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM favorites WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return int(row["c"])

    # --- Watchlist ---
    def add_watchlist(self, telegram_id: int, movie_id: int, title: str) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO watchlist (telegram_id, movie_id, title) VALUES (?, ?, ?)",
                    (telegram_id, movie_id, title),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_watchlist(self, telegram_id: int, movie_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM watchlist WHERE telegram_id = ? AND movie_id = ?",
                (telegram_id, movie_id),
            )
            return cur.rowcount > 0

    def is_watchlist(self, telegram_id: int, movie_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM watchlist WHERE telegram_id = ? AND movie_id = ?",
                (telegram_id, movie_id),
            ).fetchone()
            return row is not None

    def list_watchlist(self, telegram_id: int, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT movie_id, title FROM watchlist WHERE telegram_id = ? ORDER BY added_at DESC LIMIT ? OFFSET ?",
                (telegram_id, limit, offset),
            ).fetchall()

    def count_watchlist(self, telegram_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM watchlist WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return int(row["c"])

    # --- Ratings ---
    def add_rating(self, telegram_id: int, movie_id: int, rating: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ratings (telegram_id, movie_id, rating)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id, movie_id) DO UPDATE SET rating = excluded.rating, rated_at = CURRENT_TIMESTAMP
                """,
                (telegram_id, movie_id, rating),
            )

    def get_rating(self, telegram_id: int, movie_id: int) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rating FROM ratings WHERE telegram_id = ? AND movie_id = ?",
                (telegram_id, movie_id),
            ).fetchone()
            return int(row["rating"]) if row else None

    def get_user_avg_rating(self, telegram_id: int) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT AVG(rating) AS avg FROM ratings WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return float(row["avg"]) if row and row["avg"] else None

    def get_user_rating_count(self, telegram_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM ratings WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return int(row["c"])

    # --- Search History ---
    def add_search_history(self, telegram_id: int, query: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO search_history (telegram_id, query) VALUES (?, ?)",
                (telegram_id, query),
            )
            conn.execute(
                """
                DELETE FROM search_history
                WHERE id NOT IN (
                    SELECT id FROM search_history WHERE telegram_id = ? ORDER BY searched_at DESC LIMIT 50
                ) AND telegram_id = ?
                """,
                (telegram_id, telegram_id),
            )

    def get_search_history(self, telegram_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT query, searched_at FROM search_history WHERE telegram_id = ? ORDER BY searched_at DESC LIMIT ?",
                (telegram_id, limit),
            ).fetchall()

    def clear_search_history(self, telegram_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM search_history WHERE telegram_id = ?", (telegram_id,))

    # --- User Settings ---
    def get_user_settings(self, telegram_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            # Guarantees a row exists even if this is reached without going
            # through upsert_user first (e.g. /settings as someone's very
            # first message) — previously this could return None and crash
            # every settings screen silently. users comes first since
            # user_settings has a FOREIGN KEY on it (enforcement is ON).
            conn.execute(
                "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,)
            )
            conn.execute(
                "INSERT OR IGNORE INTO user_settings (telegram_id) VALUES (?)",
                (telegram_id,),
            )
            row = conn.execute(
                "SELECT language, daily_recommendation, adult_filter FROM user_settings WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return row

    def update_user_settings(self, telegram_id: int, **kwargs) -> None:
        allowed = {"language", "daily_recommendation", "adult_filter"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        with self._connect() as conn:
            for col, val in updates.items():
                conn.execute(
                    f"UPDATE user_settings SET {col} = ? WHERE telegram_id = ?",
                    (val, telegram_id),
                )

    # --- Admin / Stats ---
    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])

    def get_all_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_id FROM users").fetchall()
            return [int(r["telegram_id"]) for r in rows]

    def get_users_with_daily_enabled(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT telegram_id FROM user_settings WHERE daily_recommendation = 1"
            ).fetchall()
            return [int(r["telegram_id"]) for r in rows]


# =========================================================================
# 4. CACHE
# =========================================================================

class SimpleCache:
    """In-memory TTL cache for TMDb responses."""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def _key(self, *parts: Any) -> str:
        return ":".join(str(p) for p in parts)

    def get(self, *parts: Any) -> Any:
        key = self._key(*parts)
        if key in self._store:
            ts, data = self._store[key]
            if time.time() - ts < self._ttl:
                return data
            del self._store[key]
        return None

    def set(self, data: Any, *parts: Any) -> None:
        self._store[self._key(*parts)] = (time.time(), data)

    def clear(self) -> None:
        self._store.clear()


# =========================================================================
# 5. TMDb CLIENT
# =========================================================================

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"
REQUEST_TIMEOUT = 10
MAX_RETRIES = 2


class TMDbError(Exception):
    """Raised when TMDb cannot fulfill a request after retries."""


class TMDbClient:
    """Wrapper around TMDb REST API with retries, timeouts, and caching."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session = requests.Session()
        self._cache = SimpleCache(ttl_seconds=300)

    def _get(self, path: str, params: Optional[dict[str, Any]] = None, use_cache: bool = True) -> dict[str, Any]:
        params = dict(params or {})
        params["api_key"] = self._api_key
        params.setdefault("language", "fa-IR")
        params.setdefault("include_adult", "false")

        if use_cache:
            cached = self._cache.get(path, sorted(params.items()))
            if cached is not None:
                return cached

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                response = self._session.get(
                    f"{TMDB_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                data = response.json()
                if use_cache:
                    self._cache.set(data, path, sorted(params.items()))
                return data
            except requests.RequestException as exc:
                last_error = exc
                log.warning("TMDb request failed (attempt %s): %s", attempt, exc)
        raise TMDbError(f"TMDb request to {path} failed after retries") from last_error

    def _with_overview_fallback(self, path: str, details: dict[str, Any]) -> dict[str, Any]:
        if not details.get("overview"):
            try:
                en_details = self._get(path, params={"language": "en-US"}, use_cache=False)
                details["overview"] = en_details.get("overview") or details.get("overview", "")
            except TMDbError:
                pass
        return details

    # --- Movies ---
    def get_trending(self, media_type: str = "movie", time_window: str = "week") -> list[dict[str, Any]]:
        return self._get(f"/trending/{media_type}/{time_window}").get("results", [])

    def get_top_rated(self) -> list[dict[str, Any]]:
        return self._get("/movie/top_rated").get("results", [])

    def get_now_playing(self) -> list[dict[str, Any]]:
        return self._get("/movie/now_playing").get("results", [])

    def get_upcoming(self) -> list[dict[str, Any]]:
        return self._get("/movie/upcoming").get("results", [])

    def get_popular(self, page: int = 1) -> list[dict[str, Any]]:
        return self._get("/movie/popular", params={"page": page}).get("results", [])

    def get_random_movie(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 20)
        results = self._get("/movie/popular", params={"page": page}).get("results", [])
        return random.choice(results) if results else None

    def search_movies(self, query: str, page: int = 1, include_adult: bool = False) -> dict[str, Any]:
        return self._get(
            "/search/movie",
            params={"query": query, "page": page, "include_adult": str(include_adult).lower()},
        )

    def get_movie_details(self, movie_id: int) -> dict[str, Any]:
        details = self._get(f"/movie/{movie_id}", params={"append_to_response": "videos,credits,keywords,release_dates,external_ids"})
        return self._with_overview_fallback(f"/movie/{movie_id}", details)

    def get_movie_providers(self, movie_id: int) -> dict[str, Any]:
        return self._get(f"/movie/{movie_id}/watch/providers").get("results", {})

    def get_similar_movies(self, movie_id: int) -> list[dict[str, Any]]:
        return self._get(f"/movie/{movie_id}/similar", params={"language": "en-US"}).get("results", [])

    def get_movie_recommendations(self, movie_id: int) -> list[dict[str, Any]]:
        return self._get(f"/movie/{movie_id}/recommendations", params={"language": "en-US"}).get("results", [])

    def get_movie_reviews(self, movie_id: int) -> list[dict[str, Any]]:
        return self._get(f"/movie/{movie_id}/reviews").get("results", [])

    # --- TV ---
    def get_tv_trending(self, time_window: str = "week") -> list[dict[str, Any]]:
        return self._get(f"/trending/tv/{time_window}").get("results", [])

    def get_tv_airing_today(self) -> list[dict[str, Any]]:
        return self._get("/tv/airing_today").get("results", [])

    def get_tv_on_the_air(self) -> list[dict[str, Any]]:
        return self._get("/tv/on_the_air").get("results", [])

    def get_random_tv(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 20)
        results = self._get("/tv/popular", params={"page": page}).get("results", [])
        return random.choice(results) if results else None

    def get_tv_details(self, tv_id: int) -> dict[str, Any]:
        details = self._get(f"/tv/{tv_id}", params={"append_to_response": "videos,credits,external_ids"})
        return self._with_overview_fallback(f"/tv/{tv_id}", details)

    def get_tv_season(self, tv_id: int, season_num: int) -> dict[str, Any]:
        return self._get(f"/tv/{tv_id}/season/{season_num}")

    def search_tv(self, query: str, page: int = 1) -> dict[str, Any]:
        return self._get("/search/tv", params={"query": query, "page": page})

    # --- People ---
    def search_person(self, query: str, page: int = 1) -> dict[str, Any]:
        return self._get("/search/person", params={"query": query, "page": page})

    def get_person_details(self, person_id: int) -> dict[str, Any]:
        return self._get(f"/person/{person_id}", params={"append_to_response": "movie_credits,tv_credits,external_ids"})

    def get_popular_people(self, page: int = 1) -> list[dict[str, Any]]:
        return self._get("/person/popular", params={"page": page}).get("results", [])

    # --- Discover ---
    def get_random_anime(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 10)
        results = self._get(
            "/discover/movie",
            params={
                "with_genres": "16",
                "with_origin_country": "JP",
                "sort_by": "popularity.desc",
                "page": page,
            },
        ).get("results", [])
        return random.choice(results) if results else None

    def discover_movies(self, **params: Any) -> dict[str, Any]:
        return self._get("/discover/movie", params=params)

    def _discover_random(self, **extra_params: str) -> Optional[dict[str, Any]]:
        page = random.randint(1, 5)
        params = dict(extra_params)
        params["page"] = str(page)
        params.setdefault("sort_by", "popularity.desc")
        results = self._get("/discover/movie", params=params).get("results", [])
        return random.choice(results) if results else None

    def get_random_by_genre(self, genre_id: str) -> Optional[dict[str, Any]]:
        return self._discover_random(with_genres=genre_id)

    def get_random_by_company(self, company_id: str) -> Optional[dict[str, Any]]:
        return self._discover_random(with_companies=company_id)

    def get_random_by_crew(self, person_id: str) -> Optional[dict[str, Any]]:
        return self._discover_random(with_crew=person_id)

    def get_random_by_year(self, year: str) -> Optional[dict[str, Any]]:
        return self._discover_random(primary_release_year=year)

    def get_random_by_rating(self, min_rating: str) -> Optional[dict[str, Any]]:
        return self._discover_random(vote_average_gte=min_rating, sort_by="vote_average.desc")


# =========================================================================
# 6. GEMINI CLIENT
# =========================================================================

GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


class GeminiError(Exception):
    """Raised when the Gemini API cannot fulfill a request."""


class GeminiClient:
    """Thin wrapper around the Gemini API for free-text movie recommendations."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session = requests.Session()

    def suggest_movie_title(self, user_request: str) -> str:
        prompt = (
            "You are a movie recommendation assistant. Based on the user's "
            "request below (which may be in Persian or English), suggest "
            "exactly ONE specific real movie that best matches what they "
            "want. Reply with ONLY the movie's official English title "
            "(add the release year in parentheses if it helps disambiguate) "
            "and nothing else — no explanation, no quotes, no extra text.\n\n"
            f"User's request: {user_request}"
        )
        try:
            response = self._session.post(
                GEMINI_URL,
                headers={
                    "x-goog-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return text.strip()
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise GeminiError("Gemini request failed") from exc


# =========================================================================
# 7. FORMATTING HELPERS
# =========================================================================

def _e(text: Any) -> str:
    """Escapes a value for safe interpolation into an HTML-parsed Telegram
    message. Movie titles/overviews routinely contain '&', '<', '>' (e.g.
    "Tom & Jerry", "Star Wars: Episode I <1999>" style text) — without this,
    Telegram rejects the whole message with a parse error and the user gets
    silence instead of a movie card."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _extract_director(crew: list[dict]) -> Optional[str]:
    for person in crew:
        if person.get("job") == "Director":
            return person.get("name")
    return None


def _extract_top_cast(cast: list[dict], limit: int = 3) -> Optional[str]:
    names = [p.get("name", "") for p in cast[:limit] if p.get("name")]
    return ", ".join(names) if names else None


def _format_providers(providers: dict[str, Any], region: str = "US") -> str:
    """Format watch providers for a given region."""
    if not providers:
        return ""
    region_data = providers.get(region) or providers.get("US") or next(iter(providers.values()), {})
    if not region_data:
        return ""
    lines = []
    for provider_type in ("flatrate", "rent", "buy"):
        items = region_data.get(provider_type, [])
        if items:
            names = ", ".join(_e(p.get("provider_name", "")) for p in items[:5])
            emoji = {"flatrate": "📺", "rent": "🎫", "buy": "💳"}.get(provider_type, "▶️")
            label = {"flatrate": "Stream", "rent": "Rent", "buy": "Buy"}.get(provider_type, provider_type)
            lines.append(f"{emoji} <b>{label}:</b> {names}")
    return "\n".join(lines) + "\n" if lines else ""


def _format_certification(release_dates: list[dict], region: str = "US") -> str:
    for rd in release_dates:
        if rd.get("iso_3166_1") == region:
            certs = rd.get("release_dates", [])
            for c in certs:
                cert = c.get("certification", "")
                if cert:
                    return f"🔞 <b>Rated:</b> {_e(cert)}\n"
    return ""


def _format_keywords(keywords: list[dict]) -> str:
    if not keywords:
        return ""
    names = ", ".join(_e(k.get("name", "")) for k in keywords[:8])
    return f"🏷 <b>Keywords:</b> {names}\n"


def _format_external_ids(external_ids: dict[str, Any]) -> str:
    links = []
    imdb = external_ids.get("imdb_id")
    if imdb:
        links.append(f'<a href="https://www.imdb.com/title/{imdb}">🎬 IMDb</a>')
    tmdb = external_ids.get("id")
    if tmdb:
        links.append(f'<a href="https://www.themoviedb.org/movie/{tmdb}">📊 TMDb</a>')
    return " | ".join(links) + "\n" if links else ""


def format_movie_caption(
    item: dict[str, Any],
    media_type: str = "movie",
    user_rating: Optional[int] = None,
    providers: Optional[dict[str, Any]] = None,
) -> str:
    if media_type == "tv":
        title = _e(item.get("name") or item.get("original_name") or "بدون عنوان")
        date_value = item.get("first_air_date") or "----"
        icon = "📺"
    elif media_type == "anime":
        title = _e(item.get("title") or item.get("original_title") or "بدون عنوان")
        date_value = item.get("release_date") or "----"
        icon = "🎌"
    else:
        title = _e(item.get("title") or item.get("original_title") or "بدون عنوان")
        date_value = item.get("release_date") or "----"
        icon = "🎬"

    year = date_value[:4]
    rating = item.get("vote_average", 0)
    vote_count = item.get("vote_count", 0)
    overview = _e(item.get("overview") or "توضیحاتی برای این عنوان موجود نیست.")
    if len(overview) > 500:
        overview = overview[:497] + "..."

    tagline = _e(item.get("tagline", ""))
    tagline_line = f"<i>{tagline}</i>\n\n" if tagline else ""

    genres = item.get("genres")
    genre_line = ""
    if genres:
        names = ", ".join(_e(g["name"]) for g in genres)
        genre_line = f"🎭 <b>ژانر:</b> {names}\n"

    runtime_line = ""
    if media_type == "tv":
        episode_runtimes = item.get("episode_run_time") or []
        if episode_runtimes:
            runtime_line = f"⏱ <b>مدت هر قسمت:</b> {episode_runtimes[0]} دقیقه\n"
        seasons = item.get("number_of_seasons")
        episodes = item.get("number_of_episodes")
        if seasons:
            runtime_line += f"📊 <b>فصل:</b> {seasons} | <b>قسمت:</b> {episodes or 'N/A'}\n"
        status = _e(item.get("status", ""))
        if status:
            runtime_line += f"📡 <b>وضعیت:</b> {status}\n"
    else:
        runtime = item.get("runtime")
        if runtime:
            hours = runtime // 60
            mins = runtime % 60
            runtime_str = f"{hours}h {mins}m" if hours else f"{mins}m"
            runtime_line = f"⏱ <b>مدت زمان:</b> {runtime_str}\n"

    # Only show director/cast if we actually fetched credits for this item —
    # otherwise this silently prints "Unknown"/"N/A" on every TV/anime card.
    credits = item.get("credits") or {}
    director = _extract_director(credits.get("crew", []))
    top_cast = _extract_top_cast(credits.get("cast", []))
    crew_line = ""
    if director:
        crew_line += f"🎬 <b>کارگردان:</b> {_e(director)}\n"
    if top_cast:
        crew_line += f"🎭 <b>بازیگران:</b> {_e(top_cast)}\n"

    budget = item.get("budget")
    revenue = item.get("revenue")
    money_line = ""
    if budget and budget > 0:
        money_line += f"💰 <b>بودجه:</b> ${budget:,.0f}\n"
    if revenue and revenue > 0:
        money_line += f"💵 <b>فروش:</b> ${revenue:,.0f}\n"

    companies = item.get("production_companies", [])
    company_line = ""
    if companies:
        company_names = ", ".join(_e(c["name"]) for c in companies[:3])
        company_line = f"🏢 <b>استودیو:</b> {company_names}\n"

    # Keywords & Certification
    keywords = item.get("keywords", {}).get("keywords", [])
    keywords_line = _format_keywords(keywords)

    release_dates = item.get("release_dates", {}).get("results", [])
    cert_line = _format_certification(release_dates)

    # External links
    external_ids = item.get("external_ids", {})
    if not external_ids and "id" in item:
        external_ids = {"id": item["id"]}
    links_line = _format_external_ids(external_ids)

    # Providers
    providers_line = ""
    if providers:
        providers_line = _format_providers(providers)

    user_rating_line = ""
    if user_rating:
        stars = "⭐" * (user_rating // 2) + ("½" if user_rating % 2 else "")
        user_rating_line = f"\n🎯 <b>امتیاز شما:</b> {stars} ({user_rating}/10)"

    return (
        f"{icon} <b>{title}</b> ({year})\n"
        f"⭐ <b>امتیاز:</b> {rating:.1f}/10 <i>({vote_count:,} رأی)</i>\n"
        f"{genre_line}"
        f"{runtime_line}"
        f"{crew_line}"
        f"{company_line}"
        f"{cert_line}"
        f"{keywords_line}"
        f"{money_line}"
        f"{providers_line}"
        f"{links_line}"
        f"\n{tagline_line}"
        f"{overview}"
        f"{user_rating_line}"
    )


def format_person_caption(person: dict[str, Any]) -> str:
    name = _e(person.get("name", "Unknown"))
    known_for = _e(person.get("known_for_department", ""))
    birthday = _e(person.get("birthday") or "N/A")
    place = _e(person.get("place_of_birth") or "N/A")
    bio = _e(person.get("biography") or "Biography not available.")
    if len(bio) > 600:
        bio = bio[:597] + "..."

    movie_credits = person.get("movie_credits", {})
    cast = movie_credits.get("cast", [])
    known_titles = ", ".join(_e(m.get("title", "")) for m in cast[:5] if m.get("title"))

    # External links
    ext = person.get("external_ids", {})
    links = []
    imdb = ext.get("imdb_id")
    if imdb:
        links.append(f'<a href="https://www.imdb.com/name/{imdb}">🎬 IMDb</a>')
    tmdb_id = person.get("id")
    if tmdb_id:
        links.append(f'<a href="https://www.themoviedb.org/person/{tmdb_id}">📊 TMDb</a>')
    links_line = " | ".join(links) + "\n" if links else ""

    return (
        f"🎭 <b>{name}</b>\n"
        f"🎬 <b>حرفه:</b> {known_for}\n"
        f"🎂 <b>تولد:</b> {birthday}\n"
        f"📍 <b>محل تولد:</b> {place}\n"
        f"{links_line}\n"
        f"📝 <b>بیوگرافی:</b>\n{bio}\n"
        f"\n🎞 <b>آثار شناخته‌شده:</b> {known_titles or 'N/A'}"
    )


def format_review(review: dict[str, Any]) -> str:
    author = _e(review.get("author", "Anonymous"))
    rating = review.get("author_details", {}).get("rating")
    content = _e(review.get("content", ""))
    if len(content) > 800:
        content = content[:797] + "..."
    rating_str = f"⭐ {rating}/10" if rating else ""
    return f"📝 <b>نقد {author}</b> {rating_str}\n\n{content}"


def format_season_caption(season: dict[str, Any], tv_title: str) -> str:
    name = _e(season.get("name", "Season"))
    overview = _e(season.get("overview") or "No overview available.")
    if len(overview) > 400:
        overview = overview[:397] + "..."
    episodes = season.get("episodes", [])
    episode_count = len(episodes)
    air_date = season.get("air_date") or "----"
    return (
        f"📺 <b>{_e(tv_title)}</b> — {name}\n"
        f"📅 <b>تاریخ پخش:</b> {air_date}\n"
        f"📊 <b>تعداد قسمت:</b> {episode_count}\n\n"
        f"{overview}"
    )


def poster_url(item: dict[str, Any]) -> Optional[str]:
    path = item.get("poster_path") or item.get("profile_path")
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


def backdrop_url(item: dict[str, Any]) -> Optional[str]:
    path = item.get("backdrop_path")
    return f"{TMDB_BACKDROP_BASE}{path}" if path else None


def get_youtube_trailer(item: dict[str, Any]) -> Optional[str]:
    videos = (item.get("videos") or {}).get("results", [])
    for video in videos:
        if video.get("site") == "YouTube" and video.get("type") in ("Trailer", "Teaser"):
            return f"https://www.youtube.com/watch?v={video.get('key')}"
    return None


# =========================================================================
# 8. KEYBOARDS
# =========================================================================

GENRES: list[tuple[str, str]] = [
    ("28", "🎬 اکشن"),
    ("35", "😂 کمدی"),
    ("27", "👻 ترسناک"),
    ("10749", "❤️ عاشقانه"),
    ("878", "🚀 علمی-تخیلی"),
    ("16", "🎨 انیمیشن"),
    ("18", "🎭 درام"),
    ("53", "🔪 هیجانی"),
    ("12", "🗺 ماجراجویی"),
    ("80", "🕵️ جنایی"),
]

COLLECTIONS: list[tuple[str, str, str]] = [
    ("company", "420", "🦸 مارول"),
    ("company", "3", "🧸 پیکسار"),
    ("company", "10342", "🎨 استودیو جیبلی"),
    ("crew", "525", "🎬 کریستوفر نولان"),
]

PAGE_SIZE = 5


def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🧠 پیشنهاد هوشمند", callback_data="menu:ai")],
        [
            InlineKeyboardButton("🎲 Random Movie", callback_data="menu:random"),
            InlineKeyboardButton("🔥 Trending", callback_data="menu:trending"),
        ],
        [
            InlineKeyboardButton("⭐ Top Rated", callback_data="menu:top_rated"),
            InlineKeyboardButton("🆕 Now Playing", callback_data="menu:now_playing"),
        ],
        [
            InlineKeyboardButton("📅 Upcoming", callback_data="menu:upcoming"),
            InlineKeyboardButton("📺 TV Shows", callback_data="menu:tv"),
        ],
        [
            InlineKeyboardButton("🎌 Anime", callback_data="menu:anime"),
            InlineKeyboardButton("🎭 ژانرها", callback_data="menu:genres"),
        ],
        [
            InlineKeyboardButton("🎬 مجموعه‌ها", callback_data="menu:collections"),
            InlineKeyboardButton("🔍 Person Search", callback_data="menu:person"),
        ],
        [
            InlineKeyboardButton("❤️ Favorites", callback_data="menu:favorites"),
            InlineKeyboardButton("📋 Watchlist", callback_data="menu:watchlist"),
        ],
        [
            InlineKeyboardButton("📜 History", callback_data="menu:history"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="menu:stats"),
            InlineKeyboardButton("📤 Export", callback_data="menu:export"),
        ],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(buttons)


def genres_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(GENRES), 2):
        pair = GENRES[i : i + 2]
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"genre:{gid}") for gid, label in pair]
        )
    rows.append([InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def collections_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(COLLECTIONS), 2):
        pair = COLLECTIONS[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(label, callback_data=f"collection:{kind}:{cid}")
                for kind, cid, label in pair
            ]
        )
    rows.append([InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def simple_actions_keyboard(refresh_action: str, refresh_label: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(refresh_label, callback_data=f"menu:{refresh_action}")],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(buttons)


def movie_actions_keyboard(
    movie_id: int,
    is_favorite: bool,
    is_watchlist: bool,
    user_rating: Optional[int],
    refresh_callback: str = "menu:random",
    refresh_label: str = "🎲 Another Random",
    trailer_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    fav_label = "💔 Remove Favorite" if is_favorite else "❤️ Add Favorite"
    wl_label = "➖ Remove Watchlist" if is_watchlist else "📋 Add Watchlist"
    wl_data = f"unwl:{movie_id}" if is_watchlist else f"wl:{movie_id}"
    rate_label = f"⭐ Rate ({user_rating}/10)" if user_rating else "⭐ Rate"

    top_row = [
        InlineKeyboardButton(fav_label, callback_data=f"fav:{movie_id}"),
        InlineKeyboardButton(wl_label, callback_data=wl_data),
    ]
    second_row = [
        InlineKeyboardButton(rate_label, callback_data=f"ratemenu:{movie_id}"),
    ]
    if trailer_url:
        second_row.append(InlineKeyboardButton("🎬 تریلر", url=trailer_url))

    buttons = [
        top_row,
        second_row,
        [
            InlineKeyboardButton("👍 موارد مشابه", callback_data=f"similar:{movie_id}"),
            InlineKeyboardButton("🎯 Recommendations", callback_data=f"rec:{movie_id}"),
        ],
        [
            InlineKeyboardButton("📝 Reviews", callback_data=f"reviews:{movie_id}"),
            InlineKeyboardButton("📤 Share", switch_inline_query=f"id{movie_id}"),
        ],
        [InlineKeyboardButton(refresh_label, callback_data=refresh_callback)],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(buttons)


def tv_actions_keyboard(
    tv_id: int,
    refresh_callback: str = "menu:tv",
    refresh_label: str = "🔄 سریال دیگر",
) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📂 فصل‌ها", callback_data=f"seasons:{tv_id}")],
        [InlineKeyboardButton(refresh_label, callback_data=refresh_callback)],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(buttons)


def rating_keyboard(movie_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("⭐", callback_data=f"rate:{movie_id}:2"),
            InlineKeyboardButton("⭐⭐", callback_data=f"rate:{movie_id}:4"),
            InlineKeyboardButton("⭐⭐⭐", callback_data=f"rate:{movie_id}:6"),
            InlineKeyboardButton("⭐⭐⭐⭐", callback_data=f"rate:{movie_id}:8"),
            InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data=f"rate:{movie_id}:10"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=f"view:{movie_id}")],
    ]
    return InlineKeyboardMarkup(buttons)


def list_pagination_keyboard(
    current_page: int,
    total_pages: int,
    list_type: str,
) -> InlineKeyboardMarkup:
    buttons = []
    nav = []
    if current_page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page:{list_type}:{current_page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {current_page + 1}/{total_pages}", callback_data="noop"))
    if current_page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"page:{list_type}:{current_page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def search_results_keyboard(
    results: list[dict[str, Any]],
    prefix: str = "view",
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    # NOTE: the search query itself is deliberately NOT embedded in
    # callback_data — Telegram caps callback_data at 64 bytes, and a
    # longer (especially Persian/UTF-8) query would silently break every
    # multi-result search. The query is read back from user_data instead.
    buttons = []
    for i, item in enumerate(results[:6], 1):
        title = item.get("title") or item.get("name") or "Untitled"
        year = (item.get("release_date") or item.get("first_air_date") or "----")[:4]
        buttons.append([InlineKeyboardButton(f"{i}. {title} ({year})", callback_data=f"{prefix}:{item['id']}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"searchpage:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"searchpage:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def person_results_keyboard(people: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for i, person in enumerate(people[:6], 1):
        name = person.get("name", "Unknown")
        dept = person.get("known_for_department", "")
        label = f"{i}. {name}" + (f" ({dept})" if dept else "")
        buttons.append([InlineKeyboardButton(label, callback_data=f"person:{person['id']}")])
    buttons.append([InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def seasons_keyboard(tv_id: int, seasons: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for season in seasons:
        num = season.get("season_number")
        name = season.get("name", f"Season {num}")
        if num is not None:
            buttons.append([InlineKeyboardButton(name, callback_data=f"season:{tv_id}:{num}")])
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data=f"viewtv:{tv_id}")])
    buttons.append([InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def settings_keyboard(lang: str, daily: int, adult_filter: int = 1) -> InlineKeyboardMarkup:
    daily_label = "🔔 Daily: ON" if daily else "🔕 Daily: OFF"
    adult_label = "🔞 محتوای بزرگسال: مسدود" if adult_filter else "🔞 محتوای بزرگسال: آزاد"
    buttons = [
        [InlineKeyboardButton(daily_label, callback_data="toggle:daily")],
        [InlineKeyboardButton(adult_label, callback_data="toggle:adult")],
        [InlineKeyboardButton("🗑 Clear History", callback_data="clear:history")],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(buttons)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")]]
    )


# =========================================================================
# 9. GLOBALS
# =========================================================================

WELCOME_TEXT = (
    "🎬 <b>Welcome to MovieBot!</b>\n\n"
    "I help you discover movies to watch — trending picks, top-rated "
    "classics, random surprises, search by title, TV shows, anime, "
    "person search, favorites, watchlist, ratings, and more.\n\n"
    "Use the menu below to get started, or just type a movie name to search."
)

HELP_TEXT = (
    "❓ <b>How to use MovieBot</b>\n\n"
    "🎲 <b>Random Movie</b> — get a surprise pick\n"
    "🔥 <b>Trending</b> — what's popular this week\n"
    "⭐ <b>Top Rated</b> — all-time highest rated\n"
    "🆕 <b>Now Playing</b> — currently in theaters\n"
    "📅 <b>Upcoming</b> — coming soon\n"
    "📺 <b>TV Shows</b> — popular series\n"
    "🎌 <b>Anime</b> — Japanese animation\n"
    "🎭 <b>Genres</b> — browse by genre\n"
    "🎬 <b>Collections</b> — Marvel, Pixar, Ghibli, Nolan\n"
    "🔍 <b>Person Search</b> — actors & directors\n"
    "❤️ <b>Favorites</b> — movies you've saved\n"
    "📋 <b>Watchlist</b> — movies to watch later\n"
    "⭐ <b>Rate</b> — rate any movie 1–10\n"
    "📜 <b>History</b> — your recent searches\n"
    "⚙️ <b>Settings</b> — daily recommendations toggle\n"
    "📊 <b>My Stats</b> — your activity summary\n"
    "📤 <b>Export</b> — download favorites/watchlist as CSV\n"
    "🧠 <b>AI Recommend</b> — smart suggestions via Gemini\n\n"
    "You can also just type any movie title to search for it directly."
)


# =========================================================================
# 10. HANDLERS
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if user is not None:
        db.upsert_user(user.id, user.username)
        log.info("User started bot: id=%s username=%s", user.id, user.username)

    await update.message.reply_text(
        WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard()
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    user = update.effective_user
    if user is None or config.admin_id is None or user.id != config.admin_id:
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    db: Database = context.bot_data["db"]
    await update.message.reply_text(f"👥 Total users: {db.user_count()}")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    user = update.effective_user
    if user is None or config.admin_id is None or user.id != config.admin_id:
        await update.message.reply_text("⛔ Admin only.")
        return
    context.user_data["awaiting_broadcast"] = True
    await update.message.reply_text(
        "📢 Send the message you want to broadcast to all users.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="menu:home")]]),
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    telegram_id = update.effective_user.id
    settings = db.get_user_settings(telegram_id)
    await update.message.reply_text(
        "⚙️ <b>Settings</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(settings["language"], settings["daily_recommendation"], settings["adult_filter"]),
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    telegram_id = update.effective_user.id
    history = db.get_search_history(telegram_id, limit=10)
    if not history:
        await update.message.reply_text(
            "📜 No search history yet.", reply_markup=back_to_menu_keyboard()
        )
        return
    lines = [f"• {_e(row['query'])} — <i>{row['searched_at']}</i>" for row in history]
    await update.message.reply_text(
        "📜 <b>Recent Searches:</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_menu_keyboard(),
    )


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    telegram_id = update.effective_user.id
    fav_count = db.count_favorites(telegram_id)
    wl_count = db.count_watchlist(telegram_id)
    rating_count = db.get_user_rating_count(telegram_id)
    avg_rating = db.get_user_avg_rating(telegram_id)
    history = db.get_search_history(telegram_id, limit=1)

    avg_str = f"{avg_rating:.1f}/10" if avg_rating else "N/A"
    last_search = history[0]["query"] if history else "N/A"

    text = (
        f"📊 <b>Your Stats</b>\n\n"
        f"❤️ <b>Favorites:</b> {fav_count}\n"
        f"📋 <b>Watchlist:</b> {wl_count}\n"
        f"⭐ <b>Ratings given:</b> {rating_count}\n"
        f"📈 <b>Average rating:</b> {avg_str}\n"
        f"🔍 <b>Last search:</b> {last_search}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    telegram_id = update.effective_user.id

    favorites = db.list_favorites(telegram_id, limit=1000)
    watchlist = db.list_watchlist(telegram_id, limit=1000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Type", "Movie ID", "Title"])
    for row in favorites:
        writer.writerow(["favorite", row["movie_id"], row["title"]])
    for row in watchlist:
        writer.writerow(["watchlist", row["movie_id"], row["title"]])

    data = output.getvalue().encode("utf-8")
    await update.message.reply_document(
        document=data,
        filename="moviebot_export.csv",
        caption="📤 <b>Your exported data</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_menu_keyboard(),
    )


# --- Shared send helpers ---

async def _send_movie(
    update_or_query, context: ContextTypes.DEFAULT_TYPE, movie: dict[str, Any], telegram_id: int
) -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    try:
        details = tmdb.get_movie_details(movie["id"])
    except TMDbError as exc:
        log.error("Failed to fetch movie details for id=%s: %s", movie.get("id"), exc)
        details = movie

    # Fetch providers (best-effort — a failure here shouldn't block the card)
    try:
        providers = tmdb.get_movie_providers(movie["id"])
    except TMDbError:
        providers = None

    user_rating = db.get_rating(telegram_id, details["id"])
    caption = format_movie_caption(details, user_rating=user_rating, providers=providers)
    trailer_url = get_youtube_trailer(details)
    keyboard = movie_actions_keyboard(
        details["id"],
        db.is_favorite(telegram_id, details["id"]),
        db.is_watchlist(telegram_id, details["id"]),
        user_rating,
        trailer_url=trailer_url,
    )
    image = poster_url(details)

    chat = update_or_query.effective_chat if hasattr(update_or_query, "effective_chat") else None
    target_chat_id = chat.id if chat else update_or_query.message.chat_id

    # One photo (poster) with the full caption — a separate backdrop photo
    # was dropped to keep this to a single image/request per view.
    if image:
        await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def _send_tv_or_anime(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    item: dict[str, Any],
    media_type: str,
    refresh_action: str,
    refresh_label: str,
) -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        details = tmdb.get_tv_details(item["id"]) if media_type == "tv" else item
    except TMDbError as exc:
        log.error("Failed to fetch TV details for id=%s: %s", item.get("id"), exc)
        details = item

    caption = format_movie_caption(details, media_type=media_type)
    keyboard = tv_actions_keyboard(details["id"], refresh_callback=f"menu:{refresh_action}", refresh_label=refresh_label)
    image = poster_url(details)

    chat = update_or_query.effective_chat if hasattr(update_or_query, "effective_chat") else None
    target_chat_id = chat.id if chat else update_or_query.message.chat_id

    if image:
        await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def _send_person(update_or_query, context: ContextTypes.DEFAULT_TYPE, person: dict[str, Any]) -> None:
    caption = format_person_caption(person)
    image = poster_url(person)

    chat = update_or_query.effective_chat if hasattr(update_or_query, "effective_chat") else None
    target_chat_id = chat.id if chat else update_or_query.message.chat_id

    if image:
        await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )
    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )


# --- Menu button handler ---

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    telegram_id = query.from_user.id

    try:
        if action == "home":
            await query.message.reply_text(
                WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
            )

        elif action == "ai":
            gemini = context.bot_data.get("gemini")
            if gemini is None:
                await query.message.reply_text(
                    "⚠️ این قابلیت هنوز فعال نشده (کلید GEMINI_API_KEY تنظیم نشده).",
                    reply_markup=back_to_menu_keyboard(),
                )
                return
            context.user_data["awaiting_ai_recommendation"] = True
            await query.message.reply_text(
                "🧠 چه فیلمی دلت می‌خواد ببینی؟ توضیح بده — مثلاً «یه فیلم علمی-تخیلی "
                "شبیه اینترستلار» یا «یه کمدی سبک برای شب جمعه»."
            )

        elif action == "help":
            await query.message.reply_text(
                HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard()
            )

        elif action == "random":
            movie = tmdb.get_random_movie()
            if movie is None:
                await query.message.reply_text("😕 Couldn't find a movie right now, try again.")
                return
            await _send_movie(query, context, movie, telegram_id)

        elif action == "tv":
            show = tmdb.get_random_tv()
            if show is None:
                await query.message.reply_text("😕 سریالی پیدا نشد، دوباره امتحان کن.")
                return
            await _send_tv_or_anime(
                query, context, show, "tv", refresh_action="tv", refresh_label="🔄 سریال دیگر"
            )

        elif action == "anime":
            anime = tmdb.get_random_anime()
            if anime is None:
                await query.message.reply_text("😕 انیمه‌ای پیدا نشد، دوباره امتحان کن.")
                return
            await _send_tv_or_anime(
                query, context, anime, "anime", refresh_action="anime", refresh_label="🔄 انیمه دیگر"
            )

        elif action == "genres":
            await query.message.reply_text("🎭 یک ژانر انتخاب کن:", reply_markup=genres_keyboard())

        elif action == "collections":
            await query.message.reply_text("🎬 یک مجموعه انتخاب کن:", reply_markup=collections_keyboard())

        elif action == "trending":
            movies = tmdb.get_trending("movie", "week")[:8]
            if not movies:
                await query.message.reply_text("😕 No trending movies found right now.")
                return
            await query.message.reply_text(
                "🔥 <b>Trending Movies this week:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=search_results_keyboard(movies, prefix="view"),
            )

        elif action == "top_rated":
            movies = tmdb.get_top_rated()[:8]
            if not movies:
                await query.message.reply_text("😕 No top rated movies found right now.")
                return
            await query.message.reply_text(
                "⭐ <b>Top Rated:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=search_results_keyboard(movies, prefix="view"),
            )

        elif action == "now_playing":
            movies = tmdb.get_now_playing()[:8]
            if not movies:
                await query.message.reply_text("😕 No now playing movies found.")
                return
            await query.message.reply_text(
                "🆕 <b>Now Playing in Theaters:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=search_results_keyboard(movies, prefix="view"),
            )

        elif action == "upcoming":
            movies = tmdb.get_upcoming()[:8]
            if not movies:
                await query.message.reply_text("😕 No upcoming movies found.")
                return
            await query.message.reply_text(
                "📅 <b>Upcoming Movies:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=search_results_keyboard(movies, prefix="view"),
            )

        elif action == "favorites":
            context.user_data["list_type"] = "favorites"
            context.user_data["list_page"] = 0
            await _show_paginated_list(query, context, telegram_id)

        elif action == "watchlist":
            context.user_data["list_type"] = "watchlist"
            context.user_data["list_page"] = 0
            await _show_paginated_list(query, context, telegram_id)

        elif action == "history":
            history = db.get_search_history(telegram_id, limit=10)
            if not history:
                await query.message.reply_text(
                    "📜 No search history yet.", reply_markup=back_to_menu_keyboard()
                )
                return
            lines = [f"• {_e(row['query'])} — <i>{row['searched_at']}</i>" for row in history]
            await query.message.reply_text(
                "📜 <b>Recent Searches:</b>\n\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=back_to_menu_keyboard(),
            )

        elif action == "settings":
            settings = db.get_user_settings(telegram_id)
            await query.message.reply_text(
                "⚙️ <b>Settings</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=settings_keyboard(settings["language"], settings["daily_recommendation"], settings["adult_filter"]),
            )

        elif action == "person":
            context.user_data["awaiting_person_search"] = True
            await query.message.reply_text(
                "🔍 نام بازیگر یا کارگردان را وارد کنید:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="menu:home")]]),
            )

        elif action == "stats":
            fav_count = db.count_favorites(telegram_id)
            wl_count = db.count_watchlist(telegram_id)
            rating_count = db.get_user_rating_count(telegram_id)
            avg_rating = db.get_user_avg_rating(telegram_id)
            history = db.get_search_history(telegram_id, limit=1)
            avg_str = f"{avg_rating:.1f}/10" if avg_rating else "N/A"
            last_search = history[0]["query"] if history else "N/A"
            text = (
                f"📊 <b>Your Stats</b>\n\n"
                f"❤️ <b>Favorites:</b> {fav_count}\n"
                f"📋 <b>Watchlist:</b> {wl_count}\n"
                f"⭐ <b>Ratings given:</b> {rating_count}\n"
                f"📈 <b>Average rating:</b> {avg_str}\n"
                f"🔍 <b>Last search:</b> {last_search}\n"
            )
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())

        elif action == "export":
            favorites = db.list_favorites(telegram_id, limit=1000)
            watchlist = db.list_watchlist(telegram_id, limit=1000)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Type", "Movie ID", "Title"])
            for row in favorites:
                writer.writerow(["favorite", row["movie_id"], row["title"]])
            for row in watchlist:
                writer.writerow(["watchlist", row["movie_id"], row["title"]])
            data = output.getvalue().encode("utf-8")
            await query.message.reply_document(
                document=data,
                filename="moviebot_export.csv",
                caption="📤 <b>Your exported data</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_to_menu_keyboard(),
            )

    except TMDbError as exc:
        log.error("TMDb error in menu action '%s': %s", action, exc)
        await query.message.reply_text("⚠️ Movie service is temporarily unavailable. Please try again shortly.")


async def _show_paginated_list(query, context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> None:
    db: Database = context.bot_data["db"]
    list_type = context.user_data.get("list_type", "favorites")
    page = context.user_data.get("list_page", 0)

    if list_type == "favorites":
        total = db.count_favorites(telegram_id)
        items = db.list_favorites(telegram_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        header = "❤️ <b>Your Favorites:</b>"
    else:
        total = db.count_watchlist(telegram_id)
        items = db.list_watchlist(telegram_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        header = "📋 <b>Your Watchlist:</b>"

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    if not items:
        await query.message.reply_text(
            f"{'❤️' if list_type == 'favorites' else '📋'} Your {list_type} is empty.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    lines = [f"• {row['title']}" for row in items]
    await query.message.reply_text(
        f"{header}\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=list_pagination_keyboard(page, total_pages, list_type),
    )


# --- Favorite / Watchlist / Rating handlers ---

async def on_favorite_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    telegram_id = query.from_user.id

    action, movie_id_str = query.data.split(":", 1)
    movie_id = int(movie_id_str)

    if action == "fav":
        try:
            details = tmdb.get_movie_details(movie_id)
            title = details.get("title", "Unknown")
        except TMDbError:
            title = "Unknown"
        added = db.add_favorite(telegram_id, movie_id, title)
        await query.answer("Added to favorites ❤️" if added else "Already in favorites", show_alert=False)
    elif action == "unfav":
        db.remove_favorite(telegram_id, movie_id)
        await query.answer("Removed from favorites 💔", show_alert=False)


async def on_watchlist_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    telegram_id = query.from_user.id

    action, movie_id_str = query.data.split(":", 1)
    movie_id = int(movie_id_str)

    if action == "wl":
        try:
            details = tmdb.get_movie_details(movie_id)
            title = details.get("title", "Unknown")
        except TMDbError:
            title = "Unknown"
        added = db.add_watchlist(telegram_id, movie_id, title)
        await query.answer("Added to watchlist 📋" if added else "Already in watchlist", show_alert=False)
    elif action == "unwl":
        db.remove_watchlist(telegram_id, movie_id)
        await query.answer("Removed from watchlist ➖", show_alert=False)


async def on_rating_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":", 1)[1])
    await query.message.reply_text(
        "⭐ امتیاز خود را انتخاب کنید:",
        reply_markup=rating_keyboard(movie_id),
    )


async def on_rating_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db: Database = context.bot_data["db"]
    telegram_id = query.from_user.id

    _, movie_id_str, rating_str = query.data.split(":")
    movie_id = int(movie_id_str)
    rating = int(rating_str)

    db.add_rating(telegram_id, movie_id, rating)
    await query.answer(f"Rated {rating}/10 ⭐", show_alert=False)
    await query.message.reply_text(
        f"✅ امتیاز {rating}/10 ثبت شد.",
        reply_markup=back_to_menu_keyboard(),
    )


# --- Similar / Recommendations / Reviews ---

async def on_similar_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        similar = tmdb.get_similar_movies(movie_id)
    except TMDbError as exc:
        log.error("Failed to fetch similar movies for id=%s: %s", movie_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    if not similar:
        await query.message.reply_text("😕 مورد مشابهی پیدا نشد.")
        return

    await query.message.reply_text(
        "👍 <b>Similar Movies:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=search_results_keyboard(similar[:6], prefix="view"),
    )


async def on_recommendation_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        recs = tmdb.get_movie_recommendations(movie_id)
    except TMDbError as exc:
        log.error("Failed to fetch recommendations for id=%s: %s", movie_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    if not recs:
        await query.message.reply_text("😕 پیشنهادی پیدا نشد.")
        return

    await query.message.reply_text(
        "🎯 <b>Recommended for you:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=search_results_keyboard(recs[:6], prefix="view"),
    )


async def on_reviews_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        reviews = tmdb.get_movie_reviews(movie_id)
    except TMDbError as exc:
        log.error("Failed to fetch reviews for id=%s: %s", movie_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    if not reviews:
        await query.message.reply_text("😕 نقدی برای این فیلم پیدا نشد.")
        return

    review = reviews[0]
    text = format_review(review)
    await query.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_menu_keyboard(),
    )


# --- Genre / Collection / View / Person / Page / Seasons ---

async def on_genre_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    genre_id = query.data.split(":", 1)[1]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    telegram_id = query.from_user.id

    try:
        movie = tmdb.get_random_by_genre(genre_id)
    except TMDbError as exc:
        log.error("TMDb genre discover failed for genre=%s: %s", genre_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    if movie is None:
        await query.message.reply_text("😕 فیلمی در این ژانر پیدا نشد.")
        return

    await _send_movie(query, context, movie, telegram_id)


async def on_collection_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, kind, collection_id = query.data.split(":", 2)
    tmdb: TMDbClient = context.bot_data["tmdb"]
    telegram_id = query.from_user.id

    try:
        if kind == "company":
            movie = tmdb.get_random_by_company(collection_id)
        else:
            movie = tmdb.get_random_by_crew(collection_id)
    except TMDbError as exc:
        log.error("TMDb collection discover failed for %s=%s: %s", kind, collection_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    if movie is None:
        await query.message.reply_text("😕 فیلمی در این مجموعه پیدا نشد.")
        return

    await _send_movie(query, context, movie, telegram_id)


async def on_view_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]
    telegram_id = query.from_user.id

    try:
        movie = tmdb.get_movie_details(movie_id)
    except TMDbError as exc:
        log.error("Failed to fetch movie details for view id=%s: %s", movie_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    await _send_movie(query, context, movie, telegram_id)


async def on_view_tv_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tv_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        show = tmdb.get_tv_details(tv_id)
    except TMDbError as exc:
        log.error("Failed to fetch TV details for id=%s: %s", tv_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    await _send_tv_or_anime(query, context, show, "tv", refresh_action="tv", refresh_label="🔄 سریال دیگر")


async def on_person_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    person_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        person = tmdb.get_person_details(person_id)
    except TMDbError as exc:
        log.error("Failed to fetch person details for id=%s: %s", person_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    await _send_person(query, context, person)


async def on_page_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, list_type, page_str = query.data.split(":")
    page = int(page_str)
    context.user_data["list_type"] = list_type
    context.user_data["list_page"] = page
    await _show_paginated_list(query, context, query.from_user.id)


async def on_seasons_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tv_id = int(query.data.split(":", 1)[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        show = tmdb.get_tv_details(tv_id)
    except TMDbError as exc:
        log.error("Failed to fetch TV seasons for id=%s: %s", tv_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    seasons = show.get("seasons", [])
    if not seasons:
        await query.message.reply_text("😕 فصلی پیدا نشد.")
        return

    await query.message.reply_text(
        "📂 <b>فصل‌ها:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=seasons_keyboard(tv_id, seasons),
    )


async def on_season_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, tv_id_str, season_num_str = query.data.split(":")
    tv_id = int(tv_id_str)
    season_num = int(season_num_str)
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        show = tmdb.get_tv_details(tv_id)
        season = tmdb.get_tv_season(tv_id, season_num)
    except TMDbError as exc:
        log.error("Failed to fetch season %s for tv=%s: %s", season_num, tv_id, exc)
        await query.message.reply_text("⚠️ سرویس موقتاً در دسترس نیست.")
        return

    caption = format_season_caption(season, show.get("name", "TV Show"))
    image = poster_url(season)

    chat = query.effective_chat
    target_chat_id = chat.id if chat else query.message.chat_id

    if image:
        await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )
    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=back_to_menu_keyboard(),
        )


async def on_noop_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # The page-indicator button doesn't do anything — just clear Telegram's
    # loading spinner instead of leaving it spinning forever on tap.
    await update.callback_query.answer()


async def on_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db: Database = context.bot_data["db"]
    telegram_id = query.from_user.id
    action = query.data.split(":", 1)[1]

    if action == "daily":
        settings = db.get_user_settings(telegram_id)
        new_val = 0 if settings["daily_recommendation"] else 1
        db.update_user_settings(telegram_id, daily_recommendation=new_val)
        settings = db.get_user_settings(telegram_id)
        await query.message.reply_text(
            "⚙️ <b>Settings updated.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(settings["language"], settings["daily_recommendation"], settings["adult_filter"]),
        )
    elif action == "adult":
        settings = db.get_user_settings(telegram_id)
        new_val = 0 if settings["adult_filter"] else 1
        db.update_user_settings(telegram_id, adult_filter=new_val)
        settings = db.get_user_settings(telegram_id)
        await query.message.reply_text(
            "⚙️ <b>Settings updated.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(settings["language"], settings["daily_recommendation"], settings["adult_filter"]),
        )
    elif action == "history":
        db.clear_search_history(telegram_id)
        await query.answer("History cleared 🗑", show_alert=False)
        await query.message.reply_text("📜 تاریخچه جستجو پاک شد.", reply_markup=back_to_menu_keyboard())


# --- Text search handler ---

async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return

    query_text = update.message.text.strip()
    if not query_text:
        return

    telegram_id = update.effective_user.id
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    # Handle admin broadcast
    if context.user_data.get("awaiting_broadcast"):
        context.user_data["awaiting_broadcast"] = False
        config: Config = context.bot_data["config"]
        if config.admin_id is None or telegram_id != config.admin_id:
            await update.message.reply_text("⛔ Admin only.")
            return
        all_users = db.get_all_user_ids()
        sent = 0
        failed = 0
        for uid in all_users:
            try:
                await context.bot.send_message(chat_id=uid, text=query_text, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception as exc:
                log.warning("Broadcast failed for user %s: %s", uid, exc)
                failed += 1
            await asyncio.sleep(0.05)  # stay comfortably under Telegram's rate limits
        await update.message.reply_text(
            f"📢 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # Handle AI recommendation
    if context.user_data.get("awaiting_ai_recommendation"):
        context.user_data["awaiting_ai_recommendation"] = False
        gemini: Optional[GeminiClient] = context.bot_data.get("gemini")

        try:
            suggested_title = gemini.suggest_movie_title(query_text)
        except GeminiError as exc:
            log.error("Gemini suggestion failed: %s", exc)
            await update.message.reply_text(
                "⚠️ پیشنهاد هوشمند موقتاً در دسترس نیست، بعداً دوباره امتحان کن."
            )
            return

        try:
            data = tmdb.search_movies(suggested_title)
        except TMDbError as exc:
            log.error("TMDb search failed for AI suggestion='%s': %s", suggested_title, exc)
            await update.message.reply_text("⚠️ Search is temporarily unavailable. Please try again shortly.")
            return

        results = data.get("results", [])
        if not results:
            await update.message.reply_text(
                f"😕 برای پیشنهاد «{suggested_title}» چیزی روی TMDb پیدا نشد.",
                reply_markup=back_to_menu_keyboard(),
            )
            return

        await update.message.reply_text(
            f"🧠 <b>پیشنهاد هوشمند:</b> {_e(suggested_title)}", parse_mode=ParseMode.HTML
        )
        await _send_movie(update, context, results[0], telegram_id)
        return

    # Handle person search
    if context.user_data.get("awaiting_person_search"):
        context.user_data["awaiting_person_search"] = False
        try:
            data = tmdb.search_person(query_text)
        except TMDbError as exc:
            log.error("Person search failed for '%s': %s", query_text, exc)
            await update.message.reply_text("⚠️ Search is temporarily unavailable.")
            return
        people = data.get("results", [])
        if not people:
            await update.message.reply_text(
                f"😕 هنرمندی با نام «{query_text}» پیدا نشد.",
                reply_markup=back_to_menu_keyboard(),
            )
            return
        await update.message.reply_text(
            "🔍 <b>Search Results:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=person_results_keyboard(people),
        )
        return

    # Regular movie search
    db.add_search_history(telegram_id, query_text)
    user_settings = db.get_user_settings(telegram_id)
    allow_adult = not user_settings["adult_filter"] if user_settings else False

    try:
        data = tmdb.search_movies(query_text, include_adult=allow_adult)
    except TMDbError as exc:
        log.error("TMDb search failed for query='%s': %s", query_text, exc)
        await update.message.reply_text("⚠️ Search is temporarily unavailable. Please try again shortly.")
        return

    results = data.get("results", [])
    total_pages = data.get("total_pages", 1)

    if not results:
        await update.message.reply_text(
            f"😕 No results found for \"{query_text}\". Try a different title.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # Store search state for pagination (query stays server-side, never in
    # callback_data — see the note in search_results_keyboard).
    context.user_data["last_search_query"] = query_text

    if len(results) == 1:
        await _send_movie(update, context, results[0], telegram_id)
    else:
        await update.message.reply_text(
            f"🔍 <b>Results for \"{_e(query_text)}\":</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=search_results_keyboard(results[:6], prefix="view", page=1, total_pages=min(total_pages, 10)),
        )


async def on_search_page_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 1)[1])
    query_text = context.user_data.get("last_search_query")
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    if not query_text:
        await query.message.reply_text(
            "⚠️ این جستجو منقضی شده، دوباره یک اسم فیلم بفرست.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    user_settings = db.get_user_settings(query.from_user.id)
    allow_adult = not user_settings["adult_filter"] if user_settings else False

    try:
        data = tmdb.search_movies(query_text, page=page, include_adult=allow_adult)
    except TMDbError as exc:
        log.error("TMDb search page failed: %s", exc)
        await query.message.reply_text("⚠️ Search is temporarily unavailable.")
        return

    results = data.get("results", [])
    total_pages = data.get("total_pages", 1)

    if not results:
        await query.message.reply_text("😕 No more results.")
        return

    await query.message.reply_text(
        f"🔍 <b>Results for \"{_e(query_text)}\" (page {page}):</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=search_results_keyboard(results[:6], prefix="view", page=page, total_pages=min(total_pages, 10)),
    )


# --- Inline query handler ---

async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_query = update.inline_query.query.strip()
    if not raw_query:
        return

    tmdb: TMDbClient = context.bot_data["tmdb"]
    articles = []

    # The "Share" button on a movie card sends "id<movie_id>" as the inline
    # query — resolve that directly instead of treating the ID as a title
    # search (which would return nothing, or the wrong movie).
    if raw_query.startswith("id") and raw_query[2:].isdigit():
        movie_id = int(raw_query[2:])
        try:
            movie = tmdb.get_movie_details(movie_id)
        except TMDbError:
            movie = None
        if movie:
            title = movie.get("title") or "Untitled"
            year = (movie.get("release_date") or "----")[:4]
            overview = movie.get("overview") or "No description."
            if len(overview) > 200:
                overview = overview[:197] + "..."
            articles.append(
                InlineQueryResultArticle(
                    id=str(movie["id"]),
                    title=f"{title} ({year})",
                    input_message_content=InputTextMessageContent(
                        message_text=f"🎬 <b>{_e(title)}</b> ({year})\n⭐ {movie.get('vote_average', 0):.1f}/10\n\n{_e(overview)}",
                        parse_mode=ParseMode.HTML,
                    ),
                    description=overview,
                    thumbnail_url=poster_url(movie) or "",
                )
            )
        await update.inline_query.answer(articles, cache_time=300)
        return

    try:
        data = tmdb.search_movies(raw_query)
        results = data.get("results", [])[:10]
    except TMDbError:
        return

    for movie in results:
        title = movie.get("title") or "Untitled"
        year = (movie.get("release_date") or "----")[:4]
        overview = movie.get("overview") or "No description."
        if len(overview) > 200:
            overview = overview[:197] + "..."

        articles.append(
            InlineQueryResultArticle(
                id=str(movie["id"]),
                title=f"{title} ({year})",
                input_message_content=InputTextMessageContent(
                    message_text=f"🎬 <b>{_e(title)}</b> ({year})\n⭐ {movie.get('vote_average', 0):.1f}/10\n\n{_e(overview)}",
                    parse_mode=ParseMode.HTML,
                ),
                description=overview,
                thumbnail_url=poster_url(movie) or "",
            )
        )

    await update.inline_query.answer(articles, cache_time=300)


# --- Error handler ---

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception while processing update: %s", context.error, exc_info=context.error)


# =========================================================================
# 11. JOBS
# =========================================================================

async def daily_recommendation_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a daily random movie recommendation to subscribed users."""
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    users = db.get_users_with_daily_enabled()

    if not users:
        return

    movie = tmdb.get_random_movie()
    if movie is None:
        return

    try:
        details = tmdb.get_movie_details(movie["id"])
    except TMDbError:
        details = movie

    caption = format_movie_caption(details)
    image = poster_url(details)

    for uid in users:
        try:
            if image:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=image,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_to_menu_keyboard(),
                )
            else:
                await context.bot.send_message(
                    chat_id=uid,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_to_menu_keyboard(),
                )
        except Exception as exc:
            log.warning("Daily recommendation failed for user %s: %s", uid, exc)


# =========================================================================
# 12. APPLICATION BOOTSTRAP
# =========================================================================

def build_application(config: Config) -> Application:
    application = (
        Application.builder()
        .token(config.bot_token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    application.bot_data["config"] = config
    application.bot_data["db"] = Database(DB_PATH)
    application.bot_data["tmdb"] = TMDbClient(config.tmdb_api_key)
    application.bot_data["gemini"] = (
        GeminiClient(config.gemini_api_key) if config.gemini_api_key else None
    )

    # Commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("mystats", cmd_mystats))
    application.add_handler(CommandHandler("export", cmd_export))

    # Callbacks
    application.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(on_favorite_button, pattern=r"^(fav|unfav):"))
    application.add_handler(CallbackQueryHandler(on_watchlist_button, pattern=r"^(wl|unwl):"))
    application.add_handler(CallbackQueryHandler(on_rating_menu, pattern=r"^ratemenu:"))
    application.add_handler(CallbackQueryHandler(on_rating_button, pattern=r"^rate:"))
    application.add_handler(CallbackQueryHandler(on_similar_button, pattern=r"^similar:"))
    application.add_handler(CallbackQueryHandler(on_recommendation_button, pattern=r"^rec:"))
    application.add_handler(CallbackQueryHandler(on_reviews_button, pattern=r"^reviews:"))
    application.add_handler(CallbackQueryHandler(on_genre_button, pattern=r"^genre:"))
    application.add_handler(CallbackQueryHandler(on_collection_button, pattern=r"^collection:"))
    application.add_handler(CallbackQueryHandler(on_view_button, pattern=r"^view:"))
    application.add_handler(CallbackQueryHandler(on_view_tv_button, pattern=r"^viewtv:"))
    application.add_handler(CallbackQueryHandler(on_person_button, pattern=r"^person:"))
    application.add_handler(CallbackQueryHandler(on_page_button, pattern=r"^page:"))
    application.add_handler(CallbackQueryHandler(on_search_page_button, pattern=r"^searchpage:"))
    application.add_handler(CallbackQueryHandler(on_seasons_button, pattern=r"^seasons:"))
    application.add_handler(CallbackQueryHandler(on_season_button, pattern=r"^season:"))
    application.add_handler(CallbackQueryHandler(on_noop_button, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(on_settings_toggle, pattern=r"^(toggle|clear):"))

    # Inline
    application.add_handler(InlineQueryHandler(on_inline_query))

    # Messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))

    # Errors
    application.add_error_handler(on_error)

    # Jobs (only if the job-queue extra / APScheduler is installed; otherwise
    # this is silently skipped instead of crashing at startup)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(
            daily_recommendation_job,
            interval=86400,
            first=3600,
            name="daily_recommendation",
        )
    else:
        log.warning(
            "JobQueue not available (install python-telegram-bot[job-queue]) "
            "— daily recommendations are disabled."
        )

    return application


def main() -> None:
    try:
        config = Config.load()
    except RuntimeError as exc:
        log.critical("Startup failed: %s", exc)
        sys.exit(1)

    log.info("Starting MovieBot v%s...", __version__)
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
