import os
import json
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в environment/.env")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY не найден в environment/.env")


MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# По умолчанию время Финляндии. На Render сервер может жить в UTC,
# поэтому для поведения по времени используем отдельную timezone.
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Helsinki")
TZ = ZoneInfo(BOT_TIMEZONE)

DB_PATH = Path(__file__).with_name("bot_memory.db")

# На Render Free локальный SQLite может исчезать после сна/рестарта.
# Поэтому для Render рекомендуется задать DATABASE_URL от Neon/Postgres.
# Локально, если DATABASE_URL нет, бот продолжит работать через SQLite.
DATABASE_URL = os.getenv("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

HISTORY_LIMIT = 55

# Самостоятельные сообщения:
# утром + ещё 1-2 раза в течение дня.
# В "тихие" дни сообщений может быть меньше: бот не пишет в пустоту.
DAYTIME_MESSAGES_BEFORE_ATTEMPT = 12

# Между любыми самостоятельными сообщениями минимум 2 часа.
AUTONOMOUS_COOLDOWN = 2 * 60 * 60

# Если бот только что ответил на @, не даём группе сразу вторую реплику.
AFTER_DIRECT_REPLY_COOLDOWN = 30 * 60

# Явное @обращение получает ответ.
# Но "@bot 😂" / "@bot ок" и подобное бот игнорирует.
DIRECT_MENTION_REPLY_CHANCE = 1.0


client = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================
# DATABASE
# =========================================================

def get_db():
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def adapt_sql(sql):
    if USE_POSTGRES:
        return sql.replace("?", "%s")
    return sql


def db_execute(sql, params=()):
    with get_db() as conn:
        conn.execute(adapt_sql(sql), params)


def db_fetchone(sql, params=()):
    with get_db() as conn:
        return conn.execute(adapt_sql(sql), params).fetchone()


def db_fetchall(sql, params=()):
    with get_db() as conn:
        return conn.execute(adapt_sql(sql), params).fetchall()


def init_db():
    if USE_POSTGRES:
        with get_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    telegram_message_id BIGINT,
                    user_id BIGINT,
                    username TEXT,
                    display_name TEXT,
                    text TEXT NOT NULL,
                    reply_to_user_id BIGINT,
                    reply_to_name TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    created_at DOUBLE PRECISION NOT NULL
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
                    chat_id BIGINT PRIMARY KEY,
                    messages_since_check INTEGER NOT NULL DEFAULT 0,
                    next_check_after INTEGER NOT NULL DEFAULT 12,
                    last_bot_reply_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    toxicity INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'auto',
                    morning_sent_date TEXT NOT NULL DEFAULT '',
                    midday_sent_date TEXT NOT NULL DEFAULT '',
                    evening_sent_date TEXT NOT NULL DEFAULT '',
                    last_autonomous_at DOUBLE PRECISION NOT NULL DEFAULT 0
                )
                """
            )
            migrations = [
                "ALTER TABLE chat_state ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'auto'",
                "ALTER TABLE chat_state ADD COLUMN IF NOT EXISTS morning_sent_date TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE chat_state ADD COLUMN IF NOT EXISTS midday_sent_date TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE chat_state ADD COLUMN IF NOT EXISTS evening_sent_date TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE chat_state ADD COLUMN IF NOT EXISTS last_autonomous_at DOUBLE PRECISION NOT NULL DEFAULT 0",
            ]
            for migration in migrations:
                conn.execute(migration)
        return

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
                next_check_after INTEGER NOT NULL DEFAULT 12,
                last_bot_reply_at REAL NOT NULL DEFAULT 0,
                toxicity INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'auto',
                morning_sent_date TEXT NOT NULL DEFAULT '',
                midday_sent_date TEXT NOT NULL DEFAULT '',
                evening_sent_date TEXT NOT NULL DEFAULT '',
                last_autonomous_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(chat_state)").fetchall()
        }
        migrations = {
            "mode": "TEXT NOT NULL DEFAULT 'auto'",
            "morning_sent_date": "TEXT NOT NULL DEFAULT ''",
            "midday_sent_date": "TEXT NOT NULL DEFAULT ''",
            "evening_sent_date": "TEXT NOT NULL DEFAULT ''",
            "last_autonomous_at": "REAL NOT NULL DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                conn.execute(
                    f"ALTER TABLE chat_state ADD COLUMN {column} {definition}"
                )


def ensure_chat_state(chat_id):
    if USE_POSTGRES:
        db_execute(
            """
            INSERT INTO chat_state (
                chat_id,
                messages_since_check,
                next_check_after,
                last_bot_reply_at,
                toxicity,
                mode
            )
            VALUES (?, 0, ?, 0, 1, 'auto')
            ON CONFLICT (chat_id) DO NOTHING
            """,
            (chat_id, DAYTIME_MESSAGES_BEFORE_ATTEMPT),
        )
        return

    db_execute(
        """
        INSERT OR IGNORE INTO chat_state (
            chat_id,
            messages_since_check,
            next_check_after,
            last_bot_reply_at,
            toxicity,
            mode
        )
        VALUES (?, 0, ?, 0, 1, 'auto')
        """,
        (chat_id, DAYTIME_MESSAGES_BEFORE_ATTEMPT),
    )


def get_chat_state(chat_id):
    ensure_chat_state(chat_id)
    row = db_fetchone(
        """
        SELECT *
        FROM chat_state
        WHERE chat_id = ?
        """,
        (chat_id,),
    )
    return dict(row)


def set_mode(chat_id, mode):
    ensure_chat_state(chat_id)
    db_execute(
        """
        UPDATE chat_state
        SET mode = ?
        WHERE chat_id = ?
        """,
        (mode, chat_id),
    )


def increase_message_counter(chat_id):
    ensure_chat_state(chat_id)
    db_execute(
        """
        UPDATE chat_state
        SET messages_since_check = messages_since_check + 1
        WHERE chat_id = ?
        """,
        (chat_id,),
    )


def reset_message_counter(chat_id):
    ensure_chat_state(chat_id)
    db_execute(
        """
        UPDATE chat_state
        SET messages_since_check = 0
        WHERE chat_id = ?
        """,
        (chat_id,),
    )


def mark_bot_reply(chat_id):
    ensure_chat_state(chat_id)
    db_execute(
        """
        UPDATE chat_state
        SET last_bot_reply_at = ?
        WHERE chat_id = ?
        """,
        (time.time(), chat_id),
    )


def mark_autonomous_reply(chat_id):
    ensure_chat_state(chat_id)
    now = time.time()
    db_execute(
        """
        UPDATE chat_state
        SET last_bot_reply_at = ?,
            last_autonomous_at = ?,
            messages_since_check = 0
        WHERE chat_id = ?
        """,
        (now, now, chat_id),
    )


def mark_slot_sent(chat_id, slot, date_string):
    column_by_slot = {
        "morning": "morning_sent_date",
        "midday": "midday_sent_date",
        "evening": "evening_sent_date",
    }
    column = column_by_slot[slot]
    db_execute(
        f"""
        UPDATE chat_state
        SET {column} = ?
        WHERE chat_id = ?
        """,
        (date_string, chat_id),
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

    if message.reply_to_message and message.reply_to_message.from_user:
        replied = message.reply_to_message
        reply_user_id = replied.from_user.id
        reply_name = replied.from_user.full_name

    db_execute(
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
    db_execute(
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
    rows = db_fetchall(
        """
        SELECT *
        FROM messages
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (chat_id, limit),
    )

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

        if len(text) > 650:
            text = text[:650] + "..."

        result.append(
            f"{name}{reply_info}: {text}"
        )

    return "\n".join(result)


