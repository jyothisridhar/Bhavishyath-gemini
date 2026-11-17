"""
Consent and intake content: texts, option lists and step logic (tracker B1-B6).

Everything a student reads during consent, intake and data deletion lives here.
The database and Telegram wiring stay in bot_gemini_fast.py.

The Telugu text is a first draft. A Telugu speaker must check it, and the
Bhavishyath team must approve the consent wording, before launch.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CONSENT_VERSION = "v1"

# ── Option lists: (stored value, English label, Telugu label) ─────────────────
LANGUAGES = [
    ("telugu",  "తెలుగు",           "తెలుగు"),
    ("english", "English",          "English"),
    ("mix",     "తెలుగు + English", "తెలుగు + English"),
]

CLASS_YEARS = [
    ("Class 8",               "8th class",      "8వ తరగతి"),
    ("Class 9",               "9th class",      "9వ తరగతి"),
    ("Class 10",              "10th class",     "10వ తరగతి"),
    ("Intermediate 1st year", "Inter 1st year", "ఇంటర్ 1వ సంవత్సరం"),
    ("Intermediate 2nd year", "Inter 2nd year", "ఇంటర్ 2వ సంవత్సరం"),
    ("Degree",                "Degree",         "డిగ్రీ"),
    ("Other",                 "Other",          "ఇతర"),
]

STREAMS = [
    ("MPC",   "MPC",   "MPC"),
    ("BiPC",  "BiPC",  "BiPC"),
    ("MEC",   "MEC",   "MEC"),
    ("CEC",   "CEC",   "CEC"),
    ("HEC",   "HEC",   "HEC"),
    ("Other", "Other", "ఇతర"),
]

# The 26 districts of Andhra Pradesh. "Anantapur" follows the Bhavishyath handbook spelling.
AP_DISTRICTS = [
    "Alluri Sitharama Raju", "Anakapalli", "Anantapur", "Annamayya", "Bapatla",
    "Chittoor", "East Godavari", "Eluru", "Guntur", "Kadapa", "Kakinada",
    "Konaseema", "Krishna", "Kurnool", "Nandyal", "Nellore", "NTR", "Palnadu",
    "Parvathipuram Manyam", "Prakasam", "Sri Sathya Sai", "Srikakulam",
    "Tirupati", "Visakhapatnam", "Vizianagaram", "West Godavari",
]
OTHER_DISTRICT = ("Other", "Other (another state or place)", "ఇతర (వేరే రాష్ట్రం / ప్రాంతం)")

OPTIONS = {
    "language":     LANGUAGES,
    "class_year":   CLASS_YEARS,
    "stream_group": STREAMS,
    "district":     [(d, d, d) for d in AP_DISTRICTS] + [OTHER_DISTRICT],
}

# Which user_memory column each intake step fills
COLUMN = {
    "language":      "language",
    "class_year":    "class_year",
    "stream_group":  "stream_group",
    "district":      "district",
    "district_text": "district",
    "name":          "name",
}

BUTTON_COLUMNS = {"language": 1, "class_year": 2, "stream_group": 3, "district": 2, "name": 1}

# ── Texts: (English, Telugu) ──────────────────────────────────────────────────
TEXT = {
    "consent": (
        "Before we start, a few things you should know:\n\n"
        "- I am an AI career guide from Bhavishyath, not a person.\n"
        "- I will save what you tell me here (your class, your interests and our chat) "
        "so I do not have to ask you again.\n"
        "- Bhavishyath counsellors may read chats, with your name and details removed, "
        "to make this tool better.\n"
        "- You, your parents or your school can ask us to delete your information "
        "at any time. Send /delete_my_data.\n"
        "- I do not replace talking to a counsellor, a teacher or someone you trust.\n\n"
        "Is that okay with you?",

        "ప్రారంభించే ముందు, మీరు తెలుసుకోవాల్సిన కొన్ని విషయాలు:\n\n"
        "- నేను భవిష్యత్ నుండి వచ్చిన AI కెరీర్ గైడ్‌ని, మనిషిని కాదు.\n"
        "- మీరు ఇక్కడ చెప్పిన వివరాలను (మీ తరగతి, ఆసక్తులు, మన సంభాషణ) నేను భద్రపరుస్తాను, "
        "అందువల్ల మళ్లీ మళ్లీ అడగాల్సిన అవసరం ఉండదు.\n"
        "- ఈ టూల్‌ను మెరుగుపరచడానికి, భవిష్యత్ కౌన్సెలర్లు మీ పేరు, వివరాలు తొలగించి "
        "సంభాషణలను చదవవచ్చు.\n"
        "- మీరు, మీ తల్లిదండ్రులు లేదా మీ పాఠశాల ఎప్పుడైనా మీ సమాచారాన్ని తొలగించమని అడగవచ్చు. "
        "/delete_my_data పంపండి.\n"
        "- కౌన్సెలర్‌తో, ఉపాధ్యాయులతో లేదా మీరు నమ్మే వ్యక్తులతో మాట్లాడటానికి నేను ప్రత్యామ్నాయం కాదు.\n\n"
        "ఇది మీకు సమ్మతమేనా?",
    ),
    "consent_declined": (
        "No problem. I have not saved anything. If you change your mind, send /start.",
        "సరే. నేను ఏమీ భద్రపరచలేదు. మీ అభిప్రాయం మారితే /start పంపండి.",
    ),
    "language": (
        "Which language would you like to chat in?",
        "మీరు ఏ భాషలో మాట్లాడాలనుకుంటున్నారు?",
    ),
    "class_year": (
        "Which class or year are you in?",
        "మీరు ఏ తరగతి లేదా సంవత్సరంలో ఉన్నారు?",
    ),
    "stream_group": (
        "Which group are you studying?",
        "మీరు ఏ గ్రూప్ చదువుతున్నారు?",
    ),
    "district": (
        "Which district do you live in?",
        "మీరు ఏ జిల్లాలో ఉంటారు?",
    ),
    "district_text": (
        "Please type your district and state.",
        "దయచేసి మీ జిల్లా మరియు రాష్ట్రం టైప్ చేయండి.",
    ),
    "name": (
        "What should I call you? Type your name, or tap the button.",
        "మిమ్మల్ని ఏమని పిలవాలి? మీ పేరు టైప్ చేయండి, లేదా బటన్ నొక్కండి.",
    ),
    "done": (
        "Thanks, {name}. What is on your mind today?",
        "ధన్యవాదాలు, {name}. ఈ రోజు మీ మనసులో ఏముంది?",
    ),
    "welcome_back": (
        "Welcome back, {name}. What would you like to explore today?",
        "మళ్లీ స్వాగతం, {name}. ఈ రోజు ఏం తెలుసుకోవాలనుకుంటున్నారు?",
    ),
    "delete_confirm": (
        "Do you want me to ask the Bhavishyath team to delete everything saved about you, "
        "including your profile and our chats?",
        "మీ గురించి భద్రపరచిన సమాచారం మొత్తాన్ని (ప్రొఫైల్, మన సంభాషణలు) తొలగించమని "
        "భవిష్యత్ బృందాన్ని అడగాలా?",
    ),
    "delete_sent": (
        "I have sent your request to the Bhavishyath team. They will delete your information.",
        "మీ అభ్యర్థనను భవిష్యత్ బృందానికి పంపాను. వారు మీ సమాచారాన్ని తొలగిస్తారు.",
    ),
    "delete_already": (
        "Your request is already with the team.",
        "మీ అభ్యర్థన ఇప్పటికే బృందం వద్ద ఉంది.",
    ),
    "delete_cancelled": (
        "Okay, nothing has been deleted.",
        "సరే, ఏమీ తొలగించలేదు.",
    ),
    "delete_nothing": (
        "I do not have any information saved about you.",
        "మీ గురించి నా దగ్గర ఎలాంటి సమాచారం లేదు.",
    ),
}

BUTTONS = {
    "consent_yes":  ("Yes, I agree", "అవును, సమ్మతమే"),
    "consent_no":   ("No, not now",  "ఇప్పుడు వద్దు"),
    "call_me":      ("Call me {name}", "{name} అని పిలవండి"),
    "delete_yes":   ("Yes, delete my data", "అవును, నా సమాచారం తొలగించండి"),
    "delete_no":    ("No, keep it", "వద్దు"),
}


def tr(pair, lang):
    """Pick the Telugu text for Telugu speakers, English for English and mix."""
    return pair[1] if lang == "telugu" else pair[0]


def text(key, lang, **kwargs):
    """Student-facing text. With lang=None (language not chosen yet) shows both languages."""
    en, te = TEXT[key]
    if lang is None:
        return f"{en}\n\n{te}".format(**kwargs)
    return tr((en, te), lang).format(**kwargs)


# ── Step logic ────────────────────────────────────────────────────────────────
def needs_stream(fields):
    return fields.get("class_year") in ("Intermediate 1st year", "Intermediate 2nd year")


def next_step(fields):
    """The next intake question for a student, given what is already known.
    Only the gating fields are asked (tracker B5). Returns None when intake is done."""
    if not fields.get("language"):
        return "language"
    if not fields.get("class_year"):
        return "class_year"
    if needs_stream(fields) and not fields.get("stream_group"):
        return "stream_group"
    if not fields.get("district"):
        return "district"
    if not fields.get("name"):
        return "name"
    return None


def prompt(step, lang):
    if step == "language":
        return text("language", None)
    return text(step, lang)


def choice_value(step, idx):
    """The stored value for button number idx of a step, or None if idx is out of range."""
    options = OPTIONS[step]
    return options[idx][0] if 0 <= idx < len(options) else None


def is_other_district(idx):
    return idx == len(AP_DISTRICTS)


# ── Keyboards (callback data: c:<0|1> consent, i:<step>:<n> intake, d:<0|1> delete) ──
def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def keyboard(step, lang, first_name=None):
    """Buttons for an intake step, or None for steps answered by typing."""
    if step == "name":
        if not first_name:
            return None
        label = tr(BUTTONS["call_me"], lang).format(name=first_name)
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="i:name:0")]])
    if step not in OPTIONS:
        return None
    pick = 2 if lang == "telugu" else 1
    buttons = [
        InlineKeyboardButton(opt[pick], callback_data=f"i:{step}:{i}")
        for i, opt in enumerate(OPTIONS[step])
    ]
    return InlineKeyboardMarkup(_rows(buttons, BUTTON_COLUMNS[step]))


def consent_text():
    return text("consent", None)


def consent_keyboard():
    yes = f"{BUTTONS['consent_yes'][0]} / {BUTTONS['consent_yes'][1]}"
    no = f"{BUTTONS['consent_no'][0]} / {BUTTONS['consent_no'][1]}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(yes, callback_data="c:1")],
        [InlineKeyboardButton(no, callback_data="c:0")],
    ])


def delete_keyboard(lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(BUTTONS["delete_yes"], lang), callback_data="d:1")],
        [InlineKeyboardButton(tr(BUTTONS["delete_no"], lang), callback_data="d:0")],
    ])
