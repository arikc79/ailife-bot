import os
import sys
import re
import html
import json
import random
import asyncio
import httpx
import urllib.parse
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
XAI_KEY = os.getenv("XAI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ailife_ua")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
KYIV_TZ = ZoneInfo("Europe/Kyiv")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = DATA_DIR / "queue.json"
HISTORY_FILE = DATA_DIR / "history.json"

SYSTEM_PROMPT = """Ти пишеш пости для Telegram-каналу @ailife_ua від імені Тараса.

Тарас — людина, яка витрачала 6 годин на задачі, які AI робить за 20 хвилин. Тепер ділиться тим, що сам перевірив.

Пости завжди від першої особи: "я", "мій", "я спробував" — НІКОЛИ "ми" чи "наш".
Стиль: БЕЗ ВОДИ. Без вступів типу "Хочу поділитись", "Нещодавно я помітив", "Друзі", "Всім привіт".
Тон: коротка записка від людини, яка знайшла щось корисне і одразу каже суть.

ОБОВ'ЯЗКОВО в кожному пості:
- Конкретний інструмент: назва + що саме він робить у цьому кейсі
- Конкретні цифри або порівняння: час, кількість кроків, результат
- Реальна дія: не "можна автоматизувати", а "я беру X, вставляю в Y, отримую Z за 3 хв"
- Один практичний крок який читач може зробити прямо зараз

НЕ ПИСАТИ: загальні фрази без прив'язки до конкретного інструменту, теоретичні міркування, пафосні вступи."""

POST_STYLES = {
    "tip": "практичний лайфхак або порада",
    "tool": "огляд AI-інструменту",
    "story": "коротка історія або кейс з реального досвіду",
    "question": "пост-запитання для залучення аудиторії",
    "news": "новина зі світу AI + коментар",
}

SUGGESTED_TOPICS = [
    "Як ChatGPT економить 3 години на день",
    "Claude vs ChatGPT: що реально краще для роботи",
    "Google Gemini 2.5: чому це змінює гру",
    "Grok від xAI: що він вміє і чим відрізняється",
    "5 AI-інструментів що замінюють цілу команду",
    "Як писати промпти які реально працюють",
    "AI для заробітку: реальні кейси з України",
    "Автоматизація рутини через Make + AI",
    "Perplexity AI: кращий пошук замість Google?",
    "Midjourney vs Ideogram: який обрати для контенту",
    "Notion AI: чи варто платити за підписку",
    "Як Claude допомагає писати код без досвіду",
    "Gemini у Google Docs: AI прямо в твоїх документах",
    "Як AI змінює ринок праці — що робити зараз",
    "Grok 3 vs GPT-4o: чесне порівняння",
]

DAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_post(text: str) -> str:
    escaped = html.escape(text)
    escaped = escaped.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    # Convert markdown **bold** to HTML <b>bold</b>
    escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped, flags=re.DOTALL)
    return escaped


def build_post_prompt(topic: str, style: str = None, previous: str = None) -> str:
    style_hint = f"\nСтиль: {POST_STYLES[style]}." if style else ""
    regen_hint = (
        f"\nПопередній варіант (напиши ІНШИЙ — інший гачок, інший підхід):\n{previous}"
        if previous else ""
    )
    return (
        f"Тема посту: {topic}.{style_hint}{regen_hint}\n\n"
        "Правила:\n"
        "- Довжина: 150–220 слів (без води — краще коротко і по суті)\n"
        "- Від першої особи: «я», «мій», «я спробував» — НІКОЛИ «ми» чи «наш»\n"
        "- НЕ починай з «Хочу поділитись», «Нещодавно», «Друзі», «Всім привіт» — одразу до суті\n"
        "- Структура: конкретна ситуація або факт → що я зробив (інструмент + дія) → результат у цифрах → один крок для читача\n"
        "- ОБОВ'ЯЗКОВО: назва конкретного інструменту + що саме він робить у цьому кейсі\n"
        "- ОБОВ'ЯЗКОВО: цифра або порівняння (час, кількість кроків, результат)\n"
        "- 3–4 емодзі органічно\n"
        "- 3–5 хештегів наприкінці: #ailife_ua + тематичні\n"
        "- <b>жирний</b> для заголовка і ключових думок\n\n"
        "Відповідай ТІЛЬКИ валідним JSON без markdown та пояснень:\n"
        '{"title":"чіпляючий заголовок","text":"повний текст посту з емодзі та хештегами",'
        '"image_prompt":"minimalist dark tech illustration, glowing neon UI elements, flat design, no people, cinematic lighting — add specific visual detail relevant to this topic, high quality digital art"}'
    )


