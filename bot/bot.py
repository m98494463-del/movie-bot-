"""
Telegram Movie & TV Recommendation Bot — PRO STABLE EDITION
================================================================
Fixes:
- Solved Telegram "Cannot edit photo to text" crash
- Solved SQLite Foreign Key missing user error
- Added TMDb language fallback for discovery & genres
- Added universal safe message navigation
- Enhanced error reporting via Telegram Alerts
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

    def upsert_user(self, telegram_id: int, username: Optional[str] = "", first_name: Optional[str] = "") -> None:
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
        self.upsert_user(telegram_id)
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

    def get_all_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_id FROM users").fetchall()
            return [row["telegram_id"] for row in rows]

    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])


# =========================================================================
# 4. ASYNC TMDb CLIENT
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
        self._client = httpx.AsyncClient(timeout=12.0)

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = dict(params or {})
        params["api_key"] = self._api_key
        params.setdefault("include_adult", "false")

        try:
            resp = await self._client.get(f"{TMDB_BASE_URL}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("TMDb Request failed for %s: %s", path, exc)
            raise TMDbError("خطا در برقراری ارتباط با بانک اطلاعاتی فیلم") from exc

    async def get_movie_details(self, movie_id: int, media_type: str = "movie") -> dict[str, Any]:
        params = {"append_to_response": "videos,credits", "language": "fa-IR"}
        try:
            details = await self._get(f"/{media_type}/{movie_id}", params=params)
        except TMDbError:
            details = await self._get(f"/{media_type}/{movie_id}", params={"append_to_response": "videos,credits", "language": "en-US"})

        if not details.get("overview"):
            en_details = await self._get(f"/{media_type}/{movie_id}", params={"language": "en-US"})
            details["overview"] = en_details.get("overview", "توضیحاتی موجود نیست.")

        return details

    async def get_similar_movies(self, movie_id: int, media_type: str = "movie") -> list[dict[str, Any]]:
        data = await self._get(f"/{media_type}/{movie_id}/similar", params={"language": "en-US"})
        return data.get("results", [])

    async def get_trending(self, media_type: str = "movie") -> list[dict[str, Any]]:
        data = await self._get(f"/trending/{media_type}/week", params={"language": "en-US"})
        return data.get("results", [])

    async def get_top_rated(self, media_type: str = "movie") -> list[dict[str, Any]]:
        data = await self._get(f"/{media_type}/top_rated", params={"language": "en-US"})
        return data.get("results", [])

    async def get_random_movie(self, media_type: str = "movie") -> Optional[dict[str, Any]]:
        page = random.randint(1, 15)
        data = await self._get(f"/{media_type}/popular", params={"page": page, "language": "en-US"})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_by_genre(self, genre_id: int, media_type: str = "movie") -> Optional[dict[str, Any]]:
        page = random.randint(1, 8)
        data = await self._get(f"/discover/{media_type}", params={"with_genres": genre_id, "page": page, "sort_by": "popularity.desc", "language": "en-US"})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def search_movies(self, query: str, page: int = 1) -> list[dict[str, Any]]:
        data = await self._get("/search/movie", params={"query": query, "page": page, "language": "en-US"})
        return data.get("results", [])

    async def search_tv(self, query: str, page: int = 1) -> list[dict[str, Any]]:
        data = await self._get("/search/tv", params={"query": query, "page": page, "language": "en-US"})
        return data.get("results", [])

    async def get_random_tv(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 10)
        data = await self._get("/tv/popular", params={"page": page, "language": "en-US"})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_random_anime(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 6)
        data = await self._get(
            "/discover/movie",
            params={"with_genres": "16", "with_origin_country": "JP", "page": page, "language": "en-US"},
        )
        results = data.get("results", [])
        return random.choice(results) if results else None


# =========================================================================
# 5. ASYNC GEMINI CLIENT
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
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as exc:
                log.warning("Gemini model %s failed: %s", model, exc)

        raise GeminiError("اتصال به هوش مصنوعی برقرار نشد.")

    async def suggest_movie_title(self, user_request: str) -> str:
        prompt = (
            "Suggest EXACTLY ONE movie title for this request. Output ONLY the official English title "
            "with no quotes or extra characters.\n\n"
            f"User request: {user_request}"
        )
        raw_title = await self._generate_content(prompt)
        return re.sub(r'[*"`\'\n]', '', raw_title).strip()

    async def chat_about_movies(self, user_query: str) -> str:
        prompt = (
            "You are an expert movie assistant. Answer this query in Persian (Farsi) concisely and warmly:\n\n"
            f"User: {user_query}"
        )
        return await self._generate_content(prompt)


# =========================================================================
# 6. FORMATTING HELPERS
# =========================================================================

def format_movie_caption(details: dict[str, Any], media_type: str = "movie") -> str:
    title = details.get("title") or details.get("name") or "بدون عنوان"
    date_val = details.get("release_date") or details.get("first_air_date") or "----"
    year = date_val[:4]
    rating = details.get("vote_average", 0)
    
    overview = details.get("overview") or "توضیحاتی ثبت نشده است."
    if len(overview) > 350:
        overview = overview[:347] + "..."

    genres = details.get("genres", [])
    genre_str = ", ".join([g["name"] for g in genres]) if genres else "نامشخص"

    credits = details.get("credits", {})
    crew = credits.get("crew", [])
    cast = credits.get("cast", [])
    
    director = next((c["name"] for c in crew if c.get("job") == "Director"), None)
    top_cast = ", ".join([c["name"] for c in cast[:3]]) if cast else None

    type_label = "📺 سریال" if media_type == "tv" else ("🎌 انیمه" if media_type == "anime" else "🎬 فیلم")

    caption = (
        f"{type_label} <b>{title}</b> ({year})\n"
        f"⭐ <b>امتیاز:</b> {rating:.1f}/10\n"
        f"🎭 <b>ژانر:</b> {genre_str}\n"
    )

    if director:
        caption += f"🎬 <b>کارگردان:</b> {director}\n"
    if top_cast:
        caption += f"👥 <b>بازیگران:</b> {top_cast}\n"

    caption += f"\n📝 <b>خلاصه داستان:</b>\n{overview}"
    return caption


def poster_url(item: dict[str, Any]) -> Optional[str]:
    path = item.get("poster_path")
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


def get_youtube_trailer(details: dict[str, Any]) -> Optional[str]:
    videos = details.get("videos", {}).get("results", [])
    for vid in videos:
        if vid.get("site") == "YouTube" and vid.get("type") in ["Trailer", "Teaser"]:
            return f"https://www.youtube.com/watch?v={vid.get('key')}"
    return None


# =========================================================================
# 7. KEYBOARDS
# =========================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 پیشنهاد هوشمند (AI)", callback_data="menu:ai"), InlineKeyboardButton("💬 چت سینمایی", callback_data="menu:aichat")],
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
    buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def movie_actions_keyboard(movie_id: int, is_favorite: bool, trailer_url: Optional[str] = None, media_type: str = "movie") -> InlineKeyboardMarkup:
    buttons = []
    
    row1 = []
    if trailer_url:
        row1.append(InlineKeyboardButton("🎬 تریلر", url=trailer_url))
    row1.append(InlineKeyboardButton("🎭 موارد مشابه", callback_data=f"similar:{movie_id}:{media_type}"))
    buttons.append(row1)

    fav_btn = (
        InlineKeyboardButton("💔 حذف از علاقه‌مندی‌ها", callback_data=f"unfav:{movie_id}:{media_type}")
        if is_favorite
        else InlineKeyboardButton("❤️ افزودن به علاقه‌مندی‌ها", callback_data=f"fav:{movie_id}:{media_type}")
    )
    
    buttons.append([fav_btn])
    buttons.append([
        InlineKeyboardButton("🎲 پیشنهاد بعدی", callback_data="menu:random"),
        InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")
    ])
    return InlineKeyboardMarkup(buttons)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")]])


# =========================================================================
# 8. SAFE NAVIGATION SYSTEM (Solves Photo-to-Text Edit Bugs)
# =========================================================================

async def send_clean_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    photo: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Safely handles sending text/photos whether triggered by Message or CallbackQuery."""
    chat_id = update.effective_chat.id
    query = update.callback_query

    # Delete previous message if triggered from CallbackQuery to prevent edit crashes
    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    try:
        if photo:
            await context.bot.send_photo(
                chat_id=chat_id, photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
    except Exception as exc:
        log.error("Failed to send response: %s", exc)
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


# =========================================================================
# 9. HANDLERS
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if user:
        db.upsert_user(user.id, user.username, user.first_name)

    welcome_text = (
        f"سلام {user.first_name if user else ''} عزیز! 👋\n\n"
        "🎬 به **ربات پیشنهاد فیلم و سریال** خوش آمدید.\n"
        "برای شروع از منوی زیر استفاده کنید یا اسم فیلم مورد نظرتون رو بفرستید!"
    )
    await update.message.reply_text(
        welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "❓ **راهنمای استفاده از ربات:**\n\n"
        "🔍 **جستجو:** نام فیلم یا سریال را چت کنید.\n"
        "🧠 **پیشنهاد هوشمند:** حس و حالت رو بگو تا هوش مصنوعی فیلم پیشنهاد بده.\n"
        "💬 **چت سینمایی:** درباره نقد و داستان فیلم‌ها سوال بپرس."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())


async def _send_movie(update_or_query: Any, context: ContextTypes.DEFAULT_TYPE, movie_id: int, media_type: str = "movie") -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    user = update_or_query.from_user if hasattr(update_or_query, "from_user") else update_or_query.effective_user

    if user:
        db.upsert_user(user.id, getattr(user, "username", ""), getattr(user, "first_name", ""))

    try:
        details = await tmdb.get_movie_details(movie_id, media_type)
    except TMDbError:
        update_obj = update_or_query if isinstance(update_or_query, Update) else Update(0)
        await send_clean_response(update_or_query, context, "❌ خطا در دریافت اطلاعات فیلم.", reply_markup=back_to_menu_keyboard())
        return

    caption = format_movie_caption(details, media_type)
    trailer = get_youtube_trailer(details)
    is_fav = db.is_favorite(user.id, details["id"], media_type) if user else False
    keyboard = movie_actions_keyboard(details["id"], is_fav, trailer, media_type)
    image = poster_url(details)

    await send_clean_response(update_or_query, context, caption, photo=image, reply_markup=keyboard)


# --- CALLBACK QUERY HANDLERS ---

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    user = query.from_user
    db.upsert_user(user.id, user.username, user.first_name)

    try:
        if action == "home":
            context.user_data.clear()
            await send_clean_response(update, context, "🏠 **منوی اصلی:**", reply_markup=main_menu_keyboard())

        elif action == "ai":
            if not context.bot_data.get("gemini"):
                await query.answer("⚠️ هوش مصنوعی فعال نیست (کلید GEMINI تنظیم نشده).", show_alert=True)
                return
            context.user_data["mode"] = "ai_recommendation"
            await send_clean_response(update, context, "🧠 **چه فیلمی تو چه سبکی دوست داری ببینی؟**\nمثلاً: «یه فیلم معمایی مثل Shutter Island»", reply_markup=back_to_menu_keyboard())

        elif action == "aichat":
            if not context.bot_data.get("gemini"):
                await query.answer("⚠️ هوش مصنوعی فعال نیست.", show_alert=True)
                return
            context.user_data["mode"] = "ai_chat"
            await send_clean_response(update, context, "💬 **دستیار سینمایی:**\nسوال خود درباره فیلم‌ها را مطرح کنید:", reply_markup=back_to_menu_keyboard())

        elif action == "genres":
            await send_clean_response(update, context, "📂 **ژانر مورد نظرتان را انتخاب کنید:**", reply_markup=genre_menu_keyboard())

        elif action == "random":
            movie = await tmdb.get_random_movie()
            if movie:
                await _send_movie(query, context, movie["id"])
            else:
                await query.answer("😕 فیلمی پیدا نشد، دوباره بزنید.", show_alert=True)

        elif action == "trending":
            movies = await tmdb.get_trending()
            lines = [f"• {m.get('title', 'نامشخص')} (⭐ {m.get('vote_average',0):.1f})" for m in movies[:10]]
            await send_clean_response(update, context, "🔥 **فیلم‌های ترند هفته:**\n\n" + "\n".join(lines), reply_markup=back_to_menu_keyboard())

        elif action == "top_rated":
            movies = await tmdb.get_top_rated()
            lines = [f"• {m.get('title', 'نامشخص')} (⭐ {m.get('vote_average',0):.1f})" for m in movies[:10]]
            await send_clean_response(update, context, "⭐ **برترین فیلم‌های تاریخ:**\n\n" + "\n".join(lines), reply_markup=back_to_menu_keyboard())

        elif action == "tv":
            tv = await tmdb.get_random_tv()
            if tv:
                await _send_movie(query, context, tv["id"], "tv")
            else:
                await query.answer("😕 سریالی پیدا نشد.", show_alert=True)

        elif action == "anime":
            anime = await tmdb.get_random_anime()
            if anime:
                await _send_movie(query, context, anime["id"], "movie")
            else:
                await query.answer("😕 انیمه‌ای پیدا نشد.", show_alert=True)

        elif action == "favorites":
            favs = db.list_favorites(user.id)
            if not favs:
                await send_clean_response(update, context, "❤️ لیست علاقه‌مندی‌های شما خالی است.", reply_markup=back_to_menu_keyboard())
                return
            
            buttons = []
            for row in favs:
                mt = row.get("media_type", "movie")
                label = "📺" if mt == "tv" else "🎬"
                buttons.append([InlineKeyboardButton(f"{label} {row['title']}", callback_data=f"show:{row['movie_id']}:{mt}")])
            buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
            await send_clean_response(update, context, "❤️ **لیست علاقه‌مندی‌های شما:**", reply_markup=InlineKeyboardMarkup(buttons))

        elif action == "help":
            await cmd_help(update, context)

    except Exception as exc:
        log.error("Error in on_menu_button action %s: %s", action, exc)
        await query.answer("❌ خطا در اجرای دستور. دوباره تلاش کنید.", show_alert=True)


async def on_genre_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    genre_id = int(query.data.split(":")[1])
    tmdb: TMDbClient = context.bot_data["tmdb"]

    movie = await tmdb.get_by_genre(genre_id)
    if movie:
        await _send_movie(query, context, movie["id"])
    else:
        await query.answer("😕 فیلمی پیدا نشد.", show_alert=True)


async def on_similar_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    tmdb: TMDbClient = context.bot_data["tmdb"]

    sim_movies = await tmdb.get_similar_movies(movie_id, media_type)
    if not sim_movies:
        await query.answer("😕 موارد مشابهی پیدا نشد.", show_alert=True)
        return

    buttons = []
    for m in sim_movies[:6]:
        title = m.get("title") or m.get("name") or "فیلم"
        buttons.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"show:{m['id']}:{media_type}")])
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")])

    await send_clean_response(update, context, "🎭 **موارد مشابه پیشنهاد شده:**", reply_markup=InlineKeyboardMarkup(buttons))


