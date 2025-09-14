import os
import sys
import time
import tempfile
import discord
import aiosqlite
from discord.ext import commands, tasks
from discord import app_commands, FFmpegPCMAudio
from openai import OpenAI

# ------- Config -------
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 1176071547476262986
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
start_time = time.time()

last_role_channel = None
sleeping_channels = set()
global_kill_switch = False  # protocol-1606

DB_FILE = "memory.db"

# ------- Database Setup -------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_id INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                content TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                preferred_title TEXT,
                style_notes TEXT
            )
        """)
        await db.commit()

async def save_memory(user_id: int, channel_id: int, content: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO memory (user_id, channel_id, content) VALUES (?, ?, ?)",
            (user_id, channel_id, content),
        )
        await db.commit()

async def get_memory(channel_id: int, limit: int = 6):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id, content FROM memory WHERE channel_id=? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return rows[::-1]  # oldest first

async def get_pref(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT preferred_title FROM user_prefs WHERE user_id=?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_pref(user_id: int, title: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, preferred_title) VALUES (?, ?)",
            (user_id, title),
        )
        await db.commit()

# ------- AI + TTS -------
async def ai_reply(system_prompt: str, user_prompt: str) -> str:
    if not oai:
        return "⚠️ OpenAI not configured. Add OPENAI_API_KEY."
    try:
        resp = oai.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        for item in resp.output:
            if item.type == "message":
                return "".join(
                    [part.text for part in item.content if getattr(part, "type", "") == "output_text"]
                ) or "✅ Done."
        return "✅ Done."
    except Exception as e:
        return f"❌ AI error: {e}"

async def tts_speak(text: str, vc: discord.VoiceClient):
    if not oai or not vc or not vc.is_connected():
        return
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            response = oai.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="alloy",
                input=text
            )
            response.stream_to_file(fp.name)
            audio_path = fp.name
        vc.play(FFmpegPCMAudio(audio_path))
    except Exception as e:
        print(f"❌ TTS error: {e}")

# ------- Helpers -------
def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID

# ------- Events -------
@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Stark Discoveries")
    )
    print(f"✅ Jarvis online as {bot.user}")
    await init_db()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")
    auto_update_roles.start()

@bot.event
async def on_message(message: discord.Message):
    global global_kill_switch
    if message.author.bot:
        return

    # Global Kill Switch Reactivation
    if global_kill_switch and ("tony stark" in message.content.lower() or "pepper" in message.content.lower()):
        global_kill_switch = False
        await message.channel.send("☀️ Override disengaged. Back online.")
        return
    if global_kill_switch:
        return

    # Protocols
    if message.content.startswith("!protocol-") and is_owner(message.author):
        code = message.content.lower().strip()
        if code == "!protocol-1606":
            global_kill_switch = True
            await message.reply("🛑 Protocol-1606: Global Silent Mode. Say 'Pepper' or 'Tony Stark' to reactivate.")
            return
        if code == "!protocol-01":
            sleeping_channels.add(message.channel.id)
            await message.reply("🛑 Silent Mode activated here.")
            return
        if code == "!protocol-02":
            uptime_seconds = round(time.time() - start_time)
            m, s = divmod(uptime_seconds, 60)
            h, m = divmod(m, 60)
            embed = discord.Embed(title="📊 System Check", color=0xffcc00)
            embed.add_field(name="Uptime", value=f"{h}h {m}m {s}s")
            embed.add_field(name="Servers", value=f"{len(bot.guilds)}")
            embed.add_field(name="Users", value=f"{len(bot.users)}")
            await message.reply(embed=embed)
            return
        if code == "!protocol-03":
            await message.reply("🤖 Greetings. I am J.A.R.V.I.S., Tony Stark's assistant.")
            return
        if code == "!protocol-99":
            await message.reply("⚠️ Shutting down...")
            await bot.close()
            sys.exit(0)

    # Confidentiality
    if any(x in message.channel.name.lower() for x in ["staff", "admin", "management"]):
        return

    # Respond Logic
    if bot.user in message.mentions or (message.channel.id not in sleeping_channels):
        userq = message.clean_content.replace(f"@{bot.user.name}", "").strip()

        # Save to memory
        await save_memory(message.author.id, message.channel.id, f"{message.author.display_name}: {userq}")

        # Fetch memory
        mem = await get_memory(message.channel.id)
        mem_text = "\n".join([f"{uid}: {c}" for uid, c in mem])

        # Get preference
        pref = await get_pref(message.author.id)

        system = (
            "You are J.A.R.V.I.S., Tony Stark's AI assistant. "
            "Be professional, witty, concise. Use memory context. "
            "Never expose confidential info."
        )
        reply = await ai_reply(system, f"Memory:\n{mem_text}\n\nUser: {userq}")

        if is_owner(message.author):
            reply = f"Yes, sir. {reply}"
        elif pref:
            reply = f"Yes, {pref}. {reply}"
        else:
            reply = f"{reply}\n\n💡 I am Tony Stark's personal assistant."

        reply = reply.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
        await message.reply(reply[:1900])

        # Speak if in VC
        if message.guild.voice_client:
            await tts_speak(reply, message.guild.voice_client)

    await bot.process_commands(message)

# ------- Commands -------
@bot.tree.command(name="setpref", description="Set your preferred title")
@app_commands.describe(title="How Jarvis should address you")
async def setpref(interaction: discord.Interaction, title: str):
    await set_pref(interaction.user.id, title)
    await interaction.response.send_message(f"✅ Got it. I’ll call you **{title}**.")

# -------- Auto-update Roles --------
@tasks.loop(hours=3)
async def auto_update_roles():
    global last_role_channel
    if last_role_channel is None:
        return
    try:
        guild = last_role_channel.guild
        roles = guild.roles[::-1]
        formatted = "\n\n".join([f"**{r.name}** — `{r.id}`" for r in roles])
        chunks = [formatted[i:i+1900] for i in range(0, len(formatted), 1900)]
        await last_role_channel.send("🔄 Auto-refreshed Role IDs:")
        for c in chunks:
            await last_role_channel.send(c)
    except Exception as e:
        print(f"⚠️ Auto-update failed: {e}")

# ------- Run -------
bot.run(TOKEN)
