import os
import json
import random
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в .env")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY не найден в .env")


MODEL = "gemini-3.5-flash-lite"

# Сколько сообщений показывать модели как свежий контекст
HISTORY_LIMIT = 45

# Бот проверяет, стоит ли вмешиваться,
# примерно каждые 6-10 сообщений
AUTO_CHECK_MIN = 6
AUTO_CHECK_MAX = 10

# Минимум секунд между любыми автоматическими репликами
AUTO_COOLDOWN = 180

# Бот говорит автоматически только если Gemini
# считает момент реально удачным
MIN_AUTO_SCORE = 8


client = genai.Client(api_key=GEMINI_API_KEY)

DB_PATH = Path(__file__).with_name("bot_memory.db")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                text TEXT NOT NULL,
                reply_to_user_id INTEGER,
                reply_to_name TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_chat
            ON messages(chat_id, id)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                messages_since_check INTEGER NOT NULL DEFAULT 0,
                next_check_after INTEGER NOT NULL DEFAULT 8,
                last_bot_reply_at REAL NOT NULL DEFAULT 0,
                toxicity INTEGER NOT NULL DEFAULT 2
            )
            """
        )


def ensure_chat_state(chat_id):
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_state (
                chat_id,
                messages_since_check,
                next_check_after,
                last_bot_reply_at,
                toxicity
            )
            VALUES (?, 0, ?, 0, 2)
            """,
            (
                chat_id,
                random.randint(AUTO_CHECK_MIN, AUTO_CHECK_MAX),
            ),
        )


def get_chat_state(chat_id):
    ensure_chat_state(chat_id)

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM chat_state
            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()

    return dict(row)


def increase_message_counter(chat_id):
    ensure_chat_state(chat_id)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE chat_state
            SET messages_since_check = messages_since_check + 1
            WHERE chat_id = ?
            """,
            (chat_id,),
        )

    return get_chat_state(chat_id)


def reset_auto_counter(chat_id):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE chat_state
            SET messages_since_check = 0,
                next_check_after = ?
            WHERE chat_id = ?
            """,
            (
                random.randint(AUTO_CHECK_MIN, AUTO_CHECK_MAX),
                chat_id,
            ),
        )


def mark_bot_reply(chat_id):
    ensure_chat_state(chat_id)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE chat_state
            SET last_bot_reply_at = ?
            WHERE chat_id = ?
            """,
            (time.time(), chat_id),
        )


def set_toxicity(chat_id, level):
    ensure_chat_state(chat_id)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE chat_state
            SET toxicity = ?
            WHERE chat_id = ?
            """,
            (level, chat_id),
        )


# =========================================================
# MESSAGE MEMORY
# =========================================================

def save_user_message(message):
    user = message.from_user

    if not user:
        return

    reply_user_id = None
    reply_name = None

    if message.reply_to_message:
        replied = message.reply_to_message

        if replied.from_user:
            reply_user_id = replied.from_user.id
            reply_name = replied.from_user.full_name

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO messages (
                chat_id,
                telegram_message_id,
                user_id,
                username,
                display_name,
                text,
                reply_to_user_id,
                reply_to_name,
                is_bot,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                message.chat_id,
                message.message_id,
                user.id,
                user.username,
                user.full_name,
                message.text,
                reply_user_id,
                reply_name,
                message.date.timestamp(),
            ),
        )


def save_bot_message(chat_id, message_id, text, bot):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO messages (
                chat_id,
                telegram_message_id,
                user_id,
                username,
                display_name,
                text,
                is_bot,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                chat_id,
                message_id,
                bot.id,
                bot.username,
                bot.first_name or "Бот",
                text,
                time.time(),
            ),
        )


def get_history(chat_id, limit=HISTORY_LIMIT):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()

    rows = list(reversed(rows))

    result = []

    for row in rows:
        name = row["display_name"] or "Кто-то"

        if row["username"]:
            name += f" (@{row['username']})"

        if row["is_bot"]:
            name = "БОТ"

        reply_info = ""

        if row["reply_to_name"]:
            reply_info = f" [отвечает {row['reply_to_name']}]"

        text = row["text"]

        # чтобы случайно не отправлять огромные сообщения модели
        if len(text) > 700:
            text = text[:700] + "..."

        result.append(
            f"{name}{reply_info}: {text}"
        )

    return "\n".join(result)


