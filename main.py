"""
Discord Backup-Bot
==================
Befehle:
  /backup channel:<Kanal> amount:<Anzahl>   -> Speichert die letzten X Nachrichten eines Kanals in eine JSON-Datei
  /backup_all amount:<Anzahl pro Kanal>     -> Sichert alle Textkanäle des Servers
  /restore channel:<Kanal> backup:<Name>    -> Stellt Nachrichten aus einem Backup in einen Kanal wieder her
  /backups                                  -> Listet alle vorhandenen Backups auf

Installation:
  pip install -U discord.py

Start:
  1. Bot-Token unten bei TOKEN eintragen (oder als Umgebungsvariable DISCORD_TOKEN setzen)
  2. python backup_bot.py

Wichtig: Der Bot braucht die Berechtigungen "Read Message History",
"Send Messages", "Manage Webhooks" und die "Message Content Intent"
(im Developer Portal unter Bot -> Privileged Gateway Intents aktivieren).
"""

import os
import json
import discord
from discord import app_commands
from datetime import datetime

TOKEN = os.getenv("DISCORD_TOKEN", "HIER_DEIN_TOKEN")
BACKUP_DIR = "backups"

os.makedirs(BACKUP_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True  # nötig, um Nachrichteninhalte zu lesen

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def backup_path(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
    return os.path.join(BACKUP_DIR, f"{safe}.json")


async def save_channel(channel: discord.TextChannel, amount: int) -> dict:
    """Liest die letzten <amount> Nachrichten eines Kanals aus."""
    messages = []
    async for msg in channel.history(limit=amount, oldest_first=True):
        messages.append({
            "author_name": msg.author.display_name,
            "author_avatar": str(msg.author.display_avatar.url),
            "content": msg.content,
            "timestamp": msg.created_at.isoformat(),
            "attachments": [a.url for a in msg.attachments],
            "embeds": [e.to_dict() for e in msg.embeds],
        })
    return {
        "channel_name": channel.name,
        "channel_topic": channel.topic,
        "saved_at": datetime.utcnow().isoformat(),
        "guild_name": channel.guild.name,
        "messages": messages,
    }


@client.event
async def on_ready():
    await tree.sync()
    print(f"Eingeloggt als {client.user} – Slash-Commands synchronisiert.")


@tree.command(name="backup", description="Sichert die letzten Nachrichten eines Kanals")
@app_commands.describe(
    channel="Der Kanal, der gesichert werden soll",
    amount="Wie viele Nachrichten gesichert werden sollen (max. 1000)",
)
@app_commands.checks.has_permissions(administrator=True)
async def backup(interaction: discord.Interaction, channel: discord.TextChannel, amount: int):
    if amount < 1 or amount > 1000:
        await interaction.response.send_message("Bitte eine Anzahl zwischen 1 und 1000 angeben.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    data = await save_channel(channel, amount)
    path = backup_path(channel.name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    await interaction.followup.send(
        f"✅ **{len(data['messages'])}** Nachrichten aus {channel.mention} gesichert als `{os.path.basename(path)}`.",
        ephemeral=True,
    )


@tree.command(name="backup_all", description="Sichert alle Textkanäle des Servers")
@app_commands.describe(amount="Nachrichten pro Kanal (max. 500)")
@app_commands.checks.has_permissions(administrator=True)
async def backup_all(interaction: discord.Interaction, amount: int = 100):
    if amount < 1 or amount > 500:
        await interaction.response.send_message("Bitte eine Anzahl zwischen 1 und 500 angeben.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    count = 0
    for channel in interaction.guild.text_channels:
        try:
            data = await save_channel(channel, amount)
            with open(backup_path(channel.name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            count += 1
        except discord.Forbidden:
            continue  # keine Leserechte in diesem Kanal

    await interaction.followup.send(f"✅ {count} Kanäle gesichert.", ephemeral=True)


@tree.command(name="backups", description="Zeigt alle vorhandenen Backups")
@app_commands.checks.has_permissions(administrator=True)
async def backups(interaction: discord.Interaction):
    files = [f[:-5] for f in os.listdir(BACKUP_DIR) if f.endswith(".json")]
    if not files:
        await interaction.response.send_message("Keine Backups vorhanden.", ephemeral=True)
        return
    await interaction.response.send_message(
        "**Vorhandene Backups:**\n" + "\n".join(f"• `{f}`" for f in files), ephemeral=True
    )


@tree.command(name="restore", description="Stellt ein Backup in einem Kanal wieder her")
@app_commands.describe(
    channel="Zielkanal (z.B. auf dem neuen Server)",
    backup="Name des Backups (Kanalname vom alten Server)",
)
@app_commands.checks.has_permissions(administrator=True)
async def restore(interaction: discord.Interaction, channel: discord.TextChannel, backup: str):
    path = backup_path(backup)
    if not os.path.exists(path):
        await interaction.response.send_message(
            f"❌ Backup `{backup}` nicht gefunden. Nutze `/backups` für eine Liste.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Webhook nutzen, damit Name + Avatar der Original-Autoren angezeigt werden
    webhook = await channel.create_webhook(name="Backup-Restore")

    restored = 0
    try:
        for msg in data["messages"]:
            content = msg["content"] or ""
            # Anhänge als Links anhängen
            if msg["attachments"]:
                content += "\n" + "\n".join(msg["attachments"])
            if not content.strip() and not msg["embeds"]:
                continue

            embeds = [discord.Embed.from_dict(e) for e in msg["embeds"][:10]]
            await webhook.send(
                content=content[:2000] or None,
                username=msg["author_name"][:80],
                avatar_url=msg["author_avatar"],
                embeds=embeds,
            )
            restored += 1
    finally:
        await webhook.delete()

    await interaction.followup.send(
        f"✅ **{restored}** Nachrichten aus `{backup}` in {channel.mention} wiederhergestellt.",
        ephemeral=True,
    )


@backup.error
@backup_all.error
@restore.error
@backups.error
async def on_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Nur Administratoren dürfen diesen Befehl nutzen.", ephemeral=True)
    else:
        raise error


client.run(TOKEN)