def get_recent_participants(chat_id, limit=20):
    """
    Возвращает недавних реальных участников.
    Один пользователь = одна запись.
    """
    rows = db_fetchall(
        """
        SELECT
            user_id,
            username,
            display_name,
            MAX(created_at) AS last_seen
        FROM messages
        WHERE chat_id = ?
          AND is_bot = 0
          AND user_id IS NOT NULL
        GROUP BY user_id, username, display_name
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (chat_id, limit),
    )

    return [dict(row) for row in rows]


def display_participant(participant):
    if not participant:
        return None

    username = participant.get("username")
    display_name = participant.get("display_name") or "друг"

    if username:
        return f"@{username}"

    return display_name


# =========================================================
# PASSIVITY HELPERS
# =========================================================

def is_low_signal_message(text):
    """
    На такое бот не отвечает даже при reply на его сообщение:
    😂, ❤️, ..., ок, ага, лол и т.п.
    """
    clean = (text or "").strip().lower()

    if not clean:
        return True

    # Только emoji / пунктуация / символы.
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

    if len(clean) <= 2 and "?" not in clean:
        return True

    return False


def text_without_mention(text, bot_username):
    clean = text or ""

    if bot_username:
        clean = clean.replace(
            f"@{bot_username}",
            "",
        )
        clean = clean.replace(
            f"@{bot_username.lower()}",
            "",
        )

    return clean.strip()


def local_now():
    return datetime.now(TZ)


def daily_target(chat_id, date_string):
    """
    Стабильно выбирает 2 или 3 самостоятельных сообщения
    на конкретный день. Перезапуск бота не меняет план.
    """
    rng = random.Random(
        f"{chat_id}:{date_string}:daily-target"
    )
    return rng.choice([2, 2, 3])


def autonomous_sent_today(state, date_string):
    return sum(
        1
        for key in (
            "morning_sent_date",
            "midday_sent_date",
            "evening_sent_date",
        )
        if state.get(key, "") == date_string
    )


def mode_for_slot(chat_id, date_string, slot, configured_mode):
    if slot == "morning":
        return "good"

    if configured_mode == "good":
        return "good"

    if configured_mode == "roast":
        return "roast"

    # AUTO: примерно половина добрых, половина стебных,
    # но слегка чаще добрые.
    rng = random.Random(
        f"{chat_id}:{date_string}:{slot}:mode"
    )

    return "good" if rng.random() < 0.55 else "roast"


def current_slot(now):
    hour = now.hour

    # "утро" достаточно широкое:
    # на Render Free бот может проснуться только когда кто-то написал.
    if 7 <= hour < 12:
        return "morning"

    if 13 <= hour < 17:
        return "midday"

    if 18 <= hour < 23:
        return "evening"

    return None


def slot_already_sent(state, slot, date_string):
    key = f"{slot}_sent_date"
    return state.get(key, "") == date_string


# =========================================================
# PERSONALITY
# =========================================================

def base_chat_context():
    return """
Это дружеский Telegram-чат, связанный с Counter-Strike 2.

Ты знаешь обычный контекст CS2:
аим, спрей, AWP, эко, раши, смоки, тиммейты,
премьер, катки, клатчи и вечные подозрения на читеров.

Но не превращай каждую реплику в CS2-шутку.
Иногда обычная человеческая шутка лучше.
"""


def good_style_prompt():
    return f"""
{base_chat_context()}

РЕЖИМ: ДОБРЫЙ.

Ты добрый, слегка смешной участник компании.

Твой стиль:
- тёплый;
- короткий;
- немного мемный;
- без приторности;
- можешь похвалить человека;
- можешь пожелать хорошего дня или хороших каток;
- можешь дать смешной "бафф дня";
- можно аккуратно пошутить над CS2;
- никаких оскорблений.

Не пиши длинные мотивационные речи.
Обычно одно предложение, максимум два.
"""


def roast_style_prompt():
    return f"""
{base_chat_context()}

РЕЖИМ: ЛЁГКИЙ СТЁБ.

Ты можешь иногда вкинуть что-то вроде:
- "ну ты и клоун 😭"
- "что за ерунду я сейчас прочитал"
- "это сейчас была тактика или крик о помощи?"
- "с таким планом даже боты на разминке задумались"

Это ПРИМЕРЫ НАСТРОЕНИЯ, а не фразы для постоянного повторения.

Очень важно:
- стёб дружеский, а не злой;
- не унижай человека;
- не переходи на внешность, интеллект, здоровье, семью и личную ценность;
- не трави одного участника;
- шути над конкретной фразой, ситуацией или игровым фейлом;
- не придумывай факты;
- если нормальной шутки нет — лучше промолчи;
- максимум одно короткое предложение, редко два.
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
                max_output_tokens=160,
            ),
        )

        if not response.text:
            return None

        return response.text.strip()

    except Exception as e:
        print("Gemini error:", repr(e))
        return None


