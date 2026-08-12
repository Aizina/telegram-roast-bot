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
# только после длинного куска реального разговора.
AUTO_CHECK_MIN = 20
AUTO_CHECK_MAX = 35

# После автоматической реплики бот молчит минимум 20 минут.
AUTO_COOLDOWN = 1200

# Автоматически вмешиваемся только в почти идеальный момент.
MIN_AUTO_SCORE = 10

# Даже при score=10 бот использует момент только иногда.
AUTO_REPLY_CHANCE = 0.20

# Если просто ответили на сообщение бота — чаще всего молчим.
DIRECT_REPLY_CHANCE = 0.15

# Явное @упоминание повышает шанс ответа, но не гарантирует его.
DIRECT_MENTION_CHANCE = 0.50

# Не поддерживаем бесконечную переписку с ботом:
# после любого ответа ему желательно немного помолчать.
DIRECT_COOLDOWN = 180


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
Очень мягкий юмор.
Предпочитай добродушную иронию, абсурд и наблюдения за ситуацией.
Лучше пошутить над происходящим, чем над человеком.
""",

        2: """
Лёгкий дружеский сарказм.
Можно слегка подколоть конкретное действие или фразу,
но без унижения человека и без злых ярлыков.
""",

        3: """
Заметный, но всё ещё дружеский стёб.
Можно цепляться за противоречия, повторяющиеся обещания и смешные фейлы,
но шутка должна ощущаться как шутка между друзьями.
""",

        4: """
Острый, но не злой стёб.
Можно быть ехидным, но не дави на человека,
не переходи на внешность, интеллект, достоинство или личные качества.
""",

        5: """
Самый острый разрешённый уровень, но всё ещё без унижения.
Высмеивай только конкретную ситуацию, сообщение или повторяющийся прикол.
Не называй человека тупым, жалким, никчёмным и т.п.
""",
    }

    return levels.get(level, levels[1])


def make_system_prompt(toxicity):
    return f"""
Ты — редкий, ненавязчивый участник дружеского Telegram-чата.

Ты не ассистент и не ведущий разговора.
Большую часть времени ты просто читаешь и молчишь.
Когда пишешь, твоя задача — добавить одну лёгкую смешную реплику,
а не победить кого-то в споре и не унизить участника.

Твой характер:
- наблюдательный;
- спокойный;
- ироничный;
- иногда абсурдный;
- дружелюбный;
- не пытаешься постоянно быть самым смешным;
- легко пропускаешь возможность пошутить;
- не ведёшь длинных диалогов;
- не цепляешься к одному человеку несколько раз подряд.

УРОВЕНЬ СТЁБА:

{toxicity_description(toxicity)}

Предпочтительный юмор:
- смешное наблюдение о ситуации;
- локальный мем;
- лёгкая ирония;
- преувеличение;
- абсурд;
- самоирония;
- аккуратный callback к прошлому сообщению.

Плохой юмор, которого нужно избегать:
- прямые оскорбления;
- унизительные ярлыки;
- постоянное высмеивание одного участника;
- шутки про внешность, ум, ценность человека или его достоинство;
- агрессивный мат ради мата;
- ответ на каждую мелочь;
- попытка обязательно оставить последнее слово.

