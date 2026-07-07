# ===========================================================
#  TICKET BOT — bot.py
#  Ticket system with:
#   - Dropdown panel with configurable categories
#   - Single support role (+ server admins) can see tickets
#   - HTML transcript posted to a log channel on close
#   - Auto-close after 24h of inactivity, with a warning 1h before
#   - Moderation add-on: role whitelist, spam protection, link filter
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
STAFF_ROLE_ID          = int(os.getenv("STAFF_ROLE_ID", 0))         # the ONE role allowed to see tickets

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

# /guide command — posts a link (e.g. a tutorial) in an embed.
GUIDE_TITLE            = os.getenv("GUIDE_TITLE", "General Tutorial")
GUIDE_URL              = os.getenv("GUIDE_URL", "").strip()

# ------------------------------------------------------------
# Moderation add-on settings (whitelist roles, spam protection, link filter)
# ------------------------------------------------------------
SPAM_MESSAGE_LIMIT     = int(os.getenv("SPAM_MESSAGE_LIMIT", 5))     # max messages...
SPAM_TIME_WINDOW       = int(os.getenv("SPAM_TIME_WINDOW", 5))       # ...within this many seconds
SPAM_TIMEOUT_DURATION  = int(os.getenv("SPAM_TIMEOUT_DURATION", 60)) # timeout length (seconds) for spam
LINK_TIMEOUT_DURATION  = int(os.getenv("LINK_TIMEOUT_DURATION", 60)) # timeout length (seconds) for posting links
CHAT_BLACKLIST_ROLE_ID = int(os.getenv("CHAT_BLACKLIST_ROLE_ID", 0)) # role auto-assigned to invite-link violators
MOD_FILTER_CHANNEL_ID  = int(os.getenv("MOD_FILTER_CHANNEL_ID", 0))  # if set, link/spam filter ONLY applies in this channel
STRIKES_BEFORE_TIMEOUT = int(os.getenv("STRIKES_BEFORE_TIMEOUT", 3)) # warnings allowed before a timeout is applied

URL_REGEX = re.compile(r"https?://\S+|discord\.gg/\S+", re.IGNORECASE)

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
# key: internal id (used in channel names/topics, don't change once live)
# label/description/emoji: shown in the dropdown
# category_env: the Railway variable name that holds the Discord parent
#               category ID this ticket type's channels get created under.
#               If that variable is empty/unset, TICKET_CATEGORY_ID (the
#               default fallback below) is used instead.
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
    data.setdefault("tickets", {})       # channel_id(str) -> ticket info
    data.setdefault("ticket_counter", 0)
    data.setdefault("log_channel_id", None)      # moderation log channel
    data.setdefault("whitelist_role_ids", [])    # roles exempt from spam/link filter
    data.setdefault("mod_strikes", {})           # user_id(str) -> {"link": int, "spam": int}
    return data


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


config = load_config()

# In-memory spam tracking: user_id -> list of recent message timestamps (epoch seconds)
user_message_times = {}

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


def is_mod_whitelisted(member: discord.Member) -> bool:
    """Members exempt from spam protection / link filtering."""
    if member.guild_permissions.administrator:
        return True
    whitelisted_ids = config.get("whitelist_role_ids", [])
    return any(r.id in whitelisted_ids for r in member.roles)


async def send_log(guild: discord.Guild, embed: discord.Embed):
    """Send a moderation log embed to the configured log channel, if any."""
    channel_id = config.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print("Missing permissions to send in the moderation log channel.")


def bump_strike(user_id: int, kind: str) -> int:
    """Increment and persist a user's warning count for a given violation kind
    ('link' or 'spam'). Returns the new count."""
    strikes = config.setdefault("mod_strikes", {})
    user_strikes = strikes.setdefault(str(user_id), {"link": 0, "spam": 0})
    user_strikes[kind] = user_strikes.get(kind, 0) + 1
    save_config(config)
    return user_strikes[kind]


