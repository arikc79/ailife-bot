import os
import sys
import html
import json
import asyncio
import httpx
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ailife_ua")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
KYIV_TZ = ZoneInfo("Europe/Kyiv")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = DATA_DIR / "queue.json"
HISTORY_FILE = DATA_DIR / "history.json"

SYSTEM_PROMPT = """Ти — контент-менеджер українського Telegram-каналу @ailife_ua про AI та продуктивність.

Твоє завдання: генерувати короткі, живі та корисні пости для підписників.

Правила для кожного посту:
- Мова: українська
- Довжина: 150–300 слів
- Стиль: дружній, живий, без зайвого пафосу
- Структура: гачок (1–2 речення) → суть → практична порада або приклад → заклик до дії або питання
- Використовуй 2–4 емодзі органічно
- Додавай 3–5 хештегів наприкінці: #ailife_ua + тематичні
- Форматування Telegram HTML: <b>жирний</b> для ключових думок
- Тематика: AI-інструменти, продуктивність, автоматизація, лайфхаки, нейромережі в роботі

Генеруй ТІЛЬКИ текст посту — без пояснень, без "ось пост:", просто сам пост."""

POST_STYLES = {
    "tip": "практичний лайфхак або порада",
    "tool": "огляд AI-інструменту",
    "story": "коротка історія або кейс з реального досвіду",
    "question": "пост-запитання для залучення аудиторії",
    "news": "новина зі світу AI + коментар",
}


def sanitize_post(text: str) -> str:
    """Escape HTML special chars in LLM output, but keep <b> tags for bold."""
    escaped = html.escape(text)
    escaped = escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    return escaped


