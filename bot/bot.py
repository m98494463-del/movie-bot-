"""
Telegram Movie Recommendation & Discovery Bot — Pro Edition
================================================================

Features:
- Async TMDb & Gemini API Integration (httpx)
- Smart AI Movie Recommendations
- Interactive Genre Browsing
- YouTube Trailers & Cast/Crew Details
- Favorites Management with Interactive Pagination
- Inline Search Mode (Search anywhere in Telegram)
- Admin Panel with Mass Broadcast & User Stats
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
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
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

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
# 3. DATABASE (Repository Pattern)
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
        # Append credits and videos in a single request!
        params = {"append_to_response": "videos,credits", "language": "fa-IR"}
        details = await self._get(f"/movie/{movie_id}", params=params)
        
        # Fallback to English if Persian overview is empty
        if not details.get("overview"):
            params["language"] = "en-US"
            en_details = await self._get(f"/movie/{movie_id}", params=params)
            details["overview"] = en_details.get("overview", "توضیحاتی موجود نیست.")
            
        return details

    async def get_trending((self) -> list[dict[str, Any]]:
        data = await self._get("/trending/movie/week")
        return data.get("results", [])

    async def get_top_rated(self) -> list[dict[str, Any]]:
        data = await self._get("/movie/top_rated")
        return data.get("results", [])

    async def get_random_movie(self) -> Optional[dict[str, Any]]:
        page = random.randint(1, 15)
        data = await self._get("/movie/popular", params={"page": page})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def get_by_genre(self, genre_id: int) -> Optional[dict[str, Any]]:
        page = random.randint(1, 10)
        data = await self._get("/discover/movie", params={"with_genres": genre_id, "page": page, "sort_by": "popularity.desc"})
        results = data.get("results", [])
        return random.choice(results) if results else None

    async def search_movies(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/search/movie", params={"query": query})
        return data.get("results", [])

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
# 5. ASYNC GEMINI CLIENT
# =========================================================================

GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

class GeminiError(Exception):
    pass

class GeminiClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=12.0)

    async def close(self):
        await self._client.aclose()

    async def suggest_movie_title(self, user_request: str) -> str:
        prompt = (
            "You are a movie recommendation assistant. Based on the user prompt below, "
            "suggest ONE single best movie title. Output ONLY the official English movie title "
            "and nothing else (no extra text, quotes, or release years unless essential).\n\n"
            f"User request: {user_request}"
        )
        try:
            resp = await self._client.post(
                GEMINI_URL,
                headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as exc:
            log.error("Gemini request failed: %s", exc)
            raise GeminiError("خطا در پردازش هوش مصنوعی") from exc


# =========================================================================
# 6. FORMATTING HELPERS
# =========================================================================

def format_movie_caption(details: dict[str, Any], media_type: str = "movie") -> str:
    title = details.get("title") or details.get("name") or "بدون عنوان"
    date_val = details.get("release_date") or details.get("first_air_date") or "----"
    year = date_val[:4]
    rating = details.get("vote_average", 0)
    
    overview = details.get("overview") or "توضیحاتی ثبت نشده است."
    if len(overview) > 400:
        overview = overview[:397] + "..."

    # Extract Genres
    genres = details.get("genres", [])
    genre_str = ", ".join([g["name"] for g in genres]) if genres else "نامشخص"

    # Extract Director & Cast
    credits = details.get("credits", {})
    crew = credits.get("crew", [])
    cast = credits.get("cast", [])
    
    director = next((c["name"] for c in crew if c.get("job") == "Director"), None)
    top_cast = ", ".join([c["name"] for c in cast[:3]]) if cast else None

    icon = "📺" if media_type == "tv" else ("🎌" if media_type == "anime" else "🎬")

    caption = (
        f"{icon} <b>{title}</b> ({year})\n"
        f"⭐ <b>امتیاز:</b> {rating:.1f}/10\n"
        f"🎭 <b>ژانر:</b> {genre_str}\n"
    )

    if director:
        caption += f"🎬 <b>کارگردان:</b> {director}\n"
    if top_cast:
        caption += f"👥 <b>بازیگران:</b> {top_cast}\n"

    caption += f"\n📝 <b>خلاصه داستان:</b>\n{overview}"
    return caption


def get_youtube_trailer(details: dict[str, Any]) -> Optional[str]:
    videos = details.get("videos", {}).get("results", [])
    for vid in videos:
        if vid.get("site") == "YouTube" and vid.get("type") in ["Trailer", "Teaser"]:
            return f"https://www.youtube.com/watch?v={vid.get('key')}"
    return None


def poster_url(item: dict[str, Any]) -> Optional[str]:
    path = item.get("poster_path")
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


# =========================================================================
# 7. KEYBOARDS
# =========================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 پیشنهاد هوشمند (AI)", callback_data="menu:ai")],
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


def movie_actions_keyboard(movie_id: int, is_favorite: bool, trailer_url: Optional[str] = None) -> InlineKeyboardMarkup:
    buttons = []
    
    if trailer_url:
        buttons.append([InlineKeyboardButton("🎬 تماشای تریلر (یوتیوب)", url=trailer_url)])

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


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")]])


# =========================================================================
# 8. HANDLERS
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if user:
        db.upsert_user(user.id, user.username)

    welcome_text = (
        f"سلام {user.first_name if user else ''} عزیز! 👋\n\n"
        "🎬 به **ربات پیشنهاد فیلم و سریال** خوش آمدید.\n"
        "با استفاده از دکمه‌های زیر فیلم پیدا کنید یا اسم فیلم مورد نظرتون رو مستقیم برام بفرستید!"
    )
    await update.message.reply_text(
        welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "❓ **راهنمای ربات:**\n\n"
        "🔍 **جستجو:** کافیه اسم فیلم رو (فارسی یا انگلیسی) چت کنی.\n"
        "🧠 **پیشنهاد هوشمند:** حس و حالت رو بگو تا هوش مصنوعی بهت فیلم معرفی کنه.\n"
        "📂 **ژانرها:** فیلم براساس سبک مورد علاقه‌ت پیدا کن.\n"
        "🔎 **جستجوی سریع در چت‌ها:** تایپ کن `@BotUsername Inception` تا سریع فیلم بفرستی."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())


# --- ADMIN COMMANDS ---

async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not update.effective_user or update.effective_user.id != config.admin_id:
        return
    db: Database = context.bot_data["db"]
    await update.message.reply_text(f"📊 **آمار ربات:**\n👥 تعداد کاربران: `{db.user_count()}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send message to all users: /broadcast <message>"""
    config: Config = context.bot_data["config"]
    if not update.effective_user or update.effective_user.id != config.admin_id:
        return

    text_to_send = " ".join(context.args)
    if not text_to_send:
        await update.message.reply_text("❌ لطفاً متن پیام را وارد کنید. مثال:\n`/broadcast سلام به همه!`", parse_mode=ParseMode.MARKDOWN)
        return

    db: Database = context.bot_data["db"]
    users = db.get_all_user_ids()
    success, failed = 0, 0

    msg = await update.message.reply_text(f"⏳ در حال ارسال به {len(users)} کاربر...")

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text_to_send)
            success += 1
            await asyncio.sleep(0.05) # Prevent flood rate limits
        except Exception:
            failed += 1

    await msg.edit_text(f"✅ **ارسال به پایان رسید.**\nموفق: {success}\nناموفق: {failed}")