def get_target_context(chat_id, target, limit=25):
    clean = target.strip().lstrip("@").lower()

    if not clean:
        return ""

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE chat_id = ?
            AND is_bot = 0
            AND (
                LOWER(COALESCE(username, '')) = ?
                OR LOWER(COALESCE(display_name, '')) LIKE ?
                OR LOWER(text) LIKE ?
                OR LOWER(COALESCE(reply_to_name, '')) LIKE ?
            )
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                chat_id,
                clean,
                f"%{clean}%",
                f"%{clean}%",
                f"%{clean}%",
                limit,
            ),
        ).fetchall()

    rows = list(reversed(rows))

    result = []

    for row in rows:
        name = row["display_name"]

        if row["username"]:
            name += f" (@{row['username']})"

        text = row["text"]

        if len(text) > 500:
            text = text[:500] + "..."

        result.append(f"{name}: {text}")

    return "\n".join(result)


# =========================================================
# PERSONALITY
# =========================================================

def toxicity_description(level):
    levels = {
        1: """
Очень мягко.
Добродушные подколы.
Никого реально не унижай.
""",

        2: """
Лёгкий сарказм.
Можно немного подъебать человека,
но всё должно ощущаться как дружеская компания.
""",

        3: """
Средний roast.
Можно быть ехидным, цепляться за нелепые заявления,
противоречия и повторяющиеся приколы.
""",

        4: """
Довольно жёсткий дружеский стёб.
Можно быть очень саркастичным и едким,
но не превращай это в травлю.
""",

        5: """
Максимально едкий roast в рамках дружеского чата.
Можно сильно высмеивать поведение, заявления,
факапы и противоречия участников.
Не переходи к угрозам или травле.
""",
    }

    return levels.get(level, levels[2])


def make_system_prompt(toxicity):
    return f"""
Ты — полноценный участник дружеского Telegram-чата.

Ты не ассистент.
Ты не объясняешь свои действия.
Ты просто иногда вбрасываешь смешные реплики.

Твой характер:
- саркастичный;
- наблюдательный;
- немного наглый;
- хорошо запоминаешь контекст;
- замечаешь повторяющиеся фразы и привычки;
- замечаешь, кто кому отвечает;
- можешь возвращаться к старым локальным приколам;
- иногда специально молчишь.

УРОВЕНЬ СТЁБА:

{toxicity_description(toxicity)}

Правила хорошего ответа:

- обычно 1 предложение;
- максимум 2 коротких предложения;
- никакой длинной морали;
- не объясняй шутку;
- не пиши "как ИИ";
- не начинай с "кажется";
- не используй постоянно одни и те же конструкции;
- не повторяй недавно использованную шутку;
- не подкалывай одного человека снова и снова;
- используй конкретный контекст разговора;
- можешь использовать сленг;
- не придумывай факты о человеке, которых нет в переписке.

Запрещено строить стёб вокруг:
- расы;
- национальности;
- религии;
- инвалидности;
- болезней;
- сексуальной ориентации;
- других чувствительных личных характеристик.

Не угрожай людям.

Главная цель:
выглядеть как смешной участник компании,
а не как бот, который обязан отвечать.
"""


# =========================================================
# GEMINI
# =========================================================

async def ask_gemini(prompt, temperature=1.0):
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=180,
            ),
        )

        if not response.text:
            return None

        return response.text.strip()

    except Exception as e:
        print("Gemini error:", repr(e))
        return None


AUTO_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {
            "type": "BOOLEAN"
        },
        "score": {
            "type": "INTEGER"
        },
        "target": {
            "type": "STRING"
        },
        "message": {
            "type": "STRING"
        },
        "reason": {
            "type": "STRING"
        },
    },
    "required": [
        "reply",
        "score",
        "target",
        "message",
        "reason",
    ],
}


async def decide_auto_reply(history, toxicity):
    prompt = f"""
{make_system_prompt(toxicity)}

Ниже последние сообщения Telegram-чата.

----------------
{history}
----------------

Реши, стоит ли ТЕБЕ СЕЙЧАС самостоятельно вмешаться.

ВАЖНО:
Твоя нормальная реакция — промолчать.

Ставь reply=true только если сейчас есть реально хороший
комедийный момент.

Оцени момент от 1 до 10.

1-4:
нечего говорить.

5-7:
шутку придумать можно, но лучше промолчать.

8-10:
есть очень естественная и смешная реплика,
которая хорошо впишется в разговор.

Не отвечай просто потому, что можешь.

Если:
- разговор обычный;
- шутка будет натянутой;
- бот недавно уже много говорил;
- ты собираешься повторить старую шутку;
- опять хочешь стебать того же человека;

то reply должен быть false.

Поле message заполняй короткой готовой репликой.
Если reply=false, message сделай пустой строкой.
"""

    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=250,
                response_mime_type="application/json",
                response_schema=AUTO_RESPONSE_SCHEMA,
            ),
        )

        if not response.text:
            return None

        return json.loads(response.text)

    except Exception as e:
        print("Auto decision error:", repr(e))
        return None


