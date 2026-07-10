# Server Structure Backup Bot

Sichert die **Struktur** deines eigenen Discord-Servers (Kategorien + Channels mit Einstellungen) als JSON und baut sie auf einem leeren Server wieder auf.

**Nicht** Teil des Backups: Nachrichten, Mitglieder, Rollen. Ein Struktur-Backup braucht keine Nutzerdaten.

## Commands (nur Admins)
- `/backup` — exportiert die Struktur als JSON-Datei (kommt privat/ephemeral)
- `/restore` (JSON anhängen) — baut Kategorien + Channels auf **diesem** Server auf

## Sicherheitsnetze
- `/restore` läuft nur auf einem (fast) leeren Server (max. 3 bestehende Channels), damit kein aktiver Server überschrieben wird.
- Beide Commands sind auf Server-Administratoren beschränkt.
- Restore prüft das Backup-Format, bevor es etwas anlegt.

## Bot erstellen
1. https://discord.com/developers/applications → New Application → Bot.
2. Bot-Token kopieren (unter "Bot" → "Reset Token").
3. Unter "Installation" / OAuth2 den Bot mit **Manage Channels** einladen
   (zum Wiederherstellen braucht er das Recht, Channels/Kategorien anzulegen).
4. Auf dem Zielserver muss die Bot-Rolle hoch genug stehen, um Channels zu erstellen.

## Deployment auf Railway
1. Diese Dateien in ein GitHub-Repo, dann in Railway "Deploy from GitHub".
2. Variable setzen: `DISCORD_TOKEN` = dein Bot-Token.
3. Deploy. Der Bot läuft als Worker (siehe Procfile).

## Lokal testen
```
pip install -r requirements.txt
export DISCORD_TOKEN=dein_token      # Windows: set DISCORD_TOKEN=...
python bot.py
```

## Typischer Ablauf
1. Auf deinem Hauptserver: `/backup` → JSON-Datei herunterladen und sicher aufbewahren.
2. Neuen, leeren Server erstellen, Bot einladen.
3. Dort `/restore` und die JSON-Datei anhängen → Struktur wird aufgebaut.
4. Rollen und rollenbezogene Rechte danach manuell neu setzen.
