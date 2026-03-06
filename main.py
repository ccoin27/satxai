"""
Discord чат-бот с нейросетью (Ollama).
Память, естественное поведение, досье пользователей.
"""
import disnake
from disnake.ext import commands
import aiohttp
import sqlite3
import asyncio
import os
import json
import random
import re
from urllib.parse import quote
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TENOR_API_KEY = os.getenv("TENOR_API_KEY")
# Кастомная модель из Modelfile: ollama create satx-host -f Modelfile
MODEL_NAME = "satx-host"
PROMPT_LOG_DIR = os.getenv("PROMPT_LOG_DIR", "prompt_logs")
ARCHIVE_CHANNEL_ID = 1371191476054393066  # канал для анализа

# Пассивные реакции гифками: фраза -> поиск в Tenor
GIF_REACT_KEYWORDS = {
    "лол": "laughing",
    "рофл": "laughing",
    "ору": "laughing",
    "смешно": "funny",
    "кринж": "cringe",
    "офигеть": "surprised",
    "охуеть": "shocked",
    "вайб": "vibing",
    "го": "lets go",
    "погнали": "lets go",
    "красавчик": "thumbs up",
    "респект": "respect",
    "фейл": "fail",
    "вин": "win",
    "ахаха": "laughing",
    "хаха": "laughing",
    "кек": "laughing",
    "ага": "agreement",
    "ну да": "nodding",
    "погнали": "running",
}
GIF_REACT_BASE_CHANCE = 0.04  # 4% на любое сообщение
GIF_REACT_KEYWORD_CHANCE = 0.25  # 25% если есть ключевое слово
GIF_URL_PATTERN = re.compile(
    r"https?://(?:media\.tenor\.com|c\.tenor\.com|i\.giphy\.com|media\.giphy\.com|giphy\.com/gifs|tenor\.com/view)[^\s\)\]\"<>]+",
    re.I,
)

