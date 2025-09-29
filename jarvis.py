import os
import sys
import time
import tempfile
import sqlite3
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

# Invite logger
INVITE_LOG_CHANNEL_ID = 1422164192156454932
invite_cache = {}

# ------- Database Reset Protection -------
def reset_bad_db():
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            conn.close()
        except sqlite3.DatabaseError:
            os.remove(DB_FILE)
            print("⚠️ Corrupted DB detected. Deleted and will rebuild.")

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()
    print("✅ Database initialized and ready.")

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
            return rows[::-1]

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

# ------- Settings Helpers (Primary Guild) -------
async def set_primary_guild(guild_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("primary_guild", str(guild_id))
        )
        await db.commit()

async def get_primary_guild():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", ("primary_guild",)) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else None

# ------- AI Normal Reply -------
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

# ------- TTS -------
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
    reset_bad_db()
    await init_db()
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Stark Discoveries")
    )
    print(f"✅ Jarvis online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    # Initialize invite cache
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {invite.code: invite.uses for invite in invites}
        except Exception as e:
            print(f"⚠️ Could not fetch invites for {guild.name}: {e}")

    auto_update_roles.start()

@bot.event
async def on_member_join(member: discord.Member):
    primary_guild = await get_primary_guild()
    if primary_guild and member.guild.id != primary_guild:
        return  # ignore other guilds

    guild = member.guild
    invites_before = invite_cache.get(guild.id, {})

    try:
        invites_after = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses for invite in invites_after}
    except Exception as e:
        print(f"⚠️ Could not fetch invites for {guild.name}: {e}")
        return

    used_invite = None
    for invite in invites_after:
        if invites_before.get(invite.code, 0) < invite.uses:
            used_invite = invite
            break

    log_channel = guild.get_channel(INVITE_LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(
        title="🔗 Invite Used",
        color=discord.Color.blue()
    )
    embed.add_field(name="👤 User Joined", value=f"{member} (`{member.id}`)", inline=False)

    if used_invite:
        embed.add_field(name="📨 Invite Code", value=used_invite.code, inline=True)
        embed.add_field(name="👤 Invite Creator", value=f"{used_invite.inviter} (`{used_invite.inviter.id}`)", inline=True)
        embed.add_field(name="🔢 Uses", value=str(used_invite.uses), inline=True)
    else:
        embed.add_field(name="❓ Invite", value="Could not detect invite (vanity/expired?)", inline=False)

    # Account Age Check
    account_age_days = (discord.utils.utcnow() - member.created_at).days
    risk_note = "✅ Looks safe."
    if account_age_days < 1:
        risk_note = "🚨 High Risk: Account less than 24h old!"
    elif account_age_days < 7:
        risk_note = "⚠️ Possible Alt: Account less than 7 days old."

    embed.add_field(name="📅 Account Age", value=f"{account_age_days} days old\n{risk_note}", inline=False)
    embed.add_field(name="🕒 Joined At", value=discord.utils.format_dt(member.joined_at, style='F'), inline=False)

    embed.timestamp = discord.utils.utcnow()
    await log_channel.send(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    global global_kill_switch
    if message.author.bot:
        return

    content_lower = message.content.lower()

    # Owner trigger: Mark primary guild
    if is_owner(message.author) and "jarvis mark this as your primary guild" in content_lower:
        await set_primary_guild(message.guild.id)
        await message.channel.send(f"✅ Marked **{message.guild.name}** as my primary guild. I’ll ignore all others, sir.")
        return

    # Kill switch reactivation
    if global_kill_switch and ("tony stark" in content_lower or "pepper" in content_lower):
        global_kill_switch = False
        await message.channel.send("☀️ Override disengaged. Back online.")
        return
    if global_kill_switch:
        return

    # Confidential channels bypass
    if any(x in message.channel.name.lower() for x in ["staff", "admin", "management"]):
        return

    # Passive Listening
    trigger_detected = False
    if "jarvis" in content_lower:
        trigger_detected = True
    if bot.user in message.mentions:
        trigger_detected = True
    if is_owner(message.author) and message.content.lower().startswith("jarvis"):
        trigger_detected = True

    if trigger_detected:
        # Collect last 6 messages for context
        history = []
        async for msg in message.channel.history(limit=6, oldest_first=False):
            if not msg.author.bot:
                history.append(f"{msg.author.display_name}: {msg.content}")
        history_text = "\n".join(history[::-1])

        # Save memory
        await save_memory(message.author.id, message.channel.id, f"{message.author.display_name}: {message.content}")

        pref = await get_pref(message.author.id)

        system = (
            "You are J.A.R.V.I.S., Tony Stark's AI assistant. "
            "Be professional, witty, concise. Use memory context and conversation history. "
            "Never expose confidential info."
        )
        reply = await ai_reply(system, f"Context:\n{history_text}\n\nUser: {message.content}")

        if is_owner(message.author):
            reply = f"Yes, sir. {reply}"
        elif pref:
            reply = f"Yes, {pref}. {reply}"
        else:
            reply = f"{reply}\n\n💡 I am Tony Stark's personal assistant."

        reply = reply.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
        await message.reply(reply[:1900])

        if message.guild.voice_client:
            await tts_speak(reply, message.guild.voice_client)

    await bot.process_commands(message)

# ------- Slash Commands -------
@bot.tree.command(name="setpref", description="Set your preferred title")
@app_commands.describe(title="How Jarvis should address you")
async def setpref(interaction: discord.Interaction, title: str):
    await set_pref(interaction.user.id, title)
    await interaction.response.send_message(f"✅ Got it. I’ll call you **{title}**.")

@bot.tree.command(name="database-check", description="Run a background/alt check on a user")
@app_commands.describe(user="The user to check")
async def database_check(interaction: discord.Interaction, user: discord.Member):
    primary_guild = await get_primary_guild()
    if primary_guild and interaction.guild.id != primary_guild:
        await interaction.response.send_message("❌ This command only works in my primary guild.", ephemeral=True)
        return

    account_age_days = (discord.utils.utcnow() - user.created_at).days
    risk_note = "✅ Looks safe."
    if account_age_days < 1:
        risk_note = "🚨 High Risk: Account less than 24h old!"
    elif account_age_days < 7:
        risk_note = "⚠️ Possible Alt: Account less than 7 days old."

    embed = discord.Embed(title="📊 Database Check", color=discord.Color.orange())
    embed.add_field(name="👤 User", value=f"{user} (`{user.id}`)", inline=False)
    embed.add_field(name="📅 Account Age", value=f"{account_age_days} days old\n{risk_note}", inline=False)
    embed.add_field(name="🕒 Joined At", value=discord.utils.format_dt(user.joined_at, style='F'), inline=False)

    await interaction.response.send_message(embed=embed)

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
