"""
Bhavishyath Career Counselling Bot - Telegram MVP
Powered by Google Gemini 3 Flash + Supabase
Performance-optimised: background logging + async profile extraction
"""

import os
import asyncio
import hmac
import logging
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from dotenv import load_dotenv
import google.generativeai as genai
from flask import Flask, request, Response
from supabase import create_client, Client
from telegram import Update
import intake
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
# The HTTP client logs every request URL at INFO, and Telegram API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
WEBHOOK_URL       = os.getenv("WEBHOOK_URL")       # Cloud Run service URL
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET")    # sent by Telegram in a header; A-Z a-z 0-9 _ - only
TEAM_CHAT_ID      = os.getenv("TEAM_CHAT_ID")      # optional: Telegram chat that gets data-deletion requests
PORT              = int(os.getenv("PORT", "8080")) # Cloud Run sets this automatically
MODEL_NAME        = "gemini-3-flash-preview"
MAX_HISTORY_TURNS = 10
PROFILE_EXTRACT_EVERY = 2   # student messages between profile extractions
THREAD_POOL_SIZE  = 32      # blocking DB/model calls run here, off the event loop
GEMINI_ATTEMPTS   = 2       # one try plus one retry
GEMINI_TIMEOUT    = 20      # seconds allowed for each attempt
GEMINI_RETRY_WAIT = 1       # seconds between attempts
SLOW_REPLY_SECONDS = 10     # send a "still working" note if no reply by then
SLOW_REPLY_NOTICE = "Still working on it, one moment."
RECENT_UPDATES_CAP = 1000   # Telegram update ids remembered to drop redeliveries
WEBHOOK_WAIT_SECONDS = 25   # hold the webhook open this long; Cloud Run only gives CPU during a request
BOT_NAME         = "gemini"   # discriminator — keeps sessions isolated in shared Supabase
GENERIC_ERROR    = (
    "Sorry, I'm having a bit of trouble right now. Please try again in a moment. "
    "If this keeps happening, contact the Bhavishyath team."
)

# ── System Prompt ─────────────────────────────────────────────────────────────
_prompt_path = os.path.join(os.path.dirname(__file__), "prompt.txt")
try:
    with open(_prompt_path, encoding="utf-8") as _f:
        SYSTEM_PROMPT = _f.read().strip()
except FileNotFoundError:
    raise FileNotFoundError(
        "prompt.txt not found. Create a prompt.txt file in the same folder as bot.py."
    )


# ── Supabase Client ───────────────────────────────────────────────────────────
class _PerThreadClient:
    """One Supabase client per thread. A single client shared by the worker threads
    failed under concurrent calls ("deque mutated during iteration"), which silently
    dropped the chat history for that message."""
    def __init__(self):
        self._local = threading.local()

    def __getattr__(self, name):
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._local.client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return getattr(client, name)


supabase: Client = None

def init_supabase():
    global supabase
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    create_client(SUPABASE_URL, SUPABASE_KEY)   # fail at startup if the credentials are wrong
    supabase = _PerThreadClient()
    logger.info("Supabase client initialised")


# ── Database: User Memory ─────────────────────────────────────────────────────
# Columns filled by intake (tracker B4). profile_json holds only softer, model-inferred items.
STRUCTURED_FIELDS = (
    "name", "class_year", "stream_group", "district", "mandal", "school",
    "language", "first_generation", "shared_device",
)
MEMORY_COLUMNS = "profile_json, summary, consent_at, intake_step, " + ", ".join(STRUCTURED_FIELDS)


def _empty_memory(ok: bool = True) -> dict:
    return {
        "ok": ok,              # False when the database read failed, so callers don't mistake it for "new student"
        "profile": {},         # profile_json and the structured columns together, for display and context
        "profile_json": {},    # the soft items on their own, for merging
        "fields": {},          # the structured columns on their own
        "summary": "",
        "consent_at": None,
        "intake_step": None,
    }


