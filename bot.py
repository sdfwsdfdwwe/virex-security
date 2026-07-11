# ===========================================================
#  TICKET BOT — bot.py
#  Ticket system with:
#   - Dropdown panel with configurable categories
#   - Single support role (+ server admins) can see tickets
#   - HTML transcript posted to a log channel on close
#   - Auto-close after 24h of inactivity, with a warning 1h before
#   - LINK FILTER: block links in chosen channels, whitelist support
#     role + individual users, warn -> 2nd warning = timeout + blacklist role
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

# ============================================================
#  CONFIG — set these in your .env file
# ============================================================
TOKEN                  = os.getenv("DISCORD_TOKEN", "")
GUILD_ID               = int(os.getenv("GUILD_ID", 0))
TICKET_CATEGORY_ID     = int(os.getenv("TICKET_CATEGORY_ID", 0))   # Discord category new ticket channels go under
TRANSCRIPT_CHANNEL_ID  = int(os.getenv("TRANSCRIPT_CHANNEL_ID", 0)) # log channel for transcripts
STAFF_ROLE_ID          = int(os.getenv("STAFF_ROLE_ID", 0))         # the ONE role allowed to see tickets (also link-whitelisted)

AUTO_CLOSE_HOURS       = float(os.getenv("AUTO_CLOSE_HOURS", 24))   # close after this many hours of inactivity
WARNING_HOURS_BEFORE   = float(os.getenv("WARNING_HOURS_BEFORE", 1)) # send warning this many hours before auto-close

BRAND_NAME             = os.getenv("BRAND_NAME", "Support")
BRAND_LOGO             = os.getenv("BRAND_LOGO", "").strip()        # https:// image url, optional
BRAND_COLOR            = int(os.getenv("BRAND_COLOR", "1A6FFF"), 16)

# Banner image shown at the bottom of the /panel embed (like the "CONTACT US" banner).
# Set this to a direct https:// image link. Easiest way to get one:
# upload the image in any Discord channel, right-click it -> "Copy Link".
PANEL_IMAGE_URL        = os.getenv("PANEL_IMAGE_URL", "").strip()

# Banner image shown inside the welcome embed when a NEW ticket channel opens.
TICKET_OPEN_IMAGE_URL  = os.getenv("TICKET_OPEN_IMAGE_URL", "").strip()

# ------------------------------------------------------------
#  LINK FILTER CONFIG — set these in your .env / Railway variables
# ------------------------------------------------------------
# Channel(s) where links are blocked. Comma-separated list of channel IDs,
# e.g. "123456789,987654321". Leave empty to disable the filter.
LINK_FILTER_CHANNEL_IDS = [
    int(x) for x in os.getenv("LINK_FILTER_CHANNEL_IDS", "").replace(" ", "").split(",")
    if x.strip().isdigit()
]
# The role that gets applied on the 2nd warning (you already created it).
BLACKLIST_ROLE_ID      = int(os.getenv("BLACKLIST_ROLE_ID", 0))
# Channel where all filter actions (warnings, punishments, whitelisting) are logged.
MOD_LOG_CHANNEL_ID     = int(os.getenv("MOD_LOG_CHANNEL_ID", 0))
# Minutes of timeout applied when the warning limit is reached.
LINK_TIMEOUT_MINUTES   = float(os.getenv("LINK_TIMEOUT_MINUTES", 5))
# How many warnings before punishment (2 = warn once, punish on the 2nd).
LINK_WARN_LIMIT        = int(os.getenv("LINK_WARN_LIMIT", 2))

# Matches full URLs, www., discord invites, and bare domains with common TLDs.
LINK_REGEX = re.compile(
    r"(https?://|www\.|discord(?:\.gg|app\.com/invite|\.com/invite)/|"
    r"\b[a-z0-9-]+\.(?:com|net|org|gg|io|xyz|me|co|tv|ru|info|link|shop|store|app|dev|site|online|club|gg|to)\b)",
    re.IGNORECASE,
)