# --- MOVIE RENDERING HELPER ---

async def _send_movie(update_or_query, context: ContextTypes.DEFAULT_TYPE, movie_id: int) -> None:
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]
    user_id = update_or_query.from_user.id if hasattr(update_or_query, "from_user") else update_or_query.effective_user.id

    try:
        details = await tmdb.get_movie_details(movie_id)
    except TMDbError:
        text = "❌ خطا در دریافت اطلاعات فیلم."
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text)
        return

    caption = format_movie_caption(details)
    trailer = get_youtube_trailer(details)
    keyboard = movie_actions_keyboard(details["id"], db.is_favorite(user_id, details["id"]), trailer)
    image = poster_url(details)

    target_chat_id = update_or_query.message.chat_id if hasattr(update_or_query, "message") else update_or_query.effective_chat.id

    if image:
        await context.bot.send_photo(chat_id=target_chat_id, photo=image, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# --- CALLBACK QUERY HANDLERS ---

async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    if action == "home":
        await query.message.reply_text("🏠 منوی اصلی:", reply_markup=main_menu_keyboard())

    elif action == "ai":
        if not context.bot_data.get("gemini"):
            await query.message.reply_text("⚠️ هوش مصنوعی در حال حاضر غیرفعال است (کلید GEMINI تنظیم نشده).", reply_markup=back_to_menu_keyboard())
            return
        context.user_data["awaiting_ai"] = True
        await query.message.reply_text("🧠 **چه فیلمی دلت می‌خواد؟**\nمثلاً بگو: «یه فیلم ترسناک توی غار» یا «یه کمدی هیجان انگیز شبیه Mask»")

    elif action == "genres":
        await query.message.reply_text("📂 **ژانر مورد نظرت رو انتخاب کن:**", parse_mode=ParseMode.MARKDOWN, reply_markup=genre_menu_keyboard())

    elif action == "random":
        movie = await tmdb.get_random_movie()
        if movie:
            await _send_movie(query, context, movie["id"])

    elif action == "trending":
        movies = await tmdb.get_trending()
        lines = [f"• {m.get('title')} (⭐ {m.get('vote_average',0):.1f})" for m in movies[:10]]
        await query.message.reply_text("🔥 **فیلم‌های ترند هفته:**\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())

    elif action == "top_rated":
        movies = await tmdb.get_top_rated()
        lines = [f"• {m.get('title')} (⭐ {m.get('vote_average',0):.1f})" for m in movies[:10]]
        await query.message.reply_text("⭐ **برترین فیلم‌های تاریخ:**\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu_keyboard())

    elif action == "favorites":
        favs = db.list_favorites(query.from_user.id)
        if not favs:
            await query.message.reply_text("❤️ لیست علاقه‌مندی‌های شما خالی است.", reply_markup=back_to_menu_keyboard())
            return
        
        buttons = [[InlineKeyboardButton(row["title"], callback_data=f"show_fav:{row['movie_id']}")] for row in favs]
        buttons.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="menu:home")])
        await query.message.reply_text("❤️ **لیست علاقه‌مندی‌های شما:**", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

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
        await query.message.reply_text("😕 فیلمی در این ژانر پیدا نشد.", reply_markup=back_to_menu_keyboard())


async def on_favorite_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, movie_id_str = query.data.split(":")
    movie_id = int(movie_id_str)
    db: Database = context.bot_data["db"]
    tmdb: TMDbClient = context.bot_data["tmdb"]

    if action == "fav":
        details = await tmdb.get_movie_details(movie_id)
        db.add_favorite(query.from_user.id, movie_id, details.get("title", "فیلم"))
        await query.answer("به علاقه‌مندی‌ها اضافه شد ❤️")
    else:
        db.remove_favorite(query.from_user.id, movie_id)
        await query.answer("از علاقه‌مندی‌ها حذف شد 💔")

    # Refresh keyboard
    trailer = get_youtube_trailer(await tmdb.get_movie_details(movie_id))
    new_kb = movie_actions_keyboard(movie_id, db.is_favorite(query.from_user.id, movie_id), trailer)
    await query.edit_message_reply_markup(reply_markup=new_kb)


async def on_show_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    movie_id = int(query.data.split(":")[1])
    await _send_movie(query, context, movie_id)


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
        caption = f"🎬 <b>{m.get('title')}</b>\n⭐ امتیاز: {m.get('vote_average', 0)}/10\n\n{m.get('overview', '')}"
        items.append(
            InlineQueryResultArticle(
                id=str(m["id"]),
                title=m.get("title", "فیلم"),
                description=f"⭐ {m.get('vote_average', 0)}/10 | {m.get('release_date', '')[:4]}",
                thumb_url=poster_url(m),
                input_message_content=InputTextMessageContent(caption, parse_mode=ParseMode.HTML),
            )
        )
    await update.inline_query.answer(items)


# --- TEXT MESSAGES & SEARCH ---

async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    tmdb: TMDbClient = context.bot_data["tmdb"]

    # AI Prompt Handler
    if context.user_data.get("awaiting_ai"):
        context.user_data["awaiting_ai"] = False
        gemini: GeminiClient = context.bot_data.get("gemini")
        
        msg = await update.message.reply_text("🤖 در حال فکر کردن و پیدا کردن بهترین پیشنهاد...")
        try:
            suggested_title = await gemini.suggest_movie_title(text)
            results = await tmdb.search_movies(suggested_title)
            await msg.delete()
            if results:
                await update.message.reply_text(f"🧠 **پیشنهاد هوش مصنوعی:** `{suggested_title}`", parse_mode=ParseMode.MARKDOWN)
                await _send_movie(update, context, results[0]["id"])
            else:
                await update.message.reply_text("😕 متاسفانه فیلمی با پیشنهاد هوش مصنوعی پیدا نشد.")
        except Exception:
            await msg.edit_text("❌ خطایی در ارتباط با هوش مصنوعی رخ داد.")
        return

    # Normal Search
    results = await tmdb.search_movies(text)
    if not results:
        await update.message.reply_text("😕 فیلمی با این عنوان یافت نشد. دوباره تلاش کنید.", reply_markup=back_to_menu_keyboard())
        return

    await _send_movie(update, context, results[0]["id"])


# =========================================================================
# 9. APP BOOTSTRAP
# =========================================================================

def build_application(config: Config) -> Application:
    app = Application.builder().token(config.bot_token).build()

    # Context Data
    app.bot_data["config"] = config
    app.bot_data["db"] = Database(DB_PATH)
    app.bot_data["tmdb"] = TMDbClient(config.tmdb_api_key)
    app.bot_data["gemini"] = GeminiClient(config.gemini_api_key) if config.gemini_api_key else None

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_genre_selected, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(on_favorite_toggle, pattern=r"^(fav|unfav):"))
    app.add_handler(CallbackQueryHandler(on_show_favorite, pattern=r"^show_fav:"))

    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))

    return app


def main() -> None:
    try:
        config = Config.load()
    except RuntimeError as exc:
        log.critical("Startup failed: %s", exc)
        sys.exit(1)

    log.info("Starting MovieBot Pro...")
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
