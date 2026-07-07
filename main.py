# ===========================================================
#  TICKET BOT — bot.py
#  Ticket system with:
#   - Dropdown panel with configurable categories
#   - Single support role (+ server admins) can see tickets
#   - HTML transcript posted to a log channel on close
#   - Auto-close after 24h of inactivity, with a warning 1h before
#  All IDs (guild, roles, channels) are set by YOU in the .env file.
# ============================================================

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
import re
import io
import html as html_lib
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ============================================================
#  CONFIG — set these in your .env file
# ============================================================
TOKEN                  = os.getenv("DISCORD_TOKEN", "")
GUILD_ID               = int(os.getenv("GUILD_ID", 0))
TICKET_CATEGORY_ID     = int(os.getenv("TICKET_CATEGORY_ID", 0))   # Discord category new ticket channels go under
TRANSCRIPT_CHANNEL_ID  = int(os.getenv("TRANSCRIPT_CHANNEL_ID", 0)) # log channel for transcripts
STAFF_ROLE_ID          = int(os.getenv("STAFF_ROLE_ID", 0))         # the ONE role allowed to see tickets

AUTO_CLOSE_HOURS       = float(os.getenv("AUTO_CLOSE_HOURS", 24))   # close after this many hours of inactivity
WARNING_HOURS_BEFORE   = float(os.getenv("WARNING_HOURS_BEFORE", 1)) # send warning this many hours before auto-close

BRAND_NAME             = os.getenv("BRAND_NAME", "Support")
BRAND_LOGO             = os.getenv("BRAND_LOGO", "").strip()        # https:// image url, optional
BRAND_COLOR            = int(os.getenv("BRAND_COLOR", "1A6FFF"), 16)

# Banner image shown at the bottom of the /panel embed (like the "CONTACT US" banner).
# Two ways to set it:
#   1) PANEL_IMAGE_URL = a https:// link to an already-hosted image
#   2) PANEL_IMAGE_FILE = a local file path (e.g. "banner.png") placed next to bot.py —
#      the bot will upload it itself. If both are set, the local file takes priority.
PANEL_IMAGE_URL        = os.getenv("PANEL_IMAGE_URL", "").strip()
PANEL_IMAGE_FILE       = os.getenv("PANEL_IMAGE_FILE", "").strip()

# ------------------------------------------------------------
# Dropdown categories — edit this list to whatever you need.
# key: internal id (used in channel topics, don't change once live)
# label/description/emoji: shown in the dropdown
# ------------------------------------------------------------
TICKET_CATEGORIES = {
    "support":  {"label": "General Support", "description": "Get help from our staff.",        "emoji": "🎫"},
    "purchase": {"label": "Purchase",          "description": "Request help with a purchase.",   "emoji": "🛒"},
    "other":    {"label": "Other",             "description": "Anything else.",                  "emoji": "❓"},
}

CONFIG_FILE = "tickets.json"

# ============================================================
#  PERSISTENCE (JSON — tickets + counter, survives restarts)
# ============================================================
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("tickets", {})       # channel_id(str) -> ticket info
    data.setdefault("ticket_counter", 0)
    return data


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


config = load_config()

# ============================================================
#  BOT SETUP
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def set_logo(embed: discord.Embed):
    if BRAND_LOGO.startswith("https://"):
        embed.set_thumbnail(url=BRAND_LOGO)


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:20] or "ticket"