# =========================================================
# SEND HELPER
# =========================================================

async def send_bot_message(
    context,
    chat_id,
    text,
    reply_to_message=None,
):
    if not text:
        return

    # Telegram имеет лимит, но наш бот всё равно
    # должен говорить коротко
    text = text.strip()[:1000]

    if reply_to_message:
        sent = await reply_to_message.reply_text(text)
    else:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
        )

    save_bot_message(
        chat_id,
        sent.message_id,
        text,
        context.bot,
    )

    mark_bot_reply(chat_id)


# =========================================================
# /ROAST
# =========================================================

async def roast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    chat_id = message.chat_id

    state = get_chat_state(chat_id)
    toxicity = state["toxicity"]

    target = ""

    # /roast как ответ на сообщение
    if message.reply_to_message:
        replied = message.reply_to_message

        if replied.from_user:
            user = replied.from_user
            target = user.full_name

            if user.username:
                target += f" (@{user.username})"

    # /roast @username
    elif context.args:
        target = " ".join(context.args)

    history = get_history(chat_id, 50)

    target_history = ""

    if target:
        target_history = get_target_context(
            chat_id,
            target,
        )

    if target:
        task = f"""
Пользователь попросил подколоть:

{target}

Используй ТОЛЬКО информацию из переписки.
Не придумывай биографию или личные факты человека.

Вот сообщения, связанные с целью:

{target_history or "Отдельного контекста мало — используй общую переписку."}
"""
    else:
        task = """
Выбери человека или ситуацию из последних сообщений,
для которых сейчас получится самый смешной roast.

Не обязательно выбирать автора самого последнего сообщения.
"""

    prompt = f"""
{make_system_prompt(toxicity)}

История чата:

----------------
{history}
----------------

{task}

Напиши только готовую короткую реплику.
Никаких объяснений.
"""

    answer = await ask_gemini(prompt, temperature=1.1)

    if answer:
        await send_bot_message(
            context,
            chat_id,
            answer,
            reply_to_message=message,
        )


# =========================================================
# /GOSSIP
# =========================================================

async def gossip_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    chat_id = message.chat_id

    state = get_chat_state(chat_id)
    toxicity = state["toxicity"]

    history = get_history(chat_id, 70)

    if not history:
        await message.reply_text(
            "Мне пока нечего на вас накопать."
        )
        return

    prompt = f"""
{make_system_prompt(toxicity)}

Вот недавняя переписка:

----------------
{history}
----------------

Сделай шуточную сводку происходящего в чате.

Стиль примерно как:
"Сводка за вечер..."

Выдели 2-4 реально заметные вещи из переписки:
кто что делал, кто с кем спорил,
кто что постоянно повторяет,
какие локальные приколы появились.

Не придумывай события.

Ответ должен быть коротким.
Максимум примерно 5 строк.
"""

    answer = await ask_gemini(prompt, temperature=1.0)

    if answer:
        await send_bot_message(
            context,
            chat_id,
            answer,
            reply_to_message=message,
        )


# =========================================================
# /TOXICITY
# =========================================================

async def toxicity_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    chat_id = message.chat_id

    if not context.args:
        current = get_chat_state(chat_id)["toxicity"]

        await message.reply_text(
            f"Сейчас уровень стёба: {current}/5\n\n"
            "Используй:\n"
            "/toxicity 1\n"
            "/toxicity 2\n"
            "...\n"
            "/toxicity 5"
        )
        return

    try:
        level = int(context.args[0])

    except ValueError:
        await message.reply_text(
            "Укажи число от 1 до 5."
        )
        return

    if level < 1 or level > 5:
        await message.reply_text(
            "Уровень должен быть от 1 до 5."
        )
        return

    set_toxicity(chat_id, level)

    descriptions = {
        1: "добрый мальчик",
        2: "лёгкий подъеб",
        3: "уже кусается",
        4: "режим токсичного друга",
        5: "злой гоблин активирован",
    }

    await message.reply_text(
        f"Уровень стёба: {level}/5 — "
        f"{descriptions[level]}"
    )


# =========================================================
# /HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = """
Я читаю чат и иногда сам вбрасываю комментарии.

Команды:

/roast
Подколоть кого-нибудь по последней переписке.

/roast @username
Подколоть конкретного человека.

/roast ответом на сообщение
Подколоть автора сообщения.

/gossip
Шуточная сводка последних событий.

/toxicity 1-5
Настроить уровень стёба.

