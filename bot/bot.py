"""
Telegram Movie Recommendation Bot — PRO MAX Edition (Fixed & Enhanced)
=======================================================================
Changes:
- Fixed ParseMode: switched all text to HTML for consistency & safety
- Fixed Gemini title cleaner (preserves apostrophes in titles)
- Added HTML escaping in all captions/messages
- Added Error Handler with graceful user feedback
- Added Smart Persian Search fallback via Gemini
- Added inline buttons for Trending, Top Rated, and Similar movies
- Fixed random page overflow (respects TMDb total_pages)
- Added Search History logging
- Added /actor command for cast search
- Added TV show support in _send_media
- Fixed broadcast command to preserve newlines
- Added rate-limit awareness (asyncio.sleep in loops)
- Added thumbnail safety in inline mode
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import sys
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
# 3. DATABASE
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
                CREATE TABLE IF NOT EXISTS search_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    query       TEXT NOT NULL,
                    searched_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

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

    def list_favorites(self, telegram_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT movie_id, title FROM favorites WHERE telegram_id = ? ORDER BY added_at DESC",
                (telegram_id,),
            ).fetchall()

    def get_all_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_id FROM users").fetchall()
            return [row["telegram_id"] for row in rows]

    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])

    def log_search(self, telegram_id: int, query: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO search_history (telegram_id, query) VALUES (?, ?)",
                (telegram_id, query),
            )

    def get_user_searches(self, telegram_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT query FROM search_history WHERE telegram_id = ? ORDER BY searched_at DESC LIMIT ?",
                (telegram_id, limit),
            ).fetchall()


# =========================================================================
# 4. ASYNC TMDb CLIENT
# =========================================================================

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

GENRES_MAP = {
    28: "اکشن 💥", 12: "ماجراجویی 🤠", 16: "انیمیشن 🎨", 35: "کمدی 🚀",
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
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = dict(params or {})
        params["api_key"] = self._api_key
        params.setdefault("language", "fa-IR")
        params.setdefault("include_adult", "false")

        try:
            resp = await self._client.get(f"{TMDB_BASE_URL}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("TMDb Request failed: %s", exc)
            raise TMDbError("خطا در برقراری ارتباط با TMDb") from exc

    async def get_movie_details(self, movie_id: int) -> dict[str, Any]:
        params = {"append_to_response": "videos,credits", "language": "fa-IR"}
        details = await self._get(f"/movie/{movie_id}", params=params)
        
        if not details.get("overview"):
            params["language"] = "en-US"
            en_details = await self._get(f"/movie/{movie_id}", params=params)
            details["overview"] = en_details.get("overview", "توضیحاتی موجود نیست.")
            
        return details

    async def get_tv_details(self, tv_id: int) -> dict[str, Any]:
        params = {"append_to_response": "videos,credits", "language": "fa-IR"}
        details = await self._get(f"/tv/{tv_id}", params=params)
        
        if not details.get("overview"):
            params["language"] = "en-US"
            en_details = await self._get(f"/tv/{tv_id}", params=params)
            details["overview"] = en_details.get("overview", "توضیحاتی موجود نیست.")
            
        return details

    async def get_similar_movies(self, movie_id: int) -> list[dict[str, Any]]:
        data = await self._get(f"/movie/{movie_id}/similar")
        return data.get("results", [])

    async def get_trending(self) -> list[dict[str, Any]]:
        data = await self._get("/trending/movie/week")
        return data.get("results", [])

    async def get_top_rated(self) -> list[dict[str, Any]]:
        data = await self._get("/movie/top_rated")
        return data.get("results", [])

    async def get_random_movie(self) -> Optional[dict[str, Any]]:
        data = await self._get("/movie/popular", params={"page": 1})
        total_pages = min(data.get("total_pages", 1), 15)
        page = random.randint(1, max(1, total_pages))
        data = await self._get("/movie/popular", params={"page": page})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_by_genre(self, genre_id: int) -> Optional[dict[str, Any]]:
        data = await self._get("/discover/movie", params={"with_genres": genre_id, "page": 1, "sort_by": "popularity.desc"})
        total_pages = min(data.get("total_pages", 1), 10)
        page = random.randint(1, max(1, total_pages))
        data = await self._get("/discover/movie", params={"with_genres": genre_id, "page": page, "sort_by": "popularity.desc"})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def search_movies(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/search/movie", params={"query": query})
        return data.get("results", [])

    async def search_tv(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/search/tv", params={"query": query})
        return data.get("results", [])

    async def search_person(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/search/person", params={"query": query})
        return data.get("results", [])

    async def get_person_details(self, person_id: int) -> dict[str, Any]:
        params = {"append_to_response": "movie_credits", "language": "fa-IR"}
        return await self._get(f"/person/{person_id}", params=params)

    async def get_random_tv(self) -> Optional[dict[str, Any]]:
        data = await self._get("/tv/popular", params={"page": 1})
        total_pages = min(data.get("total_pages", 1), 15)
        page = random.randint(1, max(1, total_pages))
        data = await self._get("/tv/popular", params={"page": page})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_random_anime(self) -> Optional[dict[str, Any]]:
        data = await self._get(
            "/discover/movie",
            params={"with_genres": "16", "with_origin_country": "JP", "page": 1},
        )
        total_pages = min(data.get("total_pages", 1), 8)
        page = random.randint(1, max(1, total_pages))
        data = await self._get(
            "/discover/movie",
            params={"with_genres": "16", "with_origin_country": "JP", "page": page},
        )
        results = data.get("results", [])
        return random.choice(results) if results else None


# =========================================================================
# 5. ROBUST ASYNC GEMINI CLIENT (FIXED)
# =========================================================================

class GeminiError(Exception):
    pass

class GeminiClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=15.0)

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
            "and nothing else. DO NOT use markdown bold, quotes, or any punctuation.\n\n"
            f"User prompt: {user_request}"
        )
        raw_title = await self._generate_content(prompt)
        # Clean markdown / extra characters from AI response (preserve apostrophes!)
        cleaned_title = re.sub(r'[*"\n]', '', raw_title).strip()
        return cleaned_title

    async def translate_or_find_title(self, persian_query: str) -> str:
        prompt = (
            "The user is searching for a movie or TV show. They wrote the query in Persian (Farsi). "
            "Return ONLY the official English title of the most likely match. Output nothing else. "
            "If it's already in English, return it as-is.\n\n"
            f"Query: {persian_query}"
        )
        raw = await self._generate_content(prompt)
        return re.sub(r'[*"\n]', '', raw).strip()

    async def chat_about_movies(self, user_query: str) -> str:
        prompt = (
            "You are a friendly, expert cinema assistant. Answer the user's question about movies, actors, "
            "directors, or plot explanations in Persian (Farsi). Keep the response helpful, engaging, and well-formatted.\n\n"
            f"User Question: {user_query}"
        )
        return await self._generate_content(prompt)


# =========================================================================
# 6. FORMATTING HELPERS
# =========================================================================

def escape_html(text: Optional[str]) -> str:
    if not text:
        return ""
    return html.escape(str(text))

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

    icon = "📺" if media_type == "tv" else ("🎌" if media_type == "anime" else "🎬")

    caption = (
        f"{icon} <b>{escape_html(title)}</b> ({escape_html(year)})\n"
        f"⭐ <b>امتیاز:</b> {rating:.1f}/10\n"
        f"🎭 <b>ژانر:</b> {escape_html(genre_str)}\n"
    )

    if director:
        caption += f"🎬 <b>کارگردان:</b> {escape_html(director)}\n"
    if top_cast:
        caption += f"👥 <b>بازیگران:</b> {escape_html(top_cast)}\n"

    caption += f"\n📝 <b>خلاصه داستان:</b>\n{escape_html(overview)}"
    return caption


def format_person_caption(person: dict[str, Any]) -> str:
    name = person.get("name", "نامشخص")
    bio = person.get("biography") or "بیوگرافی ثبت نشده است."
    if len(bio) > 500:
        bio = bio[:497] + "..."
    
    known_for = person.get("known_for_department", "هنرپیشه")
    birthday = person.get("birthday") or "نامشخص"
    place = person.get("place_of_birth") or "نامشخص"
    
    movies = person.get("movie_credits", {}).get("cast", [])
    top_movies = ", ".join([m.get("title", "") for m in movies[:5] if m.get("title")]) or "نامشخص"
    
    return (
        f"🎭 <b>{escape_html(name)}</b>\n"
        f"🎬 <b>حرفه:</b> {escape_html(known_for)}\n"
        f"📅 <b>تولد:</b> {escape_html(birthday)}\n"
        f"📍 <b>محل تولد:</b> {escape_html(place)}\n\n"
        f"📝 <b>بیوگرافی:</b>\n{escape_html(bio)}\n\n"
        f"🎥 <b>آثار شاخص:</b> {escape_html(top_movies)}"
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
# 7. KEYBOARDS
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
    row1.append(InlineKeyboardButton("🎭 فیلم‌های مشابه", callback_data=f"similar:{movie_id}"))
    buttons.append(row1)

    fav_btn = (
        InlineKeyboardButton("💔 حذف از علاقه‌مندی‌ها", callback_data=f"unfav:{movie_id}")
        if is_favorite
        else InlineKeyboardButton("❤️ افزودن به علاقه‌مندی‌ها", callback_data=f"fav:{movie_id}")
    )
    
    buttons.append([fav_btn])
    buttons.append([
        InlineKeyboardButton("🎲 فیلم بعدی", callback_data="menu:random"),
        InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")
    ])
    return InlineKeyboardMarkup(buttons)


def list_buttons_keyboard(items: list[dict[str, Any]], prefix: str = "show") -> InlineKeyboardMarkup:
    buttons = []
    for item in items[:10]:
        title = item.get("title") or item.get("name") or "نامشخص"
        item_id = item.get("id")
        if item_id:
            buttons.append([InlineKeyboardButton(f"{title}", callback_data=f"{prefix}:{item_id}")])
    buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")]])


# =========================================================================
# 8. HANDLERS
# =========================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception while handling an update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "😕 یه خطای غیرمنتظره رخ داد. لطفاً دوباره تلاش کن یا از منوی اصلی شروع کن.",
            reply_markup=back_to_menu_keyboard()
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if user:
        db.upsert_user(user.id, user.username)

    welcome_text = (
        f"سلام {escape_html(user.first_name if user else '')} عزیز! 👋\n\n"
        "🎬 به <b>ربات حرفه‌ای پیشنهاد فیلم و سریال</b> خوش آمدید.\n"
        "با دکمه‌های زیر جستجو کنید یا اسم فیلم/بازیگر رو برام چت کنید!"
    )
    await update.message.reply_text(
        welcome_text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "❓ <b>راهنمای جامع ربات:</b>\n\n"
        "🔍 <b>جستجو:</b> نام فیلم یا بازیگر (فارسی/انگلیسی) را مستقیم ارسال کنید.\n"
        "🧠 <b>پیشنهاد هوشمند:</b> حست رو بگو تا هوش مصنوعی فیلم پیدا کنه.\n"
        "💬 <b>چت با دستیار:</b> درباره نقد، داستان یا دیالوگ فیلم‌ها با هوش مصنوعی گپ بزن.\n"
        "🔎 <b>اینلاین مود:</b> تایپ کن <code>@BotUsername Inception</code> درون هر چت برای اشتراک سریع!\n"
        "🎭 <b>جستجوی بازیگر:</b> دستور <code>/actor Leonardo DiCaprio</code>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())


async def cmd_actor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    if not context.args:
        await update.message.reply_text("❌ لطفاً نام بازیگر را وارد کنید:\n<code>/actor Leonardo DiCaprio</code>", parse_mode=ParseMode.HTML)
        return
    
    query = " ".join(context.args)
    msg = await update.message.reply_text("🔍 در حال جستجوی بازیگر...")
    
    try:
        results = await tmdb.search_person(query)
        await msg.delete()
        if not results:
            await update.message.reply_text("😕 بازیگری با این نام پیدا نشد.", reply_markup=back_to_menu_keyboard())
            return
        
        person = results[0]
        person_details = await tmdb.get_person_details(person["id"])
        caption = format_person_caption(person_details)
        image = profile_url(person_details)
        
        if image:
            await update.message.reply_photo(photo=image, caption=caption, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())
        else:
            await update.message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())
    except Exception as e:
        log.error("Actor search error: %s", e)
        await msg.edit_text("❌ خطا در جستجو. لطفاً دوباره تلاش کنید.")


# --- ADMIN COMMANDS ---

async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not update.effective_user or update.effective_user.id != config.admin_id:
        return
    db: Database = context.bot_data["db"]
    await update.message.reply_text(f"📊 <b>آمار ربات:</b>\n👥 تعداد کاربران: <code>{db.user_count()}</code>", parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not update.effective_user or update.effective_user.id != config.admin_id:
        return

    if not update.message.text or " " not in update.message.text:
        await update.message.reply_text("❌ متن پیام را وارد کنید:\n<code>/broadcast سلام!</code>", parse_mode=ParseMode.HTML)
        return

    text_to_send = update.message.text.split(" ", 1)[1]
    if not text_to_send.strip():
        await update.message.reply_text("❌ متن پیام خالی است.", parse_mode=ParseMode.HTML)
        return

    db: Database = context.bot_data["db"]
    users = db.get_all_user_ids()
    success, failed = 0, 0

    msg = await update.message.reply_text(f"⏳ در حال ارسال به {len(users)} کاربر...")

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text_to_send, parse_mode=ParseMode.HTML)
            success += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1

    await msg.edit_text(f"✅ <b>پایان ارسال.</b>\nموفق: {success}\nناموفق: {failed}", parse_mode=ParseMode.HTML)


# --- MOVIE RENDERING HELPER ---

async def _send_media(update_or_query, context: ContextTypes.DEFAULT_TYPE, media_id: int, media_type: str = "movie") -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    user_id = update_or_query.from_user.id if hasattr(update_or_query, "from_user") else update_or_query.effective_user.id

    try:
        if media_type == "tv":
            details = await tmdb.get_tv_details(media_id)
        else:
            details = await tmdb.get_movie_details(media_id)
    except TMDbError:
        text = "❌ خطا در دریافت اطلاعات."
        chat_id = update_or_query.message.chat_id if hasattr(update_or_query, "message") else update_or_query.effective_chat.id
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=back_to_menu_keyboard())
        return

    caption = format_movie_caption(details, media_type)
    trailer = get_youtube_trailer(details)
    keyboard = movie_actions_keyboard(details["id"], db.is_favorite(user_id, details["id"]), trailer, media_type)
    image = poster_url(details)

    chat_id = update_or_query.message.chat_id if hasattr(update_or_query, "message") else update_or_query.effective_chat.id

    try:
        if image:
            await context.bot.send_photo(chat_id=chat_id, photo=image, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as e:
        log.error("Send media error: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# --- CALLBACK QUERY HANDLERS ---

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    if action == "home":
        context.user_data.clear()
        await query.message.reply_text("🏠 منوی اصلی:", reply_markup=main_menu_keyboard())

    elif action == "ai":
        if not context.bot_data.get("gemini"):
            await query.message.reply_text("⚠️ کلید GEMINI_API_KEY تنظیم نشده است.", reply_markup=back_to_menu_keyboard())
            return
        context.user_data["mode"] = "ai_recommendation"
        await query.message.reply_text("🧠 <b>چه فیلمی تو چه سبکی دوست داری ببینی؟</b>\nمثلاً: «یه فیلم معمایی پیچیده مثل Shutter Island»", parse_mode=ParseMode.HTML)

    elif action == "aichat":
        if not context.bot_data.get("gemini"):
            await query.message.reply_text("⚠️ کلید GEMINI_API_KEY تنظیم نشده است.", reply_markup=back_to_menu_keyboard())
            return
        context.user_data["mode"] = "ai_chat"
        await query.message.reply_text("💬 <b>دستیار سینمایی در خدمت شماست!</b>\nهر سوالی درباره فیلم، بازیگران، داستان یا نقد داری بپرس:", parse_mode=ParseMode.HTML)

    elif action == "genres":
        await query.message.reply_text("📂 <b>ژانر مورد نظرت رو انتخاب کن:</b>", parse_mode=ParseMode.HTML, reply_markup=genre_menu_keyboard())

    elif action == "random":
        movie = await tmdb.get_random_movie()
        if movie:
            await _send_media(query, context, movie["id"], "movie")

    elif action == "trending":
        movies = await tmdb.get_trending()
        if not movies:
            await query.message.reply_text("😕 داده‌ای یافت نشد.", reply_markup=back_to_menu_keyboard())
            return
        text = "🔥 <b>فیلم‌های ترند هفته:</b>\n\nبرای جزئیات روی هر عنوان کلیک کنید."
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=list_buttons_keyboard(movies, prefix="show"))

    elif action == "top_rated":
        movies = await tmdb.get_top_rated()
        if not movies:
            await query.message.reply_text("😕 داده‌ای یافت نشد.", reply_markup=back_to_menu_keyboard())
            return
        text = "⭐ <b>برترین فیلم‌های تاریخ:</b>\n\nبرای جزئیات روی هر عنوان کلیک کنید."
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=list_buttons_keyboard(movies, prefix="show"))

    elif action == "tv":
        tv = await tmdb.get_random_tv()
        if tv:
            await _send_media(query, context, tv["id"], "tv")

    elif action == "anime":
        anime = await tmdb.get_random_anime()
        if anime:
            await _send_media(query, context, anime["id"], "movie")

    elif action == "favorites":
        favs = db.list_favorites(query.from_user.id)
        if not favs:
            await query.message.reply_text("❤️ لیست علاقه‌مندی‌های شما خالی است.", reply_markup=back_to_menu_keyboard())
            return
        
        buttons = [[InlineKeyboardButton(row["title"], callback_data=f"show_fav:{row['movie_id']}")] for row in favs]
        buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
        await query.message.reply_text("❤️ <b>لیست علاقه‌مندی‌های شما:</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

    elif action == "help":
        help_text = (
            "❓ <b>راهنمای جامع ربات:</b>\n\n"
            "🔍 <b>جستجو:</b> نام فیلم یا بازیگر (فارسی/انگلیسی) را مستقیم ارسال کنید.\n"
            "🧠 <b>پیشنهاد هوشمند:</b> حست رو بگو تا هوش مصنوعی فیلم پیدا کنه.\n"
            "💬 <b>چت با دستیار:</b> درباره نقد، داستان یا دیالوگ فیلم‌ها با هوش مصنوعی گپ بزن.\n"
            "🔎 <b>اینلاین مود:</b> تایپ کن <code>@BotUsername Inception</code> درون هر چت برای اشتراک سریع!"
        )
        await query.message.reply_text(help_text, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())


async def on_genre_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    genre_id = int(query.data.split(":")[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    movie = await tmdb.get_by_genre(genre_id)
    if movie:
        await _send_media(query, context, movie["id"], "movie")


async def on_similar_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":")[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    sim_movies = await tmdb.get_similar_movies(movie_id)
    if not sim_movies:
        await query.message.reply_text("😕 فیلم مشابهی پیدا نشد.", reply_markup=back_to_menu_keyboard())
        return

    text = "🎭 <b>فیلم‌های مشابه پیشنهاد شده:</b>\n\nبرای جزئیات روی هر عنوان کلیک کنید."
    await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=list_buttons_keyboard(sim_movies, prefix="show"))


async def on_favorite_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, movie_id_str = query.data.split(":")
    movie_id = int(movie_id_str)
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]

    if action == "fav":
        try:
            details = await tmdb.get_movie_details(movie_id)
            title = details.get("title") or details.get("name") or "فیلم"
            db.add_favorite(query.from_user.id, movie_id, title)
            await query.answer("به علاقه‌مندی‌ها اضافه شد ❤️")
        except Exception:
            await query.answer("⚠️ خطا در افزودن.")
            return
    else:
        db.remove_favorite(query.from_user.id, movie_id)
        await query.answer("از علاقه‌مندی‌ها حذف شد 💔")

    try:
        details = await tmdb.get_movie_details(movie_id)
        trailer = get_youtube_trailer(details)
        new_kb = movie_actions_keyboard(movie_id, db.is_favorite(query.from_user.id, movie_id), trailer)
        await query.edit_message_reply_markup(reply_markup=new_kb)
    except Exception as e:
        log.error("Favorite toggle refresh error: %s", e)


async def on_show_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":")[1])
    await _send_media(query, context, movie_id, "movie")


async def on_show_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":")[1])
    await _send_media(query, context, movie_id, "movie")


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
        title = m.get("title", "فیلم")
        overview = m.get("overview", "")[:200]
        caption = f"🎬 <b>{escape_html(title)}</b>\n⭐ امتیاز: {m.get('vote_average', 0)}/10\n\n{escape_html(overview)}"
        thumb = poster_url(m)
        items.append(
            InlineQueryResultArticle(
                id=str(m["id"]),
                title=title,
                description=f"⭐ {m.get('vote_average', 0)}/10 | {m.get('