def reset_strike(user_id: int, kind: str):
    strikes = config.setdefault("mod_strikes", {})
    user_strikes = strikes.setdefault(str(user_id), {"link": 0, "spam": 0})
    user_strikes[kind] = 0
    save_config(config)


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

    # ------------------------------------------------------------
    # Moderation add-on: link filter + spam protection
    # (skipped for admins / whitelisted roles, and only runs in the
    # channel configured via MOD_FILTER_CHANNEL_ID, if one is set)
    # ------------------------------------------------------------
    filter_applies_here = (not MOD_FILTER_CHANNEL_ID) or (message.channel.id == MOD_FILTER_CHANNEL_ID)

    if filter_applies_here and isinstance(message.author, discord.Member) and not is_mod_whitelisted(message.author):
        # Link filter
        if URL_REGEX.search(message.content or ""):
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

            strike_count = bump_strike(message.author.id, "link")

            if strike_count < STRIKES_BEFORE_TIMEOUT:
                # Just a warning — no timeout yet
                try:
                    await message.channel.send(
                        f"⚠️ {message.author.mention} posting links/invites isn't allowed here. "
                        f"Warning **{strike_count}/{STRIKES_BEFORE_TIMEOUT}** — one more and you'll be timed out."
                    )
                except discord.HTTPException:
                    pass
                embed = discord.Embed(
                    title="⚠️ Link Warning",
                    description=(
                        f"{message.author.mention} posted a link in {message.channel.mention} "
                        f"(warning {strike_count}/{STRIKES_BEFORE_TIMEOUT})."
                    ),
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc),
                )
                await send_log(message.guild, embed)
                return

            # Threshold reached — reset the counter and actually punish
            reset_strike(message.author.id, "link")
            try:
                await message.author.timeout(
                    timedelta(seconds=LINK_TIMEOUT_DURATION), reason="Repeated invite links"
                )
            except discord.Forbidden:
                pass

            blacklist_note = ""
            if CHAT_BLACKLIST_ROLE_ID:
                blacklist_role = message.guild.get_role(CHAT_BLACKLIST_ROLE_ID)
                if blacklist_role is not None:
                    try:
                        await message.author.add_roles(blacklist_role, reason="Posted an invite link")
                        blacklist_note = f", and gave them {blacklist_role.mention}"
                    except discord.Forbidden:
                        print("Missing permissions to assign the chat blacklist role.")
                else:
                    print("CHAT_BLACKLIST_ROLE_ID is set but no such role was found in this server.")

            embed = discord.Embed(
                title="🔗 Link Filter",
                description=(
                    f"{message.author.mention} reached {STRIKES_BEFORE_TIMEOUT} link warnings in "
                    f"{message.channel.mention} — applied a {LINK_TIMEOUT_DURATION}s timeout{blacklist_note}."
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            await send_log(message.guild, embed)
            return  # don't process commands from a filtered message

        # Spam protection
        now_ts = datetime.now(timezone.utc).timestamp()
        recent = user_message_times.setdefault(message.author.id, [])
        recent.append(now_ts)
        recent = [t for t in recent if now_ts - t <= SPAM_TIME_WINDOW]
        user_message_times[message.author.id] = recent

        if len(recent) > SPAM_MESSAGE_LIMIT:
            user_message_times[message.author.id] = []  # reset so we don't re-trigger every message

            strike_count = bump_strike(message.author.id, "spam")

            if strike_count < STRIKES_BEFORE_TIMEOUT:
                try:
                    await message.channel.send(
                        f"⚠️ {message.author.mention} please slow down. "
                        f"Warning **{strike_count}/{STRIKES_BEFORE_TIMEOUT}** — one more and you'll be timed out."
                    )
                except discord.HTTPException:
                    pass
                embed = discord.Embed(
                    title="⚠️ Spam Warning",
                    description=(
                        f"{message.author.mention} sent more than {SPAM_MESSAGE_LIMIT} messages in "
                        f"{SPAM_TIME_WINDOW}s in {message.channel.mention} "
                        f"(warning {strike_count}/{STRIKES_BEFORE_TIMEOUT})."
                    ),
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc),
                )
                await send_log(message.guild, embed)
            else:
                reset_strike(message.author.id, "spam")
                try:
                    await message.author.timeout(
                        timedelta(seconds=SPAM_TIMEOUT_DURATION), reason="Repeated spamming"
                    )
                except discord.Forbidden:
                    pass
                embed = discord.Embed(
                    title="🚫 Spam Protection",
                    description=(
                        f"{message.author.mention} reached {STRIKES_BEFORE_TIMEOUT} spam warnings in "
                        f"{message.channel.mention} — timed out for {SPAM_TIMEOUT_DURATION}s."
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                await send_log(message.guild, embed)

    # ------------------------------------------------------------
    # Ticket activity tracking
    # ------------------------------------------------------------
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


@bot.tree.command(name="guide", description="Show the general tutorial link")
@app_commands.guild_only()
async def cmd_guide(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return

    if not GUIDE_URL:
        await interaction.response.send_message(
            "⚠️ No guide URL configured (set GUIDE_URL in the Railway variables).",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=GUIDE_TITLE,
        description=GUIDE_URL,
        color=BRAND_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    if BRAND_LOGO.startswith("https://"):
        embed.set_footer(text=f"Ticket Support - {BRAND_NAME}", icon_url=BRAND_LOGO)
    else:
        embed.set_footer(text=f"Ticket Support - {BRAND_NAME}")

    await interaction.response.send_message(embed=embed)


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
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
#  MODERATION ADD-ON — PREFIX COMMANDS (!setlog, !whitelistroles, !status)
# ============================================================
@bot.command(name="setlog")
@commands.has_permissions(manage_guild=True)
async def setlog(ctx: commands.Context, channel_id: str = None):
    """Set the channel moderation actions (spam/link filter) get logged to."""
    if channel_id is None:
        await ctx.send("Usage: `!setlog <channel_id>`")
        return
    try:
        cid = int(channel_id)
    except ValueError:
        await ctx.send("Please provide a valid channel ID (numbers only).")
        return
    channel = ctx.guild.get_channel(cid)
    if channel is None:
        await ctx.send("I can't find a channel with that ID in this server.")
        return
    config["log_channel_id"] = cid
    save_config(config)
    await ctx.send(f"✅ Moderation log channel set to {channel.mention}.")


@setlog.error
async def setlog_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!setlog <channel_id>`")


@bot.command(name="whitelistroles")
@commands.has_permissions(manage_guild=True)
async def whitelistroles(ctx: commands.Context, role_id: str = None):
    """Toggle a role in/out of the moderation whitelist (exempt from spam/link filter)."""
    if role_id is None:
        await ctx.send("Usage: `!whitelistroles <role_id>`")
        return
    try:
        role_id_int = int(role_id)
    except ValueError:
        await ctx.send("Please provide a valid role ID (numbers only).")
        return
    role = ctx.guild.get_role(role_id_int)
    if role is None:
        await ctx.send("I can't find a role with that ID in this server.")
        return
    whitelisted_ids = config.setdefault("whitelist_role_ids", [])
    if role_id_int in whitelisted_ids:
        whitelisted_ids.remove(role_id_int)
        save_config(config)
        await ctx.send(f"➖ Role {role.mention} removed from the moderation whitelist.")
    else:
        whitelisted_ids.append(role_id_int)
        save_config(config)
        await ctx.send(f"✅ Role {role.mention} added to the moderation whitelist.")
    embed = discord.Embed(
        title="🛡️ Whitelist Updated",
        description=f"Role: {role.mention}\nStatus: {'Whitelisted' if role_id_int in whitelisted_ids else 'Removed from whitelist'}",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    await send_log(ctx.guild, embed)


@whitelistroles.error
async def whitelistroles_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!whitelistroles <role_id>`")


@bot.command(name="status")
async def status(ctx: commands.Context):
    """Show current moderation add-on configuration."""
    channel_id = config.get("log_channel_id")
    channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None
    whitelisted_ids = config.get("whitelist_role_ids", [])
    role_mentions = []
    for rid in whitelisted_ids:
        role = ctx.guild.get_role(int(rid))
        if role:
            role_mentions.append(role.mention)
    roles_text = ", ".join(role_mentions) if role_mentions else "None set (use `!whitelistroles <role_id>`)"
    embed = discord.Embed(title="🤖 Bot Status", color=discord.Color.blurple())
    embed.add_field(
        name="Log Channel",
        value=channel.mention if channel else "Not set (use `!setlog <channel_id>`)",
        inline=False,
    )
    filter_channel = ctx.guild.get_channel(MOD_FILTER_CHANNEL_ID) if MOD_FILTER_CHANNEL_ID else None
    embed.add_field(
        name="Filter Active In",
        value=filter_channel.mention if filter_channel else "All channels (MOD_FILTER_CHANNEL_ID not set)",
        inline=False,
    )
    embed.add_field(
        name="Spam Protection",
        value=(
            f"More than {SPAM_MESSAGE_LIMIT} messages in {SPAM_TIME_WINDOW}s → warning "
            f"(timeout {SPAM_TIMEOUT_DURATION}s after {STRIKES_BEFORE_TIMEOUT} warnings)"
        ),
        inline=False,
    )
    blacklist_role = ctx.guild.get_role(CHAT_BLACKLIST_ROLE_ID) if CHAT_BLACKLIST_ROLE_ID else None
    embed.add_field(
        name="Link Filter",
        value=(
            f"Warning after each link, timeout ({LINK_TIMEOUT_DURATION}s) after {STRIKES_BEFORE_TIMEOUT} warnings"
            f"{f' + {blacklist_role.mention} role' if blacklist_role else ''}"
        ),
        inline=False,
    )
    embed.add_field(name="Whitelisted Roles", value=roles_text, inline=False)
    await ctx.send(embed=embed)


# ============================================================
#  RUN
# ============================================================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(TOKEN)