async def on_favorite_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action, movie_id = parts[0], int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]

    if action == "fav":
        try:
            details = await tmdb.get_movie_details(movie_id, media_type)
            db.add_favorite(query.from_user.id, movie_id, details.get("title") or details.get("name", "فیلم"), media_type)
            await query.answer("به علاقه‌مندی‌ها اضافه شد ❤️")
        except Exception:
            await query.answer("❌ خطا در افزودن.", show_alert=True)
            return
    else:
        db.remove_favorite(query.from_user.id, movie_id, media_type)
        await query.answer("از علاقه‌مندی‌ها حذف شد 💔")

    # Refresh buttons
    try:
        details = await tmdb.get_movie_details(movie_id, media_type)
        trailer = get_youtube_trailer(details)
        new_kb = movie_actions_keyboard(movie_id, db.is_favorite(query.from_user.id, movie_id, media_type), trailer, media_type)
        await query.edit_message_reply_markup(reply_markup=new_kb)
    except Exception:
        pass


async def on_show_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    movie_id = int(parts[1])
    media_type = parts[2] if len(parts) > 2 else "movie"
    await _send_movie(query, context, movie_id, media_type)


# --- TEXT SEARCH ---

async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    gemini: Optional[GeminiClient] = context.bot_data.get("gemini")
    mode = context.user_data.get("mode")
    user = update.effective_user

    if user:
        db.upsert_user(user.id, user.username, user.first_name)

    # AI Recommendation
    if mode == "ai_recommendation" and gemini:
        context.user_data["mode"] = None
        msg = await update.message.reply_text("🧠 در حال آنالیز توسط هوش مصنوعی...")
        try:
            suggested_title = await gemini.suggest_movie_title(text)
            results = await tmdb.search_movies(suggested_title)
            await msg.delete()
            if results:
                await update.message.reply_text(f"💡 **پیشنهاد هوش مصنوعی:** `{suggested_title}`", parse_mode=ParseMode.MARKDOWN)
                await _send_movie(update, context, results[0]["id"])
            else:
                await update.message.reply_text(f"😕 عنوان پیشنهادی `{suggested_title}` یافت نشد.", reply_markup=back_to_menu_keyboard())
        except Exception:
            await msg.edit_text("❌ خطا در ارتباط با هوش مصنوعی.")
        return

    # AI Chat Mode
    if mode == "ai_chat" and gemini:
        msg = await update.message.reply_text("💭 در حال تایپ پاسخ...")
        try:
            response = await gemini.chat_about_movies(text)
            await msg.edit_text(response, reply_markup=back_to_menu_keyboard())
        except Exception:
            await msg.edit_text("❌ متاسفانه پاسخی دریافت نشد.")
        return

    # Search Movies/TV
    results = await tmdb.search_movies(text)
    if results:
        await _send_movie(update, context, results[0]["id"], "movie")
        return

    tv_results = await tmdb.search_tv(text)
    if tv_results:
        await _send_movie(update, context, tv_results[0]["id"], "tv")
        return

    await update.message.reply_text("😕 فیلم یا سریالی پیدا نشد. دوباره تلاش کنید.", reply_markup=back_to_menu_keyboard())


