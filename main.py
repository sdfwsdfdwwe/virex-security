"""
Discord Moderation Bot
-----------------------
Features:
- Anti-Spam: If a user sends more than 3 messages within 5 seconds,
  they receive a 1 minute timeout. All spam messages get deleted.
- Anti-Link: Any message containing a link gets deleted automatically.
- Logging: Every action (spam timeout, link deletion) is logged into
  a log channel. The log channel ID can be set with a simple command
  and is stored permanently in config.json (survives restarts on Railway).

Setup:
1. Set the environment variable DISCORD_TOKEN (your bot token) in Railway.
2. Invite the bot to your server with "Manage Messages", "Moderate Members"
   and "View Channel" / "Send Messages" permissions.
3. In any channel, run:  !setlog <channel_id>
   (only usable by members with "Manage Guild" / Administrator permission)
   This tells the bot where to send its log messages.
4. Done. The bot will now moderate spam and links automatically.
"""

import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

CONFIG_FILE = "config.json"

SPAM_MESSAGE_LIMIT = 3        # more than this many messages...
SPAM_TIME_WINDOW = 5          # ...within this many seconds...
SPAM_TIMEOUT_DURATION = 60    # ...triggers a timeout of this many seconds (1 minute)

# Simple regex to detect links (http/https URLs, www. links, discord invites, etc.)
LINK_REGEX = re.compile(
    r"(https?://\S+|www\.\S+|discord\.gg/\S+|discordapp\.com/invite/\S+)",
    re.IGNORECASE,
)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"log_channel_id": None}


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


config = load_config()

# ---------------------------------------------------------------------------
# BOT SETUP
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Tracks message timestamps per user for spam detection
# key: (guild_id, user_id) -> list of datetime objects
message_log = defaultdict(list)


def get_log_channel(guild: discord.Guild):
    """Return the configured log channel for this guild, if set and valid."""
    channel_id = config.get("log_channel_id")
    if channel_id is None:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel


async def send_log(guild: discord.Guild, embed: discord.Embed):
    channel = get_log_channel(guild)
    if channel is not None:
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print("Missing permissions to send messages in the log channel.")
    else:
        print("No log channel configured yet. Use !setlog <channel_id>.")


# ---------------------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot is online and ready to moderate.")


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots (including itself) and DMs
    if message.author.bot or message.guild is None:
        return

    # Let commands still work
    await bot.process_commands(message)

    # Skip moderation for administrators (adjust if you want to include them too)
    if isinstance(message.author, discord.Member) and message.author.guild_permissions.administrator:
        return

    guild = message.guild

    # -----------------------------------------------------------------
    # 1) ANTI-LINK CHECK
    # -----------------------------------------------------------------
    if LINK_REGEX.search(message.content):
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            print("Missing permissions to delete messages.")

        embed = discord.Embed(
            title="🔗 Link Deleted",
            description=f"A message containing a link was removed.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
        embed.add_field(name="Content", value=message.content[:1000] or "*(empty)*", inline=False)
        await send_log(guild, embed)

        try:
            await message.channel.send(
                f"{message.author.mention} links are not allowed here. Your message was deleted.",
                delete_after=5,
            )
        except discord.Forbidden:
            pass

        return  # don't also run spam check on a message we just deleted

    # -----------------------------------------------------------------
    # 2) ANTI-SPAM CHECK
    # -----------------------------------------------------------------
    key = (guild.id, message.author.id)
    now = datetime.now(timezone.utc)

    message_log[key].append(now)

    # Keep only timestamps within the spam time window
    cutoff = now - timedelta(seconds=SPAM_TIME_WINDOW)
    message_log[key] = [ts for ts in message_log[key] if ts > cutoff]

    if len(message_log[key]) > SPAM_MESSAGE_LIMIT:
        # Reset tracking for this user so we don't re-trigger immediately
        message_log[key] = []

        member = message.author

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        timeout_until = discord.utils.utcnow() + timedelta(seconds=SPAM_TIMEOUT_DURATION)
        try:
            await member.timeout(timeout_until, reason="Spamming messages")
            timeout_success = True
        except discord.Forbidden:
            timeout_success = False
        except discord.HTTPException:
            timeout_success = False

        embed = discord.Embed(
            title="⏱️ Spam Detected — User Timed Out",
            description=(
                f"{member.mention} sent more than {SPAM_MESSAGE_LIMIT} messages "
                f"within {SPAM_TIME_WINDOW} seconds."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
        embed.add_field(
            name="Action",
            value=(
                f"Timed out for {SPAM_TIMEOUT_DURATION} seconds (1 minute)."
                if timeout_success
                else "⚠️ Could not apply timeout (missing permissions)."
            ),
            inline=False,
        )
        await send_log(guild, embed)

        try:
            await message.channel.send(
                f"{member.mention} you have been timed out for 1 minute due to spamming.",
                delete_after=5,
            )
        except discord.Forbidden:
            pass


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------

@bot.command(name="setlog")
@commands.has_permissions(manage_guild=True)
async def setlog(ctx: commands.Context, channel_id: str):
    """Set the log channel by ID. Usage: !setlog <channel_id>"""
    try:
        channel_id_int = int(channel_id)
    except ValueError:
        await ctx.send("Please provide a valid channel ID (numbers only).")
        return

    channel = ctx.guild.get_channel(channel_id_int)
    if channel is None:
        await ctx.send("I can't find a channel with that ID in this server.")
        return

    config["log_channel_id"] = channel_id_int
    save_config(config)

    await ctx.send(f"✅ Log channel set to {channel.mention}.")
    embed = discord.Embed(
        title="✅ Log Channel Configured",
        description=f"This channel will now receive moderation logs.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    await channel.send(embed=embed)


@setlog.error
async def setlog_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!setlog <channel_id>`")


@bot.command(name="status")
async def status(ctx: commands.Context):
    """Show current bot configuration."""
    channel_id = config.get("log_channel_id")
    channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None

    embed = discord.Embed(title="🤖 Bot Status", color=discord.Color.blurple())
    embed.add_field(
        name="Log Channel",
        value=channel.mention if channel else "Not set (use `!setlog <channel_id>`)",
        inline=False,
    )
    embed.add_field(
        name="Spam Protection",
        value=f"More than {SPAM_MESSAGE_LIMIT} messages in {SPAM_TIME_WINDOW}s → {SPAM_TIMEOUT_DURATION}s timeout",
        inline=False,
    )
    embed.add_field(name="Link Filter", value="Enabled (all links are deleted)", inline=False)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it in Railway under Variables."
        )
    bot.run(token)
