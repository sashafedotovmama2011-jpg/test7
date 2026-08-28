import os
import random
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError(
        "Переменная окружения BOT_TOKEN не задана! "
        "Зайди в панель Bothost → настройки контейнера → Variables → добавь BOT_TOKEN"
    )

TIME_LIMIT_HOURS = 12

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- ТЕКСТ КАПЧИ ---
CAPTCHA_TEXT = """Здравствуйте, {name}!

Перед вступлением в группу подтвердите, что вы не спам-бот.

🤖 Решите простую задачу:"""

# --- ТЕКСТ ПРАВИЛ ---
RULES_TEXT = """✅ Капча пройдена! Теперь ознакомьтесь с правилами
Здравствуйте, Вы собираетесь вступить в группу родителей района Митино, воспитывающих детей с ОВЗ, инвалидностью, молодых и взрослых инвалидов " Особое Митино", входящую в АНО "МИР ОДИН НА ВСЕХ".

➡️ Вступая в группу, Вы подтверждаете, что воспитываете ребенка с ОВЗ или инвалидностью, молодого или взрослого инвалида.
➡️Вступая в группу, Вы подтверждаете свое согласие с требованием группы о заполнении анкеты участника, https://forms.yandex.ru/u/6a3d367702848f966f66bea6, и обязуетесь заполнить ее в течение трех рабочих дней с даты вступления в группу.  Вы согласны с тем, что в случае, если анкета не будет заполнена, админы вправе удалить Вас из группы
➡️Вступая в группу, Вы подтверждаете, что прочитали правила и согласны их выполнять:

Правила группы:

✅Мы уважительно относимся ко всем участникам чата. В коммуникации придерживаемся принципов ненасильственного общения (нет манипуляциям, обесцениванию и другим видам психологического насилия).
✅Не оцениваем друг друга публично, не оскорбляем. Если хочется дать корректирующую обратную связь, лучше сделать это лично (и бережно!).
✅Общаясь друг с другом, мы помним, что у каждого из нас своя беда, она не может быть больше или меньше беды остальных участников чата, поэтому мы бережем нервы друг друга.
✅ В спорах не переходим на личности, оценивающие комментарии, аргументированно отстаиваем свое мнение
✅Мы переходим в личную переписку, как только обсуждение перестало быть релевантным широкому кругу родителей.
✅Базово мы считаем, что то, что мы пишем в чат, не выходит за его пределы без согласия автора.

В группе запрещается:
❌ размещать ссылки на сторонние сообщества/чаты/сайты без согласования с админами.
❌рассылать в личку участникам рекламу чего бы то ни было и кого бы то ни было.
❌Использовать нецензурную лексику.
❌Грубо оскорблять оппонента, обесценивать его достижения, его жизнь и его действия.
❌Продажа товаров  запрещена, кроме соответственной подгруппы. 

❗️Если обсуждение всё-таки вышло за рамки правил и стало слишком горячим и активным, модератор может поставить чат на паузу (например, на час), чтобы все остыли и успели прочитать накопившиеся сообщения.
❗️Если участник чата грубо нарушил правила, первый раз получает предупреждение, если второй раз - немой режим на сутки, с третьего раза - удаление из группы.

Надеемся, правила помогут сохранять здесь комфортную атмосферу. Пожалуйста, прежде чем написать, сверяйтесь с ними.
"""

# --- КАПЧА ---
def generate_captcha():
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    correct_answer = a + b

    wrong_answers = set()
    while len(wrong_answers) < 3:
        offset = random.randint(1, 5) * random.choice([-1, 1])
        candidate = correct_answer + offset
        if candidate > 0 and candidate not in wrong_answers:
            wrong_answers.add(candidate)

    options = [correct_answer] + list(wrong_answers)
    random.shuffle(options)
    question = f"Сколько будет {a} + {b}?"
    return question, correct_answer, options


def make_captcha_keyboard(options, correct_answer):
    buttons = []
    for option in options:
        callback = f"cap_{option}_{correct_answer}"
        buttons.append(InlineKeyboardButton(str(option), callback_data=callback))
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(keyboard)


def make_rules_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("✅ Согласен с правилами", callback_data="rules_yes"),
            InlineKeyboardButton("❌ Не согласен", callback_data="rules_no"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# --- ХРАНИЛИЩЕ ---
pending_users: Dict[int, dict] = {}


# --- ФОНОВАЯ ПРОВЕРКА ТАЙМАУТОВ (БЕЗ job_queue) ---
async def timeout_checker(bot):
    """Фоновая задача: каждые 60 секунд проверяет просроченные подтверждения."""
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        to_remove = []

        for user_id, record in pending_users.items():
            if record["confirmed"]:
                continue
            if now > record["deadline"]:
                to_remove.append(user_id)

        for user_id in to_remove:
            record = pending_users.pop(user_id)
            chat_id = record["chat_id"]
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
                logger.info(f"Пользователь {user_id} исключён: не прошёл проверку за {TIME_LIMIT_HOURS} ч.")
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ Участник не подтвердил правила за {TIME_LIMIT_HOURS} часов и был удалён из группы.",
                )
            except Exception as e:
                logger.error(f"Не удалось исключить пользователя {user_id}: {e}")


