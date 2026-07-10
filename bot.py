# ============================================================
#  SERVER STRUCTURE BACKUP BOT — bot.py
#  Sichert die STRUKTUR deines EIGENEN Servers und stellt sie
#  auf einem leeren Server wieder her.
#
#  Gesichert wird:  Kategorien, Text-/Voice-Channels + deren
#                   Einstellungen (Topic, NSFW, Slowmode, Bitrate,
#                   User-Limit, Position).
#  NICHT gesichert: Nachrichten, Mitglieder, Rollen. (Bewusst so —
#                   ein Struktur-Backup braucht keine Nutzerdaten.)
#
#  Commands (nur Admins):
#    /backup           -> exportiert die Struktur als JSON-Datei
#    /restore          -> baut Struktur aus angehängtem JSON auf
#                         (nur auf einem praktisch leeren Server)
# ============================================================

import discord
from discord.ext import commands
from discord import app_commands
import json
import io
import os
from datetime import datetime, timezone

TOKEN = os.getenv("DISCORD_TOKEN", "")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ------------------------------------------------------------
#  BACKUP  — Struktur eines Servers in ein dict serialisieren
# ------------------------------------------------------------
def serialize_overwrites(channel: discord.abc.GuildChannel):
    """Permission-Overwrites als Liste speichern. Nur Overwrites für
    @everyone werden übernommen, da wir ohne Rollen restaurieren —
    rollenbezogene Overwrites lassen sich auf dem leeren Server nicht
    sinnvoll wiederherstellen und werden ausgelassen."""
    result = []
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role) and target.is_default():
            allow, deny = ow.pair()
            result.append({
                "type": "everyone",
                "allow": allow.value,
                "deny": deny.value,
            })
    return result


def backup_guild(guild: discord.Guild) -> dict:
    data = {
        "meta": {
            "guild_name": guild.name,
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
            "source_guild_id": str(guild.id),
            "format": "structure-only-v1",
        },
        "categories": [],
        "standalone_channels": [],  # Channels ohne Kategorie
    }

    # Kategorien mit ihren Kindern
    for category in sorted(guild.categories, key=lambda c: c.position):
        cat_entry = {
            "name": category.name,
            "position": category.position,
            "overwrites": serialize_overwrites(category),
            "channels": [],
        }
        for ch in sorted(category.channels, key=lambda c: c.position):
            cat_entry["channels"].append(serialize_channel(ch))
        data["categories"].append(cat_entry)

    # Channels ohne Kategorie
    for ch in guild.channels:
        if ch.category is None and not isinstance(ch, discord.CategoryChannel):
            data["standalone_channels"].append(serialize_channel(ch))

    return data


def serialize_channel(ch: discord.abc.GuildChannel) -> dict:
    base = {
        "name": ch.name,
        "position": ch.position,
        "overwrites": serialize_overwrites(ch),
    }
    if isinstance(ch, discord.TextChannel):
        base.update({
            "type": "text",
            "topic": ch.topic or "",
            "nsfw": ch.nsfw,
            "slowmode_delay": ch.slowmode_delay,
        })
    elif isinstance(ch, discord.VoiceChannel):
        base.update({
            "type": "voice",
            "bitrate": ch.bitrate,
            "user_limit": ch.user_limit,
        })
    else:
        base["type"] = "other"
    return base


# ------------------------------------------------------------
#  RESTORE  — Struktur aus dict auf einem leeren Server aufbauen
# ------------------------------------------------------------
def everyone_overwrite_from(entry, guild: discord.Guild):
    """Baut ein {role: PermissionOverwrite}-dict nur für @everyone."""
    overwrites = {}
    for ow in entry.get("overwrites", []):
        if ow.get("type") == "everyone":
            allow = discord.Permissions(ow.get("allow", 0))
            deny = discord.Permissions(ow.get("deny", 0))
            overwrites[guild.default_role] = discord.PermissionOverwrite.from_pair(allow, deny)
    return overwrites


