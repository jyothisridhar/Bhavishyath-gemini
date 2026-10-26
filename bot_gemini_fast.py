"""
Bhavishyath Career Counselling Bot - Telegram MVP
Powered by Google Gemini 3 Flash + Supabase
Performance-optimised: background logging + async profile extraction
"""

import os
import asyncio
import logging
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from dotenv import load_dotenv
import google.generativeai as genai
from flask import Flask, request, Response
from supabase import create_client, Client
from telegram import Update
from telegram.ext import (
    Application,
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

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
WEBHOOK_URL       = os.getenv("WEBHOOK_URL")       # Cloud Run service URL
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
BOT_NAME         = "gemini"   # discriminator — keeps sessions isolated in shared Supabase

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
supabase: Client = None

def init_supabase():
    global supabase
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase client initialised")


# ── Database: User Memory ─────────────────────────────────────────────────────
def get_user_memory(user_id: int) -> dict:
    try:
        res = (
            supabase.table("user_memory")
            .select("profile_json, summary")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if res.data:
            profile = res.data.get("profile_json") or {}
            return {"profile": profile, "summary": res.data.get("summary") or ""}
        return {"profile": {}, "summary": ""}
    except Exception as e:
        logger.error("get_user_memory error: %s", e)
        return {"profile": {}, "summary": ""}


def update_user_memory(user_id: int, username: str, first_name: str, profile: dict, summary: str):
    try:
        supabase.table("user_memory").upsert({
            "user_id":      user_id,
            "username":     username,
            "first_name":   first_name,
            "profile_json": profile,
            "summary":      summary,
            "updated_at":   datetime.utcnow().isoformat(),
        }, on_conflict="user_id").execute()
    except Exception as e:
        logger.error("update_user_memory error: %s", e)


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


def build_context_message(user_id: int) -> str:
    memory = get_user_memory(user_id)
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


async def get_gemini_response(user_id: int, user_message: str) -> str:
    try:
        # The three reads are independent, so run them at the same time
        history, context, kb_context = await asyncio.gather(
            timed("history", get_session_history, user_id),
            timed("memory", build_context_message, user_id),
            timed("knowledge_base", get_knowledge_base_context, user_message),
        )

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
        return (
            "Sorry, I'm having a bit of trouble right now. Please try again in a moment. "
            "If this keeps happening, contact the Bhavishyath team."
        )

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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("User %s (%s) started the bot", user.id, user.username)

    memory = await asyncio.to_thread(get_user_memory, user.id)
    is_returning = bool(memory["profile"] or memory["summary"])

    if not is_returning:
        await asyncio.to_thread(
            update_user_memory, user.id, user.username or "", user.first_name, {}, ""
        )

    if is_returning:
        name = memory["profile"].get("name", user.first_name)
        greeting = (
            f"Welcome back, {name}! 👋\n\n"
            "I remember our previous conversations. What would you like to explore today — "
            "career options, entrance exams, colleges, or something else?"
        )
    else:
        greeting = (
            f"Namaste {user.first_name}! 👋\n\n"
            "I'm *Bhavishyath*, your career counsellor. I'm here to help you think through "
            "your education and career options — whether you're in school, intermediate, or degree.\n\n"
            "To get started, could you tell me:\n"
            "• Which class/year are you in?\n"
            "• What stream or group are you studying?\n"
            "• What are you hoping to explore today?\n\n"
            "Feel free to write in Telugu or English — both are fine! 😊"
        )
    await update.message.reply_text(greeting, parse_mode="Markdown")


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
        "/profile — View what I remember about you\n\n"
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

    logger.info("Message from %s (%s): %s", user.id, user.username, user_message[:100])

    # Fire-and-forget: log incoming message without waiting
    asyncio.create_task(
        asyncio.to_thread(log_message_bg, user.id, user.username or "", "user", user_message)
    )

    if detect_crisis(user_message):
        logger.warning("Crisis keywords detected for user %s", user.id)
        asyncio.create_task(
            asyncio.to_thread(log_message_bg, user.id, user.username or "", "system", "[CRISIS KEYWORDS DETECTED]")
        )
        await update.message.reply_text(CRISIS_RESPONSE, parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # These must be sequential — history must be saved before AI call,
    # and AI response must be saved before next message
    await timed("save_user", save_to_session_history, user.id, "user", user_message)
    notice = asyncio.create_task(send_slow_notice(update))
    try:
        bot_response = await get_gemini_response(user.id, user_message)
    finally:
        notice.cancel()

    # Send the reply first, then save it. The handler still finishes only after
    # the save, so the next message sees the full history.
    try:
        await send_long_message(update, bot_response)
        logger.info("Reply sent to %s in %.0f ms", user.id, (time.perf_counter() - t0) * 1000)
    finally:
        await timed("save_model", save_to_session_history, user.id, "model", bot_response)

    # Fire-and-forget: log bot response + extract profile in background
    asyncio.create_task(
        asyncio.to_thread(log_message_bg, user.id, user.username or "", "assistant", bot_response)
    )
    asyncio.create_task(
        update_profile_from_conversation(user.id, user.username or "", user.first_name)
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice messages:
    1. Download OGG audio from Telegram
    2. Transcribe via Gemini native audio understanding
    3. Feed transcription into normal conversation flow
    """
    user = update.effective_user
    logger.info("Voice message from %s (%s)", user.id, user.username)
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

        logger.info("Voice transcribed for user %s: %s", user.id, transcribed_text[:100])

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
        bot_response = await get_gemini_response(user.id, transcribed_text)
        notice.cancel()

        try:
            await send_long_message(update, bot_response)
            logger.info("Voice reply sent to %s in %.0f ms", user.id, (time.perf_counter() - t0) * 1000)
        finally:
            await timed("save_model", save_to_session_history, user.id, "model", bot_response)

        asyncio.create_task(
            asyncio.to_thread(log_message_bg, user.id, user.username or "", "assistant", bot_response)
        )
        asyncio.create_task(
            update_profile_from_conversation(user.id, user.username or "", user.first_name)
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


def extract_profile_sync(user_id: int, username: str, first_name: str):
    """Lightweight Gemini call to extract profile facts every few student messages."""
    message_count = count_user_messages(user_id)
    if message_count == 0 or message_count % PROFILE_EXTRACT_EVERY != 0:
        return

    try:
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

        new_profile = json.loads(raw)
        memory = get_user_memory(user_id)
        merged = {**memory["profile"], **new_profile}
        update_user_memory(user_id, username, first_name, merged, memory["summary"])
        logger.info("Updated profile for user %s: %s", user_id, merged)

    except Exception as e:
        logger.debug("Profile extraction skipped: %s", e)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error)


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


@flask_app.post("/webhook/<token>")
def telegram_webhook(token):
    if token != TELEGRAM_TOKEN:
        return Response("forbidden", status=403)
    update = Update.de_json(request.get_json(force=True), ptb_app.bot)
    if is_duplicate_update(update.update_id):
        logger.info("Ignoring duplicate update %s", update.update_id)
        return Response("ok", status=200)
    # Reply 200 straight away and handle the update in the background. Waiting
    # for the answer made Telegram retry after its timeout, giving duplicate replies.
    future = asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), event_loop)
    future.add_done_callback(log_update_failure)
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
    ]:
        if not var:
            raise ValueError(f"{name} not set in environment variables")

    init_supabase()
    gemini_model = setup_gemini()

    ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start",   start))
    ptb_app.add_handler(CommandHandler("help",    help_command))
    ptb_app.add_handler(CommandHandler("reset",   reset_command))
    ptb_app.add_handler(CommandHandler("profile", profile_command))
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
        webhook_endpoint = f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}"
        await ptb_app.bot.set_webhook(webhook_endpoint)
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