def get_user_memory(user_id: int) -> dict:
    try:
        res = (
            supabase.table("user_memory")
            .select(MEMORY_COLUMNS)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        row = res.data if res else None   # maybe_single returns None when there is no row
        if not row:
            return _empty_memory()
        profile_json = row.get("profile_json") or {}
        fields = {k: row[k] for k in STRUCTURED_FIELDS if row.get(k) is not None}
        return {
            "ok": True,
            "profile": {**profile_json, **fields},
            "profile_json": profile_json,
            "fields": fields,
            "summary": row.get("summary") or "",
            "consent_at": row.get("consent_at"),
            "intake_step": row.get("intake_step"),
        }
    except Exception as e:
        logger.error("get_user_memory error: %s", e)
        return _empty_memory(ok=False)


def save_user_fields(user_id: int, fields: dict, username: str = None, first_name: str = None) -> bool:
    """Create or update a student's row, changing only the columns given."""
    row = {"user_id": user_id, **fields, "updated_at": datetime.now(timezone.utc).isoformat()}
    if username is not None:
        row["username"] = username
    if first_name is not None:
        row["first_name"] = first_name
    try:
        supabase.table("user_memory").upsert(row, on_conflict="user_id").execute()
        return True
    except Exception as e:
        logger.error("save_user_fields error: %s", e)
        return False


# ── Database: Deletion Requests ───────────────────────────────────────────────
def create_deletion_request(user_id: int):
    """Returns "created", "exists" (already pending) or None if the database call failed."""
    try:
        pending = (
            supabase.table("deletion_requests")
            .select("id")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .limit(1)
            .execute()
        )
        if pending.data:
            return "exists"
        supabase.table("deletion_requests").insert({"user_id": user_id}).execute()
        return "created"
    except Exception as e:
        logger.error("create_deletion_request error: %s", e)
        return None


# ── Database: Conversation Log (fire-and-forget) ──────────────────────────────
def log_message_bg(user_id: int, username: str, role: str, message: str):
    """
    Fire-and-forget logging — runs in background so it never
    delays the student's response.
    """
    try:
        supabase.table("conversation_log").insert({
            "user_id":   user_id,
            "username":  username,
            "role":      role,
            "message":   message,
            "bot":       BOT_NAME,
            "timestamp": datetime.utcnow().isoformat(),
        }).execute()
    except Exception as e:
        logger.error("log_message error: %s", e)


# ── Database: Session History ─────────────────────────────────────────────────
def get_session_history(user_id: int, max_turns: int = MAX_HISTORY_TURNS) -> list:
    try:
        res = (
            supabase.table("session_history")
            .select("role, content")
            .eq("user_id", user_id)
            .eq("bot", BOT_NAME)
            .order("id", desc=True)
            .limit(max_turns * 2)
            .execute()
        )
        rows = res.data or []
        return [{"role": r["role"], "parts": [r["content"]]} for r in reversed(rows)]
    except Exception as e:
        logger.error("get_session_history error: %s", e)
        return []


def save_to_session_history(user_id: int, role: str, content: str):
    try:
        supabase.table("session_history").insert({
            "user_id":   user_id,
            "role":      role,
            "content":   content,
            "bot":       BOT_NAME,
            "timestamp": datetime.utcnow().isoformat(),
        }).execute()

        # Prune to keep only latest 30 rows per user per bot
        keep_res = (
            supabase.table("session_history")
            .select("id")
            .eq("user_id", user_id)
            .eq("bot", BOT_NAME)
            .order("id", desc=True)
            .limit(30)
            .execute()
        )
        keep_ids = [r["id"] for r in (keep_res.data or [])]
        if keep_ids:
            supabase.table("session_history").delete().eq(
                "user_id", user_id
            ).eq("bot", BOT_NAME).not_.in_("id", keep_ids).execute()

    except Exception as e:
        logger.error("save_to_session_history error: %s", e)


def clear_session_history(user_id: int):
    try:
        supabase.table("session_history").delete().eq(
            "user_id", user_id
        ).eq("bot", BOT_NAME).execute()
    except Exception as e:
        logger.error("clear_session_history error: %s", e)


def count_user_messages(user_id: int) -> int:
    """
    Lifetime count of the student's messages. Read from conversation_log, not
    session_history, because session_history is pruned to 30 rows and its
    row count stops changing once it reaches the cap.
    """
    try:
        res = (
            supabase.table("conversation_log")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("bot", BOT_NAME)
            .eq("role", "user")
            .execute()
        )
        return res.count or 0
    except Exception as e:
        logger.error("count_user_messages error: %s", e)
        return 0

def get_knowledge_base_context(query_text: str, max_results: int = 5) -> str:
    """
    Search the knowledge_base table for entries relevant to the student's
    message. Returns a formatted context block, or "" if nothing relevant
    is found.
    """
    try:
        res = supabase.rpc(
            "search_knowledge_base",
            {"query_text": query_text, "max_results": max_results}
        ).execute()
        rows = res.data or []
        if not rows:
            return ""

        parts = [
            "[VERIFIED KNOWLEDGE BASE — Bhavishyath's own NGO resources. "
            "Treat the facts below as authoritative for any college names, "
            "scholarship details, or course/exam specifics they cover.]"
        ]
        for row in rows:
            parts.append(
                f"- {row['title']} ({row['category']}, {row['district']}): {row['content']}"
            )
        return "\n".join(parts)
    except Exception as e:
        logger.error("Knowledge base search error: %s", e)
        return ""

# ── Blocking-call helper ──────────────────────────────────────────────────────
async def timed(label: str, fn, *args):
    """Run a blocking call in the thread pool so it never stalls the event loop,
    and log how long it took (labels only, no student text)."""
    start = time.perf_counter()
    try:
        return await asyncio.to_thread(fn, *args)
    finally:
        logger.info("timing %s: %.0f ms", label, (time.perf_counter() - start) * 1000)


# ── Gemini Setup ──────────────────────────────────────────────────────────────
def setup_gemini():
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_PROMPT,
        generation_config=genai.GenerationConfig(
            temperature=0.7,
            max_output_tokens=1500,
        ),
        safety_settings=[
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ],
    )
    logger.info("Gemini model configured: %s", MODEL_NAME)
    return model


