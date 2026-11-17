"""
Knowledge base retrieval — drop-in addition for bot.py (all variants:
Gemini, OpenAI, Claude).

WHAT THIS DOES
--------------
Before the model generates a response, this queries your `knowledge_base`
table (already set up via knowledge_base_setup.sql) for entries relevant
to the student's message, and — if anything relevant is found — injects
it into the prompt as an authoritative context block.

WHY THIS MATTERS
-----------------
Without this, "which college in Anantapur offers Nursing?" gets answered
from the model's general training knowledge, which is exactly where
hallucination risk is highest for hyper-local AP details. With this,
the model is instructed to defer to your NGO's verified handbook data
for those specifics instead of guessing.


STEP 1 — Add this function to bot.py (near your other DB helper functions)
----------------------------------------------------------------------------
"""

def get_knowledge_base_context(query_text: str, max_results: int = 4) -> str:
    """
    Search the knowledge_base table for entries relevant to the student's
    message. Returns a formatted context block to inject into the prompt,
    or an empty string if nothing relevant is found (in which case the
    model falls back to the "say honestly you don't know" instruction
    in the system prompt, rather than being fed nothing and guessing).
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
            "[VERIFIED KNOWLEDGE BASE — Bhavishyat's own NGO resources. "
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


"""
STEP 2 — Call it on every turn and prepend to the message sent to the model
------------------------------------------------------------------------------
Find where the user's message is sent to the model (get_gemini_response /
get_claude_response / the OpenAI equivalent) and add the KB lookup BEFORE
the model call, on every turn — not just the first turn like the
profile/summary context. Students ask specific-fact questions at any
point in a conversation, not only in their opening message.

Example for the Claude variant (get_claude_response in bot.py):

    async def get_claude_response(user_id: int, user_message: str) -> str:
        try:
            history = get_session_history(user_id)
            context = build_context_message(user_id)
            kb_context = get_knowledge_base_context(user_message)   # <-- NEW

            pieces = [p for p in [kb_context, context if not history else ""] if p]
            augmented_message = "\n\n".join(pieces + [user_message]) if pieces else user_message

            messages = list(history)
            messages.append({"role": "user", "content": augmented_message})

            response = claude_client.messages.create(
                model=MODEL_NAME,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            return response.content[0].text
        ...

Apply the equivalent change to the Gemini and OpenAI variants so all
three bots behave consistently — otherwise your model comparison eval
stops being a fair comparison once only one bot has grounded answers.


STEP 3 — Add this to your system prompt (prompt.txt / SYSTEM_PROMPT)
------------------------------------------------------------------------
Paste this in as a new section, near the "WHAT YOU KNOW" section:

    KNOWLEDGE BASE PRIORITY:
    Some messages will include a block starting with "[VERIFIED KNOWLEDGE
    BASE...]". When present, treat that information as the authoritative
    source for any college names, scholarship details, eligibility
    criteria, or exam specifics it covers — even if it differs from what
    you already know. Do not supplement or contradict it with your own
    general knowledge for those specific facts.

    If no knowledge base block is present and a student asks about a
    specific college, an exact scholarship amount, or a district-specific
    detail you cannot verify, say so honestly rather than guessing —
    for example: "I don't have verified details on that specific college
    — I'd recommend checking bie.ap.gov.in or apsche.ap.gov.in, or asking
    the Bhavishyat team directly." Never invent a college name, cutoff,
    or scholarship figure that isn't backed by the knowledge base or
    something you're genuinely confident about.


STEP 4 — Test that it's actually working
------------------------------------------------------------------------
Add a couple of entries to eval_queries.json:
  - One query where the knowledge base HAS a real answer — check the
    model actually uses the specific details from your handbook rather
    than paraphrasing its own general knowledge.
  - One query about a real-sounding but absent entity (a district/college
    combo you know isn't in your knowledge base yet) — check the model
    honestly says it doesn't know rather than guessing.
This is worth re-running through comparison_report.md, since grounding
quality is exactly the kind of thing that can differ between Gemini and
OpenAI even with identical retrieved context.
"""
