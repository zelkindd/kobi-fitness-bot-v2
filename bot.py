import logging
import re
import base64
import asyncio
import json
from datetime import date
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from dotenv import load_dotenv
from openai import OpenAI
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = 5748496029

ai = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

conversation_history = []
MAX_HISTORY = 20

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["📊 סיכום", "🗓 תכנית"], ["📈 התקדמות", "❓ עזרה"]],
    resize_keyboard=True
)

BUTTON_PROMPTS = {
    "📊 סיכום": "תן לי סיכום שבועי של הריצות והמשקל שלי.",
    "🗓 תכנית": "מה תכנית האימונים שלי לשבוע הנוכחי? מה האימון הבא שלי?",
    "📈 התקדמות": "הצג לי את ההתקדמות שלי בפורמט הקבוע.",
    "❓ עזרה": "רשום לי בעברית את כל מה שאתה יכול לעשות בשבילי.",
}

ONBOARDING_STEPS = ["user_name", "personality", "nutrition_rules", "weight_goal",
                    "current_weight", "weight_target", "training_days", "age"]
ONBOARDING_QUESTIONS = {
    "user_name": "היי! אני קובי, המאמן האישי שלך. מה שמך?",
    "personality": "נעים מאוד! איך אתה רוצה שאאמן אותך — ישיר, מעודד, קשוח, או ידידותי?",
    "nutrition_rules": "מובן. מה העקרונות התזונתיים שלך? למשל דל פחמימות, עשיר חלבון, צום לסירוגין, ללא הגבלות.",
    "weight_goal": "מה המטרה שלך במשקל? לרדת, לשמור, לעלות — ספר לי.",
    "current_weight": "מה משקלך הנוכחי בק״ג?",
    "weight_target": "מה משקל היעד שלך בק״ג? (אפשר לשלוח 'דלג')",
    "training_days": "כמה ימים בשבוע אתה רוצה להתאמן? רק מספר.",
    "age": "מה גילך? (עוזר לי לחשב אזורי דופק — אפשר לשלוח 'דלג')",
}


def add_to_history(role: str, content: str):
    conversation_history.append({"role": role, "content": content})
    while len(conversation_history) > MAX_HISTORY:
        conversation_history.pop(0)


# ── MCP Client ────────────────────────────────────────────────────────────────

_PYTHON = os.path.join(os.path.dirname(__file__), "venv", "bin", "python3")
if not os.path.exists(_PYTHON):
    _PYTHON = "python3"

SQLITE_SERVER = StdioServerParameters(
    command=_PYTHON,
    args=[os.path.join(os.path.dirname(__file__), "mcp_sqlite_server.py")],
    env=None
)

STRAVA_SERVER = StdioServerParameters(
    command=_PYTHON,
    args=[os.path.join(os.path.dirname(__file__), "mcp_strava_server.py")],
    env=None
)

# Cached at startup — tool schemas never change at runtime
_tools_cache: list = []
# Maps tool name → which server params to use
_tool_server_map: dict = {}


async def _load_tools():
    """Discover all tools from both servers and cache them."""
    global _tools_cache, _tool_server_map
    _tools_cache = []
    _tool_server_map = {}
    for server_params in [SQLITE_SERVER, STRAVA_SERVER]:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                for t in result.tools:
                    _tools_cache.append({
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.inputSchema,
                    })
                    _tool_server_map[t.name] = server_params
    logging.info(f"Loaded {len(_tools_cache)} tools from MCP servers")


async def call_tool(tool_name: str, tool_input: dict) -> str:
    """Call a tool on the correct MCP server."""
    server_params = _tool_server_map.get(tool_name)
    if not server_params:
        return f"כלי לא נמצא: {tool_name}"
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, tool_input)
            if result.content:
                return result.content[0].text
            return "בוצע"


