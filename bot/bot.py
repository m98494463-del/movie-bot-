from __future__ import annotations

import logging
import os
import random
import sqlite3
import sys
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
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================================
# 1. CONFIG
# -------------------------------------------------------------------------
# Why this section exists: centralizes and validates every environment
# variable in one place so the bot fails fast (at startup) instead of
# crashing later mid-conversation with a confusing error.
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
    """Immutable runtime configuration, validated once at startup."""

    bot_token: str
    tmdb_api_key: str
    admin_id: Optional[int]

    @staticmethod
    def load() -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        tmdb_api_key = os.getenv("TMDB_API_KEY", "").strip()
        admin_id_raw = os.getenv("ADMIN_ID", "").strip()

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

        return Config(bot_token=bot_token, tmdb_api_key=tmdb_api_key, admin_id=admin_id)


# =========================================================================
# 2. LOGGING
# -------------------------------------------------------------------------
# Why: every real bot needs visibility into errors and API failures once
# it's out of your hands. Logs go to both console and a rotating-free
# simple file (kept small on purpose for an MVP).
# =========================================================================

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("movie_bot")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


log = setup_logging()


# =========================================================================
# 3. DATABASE (Repository Pattern, SQLite)
# -------------------------------------------------------------------------
# Why: isolates all SQL in one place. Handlers never write raw SQL — they
# call repository methods. This makes a future swap to PostgreSQL a
# matter of rewriting this class, not touching handler logic.
# =========================================================================

class Database:
    """Thin repository over SQLite for users and their favorite movies."""

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
            return False  # already a favorite

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

    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])


# =========================================================================
# 4. TMDb CLIENT
# -------------------------------------------------------------------------
# Why: a dedicated client keeps HTTP concerns (session reuse, timeouts,
# retries, error handling) out of the bot logic entirely. Handlers only
# ever see clean Python objects, never raw requests/response plumbing.
# =========================================================================

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
REQUEST_TIMEOUT = 8  # seconds
MAX_RETRIES = 2


class TMDbError(Exception):
    """Raised when TMDb cannot fulfill a request after retries."""


class TMDbClient:
    """Small wrapper around the TMDb REST API with retries and timeouts."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session = requests.Session()  # reused across all requests

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = dict(params or {})
        params["api_key"] = self._api_key
        params.setdefault("language", "en-US")
        params.setdefault("include_adult", "false")

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                response = self._session.get(
                    f"{TMDB_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                log.warning("TMDb request failed (attempt %s): %s", attempt, exc)
        raise TMDbError(f"TMDb request to {path} failed after retries") from last_error

    def get_trending(self) -> list[dict[str, Any]]:
        data = self._get("/trending/movie/week")
        return data.get("results", [])

    def get_top_rated(self) -> list[dict[str, Any]]:
        data = self._get("/movie/top_rated")
        return data.get("results", [])

    def get_random_movie(self) -> Optional[dict[str, Any]]:
        # Pull from a popular-movies page for a reasonable "random but decent" pick.
        page = random.randint(1, 20)
        data = self._get("/movie/popular", params={"page": page})
        results = data.get("results", [])
        return random.choice(results) if results else None

    def search_movies(self, query: str) -> list[dict[str, Any]]:
        data = self._get("/search/movie", params={"query": query})
        return data.get("results", [])

    def get_movie_details(self, movie_id: int) -> dict[str, Any]:
        return self._get(f"/movie/{movie_id}")


# =========================================================================
# 5. FORMATTING HELPERS
# =========================================================================

def format_movie_caption(movie: dict[str, Any]) -> str:
    """Builds the HTML-formatted caption shown with a movie's poster."""
    title = movie.get("title") or movie.get("original_title") or "Unknown title"
    year = (movie.get("release_date") or "----")[:4]
    rating = movie.get("vote_average", 0)
    overview = movie.get("overview") or "No description available."
    if len(overview) > 500:
        overview = overview[:497] + "..."

    genres = movie.get("genres")
    genre_line = ""
    if genres:
        names = ", ".join(g["name"] for g in genres)
        genre_line = f"🎭 <b>Genres:</b> {names}\n"

    runtime = movie.get("runtime")
    runtime_line = f"⏱ <b>Runtime:</b> {runtime} min\n" if runtime else ""

    return (
        f"🎬 <b>{title}</b> ({year})\n"
        f"⭐ <b>Rating:</b> {rating:.1f}/10\n"
        f"{genre_line}"
        f"{runtime_line}\n"
        f"{overview}"
    )


def poster_url(movie: dict[str, Any]) -> Optional[str]:
    path = movie.get("poster_path")
    return f"{TMDB_IMAGE_BASE}{path}" if path else None


