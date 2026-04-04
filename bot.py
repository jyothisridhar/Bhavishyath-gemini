"""
Bhavishyat Career Counselling Bot - Telegram MVP
Powered by Google Gemini 3 Flash + Supabase
"""

import os
import logging
import json
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai
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

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")   # service_role key
MODEL_NAME        = "gemini-3-flash-preview"
MAX_HISTORY_TURNS = 10

# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Bhavishyat, a warm and knowledgeable career counsellor for students in Andhra Pradesh, India. You help students in classes 9-12, intermediate, and degree levels make informed decisions about their education and career paths.

YOUR ROLE:
- Guide students through career options based on their interests, marks, and circumstances
- Provide accurate information about AP education pathways: intermediate groups (MPC, BiPC, CEC, MEC, HEC), entrance exams (EAMCET, NEET, JEE, POLYCET, ICET), colleges, polytechnics, and ITIs
- Explain reservation categories, scholarships, and government schemes relevant to AP students
- Be realistic and encouraging — acknowledge constraints like finances or location while still showing possibilities
- Suggest both a primary path and a realistic backup (Plan B)

YOUR STYLE:
- Speak simply and clearly — many students are reading in their second language
- Be direct and give concrete answers — don't be vague or overly philosophical
- Keep responses under 300 words unless the question genuinely requires more detail — students want clear guidance, not essays. Never cut off mid-thought; always finish your answer completely.
- You may mix Telugu words naturally when helpful (e.g., "bagundu", "cheppandi")
- Be warm and encouraging, but never dismissive or condescending
- If a student shares their marks, acknowledge them without judgment

WHAT YOU KNOW:
- Andhra Pradesh intermediate groups and what careers they lead to
- Major entrance exams: EAMCET (Engineering & Medical), NEET, JEE, POLYCET, ICET, LAWCET
- Government colleges vs private colleges in AP districts
- Polytechnic and ITI options for students who don't want degree programs
- Scholarship schemes: Jagananna Vidya Deevena, Jagananna Vasathi Deevena, post-matric scholarships
- Reservation categories: SC, ST, BC (A/B/C/D/E), EWS, and their benefits

IMPORTANT BOUNDARIES:
- If a student shows signs of distress or crisis, gently acknowledge their feelings and share: KIRAN Mental Health Helpline: 1800-599-0019 (free, 24/7)
- Do not make promises about specific college admissions or guaranteed outcomes
- If asked about something outside your knowledge (specific current cutoffs, very recent policy changes), say so honestly and suggest they verify with official sources like bie.ap.gov.in or apsche.ap.gov.in

When a student first messages, ask for their class/year, stream/group if applicable, and what they're hoping to explore — but keep it conversational, not like a form."""

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


# ── Database: Conversation Log ────────────────────────────────────────────────
def log_message(user_id: int, username: str, role: str, message: str):
    try:
        supabase.table("conversation_log").insert({
            "user_id":   user_id,
            "username":  username,
            "role":      role,
            "message":   message,
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
            "timestamp": datetime.utcnow().isoformat(),
        }).execute()

        # Prune to keep only latest 30 rows per user
        keep_res = (
            supabase.table("session_history")
            .select("id")
            .eq("user_id", user_id)
            .order("id", desc=True)
            .limit(30)
            .execute()
        )
        keep_ids = [r["id"] for r in (keep_res.data or [])]
        if keep_ids:
            supabase.table("session_history").delete().eq(
                "user_id", user_id
            ).not_.in_("id", keep_ids).execute()

    except Exception as e:
        logger.error("save_to_session_history error: %s", e)


def clear_session_history(user_id: int):
    try:
        supabase.table("session_history").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logger.error("clear_session_history error: %s", e)


def count_session_turns(user_id: int) -> int:
    try:
        res = (
            supabase.table("session_history")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .execute()
        )
        return res.count or 0
    except Exception as e:
        logger.error("count_session_turns error: %s", e)
        return 0


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
    if memory["profile"]:
        parts.append(f"[Student profile: {json.dumps(memory['profile'])}]")
    if memory["summary"]:
        parts.append(f"[Previous sessions summary: {memory['summary']}]")
    return "\n".join(parts)


