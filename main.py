import disnake
from disnake.ext import commands
import aiohttp, sqlite3, asyncio, os, re, random
from datetime import datetime
from dotenv import load_dotenv

# Поиск вынесен в отдельный легкий импорт
try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME = "satx-host"

# Работаем с одной базой
db = sqlite3.connect("intelligence.db", check_same_thread=False)
cur = db.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS logs (user_id TEXT, username TEXT, role TEXT, content TEXT, timestamp TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS dossiers (user_id TEXT PRIMARY KEY, profile TEXT)")
db.commit()

bot = commands.Bot(command_prefix="!", intents=disnake.Intents.all())

# Роли для разнообразия (выбираются случайно при ответе)
ROLES = [
    "Ты Мяуми-тян. Милая, используй ня~ и ♡. Пиши коротко.",
    "Ты Ксавия. Язвительная демонесса. Отвечай кратко и холодно.",
    "Ты обычный парень. Используй сленг (тип, хз, капец). Пиши просто."
]

def save_log(uid, name, role, content):
    now = datetime.now().strftime("%H:%M")
    cur.execute("INSERT INTO logs VALUES (?, ?, ?, ?, ?)", (uid, name, role, content, now))
    # Чистим старое (храним последние 50 фраз на юзера)
    cur.execute("DELETE FROM logs WHERE user_id = ? AND rowid NOT IN (SELECT rowid FROM logs WHERE user_id = ? ORDER BY rowid DESC LIMIT 50)", (uid, uid))
    db.commit()

async def get_web(text):
    if not DDGS: return ""
    try:
        q = re.sub(r'(поищи|найди|кто|что|такое)', '', text.lower()).strip()
        with DDGS() as ddgs:
            res = list(ddgs.text(q, max_results=1))
            return res[0]['body'][:300] if res else ""
    except: return ""

async def process_ai(message, clean_text):
    uid = str(message.author.id)
    name = message.author.display_name
    
    # Контекст из последних 10 сообщений этого юзера
    cur.execute("SELECT username, content FROM logs WHERE user_id = ? ORDER BY rowid DESC LIMIT 10", (uid,))
    history = " | ".join([f"{r[0]}: {r[1]}" for r in reversed(cur.fetchall())])
    
    # Поиск если нужно
    web_data = ""
    if any(x in clean_text.lower() for x in ["кто", "что", "поищи"]):
        web_data = f"\nИНФО ИЗ СЕТИ: {await get_web(clean_text)}"

    # Досье
    cur.execute("SELECT profile FROM dossiers WHERE user_id = ?", (uid,))
    dossier_row = cur.fetchone()
    dossier = f"\nДОСЬЕ: {dossier_row[0]}" if dossier_row else ""

    role = random.choice(ROLES)
    prompt = f"{role}{dossier}{web_data}\nПрошлое: {history}\n{name}: {clean_text}\nОтвет (до 40 слов):"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{OLLAMA_URL}/api/generate", json={
                "model": MODEL_NAME, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.4, "num_predict": 80}
            }) as resp:
                data = await resp.json()
                reply = data.get("response", "").strip().lower()
                # Убираем артефакты нейронки
                reply = re.sub(r'^(ассистент|бот|мяуми|ксавия):\s*', '', reply)
                
                if reply:
                    save_log(uid, name, "user", clean_text)
                    save_log(uid, "assistant", "assistant", reply)
                    async with message.channel.typing():
                        await asyncio.sleep(1)
                        await message.channel.send(reply[:2000])
    except Exception as e:
        print(f"AI Error: {e}")

@bot.event
async def on_message(message):
    if message.author.bot: return
    
    clean = re.sub(r'<@!?\d+>', '', message.content).strip()
    
    # Ответ на пинг или ключевые слова
    if bot.user.mentioned_in(message) or clean.lower().startswith("бот"):
        await process_ai(message, clean)
    elif clean:
        save_log(str(message.author.id), message.author.display_name, "user", clean)

@bot.event
async def on_ready():
    print(f"--- Бот {bot.user} запущен и готов к работе ---")

bot.run(TOKEN)