# =========================================================================
# 6. KEYBOARDS
# =========================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🎲 Random Movie", callback_data="menu:random")],
        [InlineKeyboardButton("🔥 Trending", callback_data="menu:trending")],
        [InlineKeyboardButton("⭐ Top Rated", callback_data="menu:top_rated")],
        [InlineKeyboardButton("❤️ Favorites", callback_data="menu:favorites")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(buttons)


def movie_actions_keyboard(movie_id: int, is_favorite: bool) -> InlineKeyboardMarkup:
    fav_button = (
        InlineKeyboardButton("💔 Remove Favorite", callback_data=f"unfav:{movie_id}")
        if is_favorite
        else InlineKeyboardButton("❤️ Add Favorite", callback_data=f"fav:{movie_id}")
    )
    buttons = [
        [fav_button],
        [InlineKeyboardButton("🎲 Another Random", callback_data="menu:random")],
        [InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(buttons)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ Main Menu", callback_data="menu:home")]]
    )


# =========================================================================
# 7. GLOBALS WIRED AT STARTUP
# -------------------------------------------------------------------------
# Why module-level globals here (instead of dependency injection through
# every function signature): python-telegram-bot handlers only receive
# (update, context). context.bot_data is the officially supported place
# to stash shared services — so we store db/tmdb there at startup and
# fetch them inside each handler. This keeps handlers testable without
# real globals leaking across the whole file.
# =========================================================================

WELCOME_TEXT = (
    "🎬 <b>Welcome to MovieBot!</b>\n\n"
    "I help you discover movies to watch — trending picks, top-rated "
    "classics, random surprises, and search by title.\n\n"
    "Use the menu below to get started, or just type a movie name to search."
)

HELP_TEXT = (
    "❓ <b>How to use MovieBot</b>\n\n"
    "🎲 <b>Random Movie</b> — get a surprise pick\n"
    "🔥 <b>Trending</b> — what's popular this week\n"
    "⭐ <b>Top Rated</b> — all-time highest rated\n"
    "❤️ <b>Favorites</b> — movies you've saved\n\n"
    "You can also just type any movie title to search for it directly."
)


# =========================================================================
# 8. HANDLERS
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
    """Admin-only: quick usage stats."""
    config: Config = context.bot_data["config"]
    user = update.effective_user
    if user is None or config.admin_id is None or user.id != config.admin_id:
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    db: Database = context.bot_data["db"]
    await update.message.reply_text(f"👥 Total users: {db.user_count()}")


async def _send_movie(
    update_or_query, context: ContextTypes.DEFAULT_TYPE, movie: dict[str, Any], telegram_id: int
) -> None:
    """Shared logic to render a movie card, used by several handlers."""
    tmdb: TMDbClient = context.bot_data["tmdb"]
    db: Database = context.bot_data["db"]

    try:
        details = tmdb.get_movie_details(movie["id"])
    except TMDbError as exc:
        log.error("Failed to fetch movie details for id=%s: %s", movie.get("id"), exc)
        details = movie  # fall back to the lighter object we already have

    caption = format_movie_caption(details)
    keyboard = movie_actions_keyboard(details["id"], db.is_favorite(telegram_id, details["id"]))
    image = poster_url(details)

    send_photo = getattr(update_or_query, "message", update_or_query)
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

        elif action == "trending":
            movies = tmdb.get_trending()[:8]
            if not movies:
                await query.message.reply_text("😕 No trending movies found right now.")
                return
            lines = [f"{i}. {m.get('title', 'Untitled')}" for i, m in enumerate(movies, 1)]
            await query.message.reply_text(
                "🔥 <b>Trending this week:</b>\n\n" + "\n".join(lines)
                + "\n\nType a title above to see full details.",
                parse_mode=ParseMode.HTML,
                reply_markup=back_to_menu_keyboard(),
            )

        elif action == "top_rated":
            movies = tmdb.get_top_rated()[:8]
            if not movies:
                await query.message.reply_text("😕 No top rated movies found right now.")
                return
            lines = [f"{i}. {m.get('title', 'Untitled')} — ⭐{m.get('vote_average', 0):.1f}" for i, m in enumerate(movies, 1)]
            await query.message.reply_text(
                "⭐ <b>Top Rated:</b>\n\n" + "\n".join(lines)
                + "\n\nType a title above to see full details.",
                parse_mode=ParseMode.HTML,
                reply_markup=back_to_menu_keyboard(),
            )

        elif action == "favorites":
            favorites = db.list_favorites(telegram_id)
            if not favorites:
                await query.message.reply_text(
                    "❤️ You have no favorites yet. Find a movie and tap 'Add Favorite'!",
                    reply_markup=back_to_menu_keyboard(),
                )
                return
            lines = [f"• {row['title']}" for row in favorites]
            await query.message.reply_text(
                "❤️ <b>Your Favorites:</b>\n\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=back_to_menu_keyboard(),
            )

    except TMDbError as exc:
        log.error("TMDb error in menu action '%s': %s", action, exc)
        await query.message.reply_text("⚠️ Movie service is temporarily unavailable. Please try again shortly.")


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


async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Treats any plain text message as a movie title search."""
    if update.message is None or not update.message.text:
        return

    query_text = update.message.text.strip()
    if not query_text:
        return

    tmdb: TMDbClient = context.bot_data["tmdb"]
    telegram_id = update.effective_user.id

    try:
        results = tmdb.search_movies(query_text)
    except TMDbError as exc:
        log.error("TMDb search failed for query='%s': %s", query_text, exc)
        await update.message.reply_text("⚠️ Search is temporarily unavailable. Please try again shortly.")
        return

    if not results:
        await update.message.reply_text(
            f"😕 No results found for \"{query_text}\". Try a different title.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await _send_movie(update, context, results[0], telegram_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler: logs everything so the bot never dies silently."""
    log.error("Unhandled exception while processing update: %s", context.error, exc_info=context.error)


# =========================================================================
# 9. APPLICATION BOOTSTRAP
# =========================================================================

def build_application(config: Config) -> Application:
    # Generous timeouts: connections routed through a VPN/proxy can be slow
    # rather than truly blocked, so we give them much more room than the
    # library's default ~5s before giving up.
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

    # Shared services, accessible in every handler via context.bot_data
    application.bot_data["config"] = config
    application.bot_data["db"] = Database(DB_PATH)
    application.bot_data["tmdb"] = TMDbClient(config.tmdb_api_key)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(on_favorite_button, pattern=r"^(fav|unfav):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))
    application.add_error_handler(on_error)

    return application


def main() -> None:
    try:
        config = Config.load()
    except RuntimeError as exc:
        log.critical("Startup failed: %s", exc)
        sys.exit(1)

    log.info("Starting MovieBot...")
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
    
