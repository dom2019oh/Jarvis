import os
import discord
import time
import sys
from discord.ext import commands
from discord import app_commands

# -----------------
# CONFIG
# -----------------
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 1176071547476262986  # Your Discord User ID

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Track uptime
start_time = time.time()

# -----------------
# EVENTS
# -----------------
@bot.event
async def on_ready():
    print(f"✅ Jarvis is online as {bot.user}")
    try:
        # Force global sync (commands available in all servers)
        synced = await bot.tree.sync()
        print(f"✅ Globally synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

# -----------------
# COMMANDS
# -----------------

# /hello command
@bot.tree.command(name="hello", description="Jarvis greets you")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Hello {interaction.user.mention}, I am Jarvis — your assistant 🤖."
    )

# /ping command
@bot.tree.command(name="ping", description="Check Jarvis' latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency is {latency}ms.")

# /status command
@bot.tree.command(name="status", description="Check Jarvis' status")
async def status(interaction: discord.Interaction):
    uptime_seconds = round(time.time() - start_time)
    uptime_minutes, uptime_seconds = divmod(uptime_seconds, 60)
    uptime_hours, uptime_minutes = divmod(uptime_minutes, 60)

    embed = discord.Embed(
        title="📊 Jarvis Status",
        color=0x00ffcc
    )
    embed.add_field(name="Uptime", value=f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s", inline=False)
    embed.add_field(name="Servers", value=f"{len(bot.guilds)}", inline=True)
    embed.add_field(name="Users", value=f"{len(bot.users)}", inline=True)
    embed.set_footer(text="Jarvis — Your Personal Assistant 🤖")

    await interaction.response.send_message(embed=embed)

# /role_ids command
@bot.tree.command(name="role_ids", description="Get all role names + IDs in this server (Owner only)")
async def role_ids(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return

    roles = interaction.guild.roles[::-1]
    embed = discord.Embed(title=f"📜 Role IDs for {interaction.guild.name}", color=0x3498db)

    description_lines = []
    for role in roles:
        description_lines.append(
            f"**Role Name:** `{role.name}`\n**Role ID:** `{role.id}`"
        )

    embed.description = "\n\n".join(description_lines)

    await interaction.response.send_message("✅ Sent you a DM with all role IDs.", ephemeral=True)

    try:
        await interaction.user.send(embed=embed)
    except discord.Forbidden:
        await interaction.followup.send("⚠️ Could not DM you. Please enable DMs from server members.", ephemeral=True)

    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"⚠️ Fail-safe sync error: {e}")

# /shutdown command
@bot.tree.command(name="shutdown", description="Shut down Jarvis (Owner only)")
async def shutdown(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Sorry, this command is restricted.", ephemeral=True)
        return

    await interaction.response.send_message("🛑 Shutting down Jarvis...", ephemeral=True)
    await bot.close()
    sys.exit(0)

# -----------------
# RUN
# -----------------
bot.run(TOKEN)