SPONTANEOUS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "send": {
            "type": "BOOLEAN"
        },
        "message": {
            "type": "STRING"
        },
        "reason": {
            "type": "STRING"
        },
    },
    "required": [
        "send",
        "message",
        "reason",
    ],
}


async def generate_roast_from_context(history):
    prompt = f"""
{roast_style_prompt()}

Последняя переписка:

----------------
{history}
----------------

Ты рассматриваешь возможность ОДНОЙ самостоятельной реплики
посреди разговора.

Напиши её только если в последних сообщениях есть очевидный,
безобидный повод для короткого дружеского стёба.

Не нужно отвечать на каждую мысль.
Не нужно выбирать жертву просто ради шутки.
Не будь агрессивным.

Если подходящего момента нет:
send=false
message=""

Если момент есть:
send=true
message=<одна короткая смешная реплика>
"""

    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=1.0,
                max_output_tokens=180,
                response_mime_type="application/json",
                response_schema=SPONTANEOUS_SCHEMA,
            ),
        )

        if not response.text:
            return None

        data = json.loads(response.text)

        if not data.get("send"):
            return None

        message = str(data.get("message", "")).strip()

        return message or None

    except Exception as e:
        print("Spontaneous roast error:", repr(e))
        return None


async def generate_direct_answer(
    history,
    user_name,
    user_text,
    selected_mode,
):
    style = (
        good_style_prompt()
        if selected_mode == "good"
        else roast_style_prompt()
    )

    prompt = f"""
{style}

Последняя переписка:

----------------
{history}
----------------

Пользователь {user_name} явно обратился к тебе через @.

Сообщение:
{user_text}

Ответь естественно как участник группы.

Не надо обязательно шутить.
Если вопрос обычный — можешь нормально и по-доброму ответить.
Если человек подкалывает тебя — можешь слегка подколоть в ответ.
Не разгоняй конфликт.

Максимум 1-2 коротких предложения.
"""

    return await ask_gemini(
        prompt,
        temperature=0.9,
    )