# Banner image shown inside the welcome embed when a NEW ticket channel opens.
# The main body text shown in every new ticket's welcome embed (edit freely).
# {brand} gets replaced with BRAND_NAME automatically.
TICKET_WELCOME_TEXT = (
    "Thank you for creating a support ticket. While you wait for a support "
    "agent to promptly assist you in your inquiry, please state the "
    "following information.\n\n"
    "**While you wait...**\n"
    "👤 While you wait for a support agent, please let us know your "
    "issue/inquiry, and provide clear screenshots if an error has occurred.\n\n"
    "If you are a customer, please try to get assistance from other "
    "customers before opening a ticket.\n\n"
    "**NO MOD WILL REQUEST THE TRANSFER OF A TICKET TO DMS FOR "
    "PAYMENTS. CONTACT A MANAGEMENT IF THIS HAPPENS!**"
)

# ------------------------------------------------------------
# Dropdown categories — edit this list to whatever you need.
# ------------------------------------------------------------
TICKET_CATEGORIES = {
    "support":  {"label": "General Support", "description": "Get help from our staff.",       "emoji": "🎫", "category_env": "TICKET_CATEGORY_ID_SUPPORT"},
    "purchase": {"label": "Purchase",         "description": "Request help with a purchase.",  "emoji": "🛒", "category_env": "TICKET_CATEGORY_ID_PURCHASE"},
    "other":    {"label": "Other",            "description": "Anything else.",                 "emoji": "❓", "category_env": "TICKET_CATEGORY_ID_OTHER"},
}


def get_category_parent(guild: discord.Guild, cat_key: str):
    """Return the Discord category (channel group) a ticket of this type
    should be created under: its own configured category if set, otherwise
    the default TICKET_CATEGORY_ID fallback."""
    cat_info = TICKET_CATEGORIES.get(cat_key, {})
    env_name = cat_info.get("category_env")
    specific_id = os.getenv(env_name, "").strip() if env_name else ""
    chosen_id = specific_id if specific_id else str(TICKET_CATEGORY_ID or "")
    if not chosen_id:
        return None
    channel = guild.get_channel(int(chosen_id))
    return channel if isinstance(channel, discord.CategoryChannel) else None

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
    data.setdefault("tickets", {})          # channel_id(str) -> ticket info
    data.setdefault("ticket_counter", 0)
    data.setdefault("link_whitelist", [])   # list of user IDs (int) allowed to post links
    data.setdefault("link_warnings", {})    # user_id(str) -> warning count (int)
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

# Accept both "!" and "$" prefixes (you use $whitelistuser).
bot = commands.Bot(command_prefix=["!", "$"], intents=intents, help_command=None)


def set_logo(embed: discord.Embed):
    if BRAND_LOGO.startswith("https://"):
        embed.set_thumbnail(url=BRAND_LOGO)


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def is_link_whitelisted(member: discord.Member) -> bool:
    """Admins, the support/staff role, and individually whitelisted users
    are allowed to post links."""
    if member.guild_permissions.administrator:
        return True
    if any(r.id == STAFF_ROLE_ID for r in getattr(member, "roles", [])):
        return True
    if member.id in config.get("link_whitelist", []):
        return True
    return False


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:20] or "ticket"


# ============================================================
#  MOD LOG HELPER
# ============================================================
async def mod_log(guild: discord.Guild, embed: discord.Embed):
    ch = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if ch is None:
        return
    try:
        await ch.send(embed=embed)
    except discord.HTTPException:
        pass