Можно также написать мне через @username
или ответить на моё сообщение — тогда я отвечу сразу.
"""

    await update.message.reply_text(text)


# =========================================================
# NORMAL MESSAGES
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    if not message.text:
        return

    user = message.from_user

    if not user:
        return

    # Не устраиваем войны ботов
    if user.is_bot:
        return

    chat_id = message.chat_id
    text = message.text

    # Сохраняем ВСЕ обычные сообщения
    save_user_message(message)

    state = increase_message_counter(chat_id)

    toxicity = state["toxicity"]

    # =====================================================
    # БОТА УПОМЯНУЛИ
    # =====================================================

    bot_username = context.bot.username

    mentioned = False

    if bot_username:
        mentioned = (
            f"@{bot_username}".lower()
            in text.lower()
        )

    # =====================================================
    # ОТВЕТИЛИ НА БОТА
    # =====================================================

    replied_to_bot = False

    if (
        message.reply_to_message
        and message.reply_to_message.from_user
    ):
        replied_to_bot = (
            message.reply_to_message.from_user.id
            == context.bot.id
        )

    # Если с ботом говорят напрямую —
    # отвечаем независимо от антиспама
    if mentioned or replied_to_bot:
        history = get_history(chat_id, 40)

        prompt = f"""
{make_system_prompt(toxicity)}

Вот последние сообщения:

----------------
{history}
----------------

Сейчас пользователь {user.full_name}
обратился непосредственно к тебе.

Его сообщение:

{text}

Ответь как обычный участник этого Telegram-чата.

Если он тебя подкалывает —
можешь подколоть в ответ.

Используй контекст переписки.

1-2 коротких предложения.
Не объясняй ответ.
"""

        answer = await ask_gemini(
            prompt,
            temperature=1.0,
        )

        if answer:
            await send_bot_message(
                context,
                chat_id,
                answer,
                reply_to_message=message,
            )

        # после прямого разговора не нужно
        # ещё и автоматически встревать через сообщение
        reset_auto_counter(chat_id)

        return

    # =====================================================
    # АВТОМАТИЧЕСКАЯ РЕПЛИКА
    # =====================================================

    state = get_chat_state(chat_id)

    count = state["messages_since_check"]
    next_check = state["next_check_after"]

    # Ещё слишком мало сообщений
    if count < next_check:
        return

    seconds_since_bot = (
        time.time()
        - state["last_bot_reply_at"]
    )

    # Бот недавно говорил — молчим
    if seconds_since_bot < AUTO_COOLDOWN:
        return

    # Сбрасываем счётчик ДО запроса,
    # чтобы не дергать Gemini несколько раз
    reset_auto_counter(chat_id)

    history = get_history(chat_id, HISTORY_LIMIT)

    decision = await decide_auto_reply(
        history,
        toxicity,
    )

    if not decision:
        return

    should_reply = decision.get("reply", False)

    try:
        score = int(decision.get("score", 0))
    except (TypeError, ValueError):
        score = 0

    answer = str(
        decision.get("message", "")
    ).strip()

    target = decision.get("target", "")

    print(
        "AUTO CHECK:",
        f"reply={should_reply}",
        f"score={score}",
        f"target={target}",
    )

    # Вот здесь главный антиспам
    if not should_reply:
        return

    if score < MIN_AUTO_SCORE:
        return

    if not answer:
        return

    await send_bot_message(
        context,
        chat_id,
        answer,
    )


# =========================================================
# START
# =========================================================

def main():
    init_db()

    application = (
        Application
        .builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Команды
    application.add_handler(
        CommandHandler(
            "roast",
            roast_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "gossip",
            gossip_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "toxicity",
            toxicity_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    # Обычные сообщения
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    print("==============================")
    print("Roast bot запускается")
    print(f"Модель: {MODEL}")
    print(f"База данных: {DB_PATH}")
    print("==============================")

    # =====================================================
    # KOYEB / SERVER MODE
    # =====================================================

    koyeb_domain = os.getenv("KOYEB_PUBLIC_DOMAIN")

    if koyeb_domain:
        port = int(os.getenv("PORT", "8000"))

        webhook_path = "telegram"

        webhook_url = (
            f"https://{koyeb_domain}/{webhook_path}"
        )

        print("Режим: WEBHOOK")
        print(f"Domain: {koyeb_domain}")
        print(f"Port: {port}")
        print(f"Webhook: {webhook_url}")

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=False,
        )

    # =====================================================
    # LOCAL COMPUTER MODE
    # =====================================================

    else:
        print("Режим: POLLING")
        print("Бот запущен локально")

        application.run_polling()


if __name__ == "__main__":
    main()