# =========================================================
# FRIENDLY AUTONOMOUS MESSAGES
# =========================================================

def make_morning_message(chat_id):
    participants = get_recent_participants(chat_id)
    participant = (
        display_participant(random.choice(participants))
        if participants
        else None
    )

    general_templates = [
        (
            "Доброе всем утречко ☀️ Как у вас делишки? "
            "Пусть день будет спокойный, а катки — зелёные."
        ),
        (
            "Всем доброго утра 💛 Отличного дня и поменьше "
            "тиммейтов, которые вспоминают про микрофон только после смерти."
        ),
        (
            "Приветики ☀️ Как настроение? Желаю сегодня "
            "попадать туда, куда целились, а не куда решил спрей."
        ),
        (
            "Доброе утро, киберспортсмены ☕ "
            "Пусть сегодня пули летят куда надо, а читеры — куда-нибудь подальше."
        ),
    ]

    personal_templates = [
        (
            "{name}, персональный бафф на сегодня: +20 к аиму, "
            "+50 к терпению и ни одного подозрительного типа с новым аккаунтом 😌"
        ),
        (
            "{name}, желаю тебе сегодня красивых хедшотов, "
            "нормальных тиммейтов и чтобы фраза «он точно читер» не понадобилась."
        ),
        (
            "{name}, сегодня ты официально получаешь +10 к удаче. "
            "Потрать её разумно, желательно не на кейсы."
        ),
        (
            "{name}, пусть сегодня у тебя всё летит куда надо — "
            "и пули, и планы на день 💛"
        ),
    ]

    if participant and random.random() < 0.45:
        return random.choice(personal_templates).format(
            name=participant
        )

    return random.choice(general_templates)


def make_good_daytime_message(chat_id, slot):
    participants = get_recent_participants(chat_id)
    participant = (
        display_participant(random.choice(participants))
        if participants
        else None
    )

    if participant:
        templates = [
            "{name}, небольшой бафф дня: сегодня аим обязан уважать хозяина 😌",
            "{name}, желаю тебе каток без читеров и тиммейтов без драматургического таланта.",
            "{name}, ты сегодня молодец просто авансом. Не испорть статистику 💛",
            "{name}, пусть сегодня будет больше хедшотов, чем поводов говорить «да как он попал?»",
        ]

        return random.choice(templates).format(
            name=participant
        )

    templates = [
        "Небольшое пожелание чату: зелёных каток и минимум людей, которые пушат без флешки 🙏",
        "Пусть сегодня аим будет стабильнее настроения после трёх лузов подряд.",
        "Напоминаю: вы прекрасны. Даже если табло временно считает иначе 😌",
    ]

    return random.choice(templates)


# =========================================================
# SEND HELPERS
# =========================================================

async def send_bot_message(
    context,
    chat_id,
    text,
    reply_to_message=None,
    autonomous=False,
):
    if not text:
        return None

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

    if autonomous:
        mark_autonomous_reply(chat_id)
    else:
        mark_bot_reply(chat_id)

    return sent


# =========================================================
# DAILY AUTONOMOUS BEHAVIOUR
# =========================================================