# ============================================================
#  LINK VIOLATION HANDLER
# ============================================================
async def handle_link_violation(message: discord.Message):
    member = message.author
    guild = message.guild

    # Delete the offending message.
    try:
        await message.delete()
    except discord.HTTPException:
        pass

    warns = config.setdefault("link_warnings", {})
    uid = str(member.id)
    warns[uid] = warns.get(uid, 0) + 1
    count = warns[uid]
    save_config(config)

    if count >= LINK_WARN_LIMIT:
        # Reset the counter so it's a fresh cycle after the punishment.
        warns[uid] = 0
        save_config(config)

        timed_out = False
        try:
            await member.timeout(
                timedelta(minutes=LINK_TIMEOUT_MINUTES),
                reason="Link posting (warning limit reached)",
            )
            timed_out = True
        except discord.HTTPException:
            pass  # bot may lack Moderate Members perm or target is higher than bot

        role_added = False
        blk = guild.get_role(BLACKLIST_ROLE_ID)
        if blk is not None:
            try:
                await member.add_roles(blk, reason="Link posting (warning limit reached)")
                role_added = True
            except discord.HTTPException:
                pass  # bot may lack Manage Roles or blacklist role is above bot's top role

        try:
            await message.channel.send(
                f"🚫 {member.mention} du wurdest wegen wiederholtem Link-Posten für "
                f"{LINK_TIMEOUT_MINUTES:g} Minuten stummgeschaltet und auf die Blacklist gesetzt.",
                delete_after=15,
            )
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="🚫 Link-Filter — Bestrafung",
            color=0xE02B2B,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Grund", value="Verwarnungslimit erreicht", inline=True)
        embed.add_field(
            name="Aktion",
            value=(
                f"{'✅' if timed_out else '❌'} Timeout {LINK_TIMEOUT_MINUTES:g} Min\n"
                f"{'✅' if role_added else '❌'} Blacklist-Rolle vergeben"
            ),
            inline=False,
        )
        if not timed_out or not role_added:
            embed.add_field(
                name="⚠️ Hinweis",
                value="Konnte nicht alles ausführen — prüfe die Bot-Rechte (Moderate Members / Manage Roles) und die Rollen-Hierarchie.",
                inline=False,
            )
        await mod_log(guild, embed)
    else:
        try:
            await message.channel.send(
                f"⚠️ {member.mention} Links sind in diesem Channel nicht erlaubt. "
                f"Verwarnung {count}/{LINK_WARN_LIMIT}.",
                delete_after=10,
            )
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="⚠️ Link-Filter — Verwarnung",
            color=0xF0A500,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Verwarnung", value=f"{count}/{LINK_WARN_LIMIT}", inline=True)
        await mod_log(guild, embed)


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
        guild = interaction.guild

        # A modal must be the FIRST response to an interaction, so we can't
        # defer() here — check for an existing open ticket first (fast) and
        # only then show the modal.
        for ch in guild.text_channels:
            if ch.topic and f"uid-{interaction.user.id}" in ch.topic:
                await interaction.response.send_message(f"❌ You already have an open ticket: {ch.mention}", ephemeral=True)
                return

        await interaction.response.send_modal(TicketQuestionsModal(cat_key))


