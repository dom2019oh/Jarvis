import os
import sys
import time
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ------- Config -------
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 1176071547476262986
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI client
try:
    from openai import OpenAI
    oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    oai = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
start_time = time.time()

# Store last channel for auto-updates
last_role_channel = None

# ------- Helpers -------
async def ai_reply(system_prompt: str, user_prompt: str) -> str:
    if not oai:
        return "⚠️ OpenAI client not configured. Add OPENAI_API_KEY in Railway."
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
                return "".join([part.text for part in item.content if getattr(part, "type", "") == "output_text"]) or "✅ Done."
        return "✅ Done."
    except Exception as e:
        return f"❌ AI error: {e}"

def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID

# ------- Events -------
@bot.event
async def on_ready():
    print(f"✅ Jarvis is online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Globally synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")
    auto_update_roles.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if bot.user and bot.user in message.mentions:
        async with message.channel.typing():
            system = "You are Jarvis, a concise professional assistant for a GTA RP network. Be helpful, accurate, and brief."
            userq = message.clean_content.replace(f"@{bot.user.name}", "").strip()
            userq = userq if userq else "Say hello and explain how to use /ask."
            reply = await ai_reply(system, userq)
            reply = reply.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
            await message.reply(reply[:1900])
    await bot.process_commands(message)

# ------- Commands -------
@bot.tree.command(name="hello", description="Jarvis greets you")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Hello {interaction.user.mention}, I am Jarvis — your assistant 🤖."
    )

@bot.tree.command(name="ping", description="Check Jarvis' latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency is {latency}ms.")

@bot.tree.command(name="status", description="Check Jarvis' status")
async def status(interaction: discord.Interaction):
    uptime_seconds = round(time.time() - start_time)
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    embed = discord.Embed(title="📊 Jarvis Status", color=0x00ffcc)
    embed.add_field(name="Uptime", value=f"{h}h {m}m {s}s", inline=False)
    embed.add_field(name="Servers", value=f"{len(bot.guilds)}", inline=True)
    embed.add_field(name="Users", value=f"{len(bot.users)}", inline=True)
    embed.set_footer(text="Jarvis — Your Personal Assistant 🤖")
    await interaction.response.send_message(embed=embed)

# -------- Role IDs --------
@bot.tree.command(name="role_ids", description="Get all role names + IDs (Owner only)")
async def role_ids(interaction: discord.Interaction):
    global last_role_channel
    if not is_owner(interaction.user):
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return

    roles = interaction.guild.roles[::-1]
    formatted = "\n\n".join([f"**Role Name:** `𝗟𝗦𝗥𝗣 | {r.name}`\n**Role ID:** `{r.id}`" for r in roles])

    chunks = [formatted[i:i+1900] for i in range(0, len(formatted), 1900)]
    for c in chunks:
        await interaction.channel.send(c)

    await interaction.response.send_message("✅ Role IDs updated in this channel.", ephemeral=True)
    last_role_channel = interaction.channel  # save for auto-update

# -------- Auto-update every 3 hours --------
@tasks.loop(hours=3)
async def auto_update_roles():
    global last_role_channel
    if last_role_channel is None:
        return
    try:
        guild = last_role_channel.guild
        roles = guild.roles[::-1]
        formatted = "\n\n".join([f"**Role Name:** `𝗟𝗦𝗥𝗣 | {r.name}`\n**Role ID:** `{r.id}`" for r in roles])
        chunks = [formatted[i:i+1900] for i in range(0, len(formatted), 1900)]
        await last_role_channel.send("🔄 Auto-refreshed Role IDs:")
        for c in chunks:
            await last_role_channel.send(c)
    except Exception as e:
        print(f"⚠️ Auto-update failed: {e}")

# Owner-only shutdown
@bot.tree.command(name="shutdown", description="Shut down Jarvis (Owner only)")
async def shutdown(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return
    await interaction.response.send_message("🛑 Shutting down Jarvis...", ephemeral=True)
    await bot.close()
    sys.exit(0)

# Ask AI
@bot.tree.command(name="ask", description="Ask Jarvis (AI) a question")
@app_commands.describe(prompt="Your question or instruction")
async def ask(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(ephemeral=False, thinking=True)
    system = "You are Jarvis, a concise professional assistant for a GTA RP network. Give direct, useful answers."
    answer = await ai_reply(system, prompt)
    answer = answer.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
    if len(answer) > 1900:
        answer = answer[:1900] + " …"
    await interaction.followup.send(answer)

# ------- Run -------
bot.run(TOKEN)
