"""
Discord Security Bot - Full Feature Security System
Railway-ready | All logs in English | Single file deployment
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import re
import time
import datetime
import logging
from collections import defaultdict, deque
from typing import Optional

# Spam thresholds
SPAM_MSG_LIMIT = 5          # messages
SPAM_TIME_WINDOW = 5        # seconds
MENTION_LIMIT = 5           # max mentions per message
INVITE_PATTERN = re.compile(r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# Anti-raid thresholds
RAID_JOIN_LIMIT = 10        # joins
RAID_TIME_WINDOW = 10       # seconds
NEW_ACCOUNT_DAYS = 7        # account age in days to flag

# Log channel names (bot creates these automatically)
LOG_CHANNELS = {
    "message":   "📝・message-logs",
    "mod":       "🔨・mod-logs",
    "join_leave":"🚪・join-leave-logs",
    "role":      "👑・role-logs",
    "channel":   "📁・channel-logs",
    "voice":     "🎙️・voice-logs",
    "raid":      "🚨・raid-logs",
    "automod":   "🤖・automod-logs",
    "server":    "⚙️・server-logs",
    "reaction":  "💬・reaction-logs",
}

# Colors
COLOR_RED    = 0xFF4444
COLOR_ORANGE = 0xFF8C00
COLOR_GREEN  = 0x44FF88
COLOR_BLUE   = 0x4488FF
COLOR_PURPLE = 0xAA44FF
COLOR_YELLOW = 0xFFDD44
COLOR_GRAY   = 0x99AABB

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("SecurityBot")

# ─────────────────────────────────────────────
#  BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─────────────────────────────────────────────
#  IN-MEMORY STATE
# ─────────────────────────────────────────────
message_history: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=20)))
raid_tracker: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))
raid_mode: dict[int, bool] = defaultdict(bool)
warnings: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
muted_users: dict[int, dict[int, float]] = defaultdict(dict)
link_whitelist_channels: dict[int, set] = defaultdict(set)
link_whitelist_roles: dict[int, set]    = defaultdict(set)
log_channel_cache: dict[int, dict[str, int]] = defaultdict(dict)
lockdown_state: dict[int, bool] = defaultdict(bool)

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def ts(dt: datetime.datetime = None) -> str:
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def duration_parse(text: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not match:
        return None
    val, unit = int(match.group(1)), match.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


async def get_log_channel(guild: discord.Guild, key: str) -> Optional[discord.TextChannel]:
    cached = log_channel_cache[guild.id].get(key)
    if cached:
        ch = guild.get_channel(cached)
        if ch:
            return ch

    name = LOG_CHANNELS[key]
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing:
        log_channel_cache[guild.id][key] = existing.id
        return existing

    category = discord.utils.get(guild.categories, name="🔒 Security Logs")
    if category is None:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            category = await guild.create_category("🔒 Security Logs", overwrites=overwrites)
        except Exception as e:
            log.warning(f"Could not create log category: {e}")
            category = None

    try:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        ch = await guild.create_text_channel(name, category=category, overwrites=overwrites)
        log_channel_cache[guild.id][key] = ch.id
        log.info(f"[{guild.name}] Created log channel: {name}")
        return ch
    except Exception as e:
        log.warning(f"[{guild.name}] Failed to create log channel '{name}': {e}")
        return None


async def send_log(guild: discord.Guild, key: str, embed: discord.Embed):
    ch = await get_log_channel(guild, key)
    if ch:
        try:
            await ch.send(embed=embed)
        except Exception as e:
            log.warning(f"send_log failed [{key}]: {e}")


def make_embed(title: str, description: str = "", color: int = COLOR_BLUE, **fields) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color,
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
    for name, value in fields.items():
        embed.add_field(name=name.replace("_", " ").title(), value=str(value), inline=True)
    embed.set_footer(text="Security Bot")
    return embed


async def get_or_create_mute_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if role is None:
        role = await guild.create_role(name="Muted", color=discord.Color.dark_gray(),
                                       reason="Security Bot: Mute role")
        for channel in guild.channels:
            try:
                await channel.set_permissions(role, send_messages=False,
                                              add_reactions=False, speak=False)
            except Exception:
                pass
    return role


# ─────────────────────────────────────────────
#  EVENTS – READY
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="for threats 🔒"),
        status=discord.Status.dnd
    )
    if not unmute_task.is_running():
        unmute_task.start()
        
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log.warning(f"Slash sync failed: {e}")

    for guild in bot.guilds:
        log.info(f"Setting up log channels for guild: {guild.name}")
        for key in LOG_CHANNELS:
            await get_log_channel(guild, key)

    embed = make_embed("🤖 Security Bot Online", "All systems operational. Monitoring started.",
                       color=COLOR_GREEN)
    for guild in bot.guilds:
        await send_log(guild, "mod", embed)


# ─────────────────────────────────────────────
#  EVENTS – MEMBER JOIN / LEAVE
# ─────────────────────────────────────────────

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    now = time.time()
    account_age = (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days

    raid_tracker[guild.id].append(now)
    recent_joins = [t for t in raid_tracker[guild.id] if now - t <= RAID_TIME_WINDOW]

    if len(recent_joins) >= RAID_JOIN_LIMIT and not raid_mode[guild.id]:
        raid_mode[guild.id] = True
        log.warning(f"[{guild.name}] RAID DETECTED — {len(recent_joins)} joins in {RAID_TIME_WINDOW}s")
        embed = make_embed(
            "🚨 RAID DETECTED",
            f"**{len(recent_joins)} members** joined within {RAID_TIME_WINDOW} seconds!\nRaid mode enabled.",
            color=COLOR_RED,
            trigger_member=str(member),
            recent_joins=len(recent_joins),
            time_window=f"{RAID_TIME_WINDOW}s"
        )
        await send_log(guild, "raid", embed)

        for m in guild.members:
            if m.joined_at and (now - m.joined_at.timestamp() <= RAID_TIME_WINDOW):
                try:
                    await m.kick(reason="[SecurityBot] Anti-Raid: Mass join detected")
                except Exception:
                    pass

        try:
            for invite in await guild.invites():
                await invite.delete(reason="[SecurityBot] Anti-Raid: Invites paused")
        except Exception:
            pass
        return

    flags = []
    if account_age < NEW_ACCOUNT_DAYS:
        flags.append(f"⚠️ New account ({account_age} days old)")
    if raid_mode[guild.id]:
        flags.append("⚠️ Joined during raid mode")

    embed = make_embed(
        "📥 Member Joined",
        f"{member.mention} joined the server.",
        color=COLOR_GREEN if not flags else COLOR_ORANGE,
        user=f"{member} ({member.id})",
        account_created=ts(member.created_at),
        account_age=f"{account_age} days",
        flags="\n".join(flags) if flags else "None"
    )
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    await send_log(guild, "join_leave", embed)

    if raid_mode[guild.id] and account_age < NEW_ACCOUNT_DAYS:
        try:
            await member.kick(reason="[SecurityBot] Raid mode: New account blocked")
            automod_embed = make_embed(
                "🚨 Auto-Kick: Raid Mode",
                f"Kicked {member} — new account during raid.",
                color=COLOR_RED,
                user=f"{member} ({member.id})",
                account_age=f"{account_age} days"
            )
            await send_log(guild, "automod", automod_embed)
        except Exception:
            pass


@bot.event
async def on_member_remove(member: discord.Member):
    embed = make_embed(
        "📤 Member Left",
        f"**{member}** left (or was removed from) the server.",
        color=COLOR_GRAY,
        user_id=member.id,
        roles=", ".join(r.name for r in member.roles[1:]) or "None",
        joined_at=ts(member.joined_at) if member.joined_at else "Unknown"
    )
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    await send_log(member.guild, "join_leave", embed)


# ─────────────────────────────────────────────
#  EVENTS – MESSAGES
# ─────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild  = message.guild
    member = message.author
    now    = time.time()

    bypass = member.guild_permissions.administrator or member.guild_permissions.manage_messages

    if not bypass:
        if INVITE_PATTERN.search(message.content):
            if message.channel.id not in link_whitelist_channels[guild.id] and \
               not any(r.id in link_whitelist_roles[guild.id] for r in member.roles):
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await member.send(f"❌ Invite links are not allowed in **{guild.name}**.")
                except Exception:
                    pass
                embed = make_embed(
                    "🔗 Invite Link Blocked",
                    f"{member.mention} posted an invite link.",
                    color=COLOR_ORANGE,
                    user=f"{member} ({member.id})",
                    channel=f"#{message.channel.name}",
                    content=message.content[:500]
                )
                await send_log(guild, "automod", embed)
                return

        if len(message.mentions) >= MENTION_LIMIT or \
           (message.mention_everyone and not member.guild_permissions.mention_everyone):
            try:
                await message.delete()
            except Exception:
                pass
            warnings[guild.id][member.id] += 1
            embed = make_embed(
                "📣 Mass Mention Detected",
                f"{member.mention} mentioned **{len(message.mentions)}** users.",
                color=COLOR_RED,
                user=f"{member} ({member.id})",
                channel=f"#{message.channel.name}",
                mentions=len(message.mentions),
                total_warnings=warnings[guild.id][member.id]
            )
            await send_log(guild, "automod", embed)
            await check_warnings(member, guild)
            return

        history = message_history[guild.id][member.id]
        history.append(now)
        recent = [t for t in history if now - t <= SPAM_TIME_WINDOW]
        if len(recent) >= SPAM_MSG_LIMIT:
            try:
                def is_spam(m):
                    return m.author.id == member.id
                await message.channel.purge(limit=10, check=is_spam)
            except Exception:
                pass
            await apply_mute(member, guild, duration=300, reason="Auto-Mod: Spam detected")
            warnings[guild.id][member.id] += 1
            embed = make_embed(
                "🤖 AutoMod: Spam Detected",
                f"{member.mention} has been muted for **5 minutes** (spam).",
                color=COLOR_RED,
                user=f"{member} ({member.id})",
                channel=f"#{message.channel.name}",
                messages_in_window=len(recent),
                total_warnings=warnings[guild.id][member.id]
            )
            await send_log(guild, "automod", embed)
            message_history[guild.id][member.id].clear()
            return

        content = message.content
        if len(content) > 10:
            caps_ratio = sum(1 for c in content if c.isupper()) / max(len(content), 1)
            if caps_ratio > 0.7:
                try:
                    await message.delete()
                except Exception:
                    pass
                embed = make_embed(
                    "🔠 Excessive Caps Removed",
                    f"{member.mention}'s message was removed for excessive capitalization.",
                    color=COLOR_YELLOW,
                    user=f"{member} ({member.id})",
                    channel=f"#{message.channel.name}",
                    caps_ratio=f"{caps_ratio:.0%}"
                )
                await send_log(guild, "automod", embed)

    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    embed = make_embed(
        "🗑️ Message Deleted",
        f"A message by {message.author.mention} was deleted in {message.channel.mention}.",
        color=COLOR_ORANGE,
        author=f"{message.author} ({message.author.id})",
        channel=f"#{message.channel.name}",
        content=message.content[:1000] or "*[No text content]*",
        attachments=len(message.attachments)
    )
    await send_log(message.guild, "message", embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return
    embed = make_embed(
        "✏️ Message Edited",
        f"{before.author.mention} edited a message in {before.channel.mention}.",
        color=COLOR_BLUE,
        author=f"{before.author} ({before.author.id})",
        channel=f"#{before.channel.name}",
        before=before.content[:500] or "*empty*",
        after=after.content[:500] or "*empty*",
        jump_to_message=after.jump_url
    )
    await send_log(before.guild, "message", embed)


# ─────────────────────────────────────────────
#  EVENTS – ROLES & MEMBERS
# ─────────────────────────────────────────────

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    guild = before.guild
    added   = [r for r in after.roles  if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]

    if added:
        embed = make_embed(
            "➕ Role(s) Added",
            f"{after.mention} received new role(s).",
            color=COLOR_GREEN,
            user=f"{after} ({after.id})",
            roles_added=", ".join(r.mention for r in added)
        )
        await send_log(guild, "role", embed)

        dangerous = [r for r in added if r.permissions.administrator or
                     r.permissions.manage_guild or r.permissions.manage_roles or
                     r.permissions.ban_members or r.permissions.kick_members]
        if dangerous:
            alert = make_embed(
                "⚠️ HIGH-PRIVILEGE ROLE GRANTED",
                f"{after.mention} was granted a **high-privilege** role!",
                color=COLOR_RED,
                user=f"{after} ({after.id})",
                dangerous_roles=", ".join(r.mention for r in dangerous),
                action_required="Review immediately if this was unauthorized."
            )
            await send_log(guild, "mod", alert)

    if removed:
        embed = make_embed(
            "➖ Role(s) Removed",
            f"{after.mention} had role(s) removed.",
            color=COLOR_ORANGE,
            user=f"{after} ({after.id})",
            roles_removed=", ".join(r.mention for r in removed)
        )
        await send_log(guild, "role", embed)

    if before.nick != after.nick:
        embed = make_embed(
            "📝 Nickname Changed",
            f"{after.mention} changed their nickname.",
            color=COLOR_BLUE,
            user=f"{after} ({after.id})",
            before=before.nick or "*None*",
            after=after.nick or "*None*"
        )
        await send_log(guild, "server", embed)


# ─────────────────────────────────────────────
#  EVENTS – CHANNELS
# ─────────────────────────────────────────────

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    embed = make_embed(
        "📁 Channel Created",
        f"A new channel was created: {channel.mention}",
        color=COLOR_GREEN,
        name=channel.name,
        type=str(channel.type),
        id=channel.id
    )
    await send_log(channel.guild, "channel", embed)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    embed = make_embed(
        "🗑️ Channel Deleted",
        f"Channel **#{channel.name}** was deleted.",
        color=COLOR_RED,
        name=channel.name,
        type=str(channel.type),
        id=channel.id
    )
    await send_log(channel.guild, "channel", embed)


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    changes = []
    if before.name != after.name:
        changes.append(f"Name: `{before.name}` → `{after.name}`")
    if hasattr(before, "topic") and getattr(before, "topic", None) != getattr(after, "topic", None):
        changes.append(f"Topic changed")
    if hasattr(before, "nsfw") and getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
        changes.append(f"NSFW: `{before.nsfw}` → `{after.nsfw}`")
    if not changes:
        return
    embed = make_embed(
        "✏️ Channel Updated",
        f"Channel {after.mention} was modified.\n" + "\n".join(changes),
        color=COLOR_BLUE,
        channel_id=after.id
    )
    await send_log(after.guild, "channel", embed)


# ─────────────────────────────────────────────
#  EVENTS – ROLES (server-level)
# ─────────────────────────────────────────────

@bot.event
async def on_guild_role_create(role: discord.Role):
    embed = make_embed(
        "👑 Role Created",
        f"New role **{role.name}** was created.",
        color=COLOR_GREEN,
        role_id=role.id,
        permissions=str(role.permissions.value)
    )
    await send_log(role.guild, "role", embed)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    embed = make_embed(
        "🗑️ Role Deleted",
        f"Role **{role.name}** was deleted.",
        color=COLOR_RED,
        role_id=role.id
    )
    await send_log(role.guild, "role", embed)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    changes = []
    if before.name != after.name:
        changes.append(f"Name: `{before.name}` → `{after.name}`")
    if before.permissions != after.permissions:
        changes.append("Permissions changed")
    if before.color != after.color:
        changes.append(f"Color: `{before.color}` → `{after.color}`")
    if not changes:
        return
    embed = make_embed(
        "✏️ Role Updated",
        f"Role **{after.name}** was modified.\n" + "\n".join(changes),
        color=COLOR_BLUE,
        role_id=after.id
    )
    await send_log(after.guild, "role", embed)


# ─────────────────────────────────────────────
#  EVENTS – VOICE
# ─────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    if before.channel is None and after.channel is not None:
        desc = f"{member.mention} joined voice channel **{after.channel.name}**."
        color = COLOR_GREEN
        action = "Joined"
    elif before.channel is not None and after.channel is None:
        desc = f"{member.mention} left voice channel **{before.channel.name}**."
        color = COLOR_GRAY
        action = "Left"
    elif before.channel != after.channel:
        desc = f"{member.mention} moved from **{before.channel.name}** to **{after.channel.name}**."
        color = COLOR_BLUE
        action = "Moved"
    else:
        changes = []
        if before.self_mute != after.self_mute:
            changes.append(f"Self-Mute: {after.self_mute}")
        if before.self_deaf != after.self_deaf:
            changes.append(f"Self-Deaf: {after.self_deaf}")
        if before.mute != after.mute:
            changes.append(f"Server-Mute: {after.mute}")
        if before.deaf != after.deaf:
            changes.append(f"Server-Deaf: {after.deaf}")
        if not changes:
            return
        desc = f"{member.mention} voice state changed: " + ", ".join(changes)
        color = COLOR_YELLOW
        action = "State Change"

    embed = make_embed(f"🎙️ Voice: {action}", desc, color=color,
                       user=f"{member} ({member.id})")
    await send_log(guild, "voice", embed)


# ─────────────────────────────────────────────
#  EVENTS – REACTIONS
# ─────────────────────────────────────────────

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user):
    if user.bot or not reaction.message.guild:
        return
    embed = make_embed(
        "💬 Reaction Added",
        f"{user.mention} reacted with {reaction.emoji} on a message.",
        color=COLOR_BLUE,
        user=f"{user} ({user.id})",
        channel=f"#{reaction.message.channel.name}",
        emoji=str(reaction.emoji),
        message_id=reaction.message.id
    )
    await send_log(reaction.message.guild, "reaction", embed)


@bot.event
async def on_reaction_remove(reaction: discord.Reaction, user):
    if user.bot or not reaction.message.guild:
        return
    embed = make_embed(
        "💬 Reaction Removed",
        f"{user.mention} removed reaction {reaction.emoji}.",
        color=COLOR_GRAY,
        user=f"{user} ({user.id})",
        channel=f"#{reaction.message.channel.name}",
        emoji=str(reaction.emoji)
    )
    await send_log(reaction.message.guild, "reaction", embed)


# ─────────────────────────────────────────────
#  EVENTS – BANS / KICKS
# ─────────────────────────────────────────────

@bot.event
async def on_member_ban(guild: discord.Guild, user):
    embed = make_embed(
        "🔨 Member Banned",
        f"**{user}** was banned from the server.",
        color=COLOR_RED,
        user=f"{user} ({user.id})"
    )
    await send_log(guild, "mod", embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user):
    embed = make_embed(
        "✅ Member Unbanned",
        f"**{user}** was unbanned.",
        color=COLOR_GREEN,
        user=f"{user} ({user.id})"
    )
    await send_log(guild, "mod", embed)


# ─────────────────────────────────────────────
#  EVENTS – SERVER UPDATES
# ─────────────────────────────────────────────

@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    changes = []
    if before.name != after.name:
        changes.append(f"Name: `{before.name}` → `{after.name}`")
    if before.icon != after.icon:
        changes.append("Server icon changed")
    if before.verification_level != after.verification_level:
        changes.append(f"Verification: `{before.verification_level}` → `{after.verification_level}`")
    if not changes:
        return
    embed = make_embed(
        "⚙️ Server Updated",
        "\n".join(changes),
        color=COLOR_BLUE
    )
    await send_log(after, "server", embed)


@bot.event
async def on_invite_create(invite: discord.Invite):
    embed = make_embed(
        "🔗 Invite Created",
        f"A new invite was created.",
        color=COLOR_BLUE,
        code=invite.code,
        creator=f"{invite.inviter} ({invite.inviter.id})" if invite.inviter else "Unknown",
        channel=f"#{invite.channel.name}" if invite.channel else "Unknown",
        max_uses=invite.max_uses or "Unlimited",
        expires=str(invite.expires_at) if invite.expires_at else "Never"
    )
    await send_log(invite.guild, "server", embed)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    embed = make_embed(
        "🗑️ Invite Deleted",
        f"Invite `{invite.code}` was deleted.",
        color=COLOR_ORANGE,
        code=invite.code,
        channel=f"#{invite.channel.name}" if invite.channel else "Unknown"
    )
    await send_log(invite.guild, "server", embed)


# ─────────────────────────────────────────────
#  HELPERS – MUTE / WARNINGS
# ─────────────────────────────────────────────

async def apply_mute(member: discord.Member, guild: discord.Guild,
                     duration: int, reason: str):
    role = await get_or_create_mute_role(guild)
    try:
        await member.add_roles(role, reason=reason)
        muted_users[guild.id][member.id] = time.time() + duration
        try:
            await member.send(f"🔇 You have been muted in **{guild.name}** for "
                              f"{duration//60} minutes.\nReason: {reason}")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"apply_mute failed: {e}")


async def check_warnings(member: discord.Member, guild: discord.Guild):
    count = warnings[guild.id][member.id]
    if count >= 5:
        try:
            await member.ban(reason=f"[SecurityBot] Auto-ban: {count} warnings")
            embed = make_embed("🔨 Auto-Ban", f"{member} reached **{count} warnings** and was auto-banned.",
                               color=COLOR_RED, user=f"{member} ({member.id})")
            await send_log(guild, "mod", embed)
        except Exception:
            pass
    elif count >= 3:
        await apply_mute(member, guild, duration=1800, reason=f"Auto-Mod: {count} warnings")
        embed = make_embed("🔇 Auto-Mute (30m)",
                           f"{member} reached **{count} warnings** → muted 30 minutes.",
                           color=COLOR_ORANGE, user=f"{member} ({member.id})")
        await send_log(guild, "mod", embed)


# ─────────────────────────────────────────────
#  BACKGROUND TASK – AUTO UNMUTE
# ─────────────────────────────────────────────

@tasks.loop(seconds=30)
async def unmute_task():
    now = time.time()
    for guild in bot.guilds:
        expired = [uid for uid, until in list(muted_users[guild.id].items()) if now >= until]
        for uid in expired:
            muted_users[guild.id].pop(uid, None)
            member = guild.get_member(uid)
            if member:
                role = discord.utils.get(guild.roles, name="Muted")
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="[SecurityBot] Auto-unmute: Duration expired")
                        embed = make_embed("🔊 Auto-Unmuted", f"{member.mention} has been automatically unmuted.",
                                          color=COLOR_GREEN, user=f"{member} ({member.id})")
                        await send_log(guild, "mod", embed)
                    except Exception as e:
                        log.warning(f"unmute_task failed: {e}")


# ─────────────────────────────────────────────
#  PERMISSION CHECKS
# ─────────────────────────────────────────────

def is_admin():
    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)


def is_mod():
    async def predicate(ctx):
        p = ctx.author.guild_permissions
        return p.administrator or p.ban_members or p.kick_members or p.manage_messages
    return commands.check(predicate)


# ─────────────────────────────────────────────
#  COMMANDS – MODERATION
# ─────────────────────────────────────────────

@bot.command(name="ban")
@is_mod()
async def cmd_ban(ctx, member: discord.Member, *, reason="No reason provided"):
    try:
        await member.send(f"🔨 You have been banned from **{ctx.guild.name}**.\nReason: {reason}")
    except Exception:
        pass
    await member.ban(reason=f"[{ctx.author}] {reason}")
    embed = make_embed("🔨 Member Banned", f"{member.mention} was banned by {ctx.author.mention}.",
                       color=COLOR_RED, target=f"{member} ({member.id})",
                       moderator=str(ctx.author), reason=reason)
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(embed=embed)


@bot.command(name="unban")
@is_mod()
async def cmd_unban(ctx, user_id: int, *, reason="No reason provided"):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user, reason=f"[{ctx.author}] {reason}")
    embed = make_embed("✅ Member Unbanned", f"**{user}** was unbanned by {ctx.author.mention}.",
                       color=COLOR_GREEN, target=f"{user} ({user.id})",
                       moderator=str(ctx.author), reason=reason)
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(embed=embed)


@bot.command(name="kick")
@is_mod()
async def cmd_kick(ctx, member: discord.Member, *, reason="No reason provided"):
    try:
        await member.send(f"👢 You have been kicked from **{ctx.guild.name}**.\nReason: {reason}")
    except Exception:
        pass
    await member.kick(reason=f"[{ctx.author}] {reason}")
    embed = make_embed("👢 Member Kicked", f"{member.mention} was kicked by {ctx.author.mention}.",
                       color=COLOR_ORANGE, target=f"{member} ({member.id})",
                       moderator=str(ctx.author), reason=reason)
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(embed=embed)


@bot.command(name="mute")
@is_mod()
async def cmd_mute(ctx, member: discord.Member, duration: str = "10m", *, reason="No reason provided"):
    secs = duration_parse(duration)
    if secs is None:
        return await ctx.send("❌ Invalid duration. Use: `10m`, `2h`, `1d`")
    await apply_mute(member, ctx.guild, secs, f"[{ctx.author}] {reason}")
    embed = make_embed("🔇 Member Muted", f"{member.mention} was muted by {ctx.author.mention}.",
                       color=COLOR_ORANGE, target=f"{member} ({member.id})",
                       duration=duration, moderator=str(ctx.author), reason=reason)
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(embed=embed)


@bot.command(name="unmute")
@is_mod()
async def cmd_unmute(ctx, member: discord.Member):
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    if role and role in member.roles:
        await member.remove_roles(role, reason=f"[{ctx.author}] Manual unmute")
        muted_users[ctx.guild.id].pop(member.id, None)
        embed = make_embed("🔊 Member Unmuted", f"{member.mention} was unmuted by {ctx.author.mention}.",
                           color=COLOR_GREEN, target=f"{member} ({member.id})",
                           moderator=str(ctx.author))
        await send_log(ctx.guild, "mod", embed)
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"❌ {member.mention} is not muted.")


@bot.command(name="warn")
@is_mod()
async def cmd_warn(ctx, member: discord.Member, *, reason="No reason provided"):
    warnings[ctx.guild.id][member.id] += 1
    count = warnings[ctx.guild.id][member.id]
    try:
        await member.send(f"⚠️ You have been warned in **{ctx.guild.name}**.\nReason: {reason}\nWarnings: {count}")
    except Exception:
        pass
    embed = make_embed("⚠️ Member Warned", f"{member.mention} was warned by {ctx.author.mention}.",
                       color=COLOR_YELLOW, target=f"{member} ({member.id})",
                       reason=reason, total_warnings=count, moderator=str(ctx.author))
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(embed=embed)
    await check_warnings(member, ctx.guild)


@bot.command(name="warnings")
@is_mod()
async def cmd_warnings(ctx, member: discord.Member):
    count = warnings[ctx.guild.id][member.id]
    await ctx.send(f"⚠️ **{member}** has **{count}** warning(s).")


@bot.command(name="clearwarnings")
@is_mod()
async def cmd_clearwarnings(ctx, member: discord.Member):
    warnings[ctx.guild.id][member.id] = 0
    embed = make_embed("✅ Warnings Cleared", f"All warnings cleared for {member.mention}.",
                       color=COLOR_GREEN, target=f"{member} ({member.id})",
                       moderator=str(ctx.author))
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(f"✅ Cleared all warnings for {member.mention}.")


@bot.command(name="purge")
@is_mod()
async def cmd_purge(ctx, amount: int, member: Optional[discord.Member] = None):
    amount = min(amount, 500)
    check = (lambda m: m.author == member) if member else None
    deleted = await ctx.channel.purge(limit=amount, check=check)
    embed = make_embed("🗑️ Messages Purged",
                       f"**{len(deleted)}** messages deleted in {ctx.channel.mention}.",
                       color=COLOR_ORANGE,
                       deleted=len(deleted),
                       filter=str(member) if member else "All",
                       moderator=str(ctx.author))
    await send_log(ctx.guild, "mod", embed)
    msg = await ctx.send(f"🗑️ Purged **{len(deleted)}** messages.")
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except Exception:
        pass


@bot.command(name="slowmode")
@is_mod()
async def cmd_slowmode(ctx, seconds: int):
    seconds = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=seconds)
    embed = make_embed("⏱️ Slowmode Updated",
                       f"Slowmode set to **{seconds}s** in {ctx.channel.mention}.",
                       color=COLOR_BLUE, moderator=str(ctx.author))
    await send_log(ctx.guild, "channel", embed)
    await ctx.send(f"✅ Slowmode set to **{seconds}s**.")


@bot.command(name="lockdown")
@is_mod()
async def cmd_lockdown(ctx):
    guild = ctx.guild
    if not lockdown_state[guild.id]:
        lockdown_state[guild.id] = True
        for channel in guild.text_channels:
            try:
                await channel.set_permissions(guild.default_role, send_messages=False,
                                              reason=f"[{ctx.author}] Lockdown enabled")
            except Exception:
                pass
        embed = make_embed("🔒 LOCKDOWN ENABLED",
                           f"Server locked down by {ctx.author.mention}. No one can send messages.",
                           color=COLOR_RED, moderator=str(ctx.author))
        await send_log(guild, "mod", embed)
        await ctx.send(embed=embed)
    else:
        lockdown_state[guild.id] = False
        for channel in guild.text_channels:
            try:
                await channel.set_permissions(guild.default_role, send_messages=None,
                                              reason=f"[{ctx.author}] Lockdown lifted")
            except Exception:
                pass
        embed = make_embed("🔓 Lockdown Lifted",
                           f"Server lockdown lifted by {ctx.author.mention}.",
                           color=COLOR_GREEN, moderator=str(ctx.author))
        await send_log(guild, "mod", embed)
        await ctx.send(embed=embed)


@bot.command(name="raidmode")
@is_admin()
async def cmd_raidmode(ctx, state: str):
    if state.lower() == "on":
        raid_mode[ctx.guild.id] = True
        await ctx.send("🚨 Raid mode **ENABLED**. New accounts will be auto-kicked.")
    else:
        raid_mode[ctx.guild.id] = False
        await ctx.send("✅ Raid mode **DISABLED**.")
    embed = make_embed(f"🚨 Raid Mode {state.upper()}",
                       f"Raid mode toggled by {ctx.author.mention}.",
                       color=COLOR_RED if state.lower() == "on" else COLOR_GREEN,
                       moderator=str(ctx.author))
    await send_log(ctx.guild, "raid", embed)


@bot.command(name="softban")
@is_mod()
async def cmd_softban(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.ban(reason=f"[Softban by {ctx.author}] {reason}", delete_message_days=7)
    await ctx.guild.unban(member, reason="Softban: immediate unban")
    embed = make_embed("🔨 Member Softbanned",
                       f"{member.mention} was softbanned by {ctx.author.mention} (messages deleted).",
                       color=COLOR_ORANGE, target=f"{member} ({member.id})",
                       moderator=str(ctx.author), reason=reason)
    await send_log(ctx.guild, "mod", embed)
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
@is_mod()
async def cmd_userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    account_age = (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days
    embed = make_embed(
        f"👤 User Info: {member}",
        color=COLOR_BLUE,
        id=member.id,
        display_name=member.display_name,
        account_created=ts(member.created_at),
        account_age=f"{account_age} days",
        joined_server=ts(member.joined_at) if member.joined_at else "Unknown",
        roles=", ".join(r.name for r in member.roles[1:])[:500] or "None",
        warnings=warnings[ctx.guild.id][member.id],
        is_muted=member.id in muted_users[ctx.guild.id]
    )
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
@is_mod()
async def cmd_serverinfo(ctx):
    g = ctx.guild
    embed = make_embed(
        f"🏠 Server Info: {g.name}",
        color=COLOR_BLUE,
        id=g.id,
        owner=str(g.owner),
        created=ts(g.created_at),
        members=g.member_count,
        channels=len(g.channels),
        roles=len(g.roles),
        verification=str(g.verification_level),
        raid_mode=raid_mode[g.id],
        lockdown=lockdown_state[g.id]
    )
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=embed)


@bot.command(name="whitelist_channel")
@is_admin()
async def cmd_whitelist_channel(ctx, channel: discord.TextChannel):
    link_whitelist_channels[ctx.guild.id].add(channel.id)
    await ctx.send(f"✅ {channel.mention} is now whitelisted for links.")


@bot.command(name="whitelist_role")
@is_admin()
async def cmd_whitelist_role(ctx, role: discord.Role):
    link_whitelist_roles[ctx.guild.id].add(role.id)
    await ctx.send(f"✅ {role.mention} is now whitelisted for links.")


@bot.command(name="help")
async def cmd_help(ctx):
    embed = discord.Embed(title="🔒 Security Bot — Command List", color=COLOR_BLUE,
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="Moderation", value="""
`!ban @user [reason]` — Ban a member
`!unban <user_id> [reason]` — Unban by ID
`!kick @user [reason]` — Kick a member
`!softban @user [reason]` — Ban + unban (clear messages)
`!mute @user [10m/2h/1d] [reason]` — Mute member
`!unmute @user` — Unmute member
`!warn @user [reason]` — Warn member
`!warnings @user` — Show warnings
`!clearwarnings @user` — Clear warnings
`!purge <amount> [@user]` — Delete messages
`!slowmode <seconds>` — Set channel slowmode
""", inline=False)
    embed.add_field(name="Server Control", value="""
`!lockdown` — Toggle server lockdown
`!raidmode on/off` — Toggle raid mode
`!whitelist_channel #channel` — Allow links in channel
`!whitelist_role @role` — Allow links for role
""", inline=False)
    embed.add_field(name="Info", value="""
`!userinfo [@user]` — User details
`!serverinfo` — Server details
`!help` — This menu
""", inline=False)
    embed.set_footer(text="Security Bot | All actions are logged")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────
#  ERROR HANDLER
# ─────────────────────────────────────────────

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {error}")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You are not authorized to use this command.")
    else:
        log.error(f"Unhandled error in {ctx.command}: {error}")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Holt das Token sicher aus den Railway-Umgebungsvariablen
    token = os.getenv("DISCORD_TOKEN")
    
    if not token:
        print("ERROR: Kein DISCORD_TOKEN gefunden! Bitte füge es in den Railway Variablen hinzu.")
    else:
        bot.run(token)