# База данных
db = sqlite3.connect("intelligence.db", check_same_thread=False)
cur = db.cursor()
cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS channel_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS dossiers (
        user_id TEXT PRIMARY KEY,
        profile TEXT,
        updated_at TEXT
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS channel_state (
        channel_id TEXT PRIMARY KEY,
        last_analyzed_msg_id INTEGER DEFAULT 0,
        summary TEXT,
        updated_at TEXT
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS channel_gifs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        gif_url TEXT NOT NULL,
        added_at TEXT NOT NULL
    )
""")
cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_logs ON channel_logs(channel_id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_gifs ON channel_gifs(channel_id)")
cur.execute(
    "CREATE TABLE IF NOT EXISTS gif_scan_done (channel_id TEXT PRIMARY KEY)"
)
db.commit()

# Отдельная база для архива всех сообщений (без дублирования)
archive_db = sqlite3.connect("archive.db", check_same_thread=False)
archive_db.execute("PRAGMA journal_mode=WAL")
archive_cur = archive_db.cursor()
archive_cur.execute("""
    CREATE TABLE IF NOT EXISTS raw_messages (
        message_id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        content TEXT NOT NULL,
        links TEXT,
        attachments TEXT,
        created_at TEXT,
        stored_at TEXT NOT NULL
    )
""")
archive_cur.execute("CREATE INDEX IF NOT EXISTS idx_archive_channel ON raw_messages(channel_id)")
archive_cur.execute("CREATE INDEX IF NOT EXISTS idx_archive_created ON raw_messages(created_at)")
archive_db.commit()

# Паттерн для извлечения всех ссылок
URL_PATTERN = re.compile(r"https?://[^\s\)\]\"<>]+", re.I)

bot = commands.Bot(command_prefix="!", intents=disnake.Intents.all())

# Промпт зашит в Modelfile (satx-host). Сюда только динамический контекст.

# Ключевые фразы — бот может ответить без @
REACT_TRIGGERS = (
    "бот", "бота", "боту", "ботом", "боте",
    "что думаешь", "твоё мнение", "твое мнение", "что скажешь",
    "а ты", "а ты как", "ты как", "согласен", "согласна",
    "что по этому", "как думаешь", "твои мысли",
)

# Интервал автоанализа: каждые N сообщений в канале
AUTO_ANALYZE_EVERY = 25


def get_user_context(user_id: str, limit: int = 12) -> list[dict]:
    """Получить историю диалога в формате для Chat API."""
    cur.execute(
        """
        SELECT role, content FROM logs 
        WHERE user_id = ? 
        ORDER BY id DESC LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    # Разворачиваем — старые сообщения первыми
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def get_dossier(user_id: str) -> Optional[str]:
    cur.execute("SELECT profile FROM dossiers WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else None


def save_message(user_id: str, role: str, content: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur.execute(
        "INSERT INTO logs (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, now),
    )
    cur.execute(
        """
        DELETE FROM logs WHERE user_id = ? AND id NOT IN (
            SELECT id FROM logs WHERE user_id = ? ORDER BY id DESC LIMIT 50
        )
        """,
        (user_id, user_id),
    )
    db.commit()


def save_channel_message(channel_id: str, user_id: str, username: str, role: str, content: str) -> int:
    """Сохранить сообщение в лог канала. Возвращает id."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur.execute(
        """INSERT INTO channel_logs (channel_id, user_id, username, role, content, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (channel_id, user_id, username, role, content, now),
    )
    row_id = cur.lastrowid
    # Оставляем последние 80 сообщений в канале
    cur.execute(
        """
        DELETE FROM channel_logs WHERE channel_id = ? AND id NOT IN (
            SELECT id FROM channel_logs WHERE channel_id = ? ORDER BY id DESC LIMIT 80
        )
        """,
        (channel_id, channel_id),
    )
    db.commit()
    return row_id or 0


def get_channel_context(channel_id: str, limit: int = 20) -> list[dict]:
    """Последние сообщения в канале для контекста обстановки."""
    cur.execute(
        """
        SELECT user_id, username, role, content FROM channel_logs
        WHERE channel_id = ? ORDER BY id DESC LIMIT ?
        """,
        (channel_id, limit),
    )
    rows = cur.fetchall()
    return [{"user_id": r[0], "username": r[1], "role": r[2], "content": r[3]} for r in reversed(rows)]


def save_channel_gif(channel_id: str, gif_url: str) -> None:
    """Сохранить URL гифки из чата."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur.execute(
        "INSERT INTO channel_gifs (channel_id, gif_url, added_at) VALUES (?, ?, ?)",
        (channel_id, gif_url, now),
    )
    cur.execute(
        """
        DELETE FROM channel_gifs WHERE channel_id = ? AND id NOT IN (
            SELECT id FROM channel_gifs WHERE channel_id = ? ORDER BY id DESC LIMIT 100
        )
        """,
        (channel_id, channel_id),
    )
    db.commit()


def is_gif_scan_done(channel_id: str) -> bool:
    cur.execute("SELECT 1 FROM gif_scan_done WHERE channel_id = ?", (channel_id,))
    return cur.fetchone() is not None


def mark_gif_scan_done(channel_id: str) -> None:
    cur.execute("INSERT OR IGNORE INTO gif_scan_done (channel_id) VALUES (?)", (channel_id,))
    db.commit()


def get_channel_gifs(channel_id: str, limit: int = 30) -> list[str]:
    """Получить гифки, которые раньше отправляли в канал."""
    cur.execute(
        "SELECT gif_url FROM channel_gifs WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
        (channel_id, limit),
    )
    return [r[0] for r in cur.fetchall()]


async def _backfill_channel_gifs(channel: disnake.TextChannel) -> None:
    """Сканировать историю канала и собрать гифки."""
    cid = str(channel.id)
    if is_gif_scan_done(cid):
        return
    try:
        async for msg in channel.history(limit=200):
            if msg.author.bot:
                continue
            for url in extract_gif_urls(msg):
                save_channel_gif(cid, url)
        mark_gif_scan_done(cid)
    except Exception as e:
        print(f"GIF backfill error: {e}")


def save_to_archive(message: disnake.Message) -> None:
    """Сохранить сообщение в архив. Без дубликатов (message_id PRIMARY KEY)."""
    msg_id = str(message.id)
    cid = str(message.channel.id)
    uid = str(message.author.id)
    username = (message.author.display_name or message.author.name)[:64]
    content = (message.content or "")[:4000]
    created = message.created_at.isoformat() if message.created_at else ""
    stored = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    links = []
    links.extend(URL_PATTERN.findall(content))
    for embed in message.embeds:
        if embed.url:
            links.append(embed.url)
        if embed.image and embed.image.url:
            links.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            links.append(embed.thumbnail.url)
        if embed.video and embed.video.url:
            links.append(embed.video.url)
    for att in message.attachments:
        links.append(att.url)
    links = list(dict.fromkeys(links))[:50]
    links_json = json.dumps(links, ensure_ascii=False) if links else ""

    atts = [{"url": a.url, "name": a.filename} for a in message.attachments[:20]]
    atts_json = json.dumps(atts, ensure_ascii=False) if atts else ""

    try:
        archive_cur.execute(
            """INSERT OR IGNORE INTO raw_messages
               (message_id, channel_id, user_id, username, content, links, attachments, created_at, stored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, cid, uid, username, content, links_json, atts_json, created, stored),
        )
        archive_db.commit()
    except Exception as e:
        print(f"Archive save error: {e}")


def extract_gif_urls(message: disnake.Message) -> list[str]:
    """Извлечь URL гифок из сообщения (embeds, attachments, content)."""
    urls = []
    for embed in message.embeds:
        if embed.image and embed.image.url:
            urls.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            urls.append(embed.thumbnail.url)
        if embed.video and embed.video.url:
            urls.append(embed.video.url)
    for att in message.attachments:
        if att.content_type and "gif" in att.content_type:
            urls.append(att.url)
        elif att.filename and att.filename.lower().endswith(".gif"):
            urls.append(att.url)
    for m in GIF_URL_PATTERN.findall(message.content or ""):
        urls.append(m)
    return urls


def get_channel_summary(channel_id: str) -> Optional[str]:
    cur.execute("SELECT summary FROM channel_state WHERE channel_id = ?", (channel_id,))
    row = cur.fetchone()
    return row[0] if row else None


def should_auto_respond(text: str) -> bool:
    """Проверить, стоит ли ответить без упоминания."""
    lower = text.lower().strip()
    return any(trigger in lower for trigger in REACT_TRIGGERS)


@asynccontextmanager
async def aio_session():
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        yield session


async def get_web(query: str) -> str:
    """Поиск в интернете по запросу (ddgs)."""
    if not DDGS:
        return ""
    try:
        q = re.sub(r"(поищи|найди|кто|что|такое|в интернете)\s*", "", query.lower()).strip()
        if len(q) < 3:
            return ""
        # DDGS синхронный — запускаем в потоке
        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=2))
        results = await asyncio.to_thread(_search)
        if results:
            return (results[0].get("body") or results[0].get("title", ""))[:400]
    except Exception as e:
        print(f"Search error: {e}")
    return ""