gemini_model = None


def build_context_message(memory: dict) -> str:
    parts = []
    known = {k: v for k, v in memory["profile"].items() if v not in (None, "", [], {})}
    if known:
        fields = "; ".join(
            f"{k.replace('_', ' ')}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
            for k, v in known.items()
        )
        parts.append(
            "[Already known about this student — do not ask for these again, use them naturally, "
            f"and re-ask only if the student contradicts them: {fields}]"
        )
    if memory["summary"]:
        parts.append(f"[Previous sessions summary: {memory['summary']}]")
    return "\n".join(parts)


async def get_gemini_response(user_id: int, user_message: str, memory: dict) -> str:
    """`memory` is the student's get_user_memory() result, already read by the caller."""
    try:
        # The two reads are independent, so run them at the same time
        history, kb_context = await asyncio.gather(
            timed("history", get_session_history, user_id),
            timed("knowledge_base", get_knowledge_base_context, user_message),
        )
        context = build_context_message(memory)

        # The caller saves the student's message before this runs, so history already
        # ends with it. Drop it here, since send_message sends it as the new turn.
        if history and history[-1]["role"] == "user" and history[-1]["parts"] == [user_message]:
            history = history[:-1]

        pieces = [p for p in [kb_context, context] if p]
        augmented_message = "\n\n".join(pieces + [user_message]) if pieces else user_message

        for attempt in range(1, GEMINI_ATTEMPTS + 1):
            try:
                # Fresh chat per attempt, so a failed call can't leave half a turn behind
                chat = gemini_model.start_chat(history=history)
                response = await timed("gemini", partial(
                    chat.send_message, augmented_message,
                    request_options={"timeout": GEMINI_TIMEOUT},
                ))
                break
            except Exception as e:
                if attempt == GEMINI_ATTEMPTS:
                    raise
                logger.warning("Gemini attempt %s/%s failed for user %s: %s",
                               attempt, GEMINI_ATTEMPTS, user_id, e)
                await asyncio.sleep(GEMINI_RETRY_WAIT)
        return response.text

    except Exception as e:
        logger.error("Gemini API error for user %s: %s", user_id, e)
        return GENERIC_ERROR

# ── Crisis Detection ──────────────────────────────────────────────────────────
CRISIS_KEYWORDS = [
    "suicide", "kill myself", "end my life", "want to die", "no reason to live",
    "can't go on", "hopeless", "self harm", "hurt myself", "not worth living",
    "జీవితం వద్దు", "చనిపోవాలి",
]

def detect_crisis(text: str) -> bool:
    return any(kw in text.lower() for kw in CRISIS_KEYWORDS)


CRISIS_RESPONSE = """I hear you, and I'm really glad you reached out. 💙

What you're feeling matters, and you don't have to go through this alone.

Please reach out to someone who can help right now:
📞 *KIRAN Mental Health Helpline: 1800-599-0019*
→ Free, 24/7, available in Telugu and other languages

You can still talk to me about your studies and future — but please also connect with someone who can support you fully right now."""


# ── Telegram Helpers ──────────────────────────────────────────────────────────
TELEGRAM_MAX_LENGTH = 4096

