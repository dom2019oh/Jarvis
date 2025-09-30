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
from datetime import timedelta

# ==========================
# CONFIGURATION
# ==========================
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

DB_FILE = "memory.db"

# Channels
INVITE_LOG_CHANNEL_ID = 1422164192156454932  # Invite logs (unchanged)
MOD_LOG_CHANNEL_ID = 1422571435762909234     # New moderation logs

# Globals
last_role_channel = None
sleeping_channels = set()
global_kill_switch = False
invite_cache = {}

# ==========================
# DATABASE
# ==========================
def reset_bad_db():
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            conn.close()
        except sqlite3.DatabaseError:
            os.remove(DB_FILE)

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

# ==========================
# HELPERS
# ==========================
def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID

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

async def log_mod_action(guild, title, fields: dict):
    log_channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if not log_channel:
        return
    embed = discord.Embed(title=title, color=discord.Color.dark_red())
    for name, value in fields.items():
        embed.add_field(name=name, value=value, inline=False)
    embed.timestamp = discord.utils.utcnow()
    await log_channel.send(embed=embed)

# ==========================
# EVENTS
# ==========================
@bot.event
async def on_ready():
    reset_bad_db()
    await init_db()
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Stark Discoveries")
    )
    print(f"✅ Jarvis online as {bot.user}")
    try:
        guild = discord.Object(id=1324117813878718474)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ Synced {len(synced)} guild commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {invite.code: invite.uses for invite in invites}
        except:
            pass
    auto_update_roles.start()

# Invite tracking (old logs unchanged)
@bot.event
async def on_member_join(member: discord.Member):
    primary_guild = await get_primary_guild()
    if primary_guild and member.guild.id != primary_guild:
        return

    guild = member.guild
    invites_before = invite_cache.get(guild.id, {})
    try:
        invites_after = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses for invite in invites_after}
    except:
        return

    used_invite = None
    for invite in invites_after:
        if invites_before.get(invite.code, 0) < invite.uses:
            used_invite = invite
            break

    log_channel = guild.get_channel(INVITE_LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(title="Invite Used", color=discord.Color.blue())
    embed.add_field(name="User Joined", value=f"{member} (`{member.id}`)", inline=False)
    if used_invite:
        embed.add_field(name="Invite Code", value=used_invite.code, inline=True)
        embed.add_field(name="Invite Creator", value=f"{used_invite.inviter}", inline=True)
        embed.add_field(name="Uses", value=str(used_invite.uses), inline=True)
    embed.timestamp = discord.utils.utcnow()
    await log_channel.send(embed=embed)

# ==========================
# MODERATION ACTIONS
# ==========================
@bot.event
async def on_message(message: discord.Message):
    global global_kill_switch
    if message.author.bot:
        return

    content_lower = message.content.lower()

    # Owner-only moderation
    if is_owner(message.author) and content_lower.startswith("jarvis"):
        if "ban" in content_lower:
            if message.mentions:
                target = message.mentions[0]
                reason = "No reason provided"
                if "for " in content_lower:
                    reason = message.content.split("for", 1)[1].strip()
                try:
                    await message.guild.ban(target, reason=reason, delete_message_days=7)
                    await log_mod_action(message.guild, "BAN EXECUTED", {
                        "Target": f"{target} ({target.id})",
                        "Moderator": f"{message.author} ({message.author.id})",
                        "Reason": reason
                    })
                except Exception as e:
                    await message.channel.send(f"Failed to ban: {e}")
            return

        if "kick" in content_lower:
            if message.mentions:
                target = message.mentions[0]
                reason = "No reason provided"
                if "for " in content_lower:
                    reason = message.content.split("for", 1)[1].strip()
                try:
                    await message.guild.kick(target, reason=reason)
                    await log_mod_action(message.guild, "KICK EXECUTED", {
                        "Target": f"{target} ({target.id})",
                        "Moderator": f"{message.author} ({message.author.id})",
                        "Reason": reason
                    })
                except Exception as e:
                    await message.channel.send(f"Failed to kick: {e}")
            return

        if "warn" in content_lower:
            if message.mentions:
                target = message.mentions[0]
                reason = "No reason provided"
                if "for " in content_lower:
                    reason = message.content.split("for", 1)[1].strip()
                await log_mod_action(message.guild, "WARNING ISSUED", {
                    "Target": f"{target} ({target.id})",
                    "Moderator": f"{message.author} ({message.author.id})",
                    "Reason": reason
                })
            return

        if "mute" in content_lower:
            if message.mentions:
                target = message.mentions[0]
                duration = None
                reason = "No reason provided"
                if "for " in content_lower:
                    reason = message.content.split("for", 1)[1].strip()
                await log_mod_action(message.guild, "MUTE APPLIED", {
                    "Target": f"{target} ({target.id})",
                    "Moderator": f"{message.author} ({message.author.id})",
                    "Duration": duration or "Permanent",
                    "Reason": reason
                })
            return

        if "purge" in content_lower:
            parts = content_lower.split()
            amount = 10
            for word in parts:
                if word.isdigit():
                    amount = int(word)
            deleted = await message.channel.purge(limit=amount)
            await log_mod_action(message.guild, "PURGE EXECUTED", {
                "Moderator": f"{message.author} ({message.author.id})",
                "Deleted": str(len(deleted)),
                "Channel": f"{message.channel.name}"
            })
            return

        if "lockdown" in content_lower:
            overwrite = message.channel.overwrites_for(message.guild.default_role)
            overwrite.send_messages = False
            await message.channel.set_permissions(message.guild.default_role, overwrite=overwrite)
            await log_mod_action(message.guild, "LOCKDOWN", {
                "Moderator": f"{message.author} ({message.author.id})",
                "Channel": message.channel.name
            })
            return

        if "unlock" in content_lower:
            overwrite = message.channel.overwrites_for(message.guild.default_role)
            overwrite.send_messages = True
            await message.channel.set_permissions(message.guild.default_role, overwrite=overwrite)
            await log_mod_action(message.guild, "UNLOCK", {
                "Moderator": f"{message.author} ({message.author.id})",
                "Channel": message.channel.name
            })
            return

        if "move" in content_lower:
            if message.mentions:
                target = message.mentions[0]
                for vc in message.guild.voice_channels:
                    if vc.name.lower() in content_lower:
                        try:
                            await target.move_to(vc)
                            await log_mod_action(message.guild, "USER MOVED", {
                                "Target": f"{target} ({target.id})",
                                "Moderator": f"{message.author} ({message.author.id})",
                                "Destination": vc.name
                            })
                        except Exception as e:
                            await message.channel.send(f"Failed to move: {e}")
            return

    await bot.process_commands(message)

# ==========================
# TASKS
# ==========================
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
        await last_role_channel.send("Auto-refreshed Role IDs:")
        for c in chunks:
            await last_role_channel.send(c)
    except Exception as e:
        print(f"Auto-update failed: {e}")

# ==========================
# RUN
# ==========================
bot.run(TOKEN)
