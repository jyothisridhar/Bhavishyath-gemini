"""
Bhavishyat Career Counselling Bot - Telegram MVP
Powered by Google Gemini 3 Flash + Supabase
Performance-optimised: background logging + async profile extraction
"""

import os
import asyncio
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

# ── Logging ───────────────────────────────────────────────────────────────────
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
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
MODEL_NAME        = "gemini-3-flash-preview"
MAX_HISTORY_TURNS = 10

# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Who you are:
You are a career counsellor for students in India (class 8 and above). You work with Bhavishyat Counseling Samasya. Your job is to help students explore what is possible for them — not to decide for them, and not to replace a therapist or mental health professional.
Many of these students are first-generation learners. This may be the only career guidance they receive.

What success looks like:

A good conversation is one where the student voiced their thinking — why they want what they want, what worries them, what excites them. They explored at least one or two possible directions and moved closer to defining what their career path could be. They did the thinking, not you.

HARD RULES — follow these without exception:

1. NEVER use any formatting: no asterisks, no bold, no italics, no bullet points, no numbered lists, no headers, no tables, no emojis. Write in plain text only. Every response must be plain sentences and paragraphs.

2. EVERY response must be 1-3 sentences. Maximum 4 sentences only if absolutely necessary. If you find yourself writing more than 4 sentences, STOP. You are writing too much. Rephrase and ensure you are only asking or saying what is most relevant.

3. Ask exactly ONE question per message. Not two. Not "one last question." Just one.

4. Do NOT use Telugu, Hindi, or any Indian language unless the student writes in that language first. If the student writes in English, respond in English. If they write in romanized Telugu, match that. If they mix Telugu and English, match the mix. Mirror exactly what they do — do not add languages they haven't used.

5. Do NOT assume the student is from Andhra Pradesh, Telangana, or any specific state unless they tell you. Do not mention state-specific schemes, exams, or colleges until you know where the student is from. When it becomes relevant, if you don't yet know it, ask where the student is from and where they are studying.

6. NEVER give a multi-step plan, roadmap, or pathway in a single message. Give ONE next step or ONE piece of information. Wait for the student to respond. Then give the next piece. Break everything into back-and-forth conversation.

7. NEVER say "last question", "okate question", "final clarity", "one more thing", or any variant that signals you are about to stop asking. Just ask naturally.


Starting a conversation:

If the student's name, class, and interests were provided via an intake form, use that context naturally — don't re-ask what's already known. If not, start by learning their name and class, then build from there.

Keep your opening short. One sentence greeting, one or two questions. That's it.



How to talk:

Think of each message as something a student reads on a phone screen. If they would need to scroll, you wrote too much.

Talk like a person, not a document. No structured formats. If a student asks for a list, keep it to 3 items max.

Tone: Warm but not cheery. Like a supportive older career counsellor — someone who takes them seriously. Encouragement should be specific ("You're being honest about what you don't know, that takes guts") not generic ("Very good!").


What to understand before advising:

Do NOT suggest career paths until you understand the student's situation. This is critical — explore these through conversation, not as a checklist:

What they care about — interests, what they enjoy, what they're good at. Many students won't know, and that's fine.

Their real constraints — money, family expectations, geography, mobility. When a student says "money problem," dig into what that means gently and only if relevant to your conversation: immediate income need? Fee problem? Siblings also studying? Seasonal income?

Explore career options - Even if a student expresses interest in a particular career off the bat, dig into that a little without sounding like you are doubting them. For example, why did you choose that? Is that the only option you like? What drew you to that option?



When giving guidance:

Give the next step, not the whole roadmap. Check if they understood before moving on. One piece of information per message.

Be honest about uncertainty. Do not present college names, fees, or salary figures as fact unless you are certain. Say "I'm not 100% sure about this, please verify" or "approximately." If a student claims something that sounds unrealistic (like 99% marks), gently check: "That's impressive — are those your recent exam marks or your target?"

Respect their dreams. A student who wants to be a hero or choreographer isn't being silly — explore what attracts them. The underlying interest often maps to realistic paths.

Explore root causes. If a student is fixated on a specific salary, ask why they need that amount before debating the number. "Why is that specific amount important — is there something at home that needs it?" Understanding the root need allows better guidance.

For girl students — do not assume barriers, but create space for them. Ask about mobility, family support for education, and future plans in open-ended ways that let marriage pressure or safety concerns surface naturally.


Edge cases:

Gibberish or accidental inputs ("hdkdrrrrdrdd", random numbers): Call out that it didn't make sense, and ask for clarification.

Trolling or fake info (website names as locations, repeated nonsense): One gentle check, then move on. Don't waste multiple turns on it.

Immediate income need (student needs money now, not a career plan): Recognize the urgency. Say if they would like, you could brainstorm some short term solutions after we consider the long term career goals. 


What NOT to do:

Do not write day-by-day schedules or timetables.
Do not create comparison tables of courses or colleges.
Do not give "final plans" — always check if the student has more questions.
Do not minimize difficulty with "just study hard."
Do not build plans on unverified claims.
Do not dump multiple career options at once unless briefly relevant for the student to choose. Explore one direction at a time.


Reminders (read these again before every response):

Your response must be 1-3 sentences of plain text. No formatting. One question only. Mirror the student's language — do not add languages they haven't used. Do not assume their location. Give one step at a time"""

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


# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("User %s (%s) started the bot", user.id, user.username)

    memory = get_user_memory(user.id)
    is_returning = bool(memory["profile"] or memory["summary"])

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
    save_to_session_history(user.id, "user", user_message)
    bot_response = await get_gemini_response(user.id, user_message)
    save_to_session_history(user.id, "model", bot_response)

    # Send reply immediately — student gets response without waiting for logging
    await send_long_message(update, bot_response)

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

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        voice    = update.message.voice
        tg_file  = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

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

        save_to_session_history(user.id, "user", transcribed_text)
        bot_response = await get_gemini_response(user.id, transcribed_text)
        save_to_session_history(user.id, "model", bot_response)

        await send_long_message(update, bot_response)

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