async def send_long_message(update: Update, text: str):
    """Split messages that exceed Telegram's 4096-char limit."""
    if len(text) <= TELEGRAM_MAX_LENGTH:
        await update.message.reply_text(text)
        return

    parts = []
    while text:
        if len(text) <= TELEGRAM_MAX_LENGTH:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()

    for part in parts:
        await update.message.reply_text(part)


async def send_slow_notice(update: Update):
    """Start this as a task and cancel it when the reply is ready. It only
    sends if the reply takes longer than SLOW_REPLY_SECONDS."""
    await asyncio.sleep(SLOW_REPLY_SECONDS)
    try:
        await update.message.reply_text(SLOW_REPLY_NOTICE)
    except Exception as e:
        logger.warning("Could not send slow-reply notice: %s", e)


# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def send_consent(bot, chat_id: int):
    await bot.send_message(chat_id=chat_id, text=intake.consent_text(), reply_markup=intake.consent_keyboard())


async def send_step(bot, chat_id: int, step: str, lang: str, first_name: str):
    await bot.send_message(
        chat_id=chat_id,
        text=intake.prompt(step, lang),
        reply_markup=intake.keyboard(step, lang, first_name),
    )


async def advance_intake(bot, user, memory: dict, step: str, value: str):
    """Save the answer to `step`, then ask the next question or finish intake."""
    column = intake.COLUMN[step]
    known = {**memory["profile"], column: value}
    nxt = intake.next_step(known)
    await asyncio.to_thread(save_user_fields, user.id, {column: value, "intake_step": nxt})
    lang = known.get("language")
    if nxt:
        await send_step(bot, user.id, nxt, lang, user.first_name)
    else:
        name = known.get("name") or user.first_name or ""
        await bot.send_message(chat_id=user.id, text=intake.opening(known, lang, name))


