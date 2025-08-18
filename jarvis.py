import os
import sys
import time
import discord
from discord.ext import commands
from discord import app_commands

# ------- Config -------
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 1176071547476262986
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # change to "gpt-5" if enabled
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI client (Responses API)
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

# ------- Helpers -------
ASYNC_TIMEOUT = 60

async def ai_reply(system_prompt: str, user_prompt: str) -> str:
    """
    Calls OpenAI Responses API and returns plain text.
    """
    if not oai:
        return "⚠️ OpenAI client not configured. Add OPENAI_API_KEY in Railway."
    try:
        # Responses API (recommended)
        # Docs: platform.openai.com/docs/api-reference/responses
        resp = oai.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # You can also set max_output_tokens, temperature, etc.
        )
        # Extract text output safely
        for item in resp.output:
            if item.type == "message":
                # Concatenate parts in case multiple
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
        synced = await bot.tree.sync()  # global sync
        print(f"✅ Globally synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

# Optional: reply when mentioned
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # If Jarvis is mentioned, answer with AI
    if bot.user and bot.user in message.mentions:
        async with message.channel.typing():
            system = "You are Jarvis, a concise professional assistant for a GTA RP network. Be helpful, accurate, and brief."
            userq = message.clean_content.replace(f"@{bot.user.name}", "").strip()
            userq = userq if userq else "Say hello and explain how to use /ask."
            reply = await ai_reply(system, userq)
            # Avoid @everyone etc.
            reply = reply.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
            await message.reply(reply[:1900])  # keep within Discord limits
    # Keep slash commands working
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

# Owner-only: DM role IDs (with fallback file)
@bot.tree.command(name="role_ids", description="Get all role names + IDs in this server (Owner only)")
async def role_ids(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return
    roles = interaction.guild.roles[::-1]
    lines = [f"Role Name: {r.name} | Role ID: {r.id}" for r in roles]
    out = "\n".join(lines)
    await interaction.response.send_message("✅ Sent you a DM with all role IDs.", ephemeral=True)
    try:
        # chunked embeds to avoid size limits
        chunks = [out[i:i+1900] for i in range(0, len(out), 1900)]
        for c in chunks:
            embed = discord.Embed(title=f"📜 Role IDs for {interaction.guild.name}", color=0x3498db)
            embed.description = f"```\n{c}\n```"
            await interaction.user.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send(
            "⚠️ Could not DM you. Here's the role list as a file:",
            file=discord.File(fp=bytes(out, "utf-8"), filename="role_ids.txt"),
            ephemeral=True
        )
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"⚠️ Fail-safe sync error: {e}")

# Owner-only: graceful shutdown
@bot.tree.command(name="shutdown", description="Shut down Jarvis (Owner only)")
async def shutdown(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return
    await interaction.response.send_message("🛑 Shutting down Jarvis...", ephemeral=True)
    await bot.close()
    sys.exit(0)

# Owner-only: test DM ability
@bot.tree.command(name="test_dm", description="Test if Jarvis can DM you (Owner only)")
async def test_dm(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return
    try:
        await interaction.user.send("✅ Test DM from Jarvis.")
        await interaction.response.send_message("📩 Sent you a test DM.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ Could not DM you. Enable DMs from server members.", ephemeral=True)

# Ask the AI via slash command
@bot.tree.command(name="ask", description="Ask Jarvis (AI) a question")
@app_commands.describe(prompt="Your question or instruction")
async def ask(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(ephemeral=False, thinking=True)
    system = "You are Jarvis, a concise professional assistant for a GTA RP network. Give direct, useful answers."
    answer = await ai_reply(system, prompt)
    # Safety: prevent mass mentions
    answer = answer.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
    # Truncate if needed
    if len(answer) > 1900:
        answer = answer[:1900] + " …"
    await interaction.followup.send(answer)

# ------- Run -------
bot.run(TOKEN)