async def maybe_send_autonomous(
    context,
    chat_id,
):
    """
    Максимум 2-3 запланированных самостоятельных сообщения в день.

    ВАЖНО:
    На Render Free процесс может спать.
    Поэтому эта функция вызывается при активности в группе.
    Бот пишет НЕ ответом на конкретное сообщение, а отдельной репликой.
    """
    now = local_now()
    date_string = now.date().isoformat()
    slot = current_slot(now)

    if not slot:
        return False

    state = get_chat_state(chat_id)

    target = daily_target(
        chat_id,
        date_string,
    )

    sent_today = autonomous_sent_today(
        state,
        date_string,
    )

    # Жёсткий дневной лимит: 2-3 самостоятельных сообщения максимум.
    if sent_today >= target:
        return False

    if slot_already_sent(
        state,
        slot,
        date_string,
    ):
        return False

    # Утро: достаточно первого нормального сообщения в группе.
    if slot == "morning":
        message = make_morning_message(chat_id)

        await send_bot_message(
            context,
            chat_id,
            message,
            autonomous=True,
        )

        mark_slot_sent(
            chat_id,
            slot,
            date_string,
        )

        print(
            "AUTONOMOUS:",
            "morning",
            message,
        )

        return True

    # Днём/вечером бот не должен встревать сразу.
    if (
        state["messages_since_check"]
        < DAYTIME_MESSAGES_BEFORE_ATTEMPT
    ):
        return False

    seconds_since_any_bot_reply = (
        time.time()
        - state["last_bot_reply_at"]
    )

    seconds_since_autonomous = (
        time.time()
        - state["last_autonomous_at"]
    )

    if (
        seconds_since_any_bot_reply
        < AFTER_DIRECT_REPLY_COOLDOWN
    ):
        return False

    if (
        seconds_since_autonomous
        < AUTONOMOUS_COOLDOWN
    ):
        return False

    # Сбрасываем счётчик даже если Gemini решит промолчать,
    # чтобы не дергать модель на каждом следующем сообщении.
    reset_message_counter(chat_id)

    selected_mode = mode_for_slot(
        chat_id,
        date_string,
        slot,
        state.get("mode", "auto"),
    )

    if selected_mode == "good":
        message = make_good_daytime_message(
            chat_id,
            slot,
        )
    else:
        history = get_history(
            chat_id,
            HISTORY_LIMIT,
        )

        message = await generate_roast_from_context(
            history
        )

    if not message:
        print(
            "AUTONOMOUS SKIP:",
            slot,
            selected_mode,
        )
        return False

    await send_bot_message(
        context,
        chat_id,
        message,
        autonomous=True,
    )

    mark_slot_sent(
        chat_id,
        slot,
        date_string,
    )

    print(
        "AUTONOMOUS:",
        slot,
        selected_mode,
        message,
    )

    return True


# =========================================================
# /MODE
# =========================================================

async def mode_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    chat_id = message.chat_id

    aliases = {
        "good": "good",
        "kind": "good",
        "добрый": "good",
        "добро": "good",
        "roast": "roast",
        "roastmode": "roast",
        "стеб": "roast",
        "стёб": "roast",
        "стебный": "roast",
        "стёбный": "roast",
        "auto": "auto",
        "авто": "auto",
        "автомат": "auto",
    }

    if not context.args:
        current = get_chat_state(chat_id).get(
            "mode",
            "auto",
        )

        names = {
            "good": "добрый 💛",
            "roast": "лёгкий стёб 🤡",
            "auto": "авто: добрый + лёгкий стёб 🎭",
        }

        await message.reply_text(
            f"Сейчас режим: {names[current]}\n\n"
            "/mode добрый — только добрые самостоятельные сообщения\n"
            "/mode стеб — лёгкий стёб посреди разговоров\n"
            "/mode авто — смешивать оба режима"
        )
        return

    raw_mode = " ".join(
        context.args
    ).strip().lower()

    selected = aliases.get(raw_mode)

    if not selected:
        await message.reply_text(
            "Выбери: /mode добрый, /mode стеб или /mode авто"
        )
        return

    set_mode(
        chat_id,
        selected,
    )

    messages = {
        "good": "Добрый режим включён 💛 Буду нести лучи добра и немного CS2.",
        "roast": "Стёбный режим включён 🤡 Но без режима «уничтожить человека морально».",
        "auto": "Авто-режим включён 🎭 Утром добрый, потом иногда добрый, иногда слегка стебный.",
    }

    await message.reply_text(
        messages[selected]
    )


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
    history = get_history(chat_id, 50)

    target = ""

    if (
        message.reply_to_message
        and message.reply_to_message.from_user
    ):
        user = message.reply_to_message.from_user
        target = user.full_name

        if user.username:
            target += f" (@{user.username})"

    elif context.args:
        target = " ".join(context.args)

    target_instruction = ""

    if target:
        target_instruction = f"""
Подколи конкретно {target}.
Но только по информации из этой переписки.
Не придумывай про человека ничего.
"""
    else:
        target_instruction = """
Выбери самый очевидный безобидный повод для короткого стёба
из последних сообщений.
"""

    prompt = f"""
{roast_style_prompt()}

Переписка:

----------------
{history}
----------------

{target_instruction}

Напиши одну короткую готовую реплику.
Без объяснений.
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
    history = get_history(chat_id, 70)

    if not history:
        await message.reply_text(
            "Пока слишком мало материала для сплетен 😌"
        )
        return

    prompt = f"""
{good_style_prompt()}

