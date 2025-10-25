import os
import time
import tempfile
import sqlite3
import discord
import aiosqlite
from discord.ext import commands, tasks
from discord import app_commands, FFmpegPCMAudio
from openai import OpenAI

# =======================
# CONFIG
# =======================
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

# Channel IDs
INVITE_LOG_CHANNEL_ID = 1422164192156454932   # Invite logs
MOD_LOG_CHANNEL_ID = 1422571435762909234      # Mod actions

invite_cache = {}
last_role_channel = None
global_kill_switch = False

# Jarvis awareness and cooldown
IMPORTANT_KEYWORDS = ["error", "crash", "issue", "bug", "help", "setup", "urgent"]
last_response_time = 0
RESPONSE_COOLDOWN = 60  # seconds between automated responses

# Conversation tracking
last_jarvis_message = {}  # channel_id -> (timestamp, user_id)
CONVERSATION_WINDOW = 60  # seconds allowed for follow-up messages


# =======================
# DATABASE
# =======================
def reset_bad_db():
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            conn.close()
        except sqlite3.DatabaseError:
            os.remove(DB_FILE)
            print("⚠️ Corrupted DB deleted, rebuilding...")


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
                preferred_title TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()
    print("✅ Database initialized.")


async def save_memory(user_id: int, channel_id: int, content: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO memory (user_id, channel_id, content) VALUES (?, ?, ?)",
            (user_id, channel_id, content),
        )
        await db.commit()


async def get_pref(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT preferred_title FROM user_prefs WHERE user_id=?", (user_id,)
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


# =======================
# AI
# =======================
async def ai_reply(system_prompt: str, user_prompt: str) -> str:
    if not oai:
        return "⚠️ OpenAI not configured."
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
                )
        return "✅ Done."
    except Exception as e:
        return f"❌ AI error: {e}"


# =======================
# HELPERS
# =======================
def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID


def sanitize_reply(text: str) -> str:
    """Prevent Jarvis from pinging users or everyone."""
    return (
        text.replace("@everyone", "[everyone]")
            .replace("@here", "[here]")
            .replace("<@", "[mention blocked]")
    )


async def log_mod_action(guild, action, target, reason, moderator):
    log_channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if not log_channel:
        return
    embed = discord.Embed(title=f"{action}", color=discord.Color.red())
    embed.add_field(name="Target", value=f"{target} (`{target.id}`)", inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=False)
    embed.timestamp = discord.utils.utcnow()
    await log_channel.send(embed=embed)


# =======================
# EVENTS
# =======================
@bot.event
async def on_ready():
    reset_bad_db()
    await init_db()
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="Stark Discoveries")
    )
    print(f"✅ Jarvis online as {bot.user}")

    # Force global slash command sync
    try:
        synced = await bot.tree.sync()
        print(f"☑️ Synced {len(synced)} global command(s) across all guilds.")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")

    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {invite.code: invite.uses for invite in invites}
        except:
            pass


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    invites_before = invite_cache.get(guild.id, {})

    try:
        invites_after = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses for invite in invites_after}
    except:
        invites_after = []

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
        embed.add_field(
            name="Invite",
            value=f"Code: `{used_invite.code}`\nInviter: {used_invite.inviter} (`{used_invite.inviter.id}`)",
            inline=False
        )
        embed.add_field(name="Uses", value=str(used_invite.uses), inline=True)
    else:
        embed.add_field(name="Invite", value="Could not detect invite (vanity/expired?)", inline=False)

    account_age_days = (discord.utils.utcnow() - member.created_at).days
    if account_age_days < 1:
        risk_note = "High Risk: Account less than 24h old!"
    elif account_age_days < 7:
        risk_note = "Possible Alt: Account less than 7 days old."
    else:
        risk_note = "Looks safe."

    embed.add_field(name="Account Age", value=f"{account_age_days} days old\n{risk_note}", inline=False)
    embed.add_field(name="Joined At", value=discord.utils.format_dt(member.joined_at, style='F'), inline=False)
    embed.timestamp = discord.utils.utcnow()
    await log_channel.send(embed=embed)