# --- ОБРАБОТКА НОВЫХ УЧАСТНИКОВ ---
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.new_chat_members:
        return

    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return

    for member in message.new_chat_members:
        if member.is_bot:
            continue

        user_id = member.id
        deadline = datetime.now() + timedelta(hours=TIME_LIMIT_HOURS)

        question, correct_answer, options = generate_captcha()
        full_text = f"{CAPTCHA_TEXT.format(name=member.full_name)}\n\n🔢 {question}"

        try:
            sent_msg = await message.reply_text(
                text=full_text,
                reply_markup=make_captcha_keyboard(options, correct_answer),
            )

            pending_users[user_id] = {
                "chat_id": chat.id,
                "deadline": deadline,
                "confirmed": False,
                "stage": "captcha",
                "message_id": sent_msg.message_id,
            }
            logger.info(f"Новый участник {member.full_name} (id={user_id}) — этап: капча, до {deadline}")
        except Exception as e:
            logger.error(f"Не удалось отправить капчу: {e}")


# --- ОБРАБОТКА НАЖАТИЙ КНОПОК ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # --- ЭТАП 1: КАПЧА ---
    if data.startswith("cap_"):
        if user_id not in pending_users:
            await query.edit_message_text("ℹ️ Это сообщение уже неактуально.")
            return

        record = pending_users[user_id]

        if record["stage"] != "captcha":
            return

        parts = data.split("_")
        if len(parts) != 3:
            return

        selected_answer = int(parts[1])
        correct_answer = int(parts[2])

        if selected_answer == correct_answer:
            record["stage"] = "rules"
            rules_text = RULES_TEXT.format(hours=TIME_LIMIT_HOURS)

            try:
                await query.edit_message_text(
                    text=rules_text,
                    reply_markup=make_rules_keyboard(),
                )
                logger.info(f"Пользователь {user_id} прошёл капчу → показываем правила.")
            except Exception as e:
                logger.error(f"Не удалось показать правила для {user_id}: {e}")
        else:
            question, new_correct, options = generate_captcha()
            new_text = (
                f"❌ Неверно! Попробуйте ещё раз.\n\n"
                f"🔢 {question}\n\n"
                f"⏰ У вас есть {TIME_LIMIT_HOURS} часа на подтверждение."
            )
            try:
                await query.edit_message_text(
                    text=new_text,
                    reply_markup=make_captcha_keyboard(options, new_correct),
                )
                logger.info(f"Пользователь {user_id} ответил неверно — новая капча.")
            except Exception as e:
                logger.error(f"Не удалось обновить капчу для {user_id}: {e}")

    # --- ЭТАП 2: ПРАВИЛА ---
    elif data == "rules_yes":
        if user_id not in pending_users:
            await query.edit_message_text("ℹ️ Это сообщение уже неактуально.")
            return

        record = pending_users[user_id]
        if record["stage"] != "rules":
            return

        record["confirmed"] = True
        pending_users.pop(user_id, None)
        await query.edit_message_text(
            "✅ Спасибо! Вы подтвердили согласие с правилами. Добро пожаловать в группу!"
        )
        logger.info(f"Пользователь {user_id} согласился с правилами — доступ открыт.")

    elif data == "rules_no":
        if user_id not in pending_users:
            await query.edit_message_text("ℹ️ Это сообщение уже неактуально.")
            return

        record = pending_users[user_id]
        chat_id = record["chat_id"]
        pending_users.pop(user_id, None)

        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await query.edit_message_text(
                "❌ Вы отказались подтвердить правила. Вы исключены из группы."
            )
            logger.info(f"Пользователь {user_id} отказался от правил — кик.")
        except Exception as e:
            logger.error(f"Не удалось исключить пользователя {user_id}: {e}")
            await query.edit_message_text("❌ Не удалось исключить. Обратитесь к админу.")


# --- БЛОКИРОВКА СООБЩЕНИЙ ДО ПОДТВЕРЖДЕНИЯ ---
async def block_unconfirmed_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in pending_users and not pending_users[user_id]["confirmed"]:
        try:
            await message.delete()
            logger.info(f"Удалено сообщение от неподтвердившего пользователя {user_id}")
        except Exception as e:
            logger.error(f"Не удалось удалить сообщение от {user_id}: {e}")


# --- ЗАПУСК БЕЗ job_queue ---
async def post_init(application):
    """Запускаем фоновый таймер через asyncio вместо job_queue."""
    asyncio.create_task(timeout_checker(application.bot))
    logger.info("✅ Фоновая проверка таймаутов запущена через asyncio.")


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, block_unconfirmed_messages))

    logger.info("🤖 Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