class TicketQuestionsModal(discord.ui.Modal):
    def __init__(self, cat_key: str):
        cat = TICKET_CATEGORIES[cat_key]
        super().__init__(title=f"Open Ticket — {cat['label']}"[:45])
        self.cat_key = cat_key

        self.reason = discord.ui.TextInput(
            label="What is the reason for your request?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )
        self.order_id = discord.ui.TextInput(
            label="What is your order ID?",
            required=False,
            max_length=100,
        )
        self.product = discord.ui.TextInput(
            label="What product do you need help with?",
            required=False,
            max_length=200,
        )
        self.add_item(self.reason)
        self.add_item(self.order_id)
        self.add_item(self.product)

    async def on_submit(self, interaction: discord.Interaction):
        # Now safe to defer — channel creation can take >3s.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await create_ticket_channel(
                interaction,
                cat_key=self.cat_key,
                reason=self.reason.value.strip(),
                order_id=self.order_id.value.strip(),
                product=self.product.value.strip(),
            )
        except Exception as e:
            print(f"[TICKET CREATE ERROR] {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(f"❌ Something went wrong creating your ticket: {e}", ephemeral=True)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[TICKET MODAL ERROR] {type(error).__name__}: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        except discord.HTTPException:
            pass


async def create_ticket_channel(interaction: discord.Interaction, cat_key: str, reason: str, order_id: str, product: str):
    guild = interaction.guild
    cat = TICKET_CATEGORIES[cat_key]

    staff_role = guild.get_role(STAFF_ROLE_ID)
    if staff_role is None:
        await interaction.followup.send(
            "⚠️ No support role configured (STAFF_ROLE_ID is missing/invalid). Ask an admin to fix the Railway variables.",
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

    parent_category = get_category_parent(guild, cat_key)
    config["ticket_counter"] = config.get("ticket_counter", 0) + 1
    num = config["ticket_counter"]

    channel_name = f"{slugify(cat_key)}-{slugify(interaction.user.name)}"

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            category=parent_category,
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
        description=TICKET_WELCOME_TEXT,
        color=BRAND_COLOR,
        timestamp=now,
    )
    if BRAND_LOGO.startswith("https://"):
        embed.set_author(name="Support Ticket", icon_url=BRAND_LOGO)
    else:
        embed.set_author(name="Support Ticket")
    embed.add_field(name="What is the reason for your request?", value=f"> {reason}" if reason else "> —", inline=False)
    embed.add_field(name="What is your order ID?", value=f"> {order_id}" if order_id else "> —", inline=False)
    embed.add_field(name="What product do you need help with?", value=f"> {product}" if product else "> —", inline=False)
    if TICKET_OPEN_IMAGE_URL:
        embed.set_image(url=TICKET_OPEN_IMAGE_URL)
    if BRAND_LOGO.startswith("https://"):
        embed.set_footer(text=f"Support Ticket - {BRAND_NAME}", icon_url=BRAND_LOGO)
    else:
        embed.set_footer(text=f"Support Ticket - {BRAND_NAME}")
    await channel.send(content=None, embed=embed, view=TicketControlView())


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[TICKET PANEL VIEW ERROR] {type(error).__name__}: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        except discord.HTTPException:
            pass


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close_button")
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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[TICKET CONTROL VIEW ERROR] {type(error).__name__}: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        except discord.HTTPException:
            pass


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

    # ---- LINK FILTER (runs before everything else) ----
    if (
        message.channel.id in LINK_FILTER_CHANNEL_IDS
        and not is_link_whitelisted(message.author)
        and LINK_REGEX.search(message.content or "")
    ):
        await handle_link_violation(message)
        return  # stop here — offending message is gone, don't process as command

    # ---- ticket activity tracking ----
    info = config["tickets"].get(str(message.channel.id))
    if info is not None:
        info["last_activity"] = datetime.now(timezone.utc).isoformat()
        info["warned"] = False
        save_config(config)

    await bot.process_commands(message)


# ============================================================
#  PREFIX COMMANDS — LINK WHITELIST
# ============================================================
@bot.command(name="whitelistuser")
async def whitelistuser(ctx: commands.Context, *, arg: str = None):
    """$whitelistuser <ID oder @mention> — erlaubt diesem User, Links zu posten."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Nur Staff kann whitelisten.", mention_author=False)
        return
    if not arg:
        await ctx.reply("Nutzung: `$whitelistuser <ID oder @mention>`", mention_author=False)
        return

    m = re.search(r"\d{15,}", arg)
    if not m:
        await ctx.reply("❌ Keine gültige User-ID gefunden.", mention_author=False)
        return
    uid = int(m.group())

    wl = config.setdefault("link_whitelist", [])
    if uid in wl:
        await ctx.reply(f"ℹ️ <@{uid}> ist bereits gewhitelistet.", mention_author=False)
        return

    wl.append(uid)
    # clear any pending warnings for this user
    config.setdefault("link_warnings", {}).pop(str(uid), None)
    save_config(config)

    await ctx.reply(f"✅ <@{uid}> wurde zur Link-Whitelist hinzugefügt.", mention_author=False)

    embed = discord.Embed(
        title="✅ Link-Whitelist — hinzugefügt",
        color=0x2ECC71,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="User", value=f"<@{uid}> (`{uid}`)", inline=False)
    embed.add_field(name="Von", value=f"{ctx.author.mention}", inline=True)
    await mod_log(ctx.guild, embed)


@bot.command(name="unwhitelistuser")
async def unwhitelistuser(ctx: commands.Context, *, arg: str = None):
    """$unwhitelistuser <ID oder @mention> — entfernt den User wieder von der Whitelist."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Nur Staff kann das.", mention_author=False)
        return
    if not arg:
        await ctx.reply("Nutzung: `$unwhitelistuser <ID oder @mention>`", mention_author=False)
        return

    m = re.search(r"\d{15,}", arg)
    if not m:
        await ctx.reply("❌ Keine gültige User-ID gefunden.", mention_author=False)
        return
    uid = int(m.group())

    wl = config.setdefault("link_whitelist", [])
    if uid not in wl:
        await ctx.reply(f"ℹ️ <@{uid}> ist nicht auf der Whitelist.", mention_author=False)
        return

    wl.remove(uid)
    save_config(config)
    await ctx.reply(f"✅ <@{uid}> wurde von der Link-Whitelist entfernt.", mention_author=False)

    embed = discord.Embed(
        title="➖ Link-Whitelist — entfernt",
        color=0xE67E22,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="User", value=f"<@{uid}> (`{uid}`)", inline=False)
    embed.add_field(name="Von", value=f"{ctx.author.mention}", inline=True)
    await mod_log(ctx.guild, embed)