# ============================================================
#  HTML TRANSCRIPT GENERATOR
# ============================================================
def generate_transcript(channel, messages, guild, category_key: str) -> str:
    cat = TICKET_CATEGORIES.get(category_key, {"label": "Support", "emoji": "🎫"})
    msgs_html = ""
    prev_id = None

    for msg in messages:
        author = html_lib.escape(str(msg.author.display_name))
        av = str(msg.author.display_avatar.url) if msg.author.display_avatar else ""
        stf = any(r.id == STAFF_ROLE_ID for r in getattr(msg.author, "roles", []))

        if msg.author.id == guild.owner_id:
            bdg = '<span class="badge owner">Owner</span>'
        elif stf:
            bdg = '<span class="badge staff">Staff</span>'
        elif msg.author.bot:
            bdg = '<span class="badge bot">BOT</span>'
        else:
            bdg = ""

        att = ""
        for a in msg.attachments:
            url = html_lib.escape(a.url)
            if a.content_type and a.content_type.startswith("image"):
                att += f'<img src="{url}" class="att-img" alt="img">'
            else:
                att += f'<a href="{url}" class="att-file" target="_blank">📎 {html_lib.escape(a.filename)}</a>'

        emb = ""
        for e in msg.embeds:
            ec = f"#{e.color.value:06x}" if e.color else "#1A6FFF"
            et = f"<div class='et'>{html_lib.escape(e.title)}</div>" if e.title else ""
            ed = f"<div class='ed'>{html_lib.escape(e.description)}</div>" if e.description else ""
            emb += f'<div class="emb" style="border-left-color:{ec}">{et}{ed}</div>'

        txt = html_lib.escape(msg.content or "")
        txt = re.sub(r"\n", "<br>", txt)

        ts = msg.created_at.strftime("%d/%m/%Y %H:%M")
        same = prev_id == msg.author.id
        prev_id = msg.author.id

        av_html = f'<img src="{av}" class="av" alt="av">' if not same else '<div class="avs"></div>'
        hdr_html = (
            f'<div class="mh"><span class="un">{author}</span>{bdg}<span class="ts">{ts}</span></div>'
            if not same else ""
        )
        msgs_html += (
            f'<div class="mg{"" if not same else " sa"}">'
            f'{av_html}<div class="mc">{hdr_html}<div class="mt">{txt}</div>{att}{emb}</div></div>'
        )

    logo_html = (
        f'<img src="{BRAND_LOGO}" class="hl" alt="logo" onerror="this.style.display=\'none\'">'
        if BRAND_LOGO.startswith("https://") else ""
    )

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Transcript — {html_lib.escape(channel.name)}</title>
<style>:root{{--bg:#04080F;--s2:#0A1020;--br:#0F1830;--bl:#1A6FFF;--blg:#4D8FFF;--tx:#D8E4FF;--mu:#4A5878;--ow:#F0A500;--bt:#5865F2}}
*{{box-sizing:border-box;margin:0;padding:0}}body{{background:var(--bg);color:var(--tx);font-family:'Inter',sans-serif;font-size:14px;line-height:1.6}}
.hd{{background:linear-gradient(135deg,#04080F 0%,#071228 50%,#0A1A3A 100%);border-bottom:1px solid var(--br);padding:24px 40px;display:flex;align-items:center;gap:20px}}
.hl{{width:60px;height:60px;border-radius:50%;border:2px solid var(--bl)}}.hi h1{{font-size:22px;color:var(--bl);font-weight:800;letter-spacing:2px}}.hi p{{color:var(--mu);font-size:12px}}
.hm{{margin-left:auto;font-size:11px;color:var(--mu)}}.hm strong{{color:var(--tx)}}
.ms{{max-width:880px;margin:0 auto;padding:20px 40px}}.mg{{display:flex;gap:12px;padding:5px 8px;border-radius:8px;margin:1px -8px}}
.av{{width:38px;height:38px;border-radius:50%;flex-shrink:0;border:1px solid var(--br)}}.avs{{width:38px;flex-shrink:0}}.mc{{flex:1}}
.mh{{display:flex;align-items:center;gap:6px;margin-bottom:2px}}.un{{font-weight:600}}.ts{{font-size:10px;color:var(--mu)}}
.badge{{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px}}.badge.staff{{background:rgba(26,110,255,.15);color:var(--blg)}}
.badge.owner{{background:rgba(240,165,0,.15);color:var(--ow)}}.badge.bot{{background:rgba(88,101,242,.15);color:var(--bt)}}
.mt{{color:#A0B4E0;word-break:break-word}}.att-img{{max-width:380px;border-radius:8px;margin-top:6px;display:block}}
.emb{{margin-top:6px;background:var(--s2);border-left:4px solid var(--bl);border-radius:4px;padding:8px 12px}}
.ft{{text-align:center;padding:36px;border-top:1px solid var(--br);color:var(--mu);font-size:11px}}</style></head>
<body><div class="hd">{logo_html}<div class="hi"><h1>{html_lib.escape(BRAND_NAME.upper())}</h1><p>{cat["emoji"]} {cat["label"]} • #{html_lib.escape(channel.name)}</p></div>
<div class="hm">Generated: <strong>{datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")} UTC</strong></div></div>
<div class="ms">{msgs_html if msgs_html else "<p>No messages in this ticket.</p>"}</div>
<div class="ft"><p>{html_lib.escape(BRAND_NAME)} • Ticket System</p></div>
</body></html>"""


# ============================================================
#  CLOSE TICKET
# ============================================================
async def close_ticket(channel: discord.TextChannel, guild: discord.Guild, closed_by=None):
    info = config["tickets"].get(str(channel.id))
    if not info:
        try:
            await channel.delete()
        except discord.HTTPException:
            pass
        return

    messages = [m async for m in channel.history(limit=1000, oldest_first=True)]
    html_doc = generate_transcript(channel, messages, guild, info.get("category", "support"))

    log_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)
    if log_channel is not None:
        cat = TICKET_CATEGORIES.get(info.get("category", "support"), {"label": "Support", "emoji": "🎫"})
        user = guild.get_member(info["user_id"])
        user_str = user.mention if user else f"<@{info['user_id']}>"
        opened_ts = int(datetime.fromisoformat(info["created_at"]).timestamp())
        closed_str = closed_by.mention if closed_by and hasattr(closed_by, "mention") else "Auto-Close ⏰"

        embed = discord.Embed(
            title=f"📋 Ticket Transcript — #{channel.name}",
            description=(
                f"**User:** {user_str}\n**Category:** {cat['emoji']} {cat['label']}\n"
                f"**Opened:** <t:{opened_ts}:F>\n**Closed by:** {closed_str}\n**Messages:** {len(messages)}"
            ),
            color=BRAND_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{BRAND_NAME} • Ticket System")
        set_logo(embed)
        try:
            await log_channel.send(
                embed=embed,
                file=discord.File(io.BytesIO(html_doc.encode()), filename=f"transcript-{channel.name}.html"),
            )
        except discord.Forbidden:
            print("Missing permissions to send in the transcript channel.")

    config["tickets"].pop(str(channel.id), None)
    save_config(config)

    try:
        await channel.delete(reason="Ticket closed")
    except discord.HTTPException:
        pass


# ============================================================
#  VIEWS
# ============================================================
class TicketSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a category to open a ticket.",
            min_values=1,
            max_values=1,
            custom_id="ticket_category_select",
            options=[
                discord.SelectOption(label=v["label"], description=v["description"], emoji=v["emoji"], value=k)
                for k, v in TICKET_CATEGORIES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        cat_key = self.values[0]
        cat = TICKET_CATEGORIES[cat_key]
        guild = interaction.guild

        # Defer immediately — channel creation can take >3s and would
        # otherwise trigger a failed-interaction error on Discord's side.
        await interaction.response.defer(ephemeral=True, thinking=True)

        for ch in guild.text_channels:
            if ch.topic and f"uid-{interaction.user.id}" in ch.topic:
                await interaction.followup.send(f"❌ You already have an open ticket: {ch.mention}", ephemeral=True)
                return

        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role is None:
            await interaction.followup.send(
                "⚠️ No support role configured (STAFF_ROLE_ID is missing/invalid). Ask an admin to fix the .env file.",
                ephemeral=True,
            )
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           attach_files=True, read_message_history=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                     attach_files=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                   manage_channels=True, read_message_history=True),
        }

        parent_category = guild.get_channel(TICKET_CATEGORY_ID)
        config["ticket_counter"] = config.get("ticket_counter", 0) + 1
        num = config["ticket_counter"]

        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{num:04d}",
                overwrites=overwrites,
                category=parent_category if isinstance(parent_category, discord.CategoryChannel) else None,
                topic=f"uid-{interaction.user.id} | {cat_key} | open",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Could not create ticket: {e}", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        config["tickets"][str(channel.id)] = {
            "user_id": interaction.user.id,
            "category": cat_key,
            "created_at": now.isoformat(),
            "last_activity": now.isoformat(),
            "warned": False,
        }
        save_config(config)

        await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)

        embed = discord.Embed(
            title=f"{cat['emoji']} {cat['label']} — Ticket #{num:04d}",
            description=(
                f"Welcome, {interaction.user.mention}! 👋\n\n"
                f"Please describe your issue and {staff_role.mention} will be with you shortly."
            ),
            color=BRAND_COLOR,
            timestamp=now,
        )
        set_logo(embed)
        embed.set_footer(text=f"{BRAND_NAME} • Ticket System")
        await channel.send(content=f"{interaction.user.mention} {staff_role.mention}", embed=embed, view=TicketControlView())


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close_button")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = config["tickets"].get(str(interaction.channel.id))
        if not info:
            await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
            return
        if not is_staff(interaction.user) and info["user_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Only staff or the ticket owner can close this.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Closing in 5 seconds...")
        await asyncio.sleep(5)
        await close_ticket(interaction.channel, interaction.guild, closed_by=interaction.user)

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋", custom_id="ticket_claim_button")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can claim tickets.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(description=f"✋ **{interaction.user.mention}** has claimed this ticket!", color=BRAND_COLOR)
        )


# ============================================================
#  BACKGROUND TASK — WARNING + AUTO-CLOSE
# ============================================================
@tasks.loop(minutes=5)
async def auto_close_task():
    now = datetime.now(timezone.utc)
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    for chan_id_str, info in list(config["tickets"].items()):
        last_activity = datetime.fromisoformat(info["last_activity"])
        elapsed_hours = (now - last_activity).total_seconds() / 3600

        channel = guild.get_channel(int(chan_id_str))
        if channel is None:
            config["tickets"].pop(chan_id_str, None)
            save_config(config)
            continue

        warn_at = AUTO_CLOSE_HOURS - WARNING_HOURS_BEFORE

        if elapsed_hours >= AUTO_CLOSE_HOURS:
            try:
                await channel.send("⏰ This ticket is inactive and is now being closed automatically...")
            except discord.HTTPException:
                pass
            await close_ticket(channel, guild)
        elif elapsed_hours >= warn_at and not info.get("warned", False):
            info["warned"] = True
            save_config(config)
            try:
                await channel.send(
                    f"⚠️ **This ticket is inactive and will be closed in {WARNING_HOURS_BEFORE:g} hour(s)** "
                    "unless there is new activity."
                )
            except discord.HTTPException:
                pass


@auto_close_task.before_loop
async def before_auto_close():
    await bot.wait_until_ready()


# ============================================================
#  EVENTS
# ============================================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())
    if not auto_close_task.is_running():
        auto_close_task.start()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except discord.HTTPException as e:
        print(f"❌ Sync error: {e}")
    print("✅ Ticket bot ready.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    info = config["tickets"].get(str(message.channel.id))
    if info is not None:
        info["last_activity"] = datetime.now(timezone.utc).isoformat()
        info["warned"] = False
        save_config(config)

    await bot.process_commands(message)


# ============================================================
#  SLASH COMMANDS
# ============================================================
@bot.tree.command(name="panel", description="Send the ticket panel (Admin/staff only)")
@app_commands.guild_only()
async def cmd_panel(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    lines = "\n".join(f"{v['emoji']} **{v['label']}** — {v['description']}" for v in TICKET_CATEGORIES.values())
    embed = discord.Embed(
        title=f"🎫 {BRAND_NAME} Support Tickets",
        description=f"**Need help? Open a ticket below!**\n\n{lines}\n\n*Select a category from the dropdown.*",
        color=BRAND_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    set_logo(embed)
    embed.set_footer(text=f"{BRAND_NAME} • Ticket System")

    # Attach the banner image, either from a local file (uploaded fresh each time)
    # or from a hosted URL. Local file takes priority if both are set.
    file = None
    if PANEL_IMAGE_FILE and os.path.exists(PANEL_IMAGE_FILE):
        filename = os.path.basename(PANEL_IMAGE_FILE)
        file = discord.File(PANEL_IMAGE_FILE, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
    elif PANEL_IMAGE_URL:
        embed.set_image(url=PANEL_IMAGE_URL)

    if file is not None:
        await interaction.channel.send(embed=embed, view=TicketPanelView(), file=file)
    else:
        await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.followup.send("✅ Panel sent!", ephemeral=True)


@bot.tree.command(name="close", description="Close the current ticket (Staff only)")
@app_commands.guild_only()
async def cmd_close(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    info = config["tickets"].get(str(interaction.channel.id))
    if not info:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return
    await interaction.response.send_message("🔒 Closing in 5 seconds...")
    await asyncio.sleep(5)
    await close_ticket(interaction.channel, interaction.guild, closed_by=interaction.user)


@bot.tree.command(name="add", description="Add a user to the current ticket (Staff only)")
@app_commands.describe(user="User to add")
@app_commands.guild_only()
async def cmd_add(interaction: discord.Interaction, user: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if str(interaction.channel.id) not in config["tickets"]:
        await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
        return
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    await interaction.response.send_message(embed=discord.Embed(description=f"✅ {user.mention} added.", color=BRAND_COLOR))


@bot.tree.command(name="remove", description="Remove a user from the current ticket (Staff only)")
@app_commands.describe(user="User to remove")
@app_commands.guild_only()
async def cmd_remove(interaction: discord.Interaction, user: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if str(interaction.channel.id) not in config["tickets"]:
        await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
        return
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(embed=discord.Embed(description=f"✅ {user.mention} removed.", color=BRAND_COLOR))


@bot.tree.command(name="tstatus", description="Show current ticket bot configuration")
@app_commands.guild_only()
async def cmd_tstatus(interaction: discord.Interaction):
    guild = interaction.guild
    log_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)
    staff_role = guild.get_role(STAFF_ROLE_ID)
    parent_category = guild.get_channel(TICKET_CATEGORY_ID)

    embed = discord.Embed(title="🤖 Ticket Bot Status", color=BRAND_COLOR)
    embed.add_field(name="Log Channel", value=log_channel.mention if log_channel else "⚠️ Not set / invalid", inline=False)
    embed.add_field(name="Support Role", value=staff_role.mention if staff_role else "⚠️ Not set / invalid", inline=False)
    embed.add_field(
        name="Ticket Category",
        value=parent_category.name if isinstance(parent_category, discord.CategoryChannel) else "⚠️ Not set / invalid",
        inline=False,
    )
    cat_text = "\n".join(f"{v['emoji']} {v['label']}" for v in TICKET_CATEGORIES.values())
    embed.add_field(name="Dropdown Categories", value=cat_text, inline=False)
    embed.add_field(name="Open Tickets", value=str(len(config["tickets"])), inline=False)
    embed.add_field(
        name="Auto-Close",
        value=f"Warning {WARNING_HOURS_BEFORE:g}h before, close after {AUTO_CLOSE_HOURS:g}h of inactivity",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
#  RUN
# ============================================================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(TOKEN)
