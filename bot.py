from pathlib import Path

src = Path("/mnt/data/Вставленный код.py")
dst = Path("/mnt/data/bot_passive.py")

text = src.read_text(encoding="utf-8")

old_config = """# Бот проверяет, стоит ли вмешиваться,
# примерно каждые 6-10 сообщений
AUTO_CHECK_MIN = 6
AUTO_CHECK_MAX = 10

# Минимум секунд между любыми автоматическими репликами
AUTO_COOLDOWN = 180

# Бот говорит автоматически только если Gemini
# считает момент реально удачным
MIN_AUTO_SCORE = 8
"""

new_config = """# Бот проверяет, стоит ли вмешиваться,
# только после довольно большого куска разговора
AUTO_CHECK_MIN = 12
AUTO_CHECK_MAX = 20

# Минимум секунд между автоматическими репликами
# 10 минут
AUTO_COOLDOWN = 600

# Автоматическая реплика только для очень сильного момента
MIN_AUTO_SCORE = 9

# Даже если Gemini решил, что момент хороший,
# бот всё равно иногда специально промолчит.
AUTO_REPLY_CHANCE = 0.45

# Если ответили непосредственно боту, он отвечает не всегда.
DIRECT_REPLY_CHANCE = 0.35

# При явном @упоминании вероятность выше, но тоже не 100%.
DIRECT_MENTION_CHANCE = 0.65
"""

if old_config not in text:
    raise RuntimeError("Не удалось найти блок CONFIG для замены.")
text = text.replace(old_config, new_config, 1)

insert_anchor = """# =========================================================
# GEMINI
# =========================================================
"""
helper = r'''# =========================================================
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
        "ахах",
        "хаха",
        "хах",
        "спс",
        "спасибо",
    }

    if clean in low_signal_words:
        return True

    # Очень короткие ответы без вопроса обычно тоже игнорируем.
    if len(clean) <= 2 and "?" not in clean:
        return True

    return False


'''
if insert_anchor not in text:
    raise RuntimeError("Не удалось найти место для helper-функции.")
text = text.replace(insert_anchor, helper + insert_anchor, 1)

text = text.replace(
"""5-7:
шутку придумать можно, но лучше промолчать.

8-10:
есть очень естественная и смешная реплика,
которая хорошо впишется в разговор.
""",
"""5-8:
даже если шутку придумать можно, лучше промолчать.

9-10:
редкий, очень естественный и реально смешной момент,
где реплика бота заметно улучшит разговор.
""",
1,
)

start = text.find("async def handle_message(")
end = text.find("# =========================================================\n# START", start)
if start == -1 or end == -1:
    raise RuntimeError("Не удалось найти handle_message.")

new_handle = r'''async def handle_message(
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
        # Больше нет правила "ответить обязательно".
        #
        # На простой reply боту: ~35%.
        # На явное @упоминание: ~65%.
        # На нормальный вопрос вероятность немного выше.
        if mentioned:
            reply_chance = DIRECT_MENTION_CHANCE
        else:
            reply_chance = DIRECT_REPLY_CHANCE

        if "?" in text:
            reply_chance = max(reply_chance, 0.70)

        # Чем содержательнее сообщение, тем логичнее иногда ответить.
        if len(text.strip()) >= 35:
            reply_chance = max(reply_chance, 0.60)

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

Ты не обязан поддерживать бесконечный диалог.
Ответь только одной короткой естественной репликой.
Не превращай разговор в чат "человек ↔ бот".
Если он тебя подкалывает — можешь подколоть в ответ.

Используй контекст переписки.
Максимум 1-2 коротких предложения.
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

    # Обычно ждём 12-20 содержательных сообщений.
    if count < next_check:
        return

    seconds_since_bot = (
        time.time()
        - state["last_bot_reply_at"]
    )

    # После своей автоматической реплики бот молчит минимум 10 минут.
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


'''
text = text[:start] + new_handle + text[end:]

dst.write_text(text, encoding="utf-8")
print(f"Готово: {dst}")
print("Изменения: 12–20 сообщений, cooldown 10 мин, score >= 9, emoji/короткие ответы игнорируются, прямой reply не гарантирует ответ.")