@bot.command(name="whitelist")
async def whitelist_list(ctx: commands.Context):
    """$whitelist — zeigt alle gewhitelisteten User."""
    if not is_staff(ctx.author):
        await ctx.reply("❌ Nur Staff.", mention_author=False)
        return
    wl = config.get("link_whitelist", [])
    if not wl:
        await ctx.reply("Die Whitelist ist leer.", mention_author=False)
        return
    lines = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in wl)
    embed = discord.Embed(title="📃 Link-Whitelist", description=lines, color=BRAND_COLOR)
    await ctx.reply(embed=embed, mention_author=False)


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
    if PANEL_IMAGE_URL:
        embed.set_image(url=PANEL_IMAGE_URL)

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

    embed = discord.Embed(title="🤖 Ticket Bot Status", color=BRAND_COLOR)
    embed.add_field(name="Log Channel", value=log_channel.mention if log_channel else "⚠️ Not set / invalid", inline=False)
    embed.add_field(name="Support Role", value=staff_role.mention if staff_role else "⚠️ Not set / invalid", inline=False)

    cat_lines = []
    for key, v in TICKET_CATEGORIES.items():
        parent = get_category_parent(guild, key)
        cat_lines.append(f"{v['emoji']} **{v['label']}** → {parent.name if parent else '⚠️ Not set / invalid'}")
    embed.add_field(name="Categories & their Discord Category", value="\n".join(cat_lines), inline=False)

    embed.add_field(name="Open Tickets", value=str(len(config["tickets"])), inline=False)
    embed.add_field(
        name="Auto-Close",
        value=f"Warning {WARNING_HOURS_BEFORE:g}h before, close after {AUTO_CLOSE_HOURS:g}h of inactivity",
        inline=False,
    )

    # ---- link filter status ----
    if LINK_FILTER_CHANNEL_IDS:
        chans = ", ".join(
            (guild.get_channel(cid).mention if guild.get_channel(cid) else f"`{cid}` ⚠️")
            for cid in LINK_FILTER_CHANNEL_IDS
        )
    else:
        chans = "⚠️ Kein Channel gesetzt"
    blk_role = guild.get_role(BLACKLIST_ROLE_ID)
    mod_log_ch = guild.get_channel(MOD_LOG_CHANNEL_ID)
    embed.add_field(
        name="🔗 Link-Filter",
        value=(
            f"**Channels:** {chans}\n"
            f"**Blacklist-Rolle:** {blk_role.mention if blk_role else '⚠️ Not set / invalid'}\n"
            f"**Mod-Log:** {mod_log_ch.mention if mod_log_ch else '⚠️ Not set / invalid'}\n"
            f"**Timeout:** {LINK_TIMEOUT_MINUTES:g} Min bei {LINK_WARN_LIMIT} Verwarnungen\n"
            f"**Gewhitelistete User:** {len(config.get('link_whitelist', []))}"
        ),
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