async def get_gif(search_term: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Поиск гифки через Tenor API. Возвращает URL или None."""
    if not TENOR_API_KEY:
        return None
    try:
        q = quote(search_term)
        url = (
            "https://tenor.googleapis.com/v2/search"
            f"?q={q}&key={TENOR_API_KEY}&client_key=satxai&limit=5"
        )
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        results = data.get("results", [])
        if not results:
            return None
        gif = random.choice(results)
        media = gif.get("media_formats", {})
        return media.get("gif", {}).get("url") or media.get("tinygif", {}).get("url")
    except Exception as e:
        print(f"Tenor error: {e}")
    return None


def _gif_react_search(text: str) -> Optional[str]:
    """Определить поисковый запрос для гифки по тексту сообщения."""
    lower = text.lower()
    for kw, search in GIF_REACT_KEYWORDS.items():
        if kw in lower:
            return search
    return None


def _log_prompt(messages: list[dict], response: Optional[str], channel_id: str = "") -> None:
    """Логирование промпта в файл."""
    try:
        os.makedirs(PROMPT_LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"{PROMPT_LOG_DIR}/{ts}_{channel_id or 'dm'}.log"
        with open(fname, "w", encoding="utf-8") as f:
            f.write("=== PROMPT ===\n\n")
            for m in messages:
                f.write(f"[{m['role'].upper()}]\n{m['content']}\n\n")
            f.write("=== RESPONSE ===\n\n")
            f.write(response or "(empty)")
    except Exception as e:
        print(f"Prompt log error: {e}")


async def ollama_chat(messages: list[dict], session: aiohttp.ClientSession) -> Optional[str]:
    """Запрос к Ollama Chat API. Параметры — из Modelfile."""
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
    }
    url = f"{OLLAMA_URL.rstrip('/')}/api/chat"
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"Ollama error {resp.status}: {text[:200]}")
                return None
            data = await resp.json()
            msg = data.get("message", {})
            return (msg.get("content") or "").strip()
    except asyncio.TimeoutError:
        print("Ollama timeout")
        return None
    except Exception as e:
        print(f"Ollama request error: {e}")
        return None


def _build_context_block(channel_ctx: list[dict], channel_summary: Optional[str]) -> str:
    """Собрать блок 'обстановка в чате' для промпта."""
    lines = []
    if channel_summary:
        lines.append(f"Обстановка: {channel_summary}")
    if channel_ctx:
        lines.append("Переписка:")
        for m in channel_ctx:
            who = "ты" if m["role"] == "assistant" else m["username"]
            lines.append(f"  {who}: {m['content']}")
    return "\n".join(lines) if lines else ""


async def process_response(message: disnake.Message, text: str) -> None:
    uid = str(message.author.id)
    cid = str(message.channel.id)
    username = message.author.display_name or message.author.name
    bot_name = message.guild.me.display_name if message.guild else "Бот"

    # Поиск в интернете
    web_ctx = ""
    if any(x in text.lower() for x in ["поищи", "найди", "кто такой", "что такое", "когда"]):
        web_result = await get_web(text)
        if web_result:
            web_ctx = f"\n\nИз интернета: {web_result}"

    # Досье участников из контекста канала
    channel_ctx = get_channel_context(cid)
    dossiers_parts = []
    seen_uids = set()
    for m in channel_ctx:
        if m["role"] == "user" and m["user_id"] not in seen_uids:
            seen_uids.add(m["user_id"])
            d = get_dossier(m["user_id"])
            if d:
                dossiers_parts.append(f"{m['username']}: {d}")
    dossier_block = "\n".join(dossiers_parts[:5]) if dossiers_parts else ""
    if dossier_block:
        dossier_block = f"\n\nДосье участников:\n{dossier_block}"

    # Контекст канала — обстановка
    channel_summary = get_channel_summary(cid)
    situation = _build_context_block(channel_ctx, channel_summary)

    # Сообщение уже сохранено в on_message

    # Собираем сообщения для Chat API
    history = get_user_context(uid)
    messages = []

    # Динамический контекст (промпт личности — в Modelfile)
    system_content = dossier_block + web_ctx
    if situation:
        system_content += f"\n\n{situation}\n(ты — {bot_name}, отвечай в контексте переписки)"
    if not system_content.strip():
        system_content = "(контекст пуст)"

    messages.append({"role": "system", "content": system_content.strip()})

    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": f"{username}: {text}"})

    async with aio_session() as session:
        response = await ollama_chat(messages, session)

    _log_prompt(messages, response, cid)

    if not response:
        response = "Что-то пошло не так, попробуй ещё раз."

    # Очистка от артефактов (если модель генерирует лишнее)
    response = re.sub(r"^(user|assistant|юзер|ассистент):\s*", "", response, flags=re.I)
    response = response.strip()

    if not response:
        response = "Хм, не совсем понял. Уточни?"

    # Сохраняем ответ в оба лога
    save_message(uid, "assistant", response)
    save_channel_message(cid, str(bot.user.id), bot_name, "assistant", response)

    # Отправка с имитацией печати
    async with message.channel.typing():
        await asyncio.sleep(min(1.5, len(response) * 0.03 + 0.3))
    await message.channel.send(response)


async def cmd_analyze(message: disnake.Message) -> None:
    """Анализ чата и создание досье (только для админов)."""
    if not message.author.guild_permissions.administrator:
        return
    msg_status = await message.channel.send("🔍 Анализирую чат...")
    users_data = {}
    async for m in message.channel.history(limit=500):
        if m.author.bot:
            continue
        uid = str(m.author.id)
        name = m.author.display_name or m.author.name
        users_data.setdefault(uid, {"name": name, "msgs": []})["msgs"].append(m.content)

    for uid, data in users_data.items():
        chunk = " | ".join(data["msgs"][:30])
        prompt = f"Кратко опиши человека по фразам (1–2 предложения): {chunk}"
        messages = [
            {"role": "system", "content": "Ты аналитик. Дай краткое описание личности."},
            {"role": "user", "content": prompt},
        ]
        try:
            async with aio_session() as session:
                summary = await ollama_chat(messages, session)
            if summary:
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                cur.execute(
                    "INSERT OR REPLACE INTO dossiers (user_id, profile, updated_at) VALUES (?, ?, ?)",
                    (uid, summary[:500], now),
                )
                db.commit()
        except Exception as e:
            print(f"Dossier error for {uid}: {e}")

    await msg_status.edit(content="✅ Досье обновлены.")


async def auto_analyze_channel(channel: disnake.TextChannel) -> None:
    """Фоновая автоанализ канала: досье + краткое резюме обстановки."""
    cid = str(channel.id)
    try:
        users_data = {}
        async for m in channel.history(limit=200):
            if m.author.bot:
                continue
            uid = str(m.author.id)
            name = m.author.display_name or m.author.name
            users_data.setdefault(uid, {"name": name, "msgs": []})["msgs"].append(m.content)

        # Обновляем досье
        for uid, data in users_data.items():
            if len(data["msgs"]) < 5:
                continue
            chunk = " | ".join(data["msgs"][:25])
            prompt = f"Кратко опиши человека по фразам (1–2 предложения): {chunk}"
            messages = [
                {"role": "system", "content": "Ты аналитик. Дай краткое описание личности."},
                {"role": "user", "content": prompt},
            ]
            try:
                async with aio_session() as session:
                    summary = await ollama_chat(messages, session)
                if summary:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M")
                    cur.execute(
                        "INSERT OR REPLACE INTO dossiers (user_id, profile, updated_at) VALUES (?, ?, ?)",
                        (uid, summary[:500], now),
                    )
                    db.commit()
            except Exception:
                pass

        # Резюме обстановки в канале
        all_msgs = []
        for uid, data in users_data.items():
            for msg in data["msgs"][:10]:
                all_msgs.append(f"{data['name']}: {msg}")
        if len(all_msgs) >= 5:
            chunk = "\n".join(all_msgs[-30:])
            prompt = f"О чём говорят в чате? Тон, темы, настроение. 1–2 предложения:\n{chunk}"
            messages = [
                {"role": "system", "content": "Ты наблюдатель. Кратко опиши обстановку в чате."},
                {"role": "user", "content": prompt},
            ]
            try:
                async with aio_session() as session:
                    summary = await ollama_chat(messages, session)
                if summary:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M")
                    cur.execute(
                        """INSERT OR REPLACE INTO channel_state (channel_id, summary, updated_at)
                           VALUES (?, ?, ?)""",
                        (cid, summary[:300], now),
                    )
                    db.commit()
            except Exception:
                pass
    except Exception as e:
        print(f"Auto-analyze error: {e}")


# Команды через on_message (без @bot.command)
CMD_ANALYZE = ("!анализ", "!analyze")
CMD_ARCHIVE = ("!архив", "!archive")


@bot.event
async def on_message(message: disnake.Message):
    # Архив: сохраняем ВСЕ сообщения (включая ботов), без дубликатов
    save_to_archive(message)

    if message.author.bot:
        return

    raw = message.content.strip()
    if raw.startswith("!"):
        cmd = raw.split()[0].lower() if raw.split() else raw.lower()
        if cmd in CMD_ANALYZE:
            await cmd_analyze(message)
        elif cmd in CMD_ARCHIVE and message.author.guild_permissions.administrator:
            await message.channel.send("⏳ Сканирую архив...")
            await _archive_backfill_and_analyze()
            await message.channel.send("✅ Архив обновлён и проанализирован.")
        return

    cid = str(message.channel.id)

    # Собираем гифки из чата (embeds, attachments, ссылки)
    for gif_url in extract_gif_urls(message):
        save_channel_gif(cid, gif_url)

    clean = re.sub(r"<@!?\d+>", "", message.content or "").strip()
    if not clean:
        return

    uid = str(message.author.id)
    username = message.author.display_name or message.author.name

    # Сохраняем в оба лога (память канала + личная)
    save_message(uid, "user", f"{username}: {clean}")
    save_channel_message(cid, uid, username, "user", clean)

    # Автоанализ каждые N сообщений (в фоне)
    cur.execute("SELECT COUNT(*) FROM channel_logs WHERE channel_id = ?", (cid,))
    count = cur.fetchone()[0]
    if count > 0 and count % AUTO_ANALYZE_EVERY == 0:
        asyncio.create_task(auto_analyze_channel(message.channel))

    # Ответ: при упоминании ИЛИ при триггерах ("бот", "что думаешь" и т.д.)
    mentioned = bot.user.mentioned_in(message)
    should_react = should_auto_respond(clean)

    if mentioned or should_react:
        await process_response(message, clean)
        return

    # Пассивный режим: реагируем гифками из истории чата (или Tenor)
    search_term = _gif_react_search(clean)
    chance = GIF_REACT_KEYWORD_CHANCE if search_term else GIF_REACT_BASE_CHANCE
    if random.random() < chance:
        gif_url = None
        stored = get_channel_gifs(cid)
        if not stored and not is_gif_scan_done(cid):
            asyncio.create_task(_backfill_channel_gifs(message.channel))
        if stored:
            gif_url = random.choice(stored)
        if not gif_url and TENOR_API_KEY:
            term = search_term or random.choice(list(GIF_REACT_KEYWORDS.values()))
            async with aio_session() as session:
                gif_url = await get_gif(term, session)
        if gif_url:
            try:
                await message.reply(gif_url)
            except Exception:
                pass


async def _archive_backfill_and_analyze() -> None:
    """Сканировать канал архива и анализировать."""
    channel = bot.get_channel(ARCHIVE_CHANNEL_ID)
    if not channel:
        for guild in bot.guilds:
            channel = guild.get_channel(ARCHIVE_CHANNEL_ID)
            if channel:
                break
    if not channel:
        print("Archive channel not found")
        return
    cid = str(channel.id)
    try:
        count = 0
        async for msg in channel.history(limit=1000):
            save_to_archive(msg)
            count += 1
        print(f"Archive backfill: {count} messages")
    except Exception as e:
        print(f"Archive backfill error: {e}")
        return

    # Анализ архива
    archive_cur.execute(
        """SELECT username, content, links FROM raw_messages
           WHERE channel_id = ? AND (content != '' OR links != '')
           ORDER BY created_at DESC LIMIT 500""",
        (cid,),
    )
    rows = archive_cur.fetchall()
    if len(rows) < 10:
        return
    chunks = []
    for username, content, links_json in rows[:200]:
        parts = [f"{username}: {content}"] if content else []
        if links_json:
            try:
                links = json.loads(links_json)
                if links:
                    parts.append(f"[ссылки: {', '.join(links[:3])}]")
            except Exception:
                pass
        if parts:
            chunks.append(" ".join(parts))
    if len(chunks) < 10:
        return
    text = "\n".join(chunks[-150:])
    prompt = f"""Проанализируй переписку из чата (сообщения + ссылки). Дай краткое резюме:
- О чём говорят, какие темы
- Тон и настроение
- Ключевые ссылки/ресурсы
- Что можно улучшить в комьюнити

Переписка:
{text[:8000]}"""
    messages = [
        {"role": "system", "content": "Ты аналитик комьюнити. Кратко и по делу."},
        {"role": "user", "content": prompt},
    ]
    try:
        async with aio_session() as session:
            summary = await ollama_chat(messages, session)
        if summary:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            cur.execute(
                """INSERT OR REPLACE INTO channel_state (channel_id, summary, updated_at)
                   VALUES (?, ?, ?)""",
                (cid, summary[:500], now),
            )
            db.commit()
            print(f"Archive analysis done: {summary[:100]}...")
    except Exception as e:
        print(f"Archive analysis error: {e}")


@bot.event
async def on_ready():
    print(f"--- Бот готов — модель: {MODEL_NAME} ---")
    asyncio.create_task(_delayed_archive_task())


async def _delayed_archive_task() -> None:
    await asyncio.sleep(10)
    await _archive_backfill_and_analyze()


if __name__ == "__main__":
    bot.run(TOKEN)
