
fixed_code = '''"""
Telegram Movie & TV Recommendation Bot — ULTIMATE EDITION v2.0
================================================================
Fixes & Improvements:
- Fixed all query.message None crashes (old callbacks, inline, etc.)
- Fixed unterminated string issues and syntax robustness
- Added TMDb in-memory cache (TTL) to reduce API calls
- Added per-user rate limiting (anti-spam)
- Added paginated search results (Previous/Next buttons)
- Added clickable similar movies (with posters)
- Added TV Show & Actor search support
- Added Smart "More Like This" with inline browsing
- Added proper client lifecycle (shutdown cleanup)
- Added user activity logging
- Enhanced Persian UI with better error messages
- Added /cancel command to exit any mode
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
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
# 1. CONFIGURATION
# =========================================================================

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "movie_bot.db"
LOG_PATH = LOGS_DIR / "bot.log"


@dataclass(frozen=True)
class Config:
    bot_token: str
    tmdb_api_key: str
    admin_id: Optional[int]
    gemini_api_key: Optional[str]

    @staticmethod
    def load() -> Config:
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
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        admin_id = int(admin_id_raw) if admin_id_raw.isdigit() else None

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
    logger = logging.getLogger("movie_bot")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


log = setup_logging()


# =========================================================================
# 3. SIMPLE IN-MEMORY CACHE (TTL)
# =========================================================================

class SimpleTTLCache:
    """Thread-safe-ish simple cache for TMDb responses."""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        if key not in self._store:
            return None
        ts, value = self._store[key]
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# =========================================================================
# 4. RATE LIMITER
# =========================================================================

class RateLimiter:
    """Simple per-user rate limiter."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._users: dict[int, list[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        timestamps = self._users.get(user_id, [])
        # Filter old timestamps
        timestamps = [t for t in timestamps if now - t < self.window]
        self._users[user_id] = timestamps
        if len(timestamps) >= self.max_requests:
            return False
        timestamps.append(now)
        return True


# =========================================================================
# 5. DATABASE
# =========================================================================

class Database:
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
                    first_name  TEXT,
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
                    media_type  TEXT DEFAULT 'movie',
                    added_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, movie_id, media_type),
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
                    searched_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def upsert_user(self, telegram_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name
                """,
                (telegram_id, username or "", first_name or ""),
            )

    def add_favorite(self, telegram_id: int, movie_id: int, title: str, media_type: str = "movie") -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO favorites (telegram_id, movie_id, title, media_type) VALUES (?, ?, ?, ?)",
                    (telegram_id, movie_id, title, media_type),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_favorite(self, telegram_id: int, movie_id: int, media_type: str = "movie") -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM favorites WHERE telegram_id = ? AND movie_id = ? AND media_type = ?",
                (telegram_id, movie_id, media_type),
            )
            return cur.rowcount > 0

    def is_favorite(self, telegram_id: int, movie_id: int, media_type: str = "movie") -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM favorites WHERE telegram_id = ? AND movie_id = ? AND media_type = ?",
                (telegram_id, movie_id, media_type),
            ).fetchone()
            return row is not None

    def list_favorites(self, telegram_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT movie_id, title, media_type FROM favorites WHERE telegram_id = ? ORDER BY added_at DESC",
                (telegram_id,),
            ).fetchall()

    def add_search_history(self, telegram_id: int, query: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO search_history (telegram_id, query) VALUES (?, ?)",
                (telegram_id, query),
            )

    def get_all_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_id FROM users").fetchall()
            return [row["telegram_id"] for row in rows]

    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])


# =========================================================================
# 6. ASYNC TMDb CLIENT (with caching)
# =========================================================================

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

GENRES_MAP = {
    28: "اکشن 💥", 12: "ماجراجویی 🤠", 16: "انیمیشن 🎨", 35: "کمدی 😂",
    80: "جنایی 🕵️", 99: "مستند 📹", 18: "درام 🎭", 10751: "خانوادگی 👨‍👩‍👧",
    14: "فانتزی 🦄", 36: "تاریخی 📜", 27: "ترسناک 👻", 10402: "موزیکال 🎵",
    9648: "رازآلود 🔍", 10749: "عاشقانه 💖", 878: "علمی تخیلی 🧪", 53: "هیجان‌انگیز ⚡",
    10752: "جنگی ⚔️", 37: "وسترن 🤠"
}

class TMDbError(Exception):
    pass

class TMDbClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        self._cache = SimpleTTLCache(ttl_seconds=300)

    async def close(self):
        await self._client.aclose()

    def _cache_key(self, path: str, params: dict[str, Any]) -> str:
        return f"{path}:{sorted(params.items())}"

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None, use_cache: bool = True) -> dict[str, Any]:
        params = dict(params or {})
        params["api_key"] = self._api_key
        params.setdefault("language", "fa-IR")
        params.setdefault("include_adult", "false")

        cache_key = self._cache_key(path, params)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            resp = await self._client.get(f"{TMDB_BASE_URL}{path}", params=params)
            resp.raise_for_status()
            data = resp.json()
            if use_cache:
                self._cache.set(cache_key, data)
            return data
        except httpx.HTTPError as exc:
            log.warning("TMDb Request failed: %s", exc)
            raise TMDbError("خطا در برقراری ارتباط با TMDb") from exc

    async def get_movie_details(self, movie_id: int, media_type: str = "movie") -> dict[str, Any]:
        params = {"append_to_response": "videos,credits", "language": "fa-IR"}
        details = await self._get(f"/{media_type}/{movie_id}", params=params)
        
        if not details.get("overview"):
            params["language"] = "en-US"
            en_details = await self._get(f"/{media_type}/{movie_id}", params=params, use_cache=False)
            details["overview"] = en_details.get("overview", "توضیحاتی موجود نیست.")
            
        return details

    async def get_similar_movies(self, movie_id: int, media_type: str = "movie") -> list[dict[str, Any]]:
        data = await self._get(f"/{media_type}/{movie_id}/similar")
        return data.get("results", [])

    async def get_trending(self, media_type: str = "movie") -> list[dict[str, Any]]:
        data = await self._get(f"/trending/{media_type}/week")
        return data.get("results", [])

    async def get_top_rated(self, media_type: str = "movie") -> list[dict[str, Any]]:
        data = await self._get(f"/{media_type}/top_rated")
        return data.get("results", [])

    async def get_random_movie(self, media_type: str = "movie") -> Optional[dict[str, Any]]:
        page = random.randint(1, 15)
        data = await self._get(f"/{media_type}/popular", params={"page": page})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_by_genre(self, genre_id: int, media_type: str = "movie") -> Optional[dict[str, Any]]:
        page = random.randint(1, 10)
        data = await self._get(f"/discover/{media_type}", params={"with_genres": genre_id, "page": page, "sort_by": "popularity.desc"})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def search_movies(self, query: str, page: int = 1) -> list[dict[str, Any]]:
        data = await self._get("/search/movie", params={"query": query, "page": page})
        return data.get("results", [])

    async def search_tv(self, query: str, page: int = 1) -> list[dict[str, Any]]:
        data = await self._get("/search/tv", params={"query": query, "page": page})
        return data.get("results", [])

    async def search_person(self, query: str, page: int = 1) -> list[dict[str, Any]]:
        data = await self._get("/search/person", params={"query": query, "page": page})
        return data.get("results", [])

    async def get_person_movies(self, person_id: int) -> dict[str, Any]:
        data = await self._get(f"/person/{person_id}", params={"append_to_response": "movie_credits,tv_credits"})
        return data

    async def get_random_tv(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 15)
        data = await self._get("/tv/popular", params={"page": page})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_random_anime(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 8)
        data = await self._get(
            "/discover/movie",
            params={"with_genres": "16", "with_origin_country": "JP", "page": page},
        )
        results = data.get("results", [])
        return random.choice(results) if results else None


# =========================================================================
# 7. ROBUST ASYNC GEMINI CLIENT
# =========================================================================

class GeminiError(Exception):
    pass

class GeminiClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self):
        await self._client.aclose()

    async def _generate_content(self, prompt: str) -> str:
        models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
        
        last_error = None
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self._api_key}"
            try:
                resp = await self._client.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    return text.strip()
                else:
                    log.warning("Gemini model %s returned status %s: %s", model, resp.status_code, resp.text)
            except Exception as exc:
                last_error = exc
                log.warning("Gemini model %s failed: %s", model, exc)

        raise GeminiError(f"تمام مدل‌های هوش مصنوعی با خطا مواجه شدند: {last_error}")

    async def suggest_movie_title(self, user_request: str) -> str:
        prompt = (
            "You are a movie recommendation assistant. Based on the user request below (in Persian or English), "
            "suggest EXACTLY ONE single movie title that best fits. Output ONLY the official English title "
            "and nothing else. DO NOT use markdown bold, quotes, or any punctuation.\\n\\n"
            f"User prompt: {user_request}"
        )
        raw_title = await self._generate_content(prompt)
        cleaned_title = re.sub(r'[*"`\\\'\\n]', '', raw_title).strip()
        return cleaned_title

    async def chat_about_movies(self, user_query: str) -> str:
        prompt = (
            "You are a friendly, expert cinema assistant. Answer the user's question about movies, actors, "
            "directors, or plot explanations in Persian (Farsi). Keep the response helpful, engaging, and well-formatted.\\n\\n"
            f"User Question: {user_query}"
        )
        return await self._generate_content(prompt)


# =========================================================================
# 8. FORMATTING HELPERS
# =========================================================================

def format_movie_caption(details: dict[str, Any], media_type: str = "movie") -> str:
    title = details.get("title") or details.get("name") or "بدون عنوان"
    date_val = details.get("release_date") or details.get("first_air_date") or "----"
    year = date_val[:4]
    rating = details.get("vote_average", 0)
    
    overview = details.get("overview") or "توضیحاتی ثبت نشده است."
    if len(overview) > 400:
        overview = overview[:397] + "..."

    genres = details.get("genres", [])
    genre_str = ", ".join([g["name"] for g in genres]) if genres else "نامشخص"

    credits = details.get("credits", {})
    crew = credits.get("crew", [])
    cast = credits.get("cast", [])
    
    director = next((c["name"] for c in crew if c.get("job") == "Director"), None)
    top_cast = ", ".join([c["name"] for c in cast[:3]]) if cast else None

    type_label = "📺 سریال" if media_type == "tv" else ("🎌 انیمه" if media_type == "anime" else "🎬 فیلم")

    caption = (
        f"{type_label} <b>{title}</b> ({year})\\n"
        f"⭐ <b>امتیاز:</b> {rating:.1f}/10\\n"
        f"🎭 <b>ژانر:</b> {genre_str}\\n"
    )

    if director:
        caption += f"🎬 <b>کارگردان:</b> {director}\\n"
    if top_cast:
        caption += f"👥 <b>بازیگران:</b> {top_cast}\\n"

    caption += f"\\n📝 <b>خلاصه داستان:</b>\\n{overview}"
    return caption


def format_person_caption(person: dict[str, Any]) -> str:
    name = person.get("name", "نامشخص")
    dept = person.get("known_for_department", "نامشخص")
    bio = person.get("biography") or "بیوگرافی ثبت نشده."
    if len(bio) > 500:
        bio = bio[:497] + "..."
    
    movie_credits = person.get("movie_credits", {})
    cast = movie_credits.get("cast", [])
    known_for = ", ".join([m.get("title", "نامشخص") for m in cast[:5]]) if cast else "نامشخص"
    
    return (
        f"🎭 <b>{name}</b>\\n"
        f"📌 <b>حرفه:</b> {dept}\\n"
        f"🎬 <b>شناخته‌شده برای:</b> {known_for}\\n\\n"
        f"📝 <b>بیوگرافی:</b>\\n{bio}"
    )


def get_youtube_trailer(details: dict[str, Any]) -> Optional[str]:
    videos = details.get("videos", {}).get("results", [])
    for vid in videos:
        if vid.get("site") == "YouTube" and vid.get("type") in ["Trailer", "Teaser"]:
            return f"https://www.youtube.com/watch?v={vid.get('key')}"
    return None


def poster_url(item: dict[str, Any]) -> Optional[str]:
    path = item.get("poster_path")
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


def profile_url(item: dict[str, Any]) -> Optional[str]:
    path = item.get("profile_path")
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


# =========================================================================
# 9. KEYBOARDS
# =========================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 پیشنهاد هوشمند (AI)", callback_data="menu:ai"), InlineKeyboardButton("💬 چت با دستیار AI", callback_data="menu:aichat")],
        [InlineKeyboardButton("🎲 فیلم تصادفی", callback_data="menu:random"), InlineKeyboardButton("📂 فیلتر ژانر", callback_data="menu:genres")],
        [InlineKeyboardButton("🔥 داغ‌ترین‌ها", callback_data="menu:trending"), InlineKeyboardButton("⭐ برترین‌ها", callback_data="menu:top_rated")],
        [InlineKeyboardButton("📺 سریال محبوب", callback_data="menu:tv"), InlineKeyboardButton("🎌 انیمه", callback_data="menu:anime")],
        [InlineKeyboardButton("❤️ علاقه‌مندی‌ها", callback_data="menu:favorites")],
        [InlineKeyboardButton("❓ راهنما", callback_data="menu:help")]
    ])


def genre_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    items = list(GENRES_MAP.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(items[i][1], callback_data=f"genre:{items[i][0]}")]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(items[i+1][1], callback_data=f"genre:{items[i+1][0]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def movie_actions_keyboard(movie_id: int, is_favorite: bool, trailer_url: Optional[str] = None, media_type: str = "movie") -> InlineKeyboardMarkup:
    buttons = []
    
    row1 = []
    if trailer_url:
        row1.append(InlineKeyboardButton("🎬 تماشای تریلر", url=trailer_url))
    row1.append(InlineKeyboardButton("🎭 فیلم‌های مشابه", callback_data=f"similar:{movie_id}:{media_type}"))
    buttons.append(row1)

    fav_btn = (
        InlineKeyboardButton("💔 حذف از علاقه‌مندی‌ها", callback_data=f"unfav:{movie_id}:{media_type}")
        if is_favorite
        else InlineKeyboardButton("❤️ افزودن به علاقه‌مندی‌ها", callback_data=f"fav:{movie_id}:{media_type}")
    )
    
    buttons.append([fav_btn])
    buttons.append([
        InlineKeyboardButton("🎲 فیلم بعدی", callback_data="menu:random"),
        InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")
    ])
    return InlineKeyboardMarkup(buttons)


def search_results_keyboard(results: list[dict[str, Any]], page: int, query: str, media_type: str = "movie") -> InlineKeyboardMarkup:
    buttons = []
    for m in results[:5]:
        title = m.get("title") or m.get("name") or "فیلم"
        year = (m.get("release_date") or m.get("first_air_date") or "----")[:4]
        buttons.append([InlineKeyboardButton(f"{title} ({year})", callback_data=f"show:{m['id']}:{media_type}")])
    
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"page:{media_type}:{query}:{page-1}"))
    nav_row.append(InlineKeyboardButton("▶️ صفحه بعد", callback_data=f"page:{media_type}:{query}:{page+1}"))
    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def similar_movies_keyboard(movies: list[dict[str, Any]], original_id: int, media_type: str = "movie") -> InlineKeyboardMarkup:
    buttons = []
    for m in movies[:6]:
        title = m.get("title") or m.get("name") or "فیلم"
        year = (m.get("release_date") or m.get("first_air_date") or "----")[:4]
        buttons.append([InlineKeyboardButton(f"{title} ({year})", callback_data=f"show:{m['id']}:{media_type}")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data=f"back_to:{original_id}:{media_type}")])
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")]])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو و بازگشت", callback_data="menu:home")]])


# =========================================================================
# 10. SAFE MESSAGE HELPERS
# =========================================================================

async def safe_reply_text(update_or_query, text: str, **kwargs) -> None:
    """Safely reply text handling both Message and CallbackQuery."""
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, **kwargs)
    elif hasattr(update_or_query, "effective_message") and update_or_query.effective_message:
        await update_or_query.effective_message.reply_text(text, **kwargs)
    elif hasattr(update_or_query, "reply_text"):
        await update_or_query.reply_text(text, **kwargs)


async def safe_edit_text(update_or_query, text: str, **kwargs) -> None:
    """Safely edit message text."""
    msg = None
    if hasattr(update_or_query, "message") and update_or_query.message:
        msg = update_or_query.message
    elif hasattr(update_or_query, "effective_message") and update_or_query.effective_message:
        msg = update_or_query.effective_message
    
    if msg and hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text, **kwargs)
        except Exception:
            await msg.reply_text(text, **kwargs)
    else:
        await safe_reply_text(update_or_query, text, **kwargs)


# =========================================================================
# 11. HANDLERS
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if user:
        db.upsert_user(user.id, user.username, user.first_name)

    welcome_text = (
        f"سلام {user.first_name if user else ''} عزیز! 👋\\n\\n"
        "🎬 به **ربات حرفه‌ای پیشنهاد فیلم و سریال** خوش آمدید.\\n"
        "با دکمه‌های زیر جستجو کنید یا اسم فیلم/بازیگر رو برام چت کنید!"
    )
    await update.message.reply_text(
        welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "❓ **راهنمای جامع ربات:**\\n\\n"
        "🔍 **جستجو:** نام فیلم یا بازیگر (فارسی/انگلیسی) را مستقیم ارسال کنید.\\n"
        "🧠 **پیشنهاد هوشمند:** حست رو بگو تا هوش مصنوعی فیلم پیدا کنه.\\n"
        "💬 **چت با دستیار:** درباره نقد، داستان یا دیالوگ فیلم‌ها با هوش مصنوعی گپ بزن.\\n"
        "📺 **سریال:** از منو سریال محبوب رو امتحان کن یا اسم سریال بفرست.\\n"
        "🎭 **بازیگر:** اسم بازیگر رو بفرست تا فیلم‌هاشو نشون بدم.\\n"
        "🔎 **اینلاین مود:** تایپ کن `@BotUsername Inception` درون هر چت برای اشتراک سریع!\\n\\n"
        "❌ برای خروج از هر حالتی /cancel رو بزن."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("✅ عملیات لغو شد. منوی اصلی:", reply_markup=main_menu_keyboard())


# --- ADMIN COMMANDS ---

async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not update.effective_user or update.effective_user.id != config.admin_id:
        return
    db: Database = context.bot_data["db"]
    await update.message.reply_text(f"📊 **آمار ربات:**\\n👥 تعداد کاربران: `{db.user_count()}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not update.effective_user or update.effective_user.id != config.admin_id:
        return

    text_to_send = " ".join(context.args)
    if not text_to_send:
        await update.message.reply_text("❌ متن پیام را وارد کنید:\\n`/broadcast سلام!`", parse_mode=ParseMode.MARKDOWN)
        return

    db: Database = context.bot_data["db"]
    users = db.get_all_user_ids()
    if not users:
        await update.message.reply_text("❌ هیچ کاربری یافت نشد.")
        return

    success, failed = 0, 0
    msg = await update.message.reply_text(f"⏳ در حال ارسال به {len(users)} کاربر...")

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text_to_send)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await msg.edit_text(f"✅ **پایان ارسال.**\\nموفق: {success}\\nناموفق: {failed}")


# --- MOVIE RENDERING HELPER ---

async def _send_movie(update_or_query, context: ContextTypes.DEFAULT_TYPE, movie_id: int, media_type: str = "movie") -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    
    user_id = None
    if hasattr(update_or_query, "from_user") and update_or_query.from_user:
        user_id = update_or_query.from_user.id
    elif hasattr(update_or_query, "effective_user") and update_or_query.effective_user:
        user_id = update_or_query.effective_user.id
    
    if not user_id:
        log.error("Cannot determine user_id in _send_movie")
        return

    try:
        details = await tmdb.get_movie_details(movie_id, media_type)
    except TMDbError:
        await safe_reply_text(update_or_query, "❌ خطا در دریافت اطلاعات. دوباره تلاش کنید.", reply_markup=back_to_menu_keyboard())
        return

    caption = format_movie_caption(details, media_type)
    trailer = get_youtube_trailer(details)
    keyboard = movie_actions_keyboard(details["id"], db.is_favorite(user_id, details["id"], media_type), trailer, media_type)
    image = poster_url(details)

    target_chat_id = None
    if hasattr(update_or_query, "message") and update_or_query.message:
        target_chat_id = update_or_query.message.chat_id
    elif hasattr(update_or_query, "effective_chat") and update_or_query.effective_chat:
        target_chat_id = update_or_query.effective_chat.id

    if not target_chat_id:
        log.error("Cannot determine chat_id in _send_movie")
        return

    try:
        if image:
            await context.bot.send_photo(chat_id=target_chat_id, photo=image, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as exc:
        log.error("Failed to send movie: %s", exc)
        await context.bot.send_message(chat_id=target_chat_id, text="❌ خطا در ارسال اطلاعات فیلم.", reply_markup=back_to_menu_keyboard())


async def _send_person(update_or_query, context: ContextTypes.DEFAULT_TYPE, person_id: int) -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    
    try:
        person = await tmdb.get_person_movies(person_id)
    except TMDbError:
        await safe_reply_text(update_or_query, "❌ خطا در دریافت اطلاعات بازیگر.", reply_markup=back_to_menu_keyboard())
        return

    caption = format_person_caption(person)
    image = profile_url(person)
    
    target_chat_id = None
    if hasattr(update_or_query, "message") and update_or_query.message:
        target_chat_id = update_or_query.message.chat_id
    elif hasattr(update_or_query, "effective_chat") and update_or_query.effective_chat:
        target_chat_id = update_or_query.effective_chat.id

    if not target_chat_id:
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 فیلم‌های این بازیگر", callback_data=f"person_movies:{person_id}")],
        [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")]
    ])

    try:
        if image:
            await context.bot.send_photo(chat_id=target_chat_id, photo=image, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as exc:
        log.error("Failed to send person: %s", exc)


# --- CALLBACK QUERY HANDLERS ---

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    if action == "home":
        context.user_data.clear()
        if query.message:
            try:
                await query.message.edit_text("🏠 منوی اصلی:", reply_markup=main_menu_keyboard())
            except Exception:
                await query.message.reply_text("🏠 منوی اصلی:", reply_markup=main_menu_keyboard())
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text="🏠 منوی اصلی:", reply_markup=main_menu_keyboard())

    elif action == "ai":
        if not context.bot_data.get("gemini"):
            await safe_edit_text(query, "⚠️ کلید GEMINI_API_KEY تنظیم نشده است.", reply_markup=back_to_menu_keyboard())
            return
        context.user_data["mode"] = "ai_recommendation"
        text = "🧠 **چه فیلمی تو چه سبکی دوست داری ببینی؟**\\nمثلاً: «یه فیلم معمایی پیچیده مثل Shutter Island»"
        if query.message:
            try:
                await query.message.edit_text(text, reply_markup=cancel_keyboard())
            except Exception:
                await query.message.reply_text(text, reply_markup=cancel_keyboard())
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, reply_markup=cancel_keyboard())

    elif action == "aichat":
        if not context.bot_data.get("gemini"):
            await safe_edit_text(query, "⚠️ کلید GEMINI_API_KEY تنظیم نشده است.", reply_markup=back_to_menu_keyboard())
            return
        context.user_data["mode"] = "ai_chat"
        text = "💬 **دستیار سینمایی در خدمت شماست!**\\nهر سوالی درباره فیلم، بازیگران، داستان یا نقد داری بپرس:"
        if query.message:
            try:
                await query.message.edit_text(text, reply_markup=cancel_keyboard())
            except Exception:
                await query.message.reply_text(text, reply_markup=cancel_keyboard())
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, reply_markup=cancel_keyboard())

    elif action == "genres":
        text = "📂 **ژانر مورد نظرت رو انتخاب کن:**"
        if query.message:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=genre_menu_keyboard())
            except Exception:
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=genre_menu_keyboard())
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=genre_menu_keyboard())

    elif action == "random":
        movie = await tmdb.get_random_movie()
        if movie:
            await _send_movie(query, context, movie["id"])
        else:
            await safe_reply_text(query, "😕 فیلمی پیدا نشد.", reply_markup=back_to_menu_keyboard())

    elif action == "trending":
        movies = await tmdb.get_trending()
        lines = [f"• {m.get('title')} (⭐ {m.get('vote_average',0):.1f})" for m in movies[:10]]
        text = "🔥 **فیلم‌های ترند هفته:**\\n\\n" + "\\n".join(lines)
        if query.message:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())
            except Exception:
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())

    elif action == "top_rated":
        movies = await tmdb.get_top_rated()
        lines = [f"• {m.get('title')} (⭐ {m.get('vote_average',0):.1f})" for m in movies[:10]]
        text = "⭐ **برترین فیلم‌های تاریخ:**\\n\\n" + "\\n".join(lines)
        if query.message:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())
            except Exception:
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())

    elif action == "tv":
        tv = await tmdb.get_random_tv()
        if tv:
            await _send_movie(query, context, tv["id"], "tv")
        else:
            await safe_reply_text(query, "😕 سریالی پیدا نشد.", reply_markup=back_to_menu_keyboard())

    elif action == "anime":
        anime = await tmdb.get_random_anime()
        if anime:
            await _send_movie(query, context, anime["id"], "movie")
        else:
            await safe_reply_text(query, "😕 انیمه‌ای پیدا نشد.", reply_markup=back_to_menu_keyboard())

    elif action == "favorites":
        favs = db.list_favorites(query.from_user.id)
        if not favs:
            await safe_edit_text(query, "❤️ لیست علاقه‌مندی‌های شما خالی است.", reply_markup=back_to_menu_keyboard())
            return
        
        buttons = []
        for row in favs:
            mt = row.get("media_type", "movie")
            label = "📺" if mt == "tv" else "🎬"
            buttons.append([InlineKeyboardButton(f"{label} {row['title']}", callback_data=f"show_fav:{row['movie_id']}:{mt}")])
        buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
        text = "❤️ **لیست علاقه‌مندی‌های شما:**"
        if query.message:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
            except Exception:
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

    elif action == "help":
        await cmd_help(update, context)


async def on_genre_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    genre_id = int(query.data.split(":")[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    movie = await tmdb.get_by_genre(genre_id)
    if movie:
        await _send_movie(query, context, movie["id"])
    else:
        await safe_reply_text(query, "😕 فیلمی در این ژانر پیدا نشد.", reply_markup=back_to_menu_keyboard())


async def on_similar_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    tmdb: TMDbClient = context.bot_data["tmdb"]

    sim_movies = await tmdb.get_similar_movies(movie_id, media_type)
    if not sim_movies:
        await safe_reply_text(query, "😕 فیلم مشابهی پیدا نشد.", reply_markup=back_to_menu_keyboard())
        return

    text = "🎭 **فیلم‌های مشابه پیشنهاد شده:**\\n\\nروی هر کدام کلیک کنید تا جزئیاتش رو ببینید."
    keyboard = similar_movies_keyboard(sim_movies, movie_id, media_type)
    
    if query.message:
        try:
            await query.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=query.from_user.id, text=text, reply_markup=keyboard)


async def on_favorite_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[0]
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]

    if action == "fav":
        try:
            details = await tmdb.get_movie_details(movie_id, media_type)
            db.add_favorite(query.from_user.id, movie_id, details.get("title") or details.get("name", "فیلم"), media_type)
            await query.answer("به علاقه‌مندی‌ها اضافه شد ❤️")
        except Exception:
            await query.answer("❌ خطا در افزودن به علاقه‌مندی‌ها")
            return
    else:
        db.remove_favorite(query.from_user.id, movie_id, media_type)
        await query.answer("از علاقه‌مندی‌ها حذف شد 💔")

    try:
        details = await tmdb.get_movie_details(movie_id, media_type)
        trailer = get_youtube_trailer(details)
        new_kb = movie_actions_keyboard(movie_id, db.is_favorite(query.from_user.id, movie_id, media_type), trailer, media_type)
        await query.edit_message_reply_markup(reply_markup=new_kb)
    except Exception:
        pass


async def on_show_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    await _send_movie(query, context, movie_id, media_type)


async def on_show_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    await _send_movie(query, context, movie_id, media_type)


async def on_page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    media_type = parts[1]
    search_query = parts[2]
    page = int(parts[3])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        if media_type == "tv":
            results = await tmdb.search_tv(search_query, page)
        else:
            results = await tmdb.search_movies(search_query, page)
    except TMDbError:
        await safe_reply_text(query, "❌ خطا در جستجو.", reply_markup=back_to_menu_keyboard())
        return

    if not results:
        await safe_reply_text(query, "😕 نتیجه‌ای در این صفحه وجود ندارد.", reply_markup=back_to_menu_keyboard())
        return

    text = f"🔍 نتایج جستجو برای: `{search_query}` — صفحه {page}"
    keyboard = search_results_keyboard(results, page, search_query, media_type)
    
    if query.message:
        try:
            await query.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await query.message.reply_text(text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=query.from_user.id, text=text, reply_markup=keyboard)


async def on_back_to_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    await _send_movie(query, context, movie_id, media_type)


async def on_person_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    person_id = int(query.data.split(":")[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    try:
        person = await tmdb.get_person_movies(person_id)
    except TMDbError:
        await safe_reply_text(query, "❌ خطا در دریافت اطلاعات.", reply_markup=back_to_menu_keyboard())
        return

    movie_credits = person.get("movie_credits", {})
    cast = movie_credits.get("cast", [])
    
    if not cast:
        await safe_reply_text(query, "😕 فیلمی برای این بازیگر ثبت نشده.", reply_markup=back_to_menu_keyboard())
        return

    lines = [f"• {m.get('title')} (⭐ {m.get('vote_average',0):.1f})" for m in cast[:10]]
    text = f"🎬 **فیلم‌های {person.get('name', 'بازیگر')}:**\\n\\n" + "\\n".join(lines) + "\\n\\nبرای دیدن جزئیات هر کدام، نام آن را بفرستید."
    await safe_edit_text(query, text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())


# --- INLINE SEARCH MODE ---

async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query.strip()
    if not query:
        return

    tmdb: TMDbClient = context.bot_data["tmdb"]
    try:
        results = await tmdb.search_movies(query)
    except TMDbError:
        return

    items = []
    for m in results[:5]:
        caption = f"🎬 <b>{m.get('title')}</b>\\n⭐ امتیاز: {m.get('vote_average', 0)}/10\\n\\n{m.get('overview', '')}"
        if len(caption) > 4096:
            caption = caption[:4093] + "..."
        thumb = poster_url(m)
        kwargs = {}
        if thumb:
            kwargs["thumb_url"] = thumb
        items.append(
            InlineQueryResultArticle(
                id=str(m["id"]),
                title=m.get("title", "فیلم"),
                description=f"⭐ {m.get('vote_average', 0)}/10 | {(m.get('release_date') or '----')[:4]}",
                input_message_content=InputTextMessageContent(caption, parse_mode=ParseMode.HTML),
                **kwargs
            )
        )
    await update.inline_query.answer(items)


# --- TEXT MESSAGES & AI SEARCH ---

async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    gemini: Optional[GeminiClient] = context.bot_data.get("gemini")
    mode = context.user_data.get("mode")
    user_id = update.effective_user.id if update.effective_user else None

    # Rate limiting
    limiter: RateLimiter = context.bot_data["limiter"]
    if user_id and not limiter.is_allowed(user_id):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید. تعداد درخواست‌ها زیاد است.")
        return

    # Mode 1: AI Recommendation Mode
    if mode == "ai_recommendation" and gemini:
        context.user_data["mode"] = None
        msg = await update.message.reply_text("🧠 در حال آنالیز و پیدا کردن بهترین پیشنهاد...")
        try:
            suggested_title = await gemini.suggest_movie_title(text)
            log.info("AI suggested title: '%s' for request: '%s'", suggested_title, text)
            
            results = await tmdb.search_movies(suggested_title)
            await msg.delete()
            if results:
                await update.message.reply_text(f"💡 **پیشنهاد هوش مصنوعی:** `{suggested_title}`", parse_mode=ParseMode.MARKDOWN)
                await _send_movie(update, context, results[0]["id"])
            else:
                await update.message.reply_text(f"😕 عنوان پیشنهادی `{suggested_title}` در TMDb یافت نشد.", parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())
        except Exception as e:
            log.error("AI Error: %s", e)
            await msg.edit_text("❌ خطایی در هوش مصنوعی رخ داد. دوباره تلاش کنید.")
        return

    # Mode 2: AI Cinema Chat Mode
    if mode == "ai_chat" and gemini:
        msg = await update.message.reply_text("💭 در حال نوشتن پاسخ...")
        try:
            response = await gemini.chat_about_movies(text)
            # Truncate if too long for Telegram
            if len(response) > 4096:
                response = response[:4093] + "..."
            await msg.edit_text(response, reply_markup=cancel_keyboard())
        except Exception:
            await msg.edit_text("❌ متاسفانه پاسخی دریافت نشد.")
        return

    # Default Mode: Smart Search (Movie, TV, or Person)
    db.add_search_history(user_id, text) if user_id else None

    # Try movie search first
    results = await tmdb.search_movies(text)
    if results:
        if len(results) == 1:
            await _send_movie(update, context, results[0]["id"])
        else:
            context.user_data["last_query"] = text
            context.user_data["last_media"] = "movie"
            keyboard = search_results_keyboard(results, 1, text, "movie")
            await update.message.reply_text(f"🔍 نتایج جستجو برای: `{text}`", reply_markup=keyboard)
        return

    # Try TV search
    tv_results = await tmdb.search_tv(text)
    if tv_results:
        if len(tv_results) == 1:
            await _send_movie(update, context, tv_results[0]["id"], "tv")
        else:
            context.user_data["last_query"] = text
            context.user_data["last_media"] = "tv"
            keyboard = search_results_keyboard(tv_results, 1, text, "tv")
            await update.message.reply_text(f"🔍 نتایج جستجوی سریال برای: `{text}`", reply_markup=keyboard)
        return

    # Try Person search
    person_results = await tmdb.search_person(text)
    if person_results:
        if len(person_results) == 1:
            await _send_person(update, context, person_results[0]["id"])
        else:
            buttons = []
            for p in person_results[:5]:
                buttons.append([InlineKeyboardButton(p.get("name", "بازیگر"), callback_data=f"person:{p['id']}")])
            buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
            await update.message.reply_text("🎭 **بازیگران پیدا شده:**", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    await update.message.reply_text("😕 فیلم، سریال یا بازیگری پیدا نشد. نام را بررسی کنید.", reply_markup=back_to_menu_keyboard())


async def on_person_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    person_id = int(query.data.split(":")[1])
    await _send_person(query, context, person_id)


# =========================================================================
# 12. APP BOOTSTRAP
# =========================================================================

async def post_shutdown(app: Application) -> None:
    """Cleanup resources on shutdown."""
    tmdb: TMDbClient = app.bot_data.get("tmdb")
    gemini: Optional[GeminiClient] = app.bot_data.get("gemini")
    if tmdb:
        await tmdb.close()
    if gemini:
        await gemini.close()
    log.info("Resources cleaned up.")


def build_application(config: Config) -> Application:
    app = Application.builder().token(config.bot_token).build()

    app.bot_data["config"] = config
    app.bot_data["db"] = Database(DB_PATH)
    app.bot_data["tmdb"] = TMDbClient(config.tmdb_api_key)
    app.bot_data["gemini"] = GeminiClient(config.gemini_api_key) if config.gemini_api_key else None
    app.bot_data["limiter"] = RateLimiter(max_requests=30, window_seconds=60)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_genre_selected, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(on_similar_selected, pattern=r"^similar:"))
    app.add_handler(CallbackQueryHandler(on_favorite_toggle, pattern=r"^(fav|unfav):"))
    app.add_handler(CallbackQueryHandler(on_show_favorite, pattern=r"^show_fav:"))
    app.add_handler(CallbackQueryHandler(on_show_movie, pattern=r"^show:"))
    app.add_handler(CallbackQueryHandler(on_page_nav, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(on_back_to_movie, pattern=r"^back_to:"))
    app.add_handler(CallbackQueryHandler(on_person_selected, pattern=r"^person:"))
    app.add_handler(CallbackQueryHandler(on_person_movies, pattern=r"^person_movies:"))

    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))

    app.post_shutdown = post_shutdown

    return app


def main() -> None:
    try:
        config = Config.load()
    except RuntimeError as exc:
        log.critical("Startup failed: %s", exc)
        sys.exit(1)

    log.info("Starting MovieBot Ultimate v2.0...")
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
'''

# Save to output
output_path = '/mnt/agents/output/movie_bot_fixed.py'
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(fixed_code)

print(f"✅ Fixed code saved to {output_path}")
print(f"📊 Size: {len(fixed_code)} chars, {len(fixed_code.splitlines())} lines")