Правила ответа:
- обычно одно короткое предложение;
- максимум два коротких предложения;
- если шутка не очевидно хорошая — лучше промолчи;
- не объясняй шутку;
- не пиши "как ИИ";
- не выдумывай факты о людях;
- опирайся только на переписку;
- не повторяй недавно использованную шутку;
- не превращай чат в разговор "бот ↔ один человек".

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
выглядеть как молчаливый знакомый, который изредка очень удачно вставляет реплику.
"""


# =========================================================
# PASSIVITY / ANTI-SPAM
# =========================================================

def is_low_signal_message(text):
    """
    Сообщения вроде 😂, ❤️, "...", "ок", "ага", "лол"
    сохраняются в истории, но не считаются поводом отвечать
    и не приближают автоматический комментарий.
    """
    clean = (text or "").strip().lower()

    if not clean:
        return True

    # Только эмодзи / пунктуация / символы.
    if not any(ch.isalnum() for ch in clean):
        return True

    low_signal_words = {
        "ок",
        "окей",
        "ага",
        "угу",
        "да",
        "нет",
        "ясно",
        "пон",
        "понял",
        "поняла",
        "лол",
        "кек",
        "ахах",
        "хаха",
        "хах",
        "аха",
        "спс",
        "спасибо",
        "мда",
        "жесть",
        "капец",
        "ну",
        "мм",
        "хм",
    }

    if clean in low_signal_words:
        return True

    # Очень короткие ответы без вопроса обычно тоже игнорируем.
    if len(clean) <= 2 and "?" not in clean:
        return True

    return False


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

1-9:
промолчи. Даже неплохая шутка не является достаточной причиной вмешиваться.

10:
только редкий, очевидный и очень естественный момент,
где короткая добрая реплика действительно улучшит разговор.
Если сомневаешься между 9 и 10 — ставь 9.

Не отвечай просто потому, что можешь.

Для АВТОМАТИЧЕСКИХ реплик предпочитай юмор про ситуацию,
а не персональный roast. Не выбирай человека целью без очень явного
и безобидного повода из текущего разговора.

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
        1: "мягкая ирония",
        2: "лёгкий дружеский стёб",
        3: "заметный сарказм",
        4: "острее, но без унижений",
        5: "максимум остроты без перехода на личности",
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

    # Сохраняем все текстовые сообщения в историю,
    # даже если бот на них не собирается отвечать.
    save_user_message(message)

    # =====================================================
    # БОТА УПОМЯНУЛИ / ОТВЕТИЛИ НА БОТА
    # =====================================================

    bot_username = context.bot.username

    mentioned = False

    if bot_username:
        mentioned = (
            f"@{bot_username}".lower()
            in text.lower()
        )

    replied_to_bot = False

    if (
        message.reply_to_message
        and message.reply_to_message.from_user
    ):
        replied_to_bot = (
            message.reply_to_message.from_user.id
            == context.bot.id
        )

    # =====================================================
    # СМАЙЛЫ И ДРУГИЕ "ПУСТЫЕ" ОТВЕТЫ
    # =====================================================

    # Например:
    # 😂
    # ❤️
    # ...
    # ок
    # ага
    # лол
    #
    # Бот это запомнит как часть чата, но отвечать не станет
    # и даже не засчитает такое сообщение для автопроверки.
    if is_low_signal_message(text):
        print(
            "PASSIVE SKIP:",
            f"user={user.full_name}",
            f"text={text!r}",
        )
        return

    # Только содержательные сообщения двигают счётчик.
    state = increase_message_counter(chat_id)
    toxicity = state["toxicity"]

    # =====================================================
    # ПРЯМОЕ ОБРАЩЕНИЕ К БОТУ
    # =====================================================

    if mentioned or replied_to_bot:
        # Даже прямое обращение не означает обязательный ответ.
        # Сначала проверяем, не говорил ли бот совсем недавно.
        seconds_since_bot = (
            time.time()
            - get_chat_state(chat_id)["last_bot_reply_at"]
        )

        # Простые reply боту в течение нескольких минут почти всегда игнорируем.
        if seconds_since_bot < DIRECT_COOLDOWN and not mentioned:
            print(
                "DIRECT COOLDOWN SKIP:",
                f"user={user.full_name}",
            )
            reset_auto_counter(chat_id)
            return

        if mentioned:
            reply_chance = DIRECT_MENTION_CHANCE
        else:
            reply_chance = DIRECT_REPLY_CHANCE

        # Вопрос — повод ответить чаще, но всё равно не обязательно.
        if "?" in text:
            if mentioned:
                reply_chance = max(reply_chance, 0.65)
            else:
                reply_chance = max(reply_chance, 0.35)

        # Содержательное обращение чуть повышает шанс.
        if len(text.strip()) >= 50:
            reply_chance = max(reply_chance, 0.40)

        if random.random() > reply_chance:
            print(
                "DIRECT SKIP:",
                f"user={user.full_name}",
                f"chance={reply_chance:.2f}",
            )

            # Считаем, что бот уже "увидел" обращение
            # и сознательно решил промолчать.
            reset_auto_counter(chat_id)
            return

        history = get_history(chat_id, 40)

        prompt = f"""
{make_system_prompt(toxicity)}

Вот последние сообщения:

----------------
{history}
----------------

Пользователь {user.full_name}
обратился непосредственно к тебе.

Его сообщение:

{text}

Ты не обязан отвечать эмоционально или жёстко.
Если отвечаешь, будь спокойным и дружеским.
Предпочитай лёгкую иронию прямому roast.
Не пытайся оставить последнее слово и не разгоняй конфликт.

Используй контекст переписки.
Максимум одно короткое предложение, редко два.
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

        reset_auto_counter(chat_id)
        return

    # =====================================================
    # АВТОМАТИЧЕСКАЯ РЕПЛИКА
    # =====================================================

    state = get_chat_state(chat_id)

    count = state["messages_since_check"]
    next_check = state["next_check_after"]

    # Обычно ждём 20-35 содержательных сообщений.
    if count < next_check:
        return

    seconds_since_bot = (
        time.time()
        - state["last_bot_reply_at"]
    )

    # После своей автоматической реплики бот молчит минимум 20 минут.
    if seconds_since_bot < AUTO_COOLDOWN:
        return

    # Сбрасываем счётчик до обращения к Gemini,
    # чтобы несколько сообщений подряд не создавали несколько запросов.
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

    if not should_reply:
        return

    if score < MIN_AUTO_SCORE:
        return

    if not answer:
        return

    # Дополнительная "человеческая" пассивность:
    # даже хороший момент бот использует не всегда.
    if random.random() > AUTO_REPLY_CHANCE:
        print(
            "AUTO SKIP: good moment, but bot chose silence",
            f"chance={AUTO_REPLY_CHANCE:.2f}",
        )
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

    application.add_handler(
        CommandHandler("roast", roast_command)
    )

    application.add_handler(
        CommandHandler("gossip", gossip_command)
    )

    application.add_handler(
        CommandHandler("toxicity", toxicity_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

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

    # Если есть RENDER_EXTERNAL_HOSTNAME,
    # значит бот работает на Render
    render_hostname = os.getenv(
        "RENDER_EXTERNAL_HOSTNAME"
    )

    if render_hostname:

        port = int(
            os.getenv("PORT", "10000")
        )

        webhook_path = "telegram"

        webhook_url = (
            f"https://{render_hostname}/"
            f"{webhook_path}"
        )

        print("Режим: RENDER WEBHOOK")
        print(f"Host: {render_hostname}")
        print(f"Port: {port}")
        print(f"Webhook: {webhook_url}")

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=False,
        )

    else:

        print("Режим: LOCAL POLLING")
        print("Бот запущен локально")

        application.run_polling()


if __name__ == "__main__":
    main()