def parse_post_json(raw: str) -> dict:
    clean = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception:
        return {"title": "", "text": raw, "image_prompt": ""}


def format_channel_post(title: str, text: str) -> str:
    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    parts.append(sanitize_post(text))
    return "\n\n".join(parts)


# ── History ───────────────────────────────────────────────────────────────────

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


# ── Queue ─────────────────────────────────────────────────────────────────────

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


# ── Image generation ──────────────────────────────────────────────────────────

async def generate_image_grok(prompt: str) -> bytes | None:
    try:
        headers = {"Authorization": f"Bearer {XAI_KEY}", "Content-Type": "application/json"}
        payload = {"model": "grok-2-image-1212", "prompt": prompt, "n": 1, "response_format": "url"}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post("https://api.x.ai/v1/images/generations", headers=headers, json=payload)
            r.raise_for_status()
            img_url = r.json()["data"][0]["url"]
            img = await client.get(img_url)
            if "image" in img.headers.get("content-type", ""):
                return img.content
    except Exception as e:
        print(f"⚠️ Grok image error: {e}")
    return None


async def generate_image_pollinations(prompt: str, retries: int = 2) -> bytes | None:
    encoded = urllib.parse.quote(prompt)
    for attempt in range(retries):
        try:
            url = f"https://image.pollinations.ai/prompt/{encoded}?model=flux&width=1024&height=1024&nologo=true&seed={random.randint(1, 99999)}"
            async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
                r = await client.get(url)
                if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                    return r.content
                print(f"⚠️ Pollinations attempt {attempt+1}: status={r.status_code}, content-type={r.headers.get('content-type','?')}")
        except Exception as e:
            print(f"⚠️ Pollinations attempt {attempt+1} error: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(5)
    return None


async def generate_image(prompt: str) -> bytes | None:
    if XAI_KEY:
        img = await generate_image_grok(prompt)
        if img:
            return img
        print("⚠️ Grok не відповів — fallback на Pollinations")
    return await generate_image_pollinations(prompt)


async def send_post_with_image(send_fn, post_msg: str, image_bytes: bytes | None, keyboard=None):
    """Надсилає пост: якщо є зображення — фото+підпис, інакше — текст."""
    if image_bytes:
        if len(post_msg) <= 1024:
            await send_fn("photo", photo=image_bytes, caption=post_msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await send_fn("photo", photo=image_bytes)
            await send_fn("text", text=post_msg, parse_mode="HTML", reply_markup=keyboard)
    else:
        await send_fn("text", text=post_msg, parse_mode="HTML", reply_markup=keyboard)


# ── Daily posting job ─────────────────────────────────────────────────────────

async def post_daily_job(context: ContextTypes.DEFAULT_TYPE):
    queue = load_queue()
    today = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    changed = False
    for item in queue:
        if item["date"] == today and not item["posted"]:
            try:
                post_text = format_channel_post(item.get("title", ""), item["text"])
                image_prompt = item.get("image_prompt", "")

                if not image_prompt:
                    topic = item.get("topic", "AI productivity")
                    image_prompt = (
                        f"minimalist dark tech illustration, glowing neon UI elements, flat design, no people, "
                        f"{topic[:60]}, cinematic lighting, high quality digital art"
                    )
                    print(f"⚠️ image_prompt порожній у черзі, використовую fallback для: {topic[:40]}")

                print(f"🎨 Генерую зображення для: {item.get('topic', '')[:40]}")
                image_bytes = await generate_image(image_prompt)
                print(f"{'✅ Зображення отримано' if image_bytes else '❌ Зображення не вдалось — публікую без картинки'}")

                if image_bytes:
                    if len(post_text) <= 1024:
                        await context.bot.send_photo(
                            chat_id=CHANNEL_ID, photo=image_bytes,
                            caption=post_text, parse_mode="HTML"
                        )
                    else:
                        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=image_bytes)
                        await context.bot.send_message(
                            chat_id=CHANNEL_ID, text=post_text, parse_mode="HTML"
                        )
                else:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID, text=post_text, parse_mode="HTML"
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

COMMANDS_TEXT = (
    "<b>📋 Команди бота:</b>\n\n"
    "/generate — обрати стиль і отримати пост з підказками тем\n"
    "/week — згенерувати 7 тем на тиждень і запланувати автопублікацію\n"
    "/queue — переглянути заплановані пости\n"
    "/testpost — перевірити чи генерується зображення (дебаг)\n"
    "/help — ця довідка\n\n"
    "💬 <b>Або просто напиши тему</b> — і отримаєш готовий пост із зображенням.\n"
    "Приклад: <code>5 AI-інструментів для продуктивності</code>"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привіт! Я генерую пости для @ailife_ua — з текстом і зображенням.\n\n"
        + COMMANDS_TEXT
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        COMMANDS_TEXT
        + "\n\n<b>Стилі постів</b> (обираєш у /generate):\n"
        "🔧 Лайфхак — практична порада\n"
        "🛠 Інструмент — огляд AI-сервісу\n"
        "📖 Історія — кейс із досвіду\n"
        "❓ Питання — залучення аудиторії\n"
        "📰 Новина — AI-новина з коментарем\n\n"
        "<b>Автопублікація:</b> щодня о 8:00 за Києвом — якщо є пости в черзі (/week → Запланувати)."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def testpost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Тестую генерацію зображення...")
    test_prompt = "minimalist dark tech illustration, glowing neon UI elements, flat design, no people, AI technology, cinematic lighting, high quality digital art"
    image_bytes = await generate_image(test_prompt)
    if image_bytes:
        await update.message.reply_photo(photo=image_bytes, caption="✅ Зображення працює! Автопублікація повинна теж генерувати картинки.")
    else:
        await update.message.reply_text("❌ Зображення не вдалось — перевір логи Railway (Grok + Pollinations обидва не відповіли).")


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
    sample = random.sample(SUGGESTED_TOPICS, 4)
    style_keyboard = [
        [InlineKeyboardButton("🔧 Лайфхак", callback_data="style:tip"),
         InlineKeyboardButton("🛠 Інструмент", callback_data="style:tool")],
        [InlineKeyboardButton("📖 Історія", callback_data="style:story"),
         InlineKeyboardButton("❓ Питання", callback_data="style:question")],
        [InlineKeyboardButton("📰 Новина", callback_data="style:news")],
    ]
    ideas = "\n".join(f"• {t}" for t in sample)
    text = (
        "Обери стиль або просто напиши тему нижче.\n\n"
        f"<b>💡 Ідеї для теми:</b>\n{ideas}"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(style_keyboard), parse_mode="HTML")


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


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking_msg = await update.message.reply_text("⏳ Генерую 7 тем на тиждень...")

    history = load_history()
    history_hint = ""
    if history:
        history_hint = "\nВже використані теми (НЕ повторюй):\n" + "\n".join(f"- {t}" for t in history[-20:])

    prompt = (
        "Згенеруй 7 різних тем для Telegram-постів каналу @ailife_ua про AI та продуктивність — по одній на кожен день тижня.\n"
        "Обов'язково використовуй РІЗНІ інструменти: ChatGPT, Claude, Gemini, Grok, Midjourney, Perplexity, Make/Zapier, Notion AI — не повторюй один і той самий.\n"
        "Типи: лайфхак, огляд інструменту, порівняння AI, кейс з досвіду, новина, питання для аудиторії.\n"
        "Відповідь — рівно 7 рядків, кожен: тільки тема без нумерації та зайвих слів.\n"
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
        status = await query.message.reply_text("⏳ Генерую всі 7 постів...")

        topics_list = "\n".join(f"{DAYS_UA[i]}: {t}" for i, t in enumerate(topics))
        prompt = (
            f"Згенеруй 7 окремих постів для Telegram-каналу @ailife_ua від імені Тараса.\n"
            f"Теми по днях:\n{topics_list}\n\n"
            f"Правила для кожного посту:\n"
            f"- 150-220 слів, від першої особи (я, мій, я спробував) — НІКОЛИ ми/наш\n"
            f"- НЕ починай з «Хочу поділитись», «Нещодавно», «Друзі» — одразу до суті\n"
            f"- ОБОВ'ЯЗКОВО: назва конкретного інструменту + що саме він робить у кейсі\n"
            f"- ОБОВ'ЯЗКОВО: цифра або порівняння (час, кроки, результат)\n"
            f"- Структура: конкретна ситуація → що зробив (інструмент + дія) → результат → крок для читача\n"
            f"- 3-4 емодзі органічно, хештеги #ailife_ua наприкінці\n"
            f"- Жирний текст — ТІЛЬКИ через HTML теги <b>текст</b>, НЕ через зірочки **текст**\n"
            f"- Мова ВИКЛЮЧНО українська\n"
            f"- Формат відповіді — рівно 7 блоків, розділених лінією '---'\n"
        )

        try:
            raw = await call_groq(prompt, status_msg=status)
            posts = [p.strip() for p in raw.split("---") if p.strip()]

            await status.delete()

            week_posts = []
            for i, post_text in enumerate(posts[:7]):
                day = DAYS_UA[i] if i < len(DAYS_UA) else f"День {i+1}"
                topic = topics[i] if i < len(topics) else ""
                fallback_img = (
                    f"minimalist dark tech illustration, glowing neon UI elements, flat design, no people, "
                    f"{topic[:60]}, cinematic lighting, high quality digital art"
                )
                week_posts.append({"topic": topic, "title": "", "text": post_text, "image_prompt": fallback_img})
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
        prompt = build_post_prompt(topic)
        raw = await call_groq(prompt, status_msg=query.message)
        post = parse_post_json(raw)

        post_text = post.get("text", raw)
        post_title = post.get("title", "")
        image_prompt = post.get("image_prompt", "")

        context.user_data["last_topic"] = topic
        context.user_data["last_post"] = post_text
        context.user_data["last_title"] = post_title

        post_msg = format_channel_post(post_title, post_text)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Ще варіант", callback_data="regen"),
            InlineKeyboardButton("📅 Назад до тижня", callback_data="week:back"),
        ]])

        await query.edit_message_text("🎨 Генерую зображення...")
        image_bytes = await generate_image(image_prompt) if image_prompt else None

        if image_bytes:
            await query.message.delete()
            if len(post_msg) <= 1024:
                await query.message.reply_photo(photo=image_bytes, caption=post_msg, parse_mode="HTML", reply_markup=keyboard)
            else:
                await query.message.reply_photo(photo=image_bytes)
                await query.message.reply_text(post_msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await query.edit_message_text(post_msg, reply_markup=keyboard, parse_mode="HTML")

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
            "title": post.get("title", ""),
            "text": post["text"],
            "image_prompt": post.get("image_prompt", ""),
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

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    style = context.user_data.pop("style", None)
    prompt = build_post_prompt(topic, style=style)

    thinking_msg = await update.message.reply_text("⏳ Генерую пост...")

    try:
        raw = await call_groq(prompt, status_msg=thinking_msg)
        post = parse_post_json(raw)

        context.user_data["last_topic"] = topic
        context.user_data["last_post"] = post.get("text", raw)
        context.user_data["last_title"] = post.get("title", "")

        post_msg = format_channel_post(post.get("title", ""), post.get("text", raw))
        image_prompt = post.get("image_prompt", "")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Ще варіант", callback_data="regen"),
            InlineKeyboardButton("✏️ Новий пост", callback_data="new"),
        ]])

        await thinking_msg.edit_text("🎨 Генерую зображення...")
        image_bytes = await generate_image(image_prompt) if image_prompt else None
        await thinking_msg.delete()

        if image_bytes:
            if len(post_msg) <= 1024:
                await update.message.reply_photo(photo=image_bytes, caption=post_msg, parse_mode="HTML", reply_markup=keyboard)
            else:
                await update.message.reply_photo(photo=image_bytes)
                await update.message.reply_text(post_msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(post_msg, parse_mode="HTML", reply_markup=keyboard)

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
        prompt = build_post_prompt(topic, previous=previous_post)
        raw = await call_groq(prompt)
        post = parse_post_json(raw)
        context.user_data["last_post"] = post.get("text", raw)
        context.user_data["last_title"] = post.get("title", "")

        post_msg = format_channel_post(post.get("title", ""), post.get("text", raw))
        image_prompt = post.get("image_prompt", "")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Ще варіант", callback_data="regen"),
            InlineKeyboardButton("✏️ Новий пост", callback_data="new"),
        ]])

        await query.edit_message_text("🎨 Генерую зображення...")
        image_bytes = await generate_image(image_prompt) if image_prompt else None

        if image_bytes:
            await query.message.delete()
            if len(post_msg) <= 1024:
                await query.message.reply_photo(photo=image_bytes, caption=post_msg, parse_mode="HTML", reply_markup=keyboard)
            else:
                await query.message.reply_photo(photo=image_bytes)
                await query.message.reply_text(post_msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await query.edit_message_text(post_msg, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        await query.edit_message_text(f"❌ Помилка: {e}")


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("Не задано TELEGRAM_BOT_TOKEN у .env файлі")
    if not GROQ_KEY:
        raise ValueError("Не задано GROQ_API_KEY у .env файлі")

    async def post_init(application):
        await application.bot.set_my_commands([
            ("generate", "Обрати стиль і згенерувати пост"),
            ("week", "7 тем на тиждень + автопублікація"),
            ("queue", "Переглянути заплановані пости"),
            ("testpost", "Перевірити генерацію зображення"),
            ("help", "Довідка по командах"),
        ])

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("generate", generate_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("testpost", testpost_command))
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