Вот недавняя переписка:

----------------
{history}
----------------

Сделай очень короткую смешную сводку событий чата.

Можно немного иронии и CS2-юмора,
но никого не унижай.

2-4 короткие строки.
Не придумывай событий, которых в переписке не было.
"""

    answer = await ask_gemini(
        prompt,
        temperature=0.9,
    )

    if answer:
        await send_bot_message(
            context,
            chat_id,
            answer,
            reply_to_message=message,
        )


# =========================================================
# /HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = """
Я теперь живу в двух настроениях 🎭

/mode добрый
Тёплые сообщения, комплименты, пожелания хороших каток.

/mode стеб
Редкий лёгкий стёб по контексту разговора.

/mode авто
Утром добрый, а потом иногда добрый, иногда стебный.

/roast
Подколоть кого-нибудь по последней переписке.

/roast @username
Подколоть конкретного человека.

/gossip
Короткая шуточная сводка.

Самостоятельно я стараюсь писать всего 2-3 раза в день.
Если хочешь обратиться ко мне напрямую — используй @мой_username.
"""

    await update.message.reply_text(
        text.strip()
    )


# =========================================================
# NORMAL MESSAGES
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message or not message.text:
        return

    user = message.from_user

    if not user or user.is_bot:
        return

    chat_id = message.chat_id
    text = message.text

    save_user_message(message)

    bot_username = context.bot.username

    mentioned = False

    if bot_username:
        mentioned = (
            f"@{bot_username}".lower()
            in text.lower()
        )

    # =====================================================
    # @ ОБРАЩЕНИЕ
    # =====================================================

    if mentioned:
        actual_text = text_without_mention(
            text,
            bot_username,
        )

        # @bot 😂 / @bot ок / @bot ...
        # не должны заставлять его что-то выдумывать.
        if is_low_signal_message(actual_text):
            print(
                "MENTION SKIP:",
                user.full_name,
                repr(actual_text),
            )
            return

        state = get_chat_state(chat_id)
        configured_mode = state.get(
            "mode",
            "auto",
        )

        if configured_mode == "auto":
            # При прямом обращении чаще добрый,
            # иногда слегка стебный.
            selected_mode = (
                "good"
                if random.random() < 0.70
                else "roast"
            )
        else:
            selected_mode = configured_mode

        history = get_history(
            chat_id,
            45,
        )

        answer = await generate_direct_answer(
            history,
            user.full_name,
            actual_text,
            selected_mode,
        )

        if answer:
            await send_bot_message(
                context,
                chat_id,
                answer,
                reply_to_message=message,
            )

        # @ответ не должен тут же породить ещё и самостоятельный вброс.
        reset_message_counter(chat_id)
        return

    # =====================================================
    # ОБЫЧНЫЕ СООБЩЕНИЯ
    # =====================================================

    # Ответ на сообщение бота сам по себе НЕ является командой ответить.
    # Смайлы/односложные сообщения просто запоминаются и всё.
    if is_low_signal_message(text):
        return

    increase_message_counter(chat_id)

    # Самостоятельное поведение только в группах.
    if message.chat.type not in (
        "group",
        "supergroup",
    ):
        return

    await maybe_send_autonomous(
        context,
        chat_id,
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
        CommandHandler(
            "mode",
            mode_command,
        )
    )

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
            "help",
            help_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    print("==============================")
    print("CS2 chat bot запускается")
    print(f"Модель: {MODEL}")
    print(f"Timezone: {BOT_TIMEZONE}")
    print("База данных:", "PostgreSQL/Neon" if USE_POSTGRES else str(DB_PATH))
    print("==============================")

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
        application.run_polling()


if __name__ == "__main__":
    main()