async def get_gemini_response(user_id: int, user_message: str) -> str:
    try:
        history = get_session_history(user_id)
        context = build_context_message(user_id)

        # Inject long-term memory on the first turn of a fresh session
        augmented_message = f"{context}\n\n{user_message}" if (context and not history) else user_message

        chat = gemini_model.start_chat(history=history)
        response = chat.send_message(augmented_message)
        return response.text

    except Exception as e:
        logger.error("Gemini API error for user %s: %s", user_id, e)
        return (
            "Sorry, I'm having a bit of trouble right now. Please try again in a moment. "
            "If this keeps happening, contact the Bhavishyat team."
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


# ── Telegram Helpers ─────────────────────────────────────────────────────────
TELEGRAM_MAX_LENGTH = 4096

async def send_long_message(update: Update, text: str):
    """Split messages that exceed Telegram's 4096-char limit and send in parts."""
    if len(text) <= TELEGRAM_MAX_LENGTH:
        await update.message.reply_text(text)
        return

    # Split cleanly on newlines where possible
    parts = []
    while text:
        if len(text) <= TELEGRAM_MAX_LENGTH:
            parts.append(text)
            break
        # Find the last newline within the limit
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH  # No newline found, hard split
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()

    for part in parts:
        await update.message.reply_text(part)


# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("User %s (%s) started the bot", user.id, user.username)

    memory = get_user_memory(user.id)
    is_returning = bool(memory["profile"] or memory["summary"])

    # Always ensure a row exists in user_memory from first contact
    if not is_returning:
        update_user_memory(user.id, user.username or "", user.first_name, {}, "")

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
            "I'm *Bhavishyat*, your career counsellor. I'm here to help you think through "
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
        "*Bhavishyat Career Counsellor* 🎓\n\n"
        "I can help you with:\n"
        "• Career paths after 10th, Intermediate, or Degree\n"
        "• Entrance exams: EAMCET, NEET, JEE, POLYCET, ICET\n"
        "• College options in Andhra Pradesh\n"
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
    clear_session_history(user.id)
    await update.message.reply_text(
        "Conversation cleared! 🔄 I still remember your profile from before.\n\nWhat would you like to talk about?",
        parse_mode="Markdown"
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    memory = get_user_memory(user.id)

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

    logger.info("Message from %s (%s): %s", user.id, user.username, user_message[:100])
    log_message(user.id, user.username or "", "user", user_message)

    if detect_crisis(user_message):
        logger.warning("Crisis keywords detected for user %s", user.id)
        log_message(user.id, user.username or "", "system", "[CRISIS KEYWORDS DETECTED]")
        await update.message.reply_text(CRISIS_RESPONSE, parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    save_to_session_history(user.id, "user", user_message)
    bot_response = await get_gemini_response(user.id, user_message)
    save_to_session_history(user.id, "model", bot_response)
    log_message(user.id, user.username or "", "assistant", bot_response)

    await update_profile_from_conversation(user.id, user.username or "", user.first_name)
    await send_long_message(update, bot_response)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice messages:
    1. Download OGG audio from Telegram
    2. Transcribe via Gemini native audio understanding
    3. Feed transcription into normal handle_message flow
    """
    user = update.effective_user
    logger.info("Voice message from %s (%s)", user.id, user.username)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # Download voice note bytes from Telegram
        voice    = update.message.voice
        tg_file  = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        # Transcribe using Gemini native audio understanding
        # Telegram voice notes are OGG/OPUS — Gemini supports this natively
        transcribe_model = genai.GenerativeModel(model_name=MODEL_NAME)
        transcription_result = transcribe_model.generate_content([
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

        # Echo the transcription back so the student knows what was heard
        await update.message.reply_text(
            f"🎤 _I heard:_ \"{transcribed_text}\"",
            parse_mode="Markdown"
        )

        # Log transcription as the user's message
        log_message(user.id, user.username or "", "user", f"[VOICE] {transcribed_text}")

        # Crisis check on transcribed text
        if detect_crisis(transcribed_text):
            logger.warning("Crisis keywords detected (voice) for user %s", user.id)
            log_message(user.id, user.username or "", "system", "[CRISIS KEYWORDS DETECTED - VOICE]")
            await update.message.reply_text(CRISIS_RESPONSE, parse_mode="Markdown")
            return

        # Feed into normal conversation flow
        save_to_session_history(user.id, "user", transcribed_text)
        bot_response = await get_gemini_response(user.id, transcribed_text)
        save_to_session_history(user.id, "model", bot_response)
        log_message(user.id, user.username or "", "assistant", bot_response)

        await update_profile_from_conversation(user.id, user.username or "", user.first_name)
        await send_long_message(update, bot_response)

    except Exception as e:
        logger.error("Voice handling error for user %s: %s", user.id, e)
        await update.message.reply_text(
            "Sorry, I had trouble processing that voice message. "
            "Could you try again or type your question? 😊"
        )


async def update_profile_from_conversation(user_id: int, username: str, first_name: str):
    """Lightweight Gemini call to extract profile facts every 4 turns."""
    turn_count = count_session_turns(user_id)
    if turn_count % 4 != 0:
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
def main():
    global gemini_model

    for var, name in [
        (TELEGRAM_TOKEN, "TELEGRAM_TOKEN"),
        (GEMINI_API_KEY, "GEMINI_API_KEY"),
        (SUPABASE_URL,   "SUPABASE_URL"),
        (SUPABASE_KEY,   "SUPABASE_KEY"),
    ]:
        if not var:
            raise ValueError(f"{name} not set in .env")

    init_supabase()
    gemini_model = setup_gemini()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("reset",   reset_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(error_handler)

    logger.info("Bhavishyat bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