async def clear_buttons(query):
    """Remove the buttons from the message that was tapped, so it can't be tapped twice."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:   # e.g. message too old to edit
        logger.debug("Could not clear buttons: %s", e)


async def notify_team(bot, text: str):
    if not TEAM_CHAT_ID:
        logger.warning("TEAM_CHAT_ID is not set, so nobody was told about this request")
        return
    try:
        await bot.send_message(chat_id=int(TEAM_CHAT_ID), text=text)
    except Exception as e:
        logger.error("Could not notify the team: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("User %s started the bot", user.id)

    memory = await asyncio.to_thread(get_user_memory, user.id)
    if not memory["ok"]:
        await update.message.reply_text(GENERIC_ERROR)
        return

    # Nothing is saved until the student agrees, so a new student gets consent first.
    if not memory["consent_at"]:
        await send_consent(context.bot, user.id)
        return

    lang = memory["profile"].get("language")
    # Mid-intake: pick up at the question they had reached (tracker B6)
    if memory["intake_step"]:
        await send_step(context.bot, user.id, memory["intake_step"], lang, user.first_name)
        return

    name = memory["profile"].get("name") or user.first_name or ""
    await update.message.reply_text(intake.text("welcome_back", lang, name=name))


async def handle_intake_text(update: Update, context: ContextTypes.DEFAULT_TYPE, memory: dict):
    """The student typed something while a question was waiting for an answer."""
    user = update.effective_user
    step = memory["intake_step"]
    if step in ("name", "district_text"):
        value = " ".join(update.message.text.split())[: 40 if step == "name" else 60]
        if value:
            await advance_intake(context.bot, user, memory, step, value)
            return
    # They typed where a button was expected: ask the question again
    await send_step(context.bot, user.id, step, memory["profile"].get("language"), user.first_name)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Taps on consent, intake and data-deletion buttons (callback data c:/i:/d:)."""
    query = update.callback_query
    user = query.from_user
    await query.answer()

    parts = (query.data or "").split(":")
    memory = await asyncio.to_thread(get_user_memory, user.id)
    if not memory["ok"]:
        await context.bot.send_message(chat_id=user.id, text=GENERIC_ERROR)
        return
    lang = memory["profile"].get("language")

    if parts[0] == "c" and len(parts) == 2:
        await clear_buttons(query)
        if parts[1] != "1":
            await context.bot.send_message(chat_id=user.id, text=intake.text("consent_declined", None))
            return
        if memory["consent_at"]:
            return   # tapped twice
        # Pilot students already have answers in profile_json: carry them into the real columns
        legacy = {
            k: memory["profile"][k]
            for k in ("name", "class_year", "stream_group", "district")
            if isinstance(memory["profile"].get(k), str) and k not in memory["fields"]
        }
        nxt = intake.next_step(memory["profile"])
        await asyncio.to_thread(
            save_user_fields, user.id,
            {
                "consent_at": datetime.now(timezone.utc).isoformat(),
                "consent_version": intake.CONSENT_VERSION,
                "intake_step": nxt,
                **legacy,
            },
            user.username or "", user.first_name,
        )
        logger.info("User %s gave consent (%s)", user.id, intake.CONSENT_VERSION)
        if nxt:
            await send_step(context.bot, user.id, nxt, lang, user.first_name)
        else:
            name = memory["profile"].get("name") or user.first_name or ""
            await context.bot.send_message(chat_id=user.id, text=intake.opening(memory["profile"], lang, name))

    elif parts[0] == "i" and len(parts) == 3:
        step = parts[1]
        # Ignore buttons from an old message, or from before consent
        if not memory["consent_at"] or memory["intake_step"] != step or step not in intake.COLUMN:
            return
        try:
            idx = int(parts[2])
        except ValueError:
            return
        await clear_buttons(query)
        if step == "name":
            value = (user.first_name or "").strip()
        elif step == "district" and intake.is_other_district(idx):
            await asyncio.to_thread(save_user_fields, user.id, {"intake_step": "district_text"})
            await send_step(context.bot, user.id, "district_text", lang, user.first_name)
            return
        else:
            value = intake.choice_value(step, idx)
        if not value:
            return
        await advance_intake(context.bot, user, memory, step, value)

    elif parts[0] == "d" and len(parts) == 2:
        await clear_buttons(query)
        if parts[1] != "1":
            await context.bot.send_message(chat_id=user.id, text=intake.text("delete_cancelled", lang))
            return
        result = await asyncio.to_thread(create_deletion_request, user.id)
        if result is None:
            await context.bot.send_message(chat_id=user.id, text=GENERIC_ERROR)
        elif result == "exists":
            await context.bot.send_message(chat_id=user.id, text=intake.text("delete_already", lang))
        else:
            logger.info("User %s asked for their data to be deleted", user.id)
            await notify_team(
                context.bot,
                f"Data deletion request from Telegram user {user.id}. "
                "It is in the deletion_requests table (status pending).",
            )
            await context.bot.send_message(chat_id=user.id, text=intake.text("delete_sent", lang))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    memory = await asyncio.to_thread(get_user_memory, user.id)
    if not memory["ok"]:
        await update.message.reply_text(GENERIC_ERROR)
        return
    lang = memory["profile"].get("language")
    if not (memory["consent_at"] or memory["profile"] or memory["summary"]):
        await update.message.reply_text(intake.text("delete_nothing", lang))
        return
    await update.message.reply_text(intake.text("delete_confirm", lang), reply_markup=intake.delete_keyboard(lang))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*Bhavishyath Career Counsellor* 🎓\n\n"
        "I can help you with:\n"
        "• Career paths after 10th, Intermediate, or Degree\n"
        "• Entrance exams: EAMCET, NEET, JEE, POLYCET, ICET\n"
        "• College options in your state\n"
        "• Scholarships and government schemes\n"
        "• Polytechnic and ITI courses\n\n"
        "*Commands:*\n"
        "/start — Start or restart the conversation\n"
        "/help — Show this help message\n"
        "/reset — Clear conversation history and start fresh\n"
        "/profile — View what I remember about you\n"
        "/delete\\_my\\_data — Ask the team to delete everything saved about you\n\n"
        "_You can type in Telugu or English._"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await asyncio.to_thread(clear_session_history, user.id)
    await update.message.reply_text(
        "Conversation cleared! 🔄 I still remember your profile from before.\n\nWhat would you like to talk about?",
        parse_mode="Markdown"
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    memory = await asyncio.to_thread(get_user_memory, user.id)

    if not memory["profile"] and not memory["summary"]:
        await update.message.reply_text(
            "I don't have a profile stored for you yet — we haven't had enough conversations! "
            "Keep chatting and I'll remember what you share with me. 😊"
        )
        return

    profile_text = "*Your Profile:*\n"
    if memory["profile"]:
        for key, value in memory["profile"].items():
            profile_text += f"• {key.replace('_', ' ').title()}: {value}\n"
    if memory["summary"]:
        profile_text += f"\n*Previous conversations summary:*\n_{memory['summary']}_"

    await update.message.reply_text(profile_text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_message = update.message.text
    t0 = time.perf_counter()

    logger.info("Message from %s (%d chars)", user.id, len(user_message))

    memory = await asyncio.to_thread(get_user_memory, user.id)
    consented = bool(memory["consent_at"])

    # Safety comes first: a crisis message gets the helpline whether or not the
    # student has consented. It is only logged if they have.
    if detect_crisis(user_message):
        logger.warning("Crisis keywords detected for user %s", user.id)
        if consented:
            asyncio.create_task(
                asyncio.to_thread(log_message_bg, user.id, user.username or "", "user", user_message)
            )
            asyncio.create_task(
                asyncio.to_thread(log_message_bg, user.id, user.username or "", "system", "[CRISIS KEYWORDS DETECTED]")
            )
        await update.message.reply_text(CRISIS_RESPONSE, parse_mode="Markdown")
        return

    # Nothing is saved, logged or sent to the model until the student has consented
    # and finished intake. A failed read must not look like a new student.
    if not memory["ok"]:
        await update.message.reply_text(GENERIC_ERROR)
        return
    if not consented:
        await send_consent(context.bot, user.id)
        return
    if memory["intake_step"]:
        await handle_intake_text(update, context, memory)
        return

    # Fire-and-forget: log incoming message without waiting
    asyncio.create_task(
        asyncio.to_thread(log_message_bg, user.id, user.username or "", "user", user_message)
    )

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # These must be sequential — history must be saved before AI call,
    # and AI response must be saved before next message
    await timed("save_user", save_to_session_history, user.id, "user", user_message)
    notice = asyncio.create_task(send_slow_notice(update))
    try:
        bot_response = await get_gemini_response(user.id, user_message, memory)
    finally:
        notice.cancel()

    # Send the reply first, then save it. The handler still finishes only after
    # the save, so the next message sees the full history.
    try:
        await send_long_message(update, bot_response)
        logger.info("Reply sent to %s in %.0f ms", user.id, (time.perf_counter() - t0) * 1000)
    finally:
        await timed("save_model", save_to_session_history, user.id, "model", bot_response)

    # The reply is already sent. Finish the logging and profile update before the
    # handler returns, so they run while the webhook request still holds the CPU.
    await asyncio.gather(
        asyncio.to_thread(log_message_bg, user.id, user.username or "", "assistant", bot_response),
        update_profile_from_conversation(user.id, user.username or "", user.first_name),
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice messages:
    1. Download OGG audio from Telegram
    2. Transcribe via Gemini native audio understanding
    3. Feed transcription into normal conversation flow
    """
    user = update.effective_user
    logger.info("Voice message from %s", user.id)

    # Same gate as text: no audio is downloaded or sent to the model before consent and intake
    memory = await asyncio.to_thread(get_user_memory, user.id)
    if not memory["ok"]:
        await update.message.reply_text(GENERIC_ERROR)
        return
    if not memory["consent_at"]:
        await send_consent(context.bot, user.id)
        return
    if memory["intake_step"]:
        await send_step(context.bot, user.id, memory["intake_step"], memory["profile"].get("language"), user.first_name)
        return

    t0 = time.perf_counter()
    notice = asyncio.create_task(send_slow_notice(update))

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        voice    = update.message.voice
        tg_file  = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        transcribe_model = genai.GenerativeModel(model_name=MODEL_NAME)
        transcription_result = await timed("transcribe", transcribe_model.generate_content, [
            {
                "inline_data": {
                    "mime_type": "audio/ogg",
                    "data":      audio_bytes,
                }
            },
            (
                "Transcribe this voice message exactly as spoken. "
                "The speaker may use Telugu, English, or a mix of both — transcribe faithfully in whatever language they used. "
                "Return ONLY the transcribed text, nothing else."
            ),
        ])
        transcribed_text = transcription_result.text.strip()

        if not transcribed_text:
            await update.message.reply_text(
                "Sorry, I couldn't understand that voice message. "
                "Could you try again or type your question? 😊"
            )
            return

        logger.info("Voice transcribed for user %s (%d chars)", user.id, len(transcribed_text))

        await update.message.reply_text(
            f"🎤 _I heard:_ \"{transcribed_text}\"",
            parse_mode="Markdown"
        )

        asyncio.create_task(
            asyncio.to_thread(log_message_bg, user.id, user.username or "", "user", f"[VOICE] {transcribed_text}")
        )

        if detect_crisis(transcribed_text):
            logger.warning("Crisis keywords detected (voice) for user %s", user.id)
            asyncio.create_task(
                asyncio.to_thread(log_message_bg, user.id, user.username or "", "system", "[CRISIS KEYWORDS DETECTED - VOICE]")
            )
            await update.message.reply_text(CRISIS_RESPONSE, parse_mode="Markdown")
            return

        await timed("save_user", save_to_session_history, user.id, "user", transcribed_text)
        bot_response = await get_gemini_response(user.id, transcribed_text, memory)
        notice.cancel()

        try:
            await send_long_message(update, bot_response)
            logger.info("Voice reply sent to %s in %.0f ms", user.id, (time.perf_counter() - t0) * 1000)
        finally:
            await timed("save_model", save_to_session_history, user.id, "model", bot_response)

        await asyncio.gather(
            asyncio.to_thread(log_message_bg, user.id, user.username or "", "assistant", bot_response),
            update_profile_from_conversation(user.id, user.username or "", user.first_name),
        )

    except Exception as e:
        logger.error("Voice handling error for user %s: %s", user.id, e)
        await update.message.reply_text(
            "Sorry, I had trouble processing that voice message. "
            "Could you try again or type your question? 😊"
        )
    finally:
        notice.cancel()


async def update_profile_from_conversation(user_id: int, username: str, first_name: str):
    """Runs the blocking extraction in a worker thread so it never stalls other students."""
    await asyncio.to_thread(extract_profile_sync, user_id, username, first_name)


EXTRACT_FILL_COLUMNS = ("name", "class_year", "stream_group", "district")   # may fill an empty column
EXTRACT_SOFT_FIELDS  = ("marks_percentage", "career_interests", "concerns")  # kept in profile_json


def extract_profile_sync(user_id: int, username: str, first_name: str):
    """Lightweight Gemini call to extract profile facts every few student messages."""
    message_count = count_user_messages(user_id)
    if message_count == 0 or message_count % PROFILE_EXTRACT_EVERY != 0:
        return

    try:
        if not get_user_memory(user_id)["consent_at"]:
            return   # no consent, no profile extraction
        history = get_session_history(user_id)
        conversation_text = "\n".join(
            f"{h['role'].upper()}: {h['parts'][0]}" for h in history[-10:]
        )
        extraction_prompt = f"""Extract key student profile facts from this conversation.
Return ONLY a JSON object (no other text) with any of these fields you can confidently infer:
name, class_year, stream_group, marks_percentage, district, career_interests, concerns

Conversation:
{conversation_text}

If you cannot infer a field, omit it. Return only valid JSON."""

        quick_model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            generation_config=genai.GenerationConfig(temperature=0, max_output_tokens=200),
        )
        result = quick_model.generate_content(extraction_prompt)
        raw = result.text.strip().replace("```json", "").replace("```", "").strip()

        extracted = json.loads(raw)
        if not isinstance(extracted, dict):
            raise ValueError("extraction did not return a JSON object")

        memory = get_user_memory(user_id)
        # Intake answers are never overwritten by a model guess. A guess only fills a
        # column that is still empty (students from before intake existed).
        column_updates = {
            k: v for k, v in extracted.items()
            if k in EXTRACT_FILL_COLUMNS and isinstance(v, str) and v.strip() and k not in memory["fields"]
        }
        soft = {k: v for k, v in extracted.items() if k in EXTRACT_SOFT_FIELDS}
        merged = {**memory["profile_json"], **soft}
        save_user_fields(user_id, {"profile_json": merged, **column_updates}, username, first_name)
        logger.info("Updated profile for user %s (fields: %s)", user_id,
                    ", ".join(sorted({**merged, **column_updates})))

    except Exception as e:
        logger.warning("Profile extraction failed for user %s: %s", user_id, e)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Log the update id only; the full update holds the student's message text
    update_id = getattr(update, "update_id", None)
    logger.error("Update %s caused error: %s", update_id, context.error)


# ── Main ──────────────────────────────────────────────────────────────────────
# ── Flask App & Webhook ───────────────────────────────────────────────────────
flask_app = Flask(__name__)
ptb_app: Application = None
event_loop: asyncio.AbstractEventLoop = None


@flask_app.get("/health")
def health():
    return Response("ok", status=200)


_recent_updates: OrderedDict = OrderedDict()
_recent_updates_lock = threading.Lock()


def is_duplicate_update(update_id: int) -> bool:
    """True if Telegram has already delivered this update (it redelivers when it
    doesn't get a quick 200). Remembers the last RECENT_UPDATES_CAP ids."""
    with _recent_updates_lock:
        if update_id in _recent_updates:
            return True
        _recent_updates[update_id] = None
        if len(_recent_updates) > RECENT_UPDATES_CAP:
            _recent_updates.popitem(last=False)
        return False


def log_update_failure(future):
    if not future.cancelled() and future.exception():
        logger.error("Update processing failed: %s", future.exception())


@flask_app.post("/webhook")
def telegram_webhook():
    # Telegram sends the secret we gave set_webhook in this header on every call
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret.encode(), WEBHOOK_SECRET.encode()):
        return Response("forbidden", status=403)
    update = Update.de_json(request.get_json(force=True), ptb_app.bot)
    if is_duplicate_update(update.update_id):
        logger.info("Ignoring duplicate update %s", update.update_id)
        return Response("ok", status=200)
    future = asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), event_loop)
    future.add_done_callback(log_update_failure)
    # Keep this request open while the update is handled. Once a Cloud Run request
    # returns, the container's CPU is throttled and any work still running crawls.
    # If it takes longer than WEBHOOK_WAIT_SECONDS, answer 200 anyway (processing
    # carries on) so Telegram doesn't time out and redeliver.
    try:
        future.result(timeout=WEBHOOK_WAIT_SECONDS)
    except Exception:
        pass  # a timeout is expected for slow replies; real failures are logged by the callback
    return Response("ok", status=200)


def run_event_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


# ── Main ──────────────────────────────────────────────────────────────────────
_initialised = False


def init_app():
    """Set up Supabase, Gemini, the Telegram app and the webhook. Safe to call twice."""
    global gemini_model, ptb_app, event_loop, _initialised
    if _initialised:
        return

    for var, name in [
        (TELEGRAM_TOKEN, "TELEGRAM_TOKEN"),
        (GEMINI_API_KEY, "GEMINI_API_KEY"),
        (SUPABASE_URL,   "SUPABASE_URL"),
        (SUPABASE_KEY,   "SUPABASE_KEY"),
        (WEBHOOK_URL,    "WEBHOOK_URL"),
        (WEBHOOK_SECRET, "WEBHOOK_SECRET"),
    ]:
        if not var:
            raise ValueError(f"{name} not set in environment variables")

    init_supabase()
    # Fail at startup, not on the first student message, if the database is not migrated
    try:
        supabase.table("user_memory").select(MEMORY_COLUMNS).limit(1).execute()
        supabase.table("deletion_requests").select("id").limit(1).execute()
    except Exception as e:
        raise RuntimeError(
            "The database is missing the consent/intake columns or the deletion_requests table. "
            "Run migrations/001_intake_consent.sql in the Supabase SQL editor, then restart."
        ) from e
    gemini_model = setup_gemini()

    ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start",   start))
    ptb_app.add_handler(CommandHandler("help",    help_command))
    ptb_app.add_handler(CommandHandler("reset",   reset_command))
    ptb_app.add_handler(CommandHandler("profile", profile_command))
    ptb_app.add_handler(CommandHandler("delete_my_data", delete_command))
    ptb_app.add_handler(CallbackQueryHandler(on_button, pattern=r"^[cid]:"))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    ptb_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    ptb_app.add_error_handler(error_handler)

    event_loop = asyncio.new_event_loop()
    # The default pool is only a few threads on a small Cloud Run instance, which
    # a handful of concurrent Gemini calls would fill.
    event_loop.set_default_executor(ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE))
    thread = threading.Thread(target=run_event_loop, args=(event_loop,), daemon=True)
    thread.start()

    async def setup():
        await ptb_app.initialize()
        await ptb_app.start()
        webhook_endpoint = f"{WEBHOOK_URL}/webhook"
        await ptb_app.bot.set_webhook(
            webhook_endpoint,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],   # buttons arrive as callback_query
        )
        logger.info("Webhook registered: %s", webhook_endpoint)

    asyncio.run_coroutine_threadsafe(setup(), event_loop).result(timeout=30)
    _initialised = True


def create_app():
    """Entry point for gunicorn: bot_gemini_fast:create_app()"""
    init_app()
    return flask_app


def main():
    """Local run with Flask's development server. Production uses gunicorn."""
    init_app()
    logger.info("Bhavishyath Gemini bot starting on port %s...", PORT)
    flask_app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