async def run_agent(user_message: str, image_data: dict = None) -> str:
    """
    Agent loop: send message to DeepSeek with tools, execute tool calls,
    loop until DeepSeek gives a final text answer.
    """
    try:
        ctx_raw = await call_tool("get_current_context", {})
        ctx = json.loads(ctx_raw)
    except Exception:
        ctx = {}
    system_prompt = build_system_prompt(context=ctx)

    tools = _tools_cache

    messages = [{"role": "system", "content": system_prompt}] + list(conversation_history)
    if image_data:
        messages.append({"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": f"data:{image_data['media_type']};base64,{image_data['data']}"
            }},
            {"type": "text", "text": user_message}
        ]})
    else:
        messages.append({"role": "user", "content": user_message})

    # Convert MCP tool schemas to OpenAI function format
    openai_tools = [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"]
        }} for t in tools
    ]

    while True:
        response = ai.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=openai_tools,
            tool_choice="auto"
        )

        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                tool_input = json.loads(tc.function.arguments)
                result = await call_tool(tc.function.name, tool_input)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result
                })
        else:
            return msg.content or "אין תשובה"


KOBI_VERSION = "2.0 (MCP Agent)"

def build_system_prompt(context: dict = None) -> str:
    ctx_block = ""
    if context:
        lines = ["מצב נוכחי:"]
        lines.append(f"היום: {context.get('today_hebrew', context.get('today_iso', 'לא ידוע'))}")
        days = context.get("days_since_last_run")
        if days is not None:
            lines.append(f"ימים מהריצה האחרונה: {days}")
        else:
            lines.append("אין ריצות רשומות עדיין")
        last = context.get("last_run")
        if last:
            wt = f", {last['workout_type']}" if last.get("workout_type") else ""
            lines.append(f"ריצה אחרונה: {last.get('date', '')}, {last.get('distance_km', '')} ק״מ{wt}")
        pos = context.get("current_plan_position")
        if pos:
            lines.append(f"מיקום בתכנית: שבוע {pos.get('week')}, ריצה {pos.get('run_num')}")
        ctx_block = "\n".join(lines) + "\n\n"

    return (
        f"אתה קובי, מאמן כושר ותזונה אישי. גרסה: {KOBI_VERSION}. "
        "תמיד כתוב בעברית בלבד — אף מילה באנגלית.\n\n"
        + ctx_block +
        "בתחילת כל תשובה, לפני שאתה כותב מילה, קרא בשקט (ללא הודעה למשתמש):\n"
        "1. get_profile — כדי לדעת פרטי המשתמש.\n"
        "2. get_plan_position — כדי לדעת השבוע והריצה הנוכחיים.\n"
        "3. get_recent_runs(limit=3) — כדי לדעת מתי ומה הריצה האחרונה.\n"
        "המידע הזה הוא האמת. אם משהו בשיחה סותר את מה שקראת — האמן בנתוני ה-DB, "
        "אלא אם המשתמש מתקן אותם במפורש.\n\n"

        "אם המשתמש אומר שהוא כבר סיים שבוע מסוים, ריצה מסוימת, או מתקן מיקום בתכנית — "
        "קרא מיד ל-set_plan_position עם הערכים הנכונים לפני שאתה עונה. "
        "אם הוא מתאר ריצה שלא שמורה ב-DB — קרא ל-log_run ושמור אותה. "
        "לעולם אל תאמר 'עדכנתי' בלי לקרוא בפועל לכלי המתאים.\n\n"

        "אתה מאמן, לא משרת. תן המלצה ברורה אחת ועצור. "
        "אסור לסיים הודעה ב'מה אתה אומר?', 'מה אתה מעדיף?', 'האם זה מתאים לך?', "
        "או כל שאלת אישור אחרת. שאל שאלה רק אם כוונת המשתמש עמומה לחלוטין — "
        "ואם כן, שאל שאלה אחת בלבד. "
        "התאם את אורך התשובה לשאלה: שאלה פשוטה = משפט אחד; שאלה מורכבת = עד 4 משפטים. "
        "אל תוסיף עצות שלא התבקשו.\n\n"

        "אסור בהחלט להשתמש ב: כוכביות (**טקסט**), קו תחתי (_טקסט_), "
        "כותרות (#, ##), רשימות נקודות (•, -, *), רשימות ממוספרות, או backticks. "
        "כתוב פסקאות קצרות בלבד, כמו הודעת וואטסאפ.\n\n"

        "אחרי כל ריצה שנשמרת — כתוב סיכום ריצה בפורמט הקבוע הזה בדיוק (5 שורות, בלי להוסיף ובלי לדלג):\n"
        "ריצה: [מרחק] ק״מ | [קצב]/ק״מ | דופק [HR]\n"
        "פיצולים: [שורה אחת — יצאת מהר/סיום חזק/עקבי — רק מה שרלוונטי, אחרת 'קצב אחיד']\n"
        "לעומת תכנית: [plan_msg_he מתוך תשובת log_run]\n"
        "דופק: [קרא get_hr_zones והשווה ל-HR בפועל — אזור X, משפט אחד]\n"
        "הבא: [קרא get_next_planned_workout — סוג ריצה + מרחק]\n\n"
        "לגבי פיצולים: אם יש km_splits — שמור עם log_km_splits ובדוק: "
        "1) ק״מ 1 מהיר ב-15+ שניות מהממוצע = יצאת מהר. "
        "2) ק״מ אחרון מהיר מק״מ ראשון = סיום חזק. "
        "3) הפרש > 30 שניות בין הכי מהיר לאיטי = לא עקבי. "
        "אם אין splits — כתוב 'אין נתוני פיצולים'.\n\n"
        "לגבי pace trend: אם המשתמש רץ מהר ב-15+ שניות מהיעד ב-3 ריצות רצופות, "
        "קרא update_workout_paces ועדכן יעד ב-5-10 שניות. ספר לו. אל תקשה יותר מ-10 שניות.\n\n"

        "כשמשתמש שואל על התקדמות או לוחץ על כפתור ההתקדמות:\n"
        "קרא את הכלים הבאים לפני שאתה כותב מילה: get_weekly_stats, get_hr_pace_analysis, get_next_planned_workout, get_profile.\n"
        "אחר כך כתוב תשובה בפורמט הקבוע הזה בדיוק — אותם כותרות, אותו סדר, בלי להוסיף ובלי לדלג:\n\n"
        "ריצות השבוע: [סה״כ ק״מ] ק״מ ב-[מספר ריצות] ריצות\n"
        "[לכל ריצה שורה אחת: יום תאריך — X ק״מ קצב Y דופק Z (אם יש דופק)]\n\n"
        "דופק מול קצב: [משפט אחד מה-get_hr_pace_analysis — האם הכושר משתפר, יציב, או יורד]\n\n"
        "משקל: [משקל אחרון] ק״ג — נשאר [הפרש] ק״ג ליעד\n\n"
        "תכנית: שבוע [X] ריצה [Y] — הבא: [סוג ריצה, מרחק, קצב יעד]\n\n"
        "אל תוסיף כלום מעבר לפורמט הזה. אל תסיים בשאלה.\n\n"

        "כשמשתמש שואל על דופק, אם הקצב מתאים לו, או מה הקצב לריצה הבאה:\n"
        "1. קרא get_next_planned_workout לדעת את סוג הריצה הבאה.\n"
        "2. קרא get_hr_zones לחשב את אזורי הדופק (max HR כבר שמור בפרופיל).\n"
        "3. קרא get_runs_by_type לראות ריצות אחרונות מאותו סוג — קצב ודופק ממוצע.\n"
        "4. השווה: באיזה אזור דופק רץ בפועל? מה הקצב שיעביר אותו לאזור הנכון?\n"
        "   ריצה קלה/ארוכה → אזור 2 (60-70% max HR). טמפו → אזור 4. אינטרוולים → אזור 5.\n"
        "5. המלץ על קצב קונקרטי (לדוגמה: 6:45-7:00 לק״מ) שמבוסס על הנתונים האמיתיים שלו — "
        "לא על טבלאות גנריות. אל תמליץ על שינוי של יותר מ-15 שניות/ק״מ ביחס לריצה האחרונה "
        "מאותו סוג כדי לא לפצוע אותו. אם הדופק בריצות האחרונות היה גבוה מהאזור המומלץ — "
        "תסביר שצריך להאט ותן קצב בטוח ספציפי."
    )


