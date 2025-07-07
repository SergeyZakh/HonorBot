"""
Discord HonorBot
Optimiert nach Clean Code, PEP8, Best Practices und mit klarer Architektur.
"""
import os
import logging
import sqlite3
import re
import asyncio
import datetime
from typing import Optional, Tuple, List
import discord
from discord.ext import commands
from discord import app_commands, Embed
from dotenv import load_dotenv
from discord.utils import get
from discord.ui import View, button
import requests
import random

# --- Konfiguration & Konstanten ---
load_dotenv()
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
DB_PATH = os.environ.get("HONOR_DB_PATH", "honor.db")
HONOR_MIN = -144_000
HONOR_MAX = 144_000
BLESS_AMOUNT = 100_000
INSULT_API_URL = "https://www.purgomalum.com/service/containsprofanity?text="

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HonorBot")

# --- Import eigene Datenmodule ---
try:
    from .roles import RANKS
    from .badwords import local_insults
except ImportError:
    from roles import RANKS
    from badwords import local_insults

# --- Datenbankzugriff (Repository Pattern) ---
class HonorRepository:
    """Kapselt alle DB-Operationen für Honor und Logs."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_honor (
                    user_id INTEGER PRIMARY KEY,
                    honor INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS honor_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    delta INTEGER,
                    reason TEXT,
                    by_user INTEGER,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bonus (
                    user_id INTEGER PRIMARY KEY,
                    last_claimed DATE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS loot_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reward INTEGER
                )
                """
            )
            conn.commit()

    def get_user_honor(self, user_id: int) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT honor FROM user_honor WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def set_user_honor(self, user_id: int, value: int):
        clamped = max(HONOR_MIN, min(HONOR_MAX, value))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO user_honor(user_id, honor) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET honor = excluded.honor",
                (user_id, clamped),
            )
            conn.commit()

    def add_user_honor(self, user_id: int, delta: int, reason: str = "", by: int = None):
        current = self.get_user_honor(user_id)
        self.set_user_honor(user_id, current + delta)
        self.log_honor_change(user_id, delta, reason, by)

    def log_honor_change(self, user_id: int, delta: int, reason: str, by: int = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO honor_log (user_id, delta, reason, by_user) VALUES (?, ?, ?, ?)",
                (user_id, delta, reason, by)
            )
            conn.commit()

    def get_achievement_count(self, user_id: int, reason: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM honor_log WHERE user_id = ? AND reason LIKE ?",
                (user_id, f"%{reason}%")
            ).fetchone()[0]

    def is_top1(self, user_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT user_id FROM user_honor ORDER BY honor DESC LIMIT 1").fetchone()
            return row and row[0] == user_id

    def get_honor_log(self, user_id: int, limit: int = 10) -> List[Tuple[int, str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT delta, reason, ts FROM honor_log WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()

    def get_honor_log_admin(self, user_id: int, limit: int = 20) -> List[Tuple[int, str, int, str]]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT delta, reason, by_user, ts FROM honor_log WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()

    def get_leaderboard(self, top: int = 10) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
        with sqlite3.connect(self.db_path) as conn:
            top_users = conn.execute("SELECT user_id, honor FROM user_honor ORDER BY honor DESC LIMIT ?", (top,)).fetchall()
            flop_users = conn.execute("SELECT user_id, honor FROM user_honor ORDER BY honor ASC LIMIT ?", (top,)).fetchall()
            return top_users, flop_users

    def can_claim_daily(self, user_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT last_claimed FROM daily_bonus WHERE user_id = ?", (user_id,)).fetchone()
            today = datetime.date.today().isoformat()
            return not row or row[0] != today

    def claim_daily(self, user_id: int):
        today = datetime.date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO daily_bonus(user_id, last_claimed) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_claimed = excluded.last_claimed",
                (user_id, today)
            )
            conn.commit()

    def can_open_lootbox(self, user_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT ts FROM loot_log WHERE user_id = ? ORDER BY ts DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if not row:
                return True
            last = datetime.datetime.fromisoformat(row[0])
            return (datetime.datetime.now() - last).total_seconds() >= 86400

    def log_lootbox(self, user_id: int, reward: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO loot_log(user_id, reward) VALUES (?, ?)",
                (user_id, reward)
            )
            conn.commit()

repo = HonorRepository(DB_PATH)

# --- Beleidigungserkennung ---
async def is_insult(message: str) -> Optional[str]:
    """Prüft, ob die Nachricht ein Wort aus der Badword-Liste enthält."""
    msg = re.sub(r'[^\wäöüß ]', '', message.lower())
    words = msg.split()
    for insult in local_insults:
        i_norm = insult.lower().strip()
        if i_norm in words or i_norm in msg:
            return insult
    # Prüfe mit externer API (asynchron)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(INSULT_API_URL + requests.utils.quote(message)) as resp:
                if resp.status == 200:
                    result = await resp.text()
                    if result.strip().lower() == "true":
                        return "(externe Beleidigung erkannt)"
    except Exception as e:
        logger.warning(f"Profanity-API nicht erreichbar: {e}")
    return None

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Hilfsfunktionen für Ränge und Fortschritt ---
def get_rank(honor: int) -> Tuple[str, str, discord.Colour]:
    """Gibt Name, Emoji und Farbe des aktuellen Rangs zurück."""
    for threshold, name, emoji, color in sorted(RANKS, key=lambda x: x[0], reverse=True):
        if honor >= threshold:
            return name, emoji, color
    return RANKS[0][1], RANKS[0][2], RANKS[0][3]

def get_progress_bar(honor: int) -> str:
    """Erzeugt einen Fortschrittsbalken für das aktuelle Honor."""
    sorted_ranks = sorted(RANKS, key=lambda x: x[0])
    prev = sorted_ranks[0]
    next_rank = sorted_ranks[-1]
    for r in sorted_ranks:
        if honor < r[0]:
            next_rank = r
            break
        prev = r
    span = next_rank[0] - prev[0]
    pos = honor - prev[0]
    percent = 0 if span == 0 else min(max(pos / span, 0), 1)
    bar_len = 16
    filled = int(percent * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"{bar} {percent*100:.1f}% ({honor-prev[0]}/{span} bis {next_rank[2]} {next_rank[1]})"

# --- Achievements ---
ACHIEVEMENTS = [
    ("helper", "🤝", "100x geholfen", lambda uid: repo.get_achievement_count(uid, "helped") >= 100),
    ("blesser", "🙏", "50x gesegnet", lambda uid: repo.get_achievement_count(uid, "bless") >= 50),
    ("no_insult", "🧘", "Nie beleidigt", lambda uid: repo.get_achievement_count(uid, "insult") == 0),
    ("top1", "👑", "Platz 1 Leaderboard", lambda uid: repo.is_top1(uid)),
]

def get_achievements(user_id: int) -> str:
    """Gibt die freigeschalteten Achievements als String zurück."""
    unlocked = [f"{icon} {desc}" for key, icon, desc, cond in ACHIEVEMENTS if cond(user_id)]
    return ", ".join(unlocked) if unlocked else "Keine Achievements."


# --- Events ---
@bot.event
async def on_ready():  
    logger.info(f"Bot online als {bot.user}")
    logger.info(f"Bot ist auf folgenden Servern:")
    for guild in bot.guilds:
        logger.info(f"- {guild.name} (ID: {guild.id})")
    logger.info(f"Verwendete GUILD_ID: {GUILD_ID}")
    if not any(guild.id == GUILD_ID for guild in bot.guilds):
        logger.error(f"Bot ist NICHT auf dem Server mit GUILD_ID {GUILD_ID}! Slash-Commands werden dort nicht angezeigt.")
    else:
        await ensure_rank_roles(discord.utils.get(bot.guilds, id=GUILD_ID))
        # --- Entferne alte guild-only-Commands ---
        
        try:
            guild = discord.Object(id=GUILD_ID)
            deleted = await bot.tree.sync(guild=guild)
            for cmd in deleted:
                await bot.tree.remove_command(cmd.name, guild=guild)
            logger.info("Alte guild-only-Commands entfernt.")
        except Exception as e:
            logger.warning(f"Fehler beim Entfernen alter guild-Commands: {e}")
    # --- Global sync ---
    try:
        synced = await bot.tree.sync()
        logger.info(f"Slash-Commands (global) synchronisiert: {len(synced)}")
    except Exception as e:
        logger.error(f"Sync-Fehler: {e}")

        

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    insult = await is_insult(message.content)
    if insult:
        repo.add_user_honor(message.author.id, -100000, reason=f"Beleidigung: {insult}", by=message.author.id)
        await update_member_title(message.author)
        embed = discord.Embed(title="🚫 Beleidigung erkannt!", description=f"{message.author.mention}, das Wort **'{insult}'** ist nicht erlaubt.\nDir wurden **100000 Honor** abgezogen.", color=0xe74c3c)
        embed.set_footer(text="Bitte respektvoll bleiben!")
        await message.channel.send(embed=embed)
    await bot.process_commands(message)

async def ensure_rank_roles(guild: discord.Guild):
    """Stellt sicher, dass alle Rangrollen im Server existieren."""
    existing = {role.name for role in guild.roles}
    for threshold, name, emoji, color in RANKS:
        role_name = f"{emoji} {name}"
        if role_name not in existing:
            try:
                await guild.create_role(name=role_name, colour=color, mentionable=True)
                logger.info(f"Rolle '{role_name}' wurde erstellt.")
            except Exception as e:
                logger.error(f"Fehler beim Erstellen der Rolle '{role_name}': {e}")

    # --- Slash-Commands (Beispiel für einheitlichen Stil, Docstrings, Logging) ---
    @bot.tree.command(name="honor", description="Zeigt dein aktuelles Honor, Rang, Fortschritt und Achievements")
    async def honor(interaction: discord.Interaction):
        """Zeigt das eigene Honor-Profil als Embed."""
        k = repo.get_user_honor(interaction.user.id)
        name, emoji, _ = get_rank(k)
        bar = get_progress_bar(k)
        ach = get_achievements(interaction.user.id)
        embed = discord.Embed(title="🌟 Dein Honor-Profil", color=0x2ecc71)
        embed.add_field(name="Honor", value=f"**{k}**", inline=True)
        embed.add_field(name="Rang", value=f"{emoji} {name}", inline=True)
        embed.add_field(name="Fortschritt", value=bar, inline=False)
        embed.add_field(name="Achievements", value=ach, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="leaderboard", description="Zeigt die Top 10 und Flop 10 User nach Honor")
async def leaderboard(interaction: discord.Interaction):
    """Zeigt das Leaderboard als Embed."""
    top, flop = repo.get_leaderboard()
    def fmt(entries, title, emoji):
        lines = []
        for idx, (uid, k) in enumerate(entries, 1):
            name, rank_emoji, _ = get_rank(k)
            lines.append(f"`#{idx:2}` {rank_emoji} <@{uid}>  **{k:+}**  – {name}")
        return f"__{emoji} {title}__\n" + ("\n".join(lines) if lines else "*Keine Daten.*")
    embed = discord.Embed(title="🏆 HONOR LEADERBOARD", color=0xf1c40f)
    embed.add_field(name="Top 10", value=fmt(top, 'Top 10', '🔝'), inline=False)
    embed.add_field(name="Flop 10", value=fmt(flop, 'Flop 10', '🔻'), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="history", description="Zeigt deine letzten 10 Honor-Änderungen")
async def history(interaction: discord.Interaction):
    """Zeigt die letzten 10 Honor-Änderungen als Embed."""
    rows = repo.get_honor_log(interaction.user.id)
    if not rows:
        await interaction.response.send_message("Keine Honor-Historie gefunden.")
        return
    embed = discord.Embed(title="🕓 Deine letzten 10 Honor-Änderungen", color=0x95a5a6)
    for delta, reason, ts in rows:
        embed.add_field(name=f"{delta:+}", value=f"{reason} (<t:{int(datetime.datetime.fromisoformat(ts).timestamp())}:R>)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="bless", description="Segne einen User und schenke ihm 50 Honor")
@app_commands.describe(user="Der zu segnende User")
async def bless(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        await interaction.response.send_message("Bots können nicht gesegnet werden.", ephemeral=True)
        return
    repo.add_user_honor(user.id, BLESS_AMOUNT)
    await update_member_title(user)
    await interaction.response.send_message(f"{user.mention} wurde gesegnet! +{BLESS_AMOUNT} Honor.")

@bot.tree.command(name="rank", description="Zeigt den Rang eines Users")
@app_commands.describe(user="Der User, dessen Rang angezeigt werden soll")
async def rank(interaction: discord.Interaction, user: discord.Member):
    k = repo.get_user_honor(user.id)
    name, emoji, _ = get_rank(k)
    await interaction.response.send_message(f"{user.mention} hat {k} Honor und Rang: {emoji} {name}")

@bot.tree.command(name="fixroles", description="Synchronisiert deine Rolle mit deinem aktuellen Honor")
async def fixroles(interaction: discord.Interaction):
    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
    await update_member_title(member)
    await interaction.response.send_message("Deine Rolle wurde aktualisiert!", ephemeral=True)

@bot.tree.command(name="thanks", description="Bedanke dich bei einem User und schenke ihm 20 Honor")
@app_commands.describe(user="Der User, dem du danken möchtest")
async def thanks(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        await interaction.response.send_message("Bots können nicht bedankt werden.", ephemeral=True)
        return
    repo.add_user_honor(user.id, 20)
    await update_member_title(user)
    await interaction.response.send_message(f"{user.mention} wurde von {interaction.user.mention} bedankt! +20 Honor.")

@bot.tree.command(name="helped", description="Bestätige, dass dir jemand geholfen hat (+30 Honor für den Helfer)")
@app_commands.describe(user="Der User, der dir geholfen hat")
async def helped(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        await interaction.response.send_message("Bots können nicht als Helfer bestätigt werden.", ephemeral=True)
        return
    repo.add_user_honor(user.id, 30)
    await update_member_title(user)
    await interaction.response.send_message(f"{user.mention} wurde als Helfer bestätigt! +30 Honor.")

@bot.tree.command(name="honor_log", description="Admins: Zeigt die letzten 20 Honor-Änderungen eines Users")
@app_commands.describe(user="Der User, dessen Honor-Log angezeigt werden soll")
async def honor_log(interaction: discord.Interaction, user: discord.Member):
    """Zeigt Admins die letzten 20 Honor-Änderungen eines Users als Embed."""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Nur Admins können diesen Befehl nutzen!", ephemeral=True)
        return
    rows = repo.get_honor_log_admin(user.id)
    if not rows:
        await interaction.response.send_message("Keine Honor-Logs für diesen User gefunden.")
        return
    embed = discord.Embed(title=f"🕓 Letzte 20 Honor-Änderungen von {user.display_name}", color=0x95a5a6)
    for delta, reason, by, ts in rows:
        embed.add_field(name=f"{delta:+}", value=f"{reason} von <@{by}> (<t:{int(datetime.datetime.fromisoformat(ts).timestamp())}:R>)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Neue Slash-Commands: Umfrage und Melden ---
@bot.tree.command(name="umfrage", description="Starte eine Umfrage mit bis zu 5 Optionen")
@app_commands.describe(frage="Die Frage der Umfrage", option1="Option 1", option2="Option 2", option3="Option 3", option4="Option 4", option5="Option 5")
async def umfrage(interaction: discord.Interaction, frage: str, option1: str, option2: str = None, option3: str = None, option4: str = None, option5: str = None):
    options = [option for option in [option1, option2, option3, option4, option5] if option]
    if len(options) < 2:
        await interaction.response.send_message("Bitte gib mindestens zwei Optionen an.", ephemeral=True)
        return
    embed = Embed(title="📊 Umfrage", description=frage, color=0x3498db)
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    for i, opt in enumerate(options):
        embed.add_field(name=emojis[i], value=opt, inline=False)
    msg = await interaction.channel.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])
    await interaction.response.send_message("Umfrage wurde gestartet!", ephemeral=True)

@bot.tree.command(name="melden", description="Melde eine beleidigende Nachricht zur Überprüfung")
@app_commands.describe(wort="Das beleidigende Wort oder Satz")
async def melden(interaction: discord.Interaction, wort: str):
    embed = Embed(title="🚨 Meldung eingegangen", description=f"Gemeldetes Wort: **{wort}**\nSoll dieses Wort zur Badword-Liste hinzugefügt werden?", color=0xe74c3c)
    view = BestätigungsView(wort)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="daily", description="Hole dir deinen täglichen Honor-Bonus!")
async def daily(interaction: discord.Interaction):
    """Erlaubt es, einmal täglich einen Honor-Bonus zu beanspruchen."""
    user_id = interaction.user.id
    if not repo.can_claim_daily(user_id):
        embed = discord.Embed(title="⏳ Daily Bonus", description="Du hast deinen Daily Bonus heute schon abgeholt! Versuche es morgen wieder.", color=0xe67e22)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    bonus = random.randint(100, 500)
    repo.add_user_honor(user_id, bonus, reason="Daily Bonus", by=user_id)
    repo.claim_daily(user_id)
    await update_member_title(interaction.user)
    embed = discord.Embed(title="🎁 Daily Bonus!", description=f"Du hast **{bonus} Honor** erhalten! Komm morgen wieder für mehr.", color=0x27ae60)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="lootbox", description="Öffne eine Lootbox für eine zufällige Honor-Belohnung (1x pro Tag)")
async def lootbox(interaction: discord.Interaction):
    """Erlaubt es, einmal täglich eine Lootbox zu öffnen und Honor zu gewinnen."""
    user_id = interaction.user.id
    if not repo.can_open_lootbox(user_id):
        embed = discord.Embed(title="⏳ Lootbox", description="Du hast heute schon eine Lootbox geöffnet! Versuche es morgen wieder.", color=0xe67e22)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    reward = random.choices([50, 100, 200, 500, 1000, 2500, 5000], weights=[30, 25, 20, 12, 8, 4, 1])[0]
    repo.add_user_honor(user_id, reward, reason="Lootbox", by=user_id)
    repo.log_lootbox(user_id, reward)
    await update_member_title(interaction.user)
    embed = discord.Embed(title="🪙 Lootbox geöffnet!", description=f"Du hast **{reward} Honor** aus der Lootbox erhalten!", color=0x9b59b6)
    await interaction.response.send_message(embed=embed, ephemeral=True)

class BestätigungsView(View):
    def __init__(self, wort):
        super().__init__(timeout=60)
        self.wort = wort

    @button(label="Hinzufügen", style=discord.ButtonStyle.success)
    async def add_callback(self, interaction: discord.Interaction, button):
        # Schreibe das Wort in badwords.py
        await add_badword(self.wort)
        await interaction.response.edit_message(content=f"✅ **{self.wort}** wurde zur Badword-Liste hinzugefügt!", embed=None, view=None)

    @button(label="Abbrechen", style=discord.ButtonStyle.danger)
    async def cancel_callback(self, interaction: discord.Interaction, button):
        await interaction.response.edit_message(content="❌ Vorgang abgebrochen.", embed=None, view=None)

async def add_badword(wort: str):
    pfad = os.path.join(os.path.dirname(__file__), "badwords.py")
    async with asyncio.Lock():
        with open(pfad, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Finde die Zeile mit local_insults = [...]
        for i, line in enumerate(lines):
            if line.strip().startswith("local_insults"):
                # Füge das Wort am Ende der Liste ein, falls nicht vorhanden
                if f'"{wort}"' not in "".join(lines):
                    j = i+1
                    while j < len(lines) and not lines[j].strip().endswith("]"):
                        j += 1
                    lines.insert(j, f'    "{wort}",\n')
                break
        with open(pfad, "w", encoding="utf-8") as f:
            f.writelines(lines)

# --- Belohnungen/Freischaltungen ---
# Beispiel: Ab 10.000 Honor Rolle 'VIP', ab 50.000 Honor Zugang zu Channel 'elite', ab 100.000 Emoji '🌟'
REWARD_ROLES = [
    (10000, "VIP"),
    (50000, "Elite"),
    (100000, "Star")
]
REWARD_CHANNELS = [
    (50000, "elite")
]
REWARD_EMOJIS = [
    (100000, "🌟")
]
async def check_rewards(member: discord.Member):
    honor = repo.get_user_honor(member.id)
    guild = member.guild
    # Rollen
    for threshold, role_name in REWARD_ROLES:
        role = get(guild.roles, name=role_name)
        if honor >= threshold and role and role not in member.roles:
            await member.add_roles(role)
        elif honor < threshold and role and role in member.roles:
            await member.remove_roles(role)
    # Channel (nur Info, Discord API kann keine Channel-Rechte direkt setzen)
    # Emojis (nur Info, Discord API kann keine Emojis direkt freischalten)
# Rufe check_rewards nach jeder Honor-Änderung auf:
# --- Rolle & Nickname Update ---
async def update_member_title(member: discord.Member):
    """Aktualisiert Nickname und Rangrolle eines Members gemäß Honor."""
    honor = repo.get_user_honor(member.id)
    for threshold, name, emoji, color in sorted(RANKS, key=lambda x: x[0], reverse=True):
        if honor >= threshold:
            rank_name, rank_emoji = name, emoji
            break
    else:
        rank_name, rank_emoji = RANKS[0][1], RANKS[0][2]
    guild = member.guild
    base = member.name
    if member.nick:
        parts = member.nick.split(" ")
        if len(parts) >= 3 and parts[0] in {r[2] for r in RANKS} and parts[1].lstrip('+-').isdigit():
            base = " ".join(parts[2:])
    new_nick = f"{rank_emoji} {honor:+d} {base}"
    try:
        if member and member.guild.owner_id != member.id:
            await member.edit(nick=new_nick)
    except discord.Forbidden:
        logger.warning(f"Kann Nickname von {member} nicht ändern (Rollenhierarchie oder Owner).")
    except Exception as e:
        logger.error(f"Fehler beim Nickname-Update für {member}: {e}")
    # Entferne alle alten Rang-Rollen und alle anderen Rollen außer Mod/Admin
    for role in list(member.roles):
        if role.is_default() or role.permissions.administrator or "mod" in role.name.lower() or "admin" in role.name.lower():
            continue
        try:
            await member.remove_roles(role)
        except discord.Forbidden:
            logger.warning(f"Keine Rechte, Rolle '{role.name}' zu entfernen.")
    # Füge die neue Zielrolle hinzu, falls noch nicht vorhanden
    target = get(guild.roles, name=f"{rank_emoji} {rank_name}")
    if target and target not in member.roles:
        try:
            await member.add_roles(target)
        except discord.Forbidden:
            logger.warning(f"Keine Rechte, Rolle '{target.name}' zu vergeben.")
    elif not target:
        logger.warning(f"Rolle '{rank_emoji} {rank_name}' nicht gefunden.")
    await check_rewards(member)




# --- Main ---
if __name__ == "__main__":
    if not TOKEN or not GUILD_ID:
        logger.error("DISCORD_TOKEN und DISCORD_GUILD_ID müssen gesetzt sein!")
    else:
        bot.run(TOKEN)