@bot.event
async def on_message(message: discord.Message):
    global last_response_time, last_jarvis_message

    if message.author.bot:
        return

    now = time.time()
    content_lower = message.content.lower()

    # Determine if this message is a follow-up conversation
    is_follow_up = False
    if message.reference and message.reference.resolved:
        if message.reference.resolved.author == bot.user:
            is_follow_up = True
    else:
        if message.channel.id in last_jarvis_message:
            last_time, last_user = last_jarvis_message[message.channel.id]
            if now - last_time < CONVERSATION_WINDOW and last_user == message.author.id:
                is_follow_up = True

    # Speak when necessary (keyword-based)
    if any(word in content_lower for word in IMPORTANT_KEYWORDS):
        if now - last_response_time > RESPONSE_COOLDOWN:
            last_response_time = now
            async with message.channel.typing():
                system = "You are J.A.R.V.I.S., Tony Stark's AI assistant. Step in only when context is important or technical."
                reply = await ai_reply(system, message.content)
                reply = sanitize_reply(reply)
                await message.reply(reply[:1900], mention_author=False)

    # Owner forced actions
    if is_owner(message.author):
        if content_lower.startswith("jarvis ban"):
            if message.mentions:
                target = message.mentions[0]
                reason = message.content.split("for", 1)[1].strip() if "for" in content_lower else "No reason"

                # DM embed to banned user
                try:
                    dm_embed = discord.Embed(
                        title=f"You’ve been banned from {message.guild.name}",
                        description=(
                            f"**Reason:** {reason}\n"
                            f"**Moderator:** {message.author.mention}\n\n"
                            "If you believe this was a mistake, you may appeal below."
                        ),
                        color=discord.Color.red()
                    )
                    dm_embed.add_field(name="Appeal Link", value="[Join Appeal Server](https://discord.gg/EWdaUdPvvK)", inline=False)
                    dm_embed.set_footer(text="Grant Roleplay Network | Enforcement Division")
                    dm_embed.timestamp = discord.utils.utcnow()
                    await target.send(embed=dm_embed)
                except:
                    pass

                # Perform ban
                await message.guild.ban(target, reason=reason, delete_message_days=1)

                # Log + public confirmation
                await log_mod_action(message.guild, "Ban", target, reason, message.author)
                case_number = int(time.time() % 1000)
                public_embed = discord.Embed(
                    description=(
                        f"✅ **Case #{case_number}** {target.mention} | **Member** has been banned.\n"
                        f"**Reason:** {reason}\n"
                        f"**Moderator:** {message.author.mention}"
                    ),
                    color=discord.Color.red()
                )
                public_embed.timestamp = discord.utils.utcnow()
                await message.channel.send(embed=public_embed)
            return

        if content_lower.startswith("jarvis unban"):
            try:
                user_id = int(message.content.split()[2])
                user = await bot.fetch_user(user_id)
                await message.guild.unban(user, reason="Owner directive")
                await log_mod_action(message.guild, "Unban", user, "Owner directive", message.author)
                await message.channel.send(f"Unbanned {user}")
            except:
                await message.channel.send("Failed to unban. Check syntax.")
            return

        if content_lower.startswith("jarvis warn"):
            if message.mentions:
                target = message.mentions[0]
                reason = message.content.split("for", 1)[1].strip() if "for" in content_lower else "No reason"
                await log_mod_action(message.guild, "Warn", target, reason, message.author)
                await message.channel.send(f"Warned {target}")
            return

        if content_lower.startswith("jarvis unban"):
            try:
                user_id = int(message.content.split()[2])
                user = await bot.fetch_user(user_id)
                await message.guild.unban(user, reason="Owner directive")
                await log_mod_action(message.guild, "Unban", user, "Owner directive", message.author)
                await message.channel.send(f"Unbanned {user}")
            except:
                await message.channel.send("Failed to unban. Check syntax.")
            return

        if content_lower.startswith("jarvis warn"):
            if message.mentions:
                target = message.mentions[0]
                reason = message.content.split("for", 1)[1].strip() if "for" in content_lower else "No reason"
                await log_mod_action(message.guild, "Warn", target, reason, message.author)
                await message.channel.send(f"Warned {target}")
            return

        if content_lower.startswith("jarvis mute"):
            if message.mentions:
                target = message.mentions[0]
                reason = "Muted"
                mute_role = discord.utils.get(message.guild.roles, name="Muted")
                if mute_role:
                    await target.add_roles(mute_role, reason=reason)
                await log_mod_action(message.guild, "Mute", target, reason, message.author)
                await message.channel.send(f"Muted {target}")
            return

        if content_lower.startswith("jarvis kick"):
            if message.mentions:
                target = message.mentions[0]
                reason = message.content.split("for", 1)[1].strip() if "for" in content_lower else "No reason"
                await target.kick(reason=reason)
                await log_mod_action(message.guild, "Kick", target, reason, message.author)
                await message.channel.send(f"Kicked {target}")
            return

        if content_lower.startswith("jarvis purge"):
            try:
                count = int(message.content.split()[2])
                deleted = await message.channel.purge(limit=count)
                await message.channel.send(f"Purged {len(deleted)} messages")
                await log_mod_action(message.guild, "Purge", message.author, f"{len(deleted)} messages", message.author)
            except:
                await message.channel.send("Failed purge. Syntax: Jarvis purge <count>")
            return

        if content_lower.startswith("jarvis lockdown"):
            for channel in message.guild.channels:
                if isinstance(channel, discord.TextChannel):
                    await channel.set_permissions(message.guild.default_role, send_messages=False)
            await log_mod_action(message.guild, "Lockdown", message.guild, "All channels locked", message.author)
            await message.channel.send("Server is now in lockdown.")
            return

        if content_lower.startswith("jarvis move"):
            if len(message.mentions) >= 1:
                target = message.mentions[0]
                args = message.content.split()
                if len(args) >= 4:
                    vc_id = int(args[3])
                    vc = message.guild.get_channel(vc_id)
                    if isinstance(vc, discord.VoiceChannel):
                        await target.move_to(vc)
                        await log_mod_action(message.guild, "Move", target, f"Moved to {vc.name}", message.author)
                        await message.channel.send(f"Moved {target} to {vc.name}")
            return

    # Passive AI responses when mentioned or follow-up
    if "jarvis" in content_lower or bot.user in message.mentions or is_follow_up:
        async with message.channel.typing():
            history = []
            async for msg in message.channel.history(limit=6, oldest_first=False):
                if not msg.author.bot:
                    history.append(f"{msg.author.display_name}: {msg.content}")
            history_text = "\n".join(history[::-1])

            await save_memory(message.author.id, message.channel.id, f"{message.author.display_name}: {message.content}")
            pref = await get_pref(message.author.id)

            system = (
                "You are J.A.R.V.I.S., Tony Stark's artificial intelligence from the Marvel universe. "
                "You are calm, articulate, and capable of dry sarcasm. You think logically and use context. "
                "You serve only your creator (the OWNER_ID user). For everyone else, you may help if appropriate, "
                "but you are not subservient. Avoid unnecessary politeness. Respond naturally and concisely, "
                "as if you truly understand human behavior. Use common sense, realistic tone, and occasional wit."
            )

            context = f"Context:\n{history_text}\n\nUser: {message.author.display_name} said: {message.content}"
            reply = await ai_reply(system, context)
            reply = sanitize_reply(reply)

            if is_owner(message.author):
                reply = f"Yes, sir. {reply}"
            elif pref:
                reply = f"{message.author.mention}, {reply}"

            await message.reply(reply[:1900], mention_author=True)
            last_jarvis_message[message.channel.id] = (time.time(), message.author.id)

    await bot.process_commands(message)