# ── History (avoid topic repeats) ────────────────────────────────────────────

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(new_topics: list):
    history = load_history()
    history.extend(new_topics)
    HISTORY_FILE.write_text(
        json.dumps(history[-30:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Queue (scheduled posts) ───────────────────────────────────────────────────

def load_queue() -> list:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_queue(queue: list):
    QUEUE_FILE.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_to_queue(items: list):
    queue = load_queue()
    queue.extend(items)
    save_queue(queue)


# ── Groq API ──────────────────────────────────────────────────────────────────

async def call_groq(prompt: str, status_msg=None, retries: int = 3) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
        "temperature": 0.9,
    }
    for attempt in range(retries):
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(GROQ_URL, headers=headers, json=payload)
            if response.status_code == 429:
                wait = 10 * (attempt + 1)
                if status_msg:
                    try:
                        await status_msg.edit_text(f"⏳ Ліміт — чекаю {wait} сек... (спроба {attempt+2}/{retries})")
                    except Exception:
                        pass
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    raise Exception("Groq перевантажений — спробуй за хвилину 🙏")


# ── Daily posting job ─────────────────────────────────────────────────────────

async def post_daily_job(context: ContextTypes.DEFAULT_TYPE):
    queue = load_queue()
    today = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    changed = False
    for item in queue:
        if item["date"] == today and not item["posted"]:
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=sanitize_post(item["text"]),
                    parse_mode="HTML"
                )
                item["posted"] = True
                changed = True
                print(f"✅ Опубліковано пост за {today}: {item.get('topic', '')[:40]}")
            except Exception as e:
                print(f"❌ Помилка публікації: {e}")
            break
    if changed:
        save_queue(queue)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привіт! Я генерую пости для @ailife_ua.\n\n"
        "Просто напиши тему — і отримаєш готовий пост.\n\n"
        "Або обери команду:\n"
        "/generate — генерувати з вибором стилю\n"
        "/week — 7 тем на тиждень + пости\n"
        "/queue — переглянути заплановані пости\n"
        "/help — довідка"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Як користуватись:</b>\n\n"
        "1️⃣ Просто напиши тему — бот згенерує пост автоматично\n"
        "   Приклад: <code>ChatGPT для написання email</code>\n\n"
        "2️⃣ /generate — вибрати стиль посту вручну\n"
        "3️⃣ /week — згенерувати 7 тем і пости, потім запланувати\n"
        "4️⃣ /queue — переглянути чергу автопублікацій\n\n"
        "<b>Стилі постів:</b>\n"
        "🔧 Лайфхак — практична порада\n"
        "🛠 Інструмент — огляд AI-сервісу\n"
        "📖 Історія — кейс із досвіду\n"
        "❓ Питання — залучення аудиторії\n"
        "📰 Новина — AI-новина з коментарем"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = load_queue()
    pending = [item for item in queue if not item["posted"]]

    if not pending:
        await update.message.reply_text("📭 Черга порожня.\n\nЗгенеруй пости через /week і заплануй їх.")
        return

    text = "<b>📋 Заплановані пости:</b>\n\n"
    for item in pending:
        text += f"📅 {item['date']} — {html.escape(item.get('topic', '')[:45])}\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔧 Лайфхак", callback_data="style:tip"),
         InlineKeyboardButton("🛠 Інструмент", callback_data="style:tool")],
        [InlineKeyboardButton("📖 Історія", callback_data="style:story"),
         InlineKeyboardButton("❓ Питання", callback_data="style:question")],
        [InlineKeyboardButton("📰 Новина", callback_data="style:news")],
    ]
    await update.message.reply_text(
        "Обери стиль посту:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def style_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    style = query.data.split(":")[1]
    context.user_data["style"] = style
    style_name = POST_STYLES.get(style, style)
    await query.edit_message_text(
        f"Стиль: <b>{style_name}</b>\n\nНапиши тему посту:",
        parse_mode="HTML"
    )


DAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking_msg = await update.message.reply_text("⏳ Генерую 7 тем на тиждень...")

    history = load_history()
    history_hint = ""
    if history:
        history_hint = "\nВже використані теми (НЕ повторюй їх):\n" + "\n".join(f"- {t}" for t in history[-20:])

    prompt = (
        "Згенеруй 7 тем для постів у Telegram-каналі про AI та продуктивність — по одній на кожен день тижня.\n"
        "Теми мають бути різноманітні: лайфхаки, огляди інструментів, мотивація, кейси, новини AI.\n"
        "Відповідь — рівно 7 рядків, кожен рядок: тільки тема без нумерації та зайвих слів.\n"
        f"Мова: українська.{history_hint}"
    )

    try:
        raw = await call_groq(prompt, status_msg=thinking_msg)
        topics = [line.strip("•–- \t") for line in raw.strip().splitlines() if line.strip()][:7]

        context.user_data["week_topics"] = topics
        save_history(topics)

        text = "📅 <b>Теми на тиждень:</b>\n\n"
        for i, topic in enumerate(topics):
            text += f"{DAYS_UA[i]}: {topic}\n"
        text += "\nНатисни на день — отримаєш готовий пост 👇"

        keyboard = [
            [InlineKeyboardButton(
                f"{DAYS_UA[i]} — {topics[i][:30]}…" if len(topics[i]) > 30 else f"{DAYS_UA[i]} — {topics[i]}",
                callback_data=f"week:{i}"
            )]
            for i in range(len(topics))
        ]
        keyboard.append([InlineKeyboardButton("🚀 Згенерувати всі 7 постів", callback_data="week:all")])

        await thinking_msg.delete()
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        await thinking_msg.edit_text(f"❌ Помилка: {e}")


async def week_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topics = context.user_data.get("week_topics", [])
    if not topics:
        await query.answer("Спочатку запусти /week", show_alert=True)
        return

    day_index = query.data.split(":")[1]

    if day_index == "all":
        await query.edit_message_reply_markup(reply_markup=None)
        status = await query.message.reply_text("⏳ Генерую всі 7 постів одним запитом...")

        topics_list = "\n".join(f"{DAYS_UA[i]}: {t}" for i, t in enumerate(topics))
        prompt = (
            f"Згенеруй 7 окремих постів для Telegram-каналу @ailife_ua.\n"
            f"Теми по днях:\n{topics_list}\n\n"
            f"Формат відповіді — рівно 7 блоків, розділених лінією '---':\n"
            f"[текст посту для Пн]\n---\n[текст посту для Вт]\n--- і так далі.\n"
            f"Кожен пост: 150-250 слів, українська, з емодзі та хештегами #ailife_ua."
        )

        try:
            raw = await call_groq(prompt, status_msg=status)
            posts = [p.strip() for p in raw.split("---") if p.strip()]

            await status.delete()

            week_posts = []
            for i, post_text in enumerate(posts[:7]):
                day = DAYS_UA[i] if i < len(DAYS_UA) else f"День {i+1}"
                topic = topics[i] if i < len(topics) else ""
                week_posts.append({"topic": topic, "text": post_text})
                await query.message.reply_text(
                    f"<b>{day} — {html.escape(topic)}</b>\n\n{sanitize_post(post_text)}",
                    parse_mode="HTML"
                )

            context.user_data["week_posts"] = week_posts

            keyboard = [[InlineKeyboardButton("📅 Запланувати на тиждень", callback_data="schedule:week")]]
            await query.message.reply_text(
                "✅ Всі 7 постів згенеровано!\n\nЗапланувати автопублікацію щодня о 8:00? 🕗",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        except Exception as e:
            await status.edit_text(f"❌ Помилка: {e}")
        return

    i = int(day_index)
    topic = topics[i]
    await query.edit_message_text(f"⏳ Генерую пост для <b>{DAYS_UA[i]}</b>...", parse_mode="HTML")

    try:
        post_text = await call_groq(f"Тема посту: {topic}.", status_msg=query.message)
        context.user_data["last_topic"] = topic
        context.user_data["last_post"] = post_text

        keyboard = [[
            InlineKeyboardButton("🔄 Ще варіант", callback_data="regen"),
            InlineKeyboardButton("📅 Назад до тижня", callback_data="week:back"),
        ]]
        await query.edit_message_text(
            f"<b>{DAYS_UA[i]} — {html.escape(topic)}</b>\n\n{sanitize_post(post_text)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Помилка: {e}")


async def schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    posts = context.user_data.get("week_posts", [])
    if not posts:
        await query.answer("Спочатку згенеруй всі пости через /week", show_alert=True)
        return

    today = datetime.now(KYIV_TZ)
    queue_items = []
    for i, post in enumerate(posts):
        post_date = today + timedelta(days=i + 1)
        queue_items.append({
            "date": post_date.strftime("%Y-%m-%d"),
            "topic": post["topic"],
            "text": post["text"],
            "posted": False,
        })

    add_to_queue(queue_items)

    lines = "\n".join(f"• {item['date']} — {html.escape(item['topic'][:40])}" for item in queue_items)
    await query.edit_message_text(
        f"✅ <b>Заплановано {len(queue_items)} постів:</b>\n\n{lines}\n\nПублікація щодня о 8:00 🕗",
        parse_mode="HTML"
    )


async def week_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topics = context.user_data.get("week_topics", [])
    if not topics:
        await query.edit_message_text("Запусти /week щоб отримати нові теми.")
        return

    text = "📅 <b>Теми на тиждень:</b>\n\n"
    for i, topic in enumerate(topics):
        text += f"{DAYS_UA[i]}: {topic}\n"
    text += "\nНатисни на день — отримаєш готовий пост 👇"

    keyboard = [
        [InlineKeyboardButton(
            f"{DAYS_UA[i]} — {topics[i][:30]}…" if len(topics[i]) > 30 else f"{DAYS_UA[i]} — {topics[i]}",
            callback_data=f"week:{i}"
        )]
        for i in range(len(topics))
    ]
    keyboard.append([InlineKeyboardButton("🚀 Згенерувати всі 7 постів", callback_data="week:all")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    style = context.user_data.pop("style", None)

    style_hint = f"\nСтиль посту: {POST_STYLES[style]}." if style else ""
    prompt = f"Тема посту: {topic}.{style_hint}"

    thinking_msg = await update.message.reply_text("⏳ Генерую пост...")

    try:
        post_text = await call_groq(prompt, status_msg=thinking_msg)
        context.user_data["last_topic"] = topic
        context.user_data["last_post"] = post_text

        keyboard = [[
            InlineKeyboardButton("🔄 Ще варіант", callback_data="regen"),
            InlineKeyboardButton("✏️ Новий пост", callback_data="new"),
        ]]

        await thinking_msg.delete()
        await update.message.reply_text(
            sanitize_post(post_text),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        await thinking_msg.edit_text(f"❌ Помилка: {e}")


async def regen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "new":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Напиши нову тему:")
        return

    topic = context.user_data.get("last_topic", "")
    previous_post = context.user_data.get("last_post", "")

    if not topic:
        await query.answer("Тема не знайдена, напиши нову.", show_alert=True)
        return

    await query.edit_message_text("⏳ Генерую інший варіант...")

    try:
        prompt = (
            f"Тема посту: {topic}.\n"
            f"Попередній варіант:\n{previous_post}\n\n"
            "Напиши інший варіант посту на ту саму тему — інший стиль, інший гачок."
        )
        post_text = await call_groq(prompt)
        context.user_data["last_post"] = post_text

        keyboard = [[
            InlineKeyboardButton("🔄 Ще варіант", callback_data="regen"),
            InlineKeyboardButton("✏️ Новий пост", callback_data="new"),
        ]]
        await query.edit_message_text(
            sanitize_post(post_text),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Помилка: {e}")


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("Не задано TELEGRAM_BOT_TOKEN у .env файлі")
    if not GROQ_KEY:
        raise ValueError("Не задано GROQ_API_KEY у .env файлі")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("generate", generate_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CallbackQueryHandler(style_selected, pattern="^style:"))
    app.add_handler(CallbackQueryHandler(regen_callback, pattern="^(regen|new)$"))
    app.add_handler(CallbackQueryHandler(schedule_callback, pattern="^schedule:"))
    app.add_handler(CallbackQueryHandler(week_back_callback, pattern="^week:back$"))
    app.add_handler(CallbackQueryHandler(week_post_callback, pattern="^week:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(
        post_daily_job,
        time=dtime(8, 0, 0, tzinfo=KYIV_TZ)
    )

    print("✅ Бот запущено! Автопублікація о 8:00 за Києвом.")
    app.run_polling()


if __name__ == "__main__":
    main()