# =========================================================================
# 10. BOOTSTRAP
# =========================================================================

async def post_shutdown(app: Application) -> None:
    tmdb: TMDbClient = app.bot_data.get("tmdb")
    gemini: Optional[GeminiClient] = app.bot_data.get("gemini")
    if tmdb:
        await tmdb.close()
    if gemini:
        await gemini.close()


def build_application(config: Config) -> Application:
    app = Application.builder().token(config.bot_token).build()

    app.bot_data["config"] = config
    app.bot_data["db"] = Database(DB_PATH)
    app.bot_data["tmdb"] = TMDbClient(config.tmdb_api_key)
    app.bot_data["gemini"] = GeminiClient(config.gemini_api_key) if config.gemini_api_key else None

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_genre_selected, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(on_similar_selected, pattern=r"^similar:"))
    app.add_handler(CallbackQueryHandler(on_favorite_toggle, pattern=r"^(fav|unfav):"))
    app.add_handler(CallbackQueryHandler(on_show_movie, pattern=r"^show:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))
    app.post_shutdown = post_shutdown

    return app


def main() -> None:
    try:
        config = Config.load()
    except RuntimeError as exc:
        log.critical("Startup failed: %s", exc)
        sys.exit(1)

    log.info("Starting Pro Stable MovieBot...")
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