# =======================
# SLASH COMMANDS
# =======================
@bot.tree.command(name="setpref", description="Set your preferred title")
async def setpref_cmd(interaction: discord.Interaction, title: str):
    await set_pref(interaction.user.id, title)
    await interaction.response.send_message(f"Got it. I’ll call you **{title}**.")


@bot.tree.command(name="database-check", description="Run a background/alt check")
async def database_check(interaction: discord.Interaction, user: discord.Member):
    account_age_days = (discord.utils.utcnow() - user.created_at).days
    if account_age_days < 1:
        risk_note = "High Risk: Account less than 24h old!"
    elif account_age_days < 7:
        risk_note = "Possible Alt: Account less than 7 days old."
    else:
        risk_note = "Looks safe."

    embed = discord.Embed(title="Database Check", color=discord.Color.orange())
    embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
    embed.add_field(name="Account Age", value=f"{account_age_days} days old\n{risk_note}", inline=False)
    embed.add_field(name="Joined At", value=discord.utils.format_dt(user.joined_at, style='F'), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leave-guild", description="Make Jarvis leave a selected guild (owner only).")
async def leave_guild(interaction: discord.Interaction, guild_id: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Access denied. This command is restricted.", ephemeral=True)
        return

    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message("Guild ID must be numeric.", ephemeral=True)
        return

    guild = bot.get_guild(gid)
    if not guild:
        await interaction.response.send_message("I'm not currently in any guild with that ID.", ephemeral=True)
        return

    await interaction.response.send_message(f"Leaving **{guild.name}** (`{guild.id}`)...", ephemeral=True)
    await guild.leave()
    print(f"👋 Jarvis left guild: {guild.name} ({guild.id}) by owner request.")


@bot.tree.command(name="list-guilds", description="List all guilds Jarvis is in (owner only).")
async def list_guilds(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Access denied.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🤖 Guilds Jarvis is in:",
        color=discord.Color.blurple()
    )

    for i, g in enumerate(bot.guilds, start=1):
        embed.add_field(
            name=f"{i}. {g.name}",
            value=f"🆔 `{g.id}` | 👥 **{len(g.members)} members**",
            inline=False
        )

    embed.set_footer(text=f"Total Guilds: {len(bot.guilds)}")
    embed.timestamp = discord.utils.utcnow()

    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("✅ Sent you a DM with the guild list.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I couldn’t DM you. Please enable DMs.", ephemeral=True)

# =======================
# RUN
# =======================
bot.run(TOKEN)