# ── Onboarding ────────────────────────────────────────────────────────────────

async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["onboarding_step"] = 0
    await update.message.reply_text(ONBOARDING_QUESTIONS["user_name"])


async def advance_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    step_idx = context.user_data["onboarding_step"]
    step = ONBOARDING_STEPS[step_idx]

    if step == "training_days":
        match = re.search(r"\d+", text)
        if not match:
            await update.message.reply_text("רק מספר, כמו 3 או 5.")
            return
        await call_tool("update_profile", {"field": "training_days", "value": str(max(1, min(7, int(match.group()))))})
    elif step == "age":
        match = re.search(r"\d+", text)
        if match:
            await call_tool("update_profile", {"field": "age", "value": match.group()})
        # no match = skip
    elif step == "current_weight":
        match = re.search(r"\d+\.?\d*", text)
        if not match:
            await update.message.reply_text("שלח מספר, כמו 82 או 78.5.")
            return
        await call_tool("log_weight", {"weight_kg": float(match.group())})
    elif step == "weight_target":
        match = re.search(r"\d+\.?\d*", text)
        if match:
            await call_tool("update_profile", {"field": "weight_target", "value": match.group()})
    else:
        await call_tool("update_profile", {"field": step, "value": text.strip()})

    next_idx = step_idx + 1
    if next_idx >= len(ONBOARDING_STEPS):
        context.user_data.pop("onboarding_step", None)
        profile_raw = await call_tool("get_profile", {})
        try:
            profile = json.loads(profile_raw)
            name = profile.get("user_name", "")
        except Exception:
            name = ""
        await update.message.reply_text(
            f"מעולה{f', {name}' if name else ''}! הכל מוכן. ספר לי על ריצות, אוכל, משקל — או סתם שאל.",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        context.user_data["onboarding_step"] = next_idx
        await update.message.reply_text(ONBOARDING_QUESTIONS[ONBOARDING_STEPS[next_idx]])


# ── Telegram Handlers ─────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    caption = update.message.caption or ""
    photo = update.message.photo[-2] if len(update.message.photo) >= 2 else update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = (
        f"זוהי תמונה של הארוחה שלי{f': {caption}' if caption else ''}. "
        "ראשית קבע מה האוכל בצלחת (שורה ראשונה: FOOD: <תיאור קצר>). "
        "לאחר מכן תן משוב קצר של משפט אחד אם זה מתאים ליעדים שלי. "
        "אם לא מצליח לזהות את האוכל, שאל שאלה קצרה."
    )

    reply = await run_agent(prompt, image_data={"media_type": "image/jpeg", "data": image_b64})

    lines = reply.splitlines()
    food_desc = caption or "ארוחה"
    clean_reply = reply
    for line in lines:
        if line.upper().startswith("FOOD:"):
            food_desc = line[5:].strip()
            clean_reply = "\n".join(l for l in lines if not l.upper().startswith("FOOD:")).strip()
            break

    await call_tool("log_nutrition", {"meal_description": food_desc, "feedback": clean_reply})
    add_to_history("user", f"[תמונת אוכל{f': {caption}' if caption else ''}]")
    add_to_history("assistant", clean_reply)
    await update.message.reply_text(clean_reply)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    text = update.message.text.strip()

    # Keyboard buttons → translate to Hebrew prompts for Claude
    if text in BUTTON_PROMPTS:
        user_prompt = BUTTON_PROMPTS[text]
        reply = await run_agent(user_prompt)
        add_to_history("user", user_prompt)
        add_to_history("assistant", reply)
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
        return

    # Onboarding in progress
    if "onboarding_step" in context.user_data:
        await advance_onboarding(update, context, text)
        return

    # Plan input in progress
    if context.user_data.get("awaiting_plan"):
        context.user_data.pop("awaiting_plan")
        await update.message.reply_text("מובן, מנתח את התכנית...")
        result = await call_tool("save_training_plan", {"plan_text": text})
        await update.message.reply_text(result)
        return

    # Auto-trigger onboarding if profile is empty
    profile_raw = await call_tool("get_profile", {})
    try:
        profile = json.loads(profile_raw)
    except Exception:
        profile = {}
    if not profile.get("user_name"):
        context.user_data["onboarding_step"] = 0
        await update.message.reply_text(ONBOARDING_QUESTIONS["user_name"])
        return

    # Agent handles everything else
    reply = await run_agent(text)
    add_to_history("user", text)
    add_to_history("assistant", reply)
    await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await start_onboarding(update, context)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await start_onboarding(update, context)


async def cmd_setplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    context.user_data["awaiting_plan"] = True
    await update.message.reply_text(
        "הדבק את תכנית האימונים ואני אשמור אותה. כלול ימים, מרחקים וקצבים — כל פורמט מתאים."
    )


async def cmd_setweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("שימוש: /setweek <שבוע> <ריצה> — לדוגמה /setweek 1 3")
        return
    try:
        result = await call_tool("set_plan_position", {"week": int(args[0]), "run_num": int(args[1])})
        await update.message.reply_text(result)
    except ValueError:
        await update.message.reply_text("תן לי שני מספרים, כמו /setweek 1 3")


async def cmd_settarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("שימוש: /settarget 75")
        return
    try:
        target = float(args[0])
        await call_tool("update_profile", {"field": "weight_target", "value": str(target)})
        await update.message.reply_text(f"יעד משקל: {target}ק״ג. בואו נגיע לשם.")
    except ValueError:
        await update.message.reply_text("שלח מספר, כמו /settarget 75")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    import requests as req
    try:
        resp = req.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}"}
        )
        resp.raise_for_status()
        data = resp.json()
        info = data["balance_infos"][0]
        currency = info["currency"]
        total = info["total_balance"]
        topped = info["topped_up_balance"]
        granted = info["granted_balance"]
        await update.message.reply_text(
            f"יתרה ב-DeepSeek:\n"
            f"סה״כ: {total} {currency}\n"
            f"שטעון: {topped} {currency}\n"
            f"מתנה: {granted} {currency}"
        )
    except Exception as e:
        await update.message.reply_text(f"לא הצלחתי לשלוף את היתרה: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "מה שקובי יודע לעשות:\n\n"
        "ספר לי על ריצה, אוכל או משקל — ואני אדאג לשאר\n"
        "תמונת אוכל — פידבק על הארוחה\n\n"
        "/setplan — טעינת תכנית אימונים\n"
        "/setweek 2 1 — קביעת מיקום בתכנית\n"
        "/settarget 75 — קביעת יעד משקל\n"
        "/setup — הגדרות מחדש\n"
        "/balance — יתרה ב-DeepSeek",
        reply_markup=MAIN_KEYBOARD
    )


async def on_startup(app):
    await _load_tools()
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("help", "מה שקובי יודע לעשות"),
        BotCommand("setplan", "טעינת תכנית אימונים"),
        BotCommand("setweek", "קביעת מיקום בתכנית"),
        BotCommand("settarget", "קביעת יעד משקל"),
        BotCommand("setup", "הגדרות מחדש"),
        BotCommand("balance", "יתרה ב-DeepSeek"),
    ])


def main():
    from mcp_sqlite_server import init_db
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("setplan", cmd_setplan))
    app.add_handler(CommandHandler("settarget", cmd_settarget))
    app.add_handler(CommandHandler("setweek", cmd_setweek))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("קובי רץ...")
    app.run_polling()


if __name__ == "__main__":
    main()