async def restore_guild(guild: discord.Guild, data: dict) -> dict:
    created = {"categories": 0, "text": 0, "voice": 0, "skipped": 0}

    async def make_channel(entry, category):
        ow = everyone_overwrite_from(entry, guild)
        ctype = entry.get("type")
        try:
            if ctype == "text":
                await guild.create_text_channel(
                    name=entry["name"],
                    category=category,
                    topic=entry.get("topic") or None,
                    nsfw=entry.get("nsfw", False),
                    slowmode_delay=entry.get("slowmode_delay", 0),
                    overwrites=ow,
                )
                created["text"] += 1
            elif ctype == "voice":
                await guild.create_voice_channel(
                    name=entry["name"],
                    category=category,
                    bitrate=min(entry.get("bitrate", 64000), guild.bitrate_limit),
                    user_limit=entry.get("user_limit", 0),
                    overwrites=ow,
                )
                created["voice"] += 1
            else:
                created["skipped"] += 1
        except discord.HTTPException as e:
            print(f"[RESTORE] Channel '{entry.get('name')}' übersprungen: {e}")
            created["skipped"] += 1

    # Standalone-Channels zuerst
    for entry in data.get("standalone_channels", []):
        await make_channel(entry, None)

    # Kategorien + Kinder
    for cat_entry in data.get("categories", []):
        try:
            category = await guild.create_category(
                name=cat_entry["name"],
                overwrites=everyone_overwrite_from(cat_entry, guild),
            )
            created["categories"] += 1
        except discord.HTTPException as e:
            print(f"[RESTORE] Kategorie '{cat_entry.get('name')}' fehlgeschlagen: {e}")
            continue
        for entry in cat_entry.get("channels", []):
            await make_channel(entry, category)

    return created


# ------------------------------------------------------------
#  COMMANDS
# ------------------------------------------------------------
@bot.tree.command(name="backup", description="Struktur dieses Servers als JSON exportieren (Admin)")
@app_commands.guild_only()
async def cmd_backup(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Nur Admins.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    data = backup_guild(interaction.guild)
    raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    fname = f"backup-{interaction.guild.name}-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    fname = "".join(c if c.isalnum() or c in "-_." else "_" for c in fname)

    n_cat = len(data["categories"])
    n_ch = sum(len(c["channels"]) for c in data["categories"]) + len(data["standalone_channels"])
    await interaction.followup.send(
        f"✅ Backup erstellt: **{n_cat} Kategorien**, **{n_ch} Channels**.\n"
        "Lade die Datei herunter und bewahre sie sicher auf. Zum Wiederherstellen "
        "auf einem leeren Server: `/restore` und die Datei anhängen.",
        file=discord.File(io.BytesIO(raw), filename=fname),
        ephemeral=True,
    )


@bot.tree.command(name="restore", description="Struktur aus JSON auf DIESEM (leeren) Server aufbauen (Admin)")
@app_commands.describe(file="Die zuvor mit /backup erstellte JSON-Datei")
@app_commands.guild_only()
async def cmd_restore(interaction: discord.Interaction, file: discord.Attachment):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Nur Admins.", ephemeral=True)
        return

    guild = interaction.guild

    # Sicherheitsnetz: Restore nur auf praktisch leerem Server, damit kein
    # aktiver Server ausversehen zugemüllt/überschrieben wird.
    existing = [c for c in guild.channels]
    if len(existing) > 3:
        await interaction.response.send_message(
            f"⛔ Dieser Server hat bereits {len(existing)} Channels. "
            "Restore läuft nur auf einem (fast) leeren Server, damit nichts "
            "überschrieben wird. Nutze einen frischen Server.",
            ephemeral=True,
        )
        return

    if not file.filename.endswith(".json"):
        await interaction.response.send_message("❌ Bitte eine `.json`-Backup-Datei anhängen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        await interaction.followup.send(f"❌ Datei konnte nicht gelesen werden: {e}", ephemeral=True)
        return

    if data.get("meta", {}).get("format") != "structure-only-v1":
        await interaction.followup.send(
            "❌ Unbekanntes Backup-Format. Bitte eine mit diesem Bot erstellte Datei nutzen.",
            ephemeral=True,
        )
        return

    result = await restore_guild(guild, data)
    await interaction.followup.send(
        "✅ Restore fertig:\n"
        f"• Kategorien: **{result['categories']}**\n"
        f"• Text-Channels: **{result['text']}**\n"
        f"• Voice-Channels: **{result['voice']}**\n"
        f"• Übersprungen: **{result['skipped']}**\n\n"
        "Hinweis: Rollen wurden bewusst nicht wiederhergestellt — "
        "rollenbezogene Rechte musst du neu setzen.",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    print(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} Slash-Commands synchronisiert")
    except discord.HTTPException as e:
        print(f"❌ Sync-Fehler: {e}")
    print("✅ Backup-Bot bereit.")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN ist nicht gesetzt. In die .env / Railway-Variablen eintragen.")
    bot.run(TOKEN)
