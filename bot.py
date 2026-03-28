"""
🎰 Shillings Casino Bot — Single File Edition
Env vars: DISCORD_TOKEN, DATABASE_URL, OWNER_ID, PORT
Start command: python bot.py
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import asyncpg
import os
import random
import math
from datetime import datetime, timezone, timedelta
from collections import Counter
from aiohttp import web

# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS & HELPERS
# ══════════════════════════════════════════════════════════════════════════════

COLOR_GREEN  = 0x2ECC71
COLOR_RED    = 0xE74C3C
COLOR_GOLD   = 0xF1C40F
COLOR_BLUE   = 0x3498DB
COLOR_PURPLE = 0x9B59B6
COLOR_ORANGE = 0xE67E22
COLOR_TEAL   = 0x1ABC9C

RANKS = [
    {"name": "Bronze",    "required": 5_000_000,   "bonus": 1_000_000,  "color": 0xCD7F32, "emoji": "🥉"},
    {"name": "Silver",    "required": 15_000_000,  "bonus": 3_000_000,  "color": 0xC0C0C0, "emoji": "🥈"},
    {"name": "Gold",      "required": 25_000_000,  "bonus": 5_000_000,  "color": 0xFFD700, "emoji": "🥇"},
    {"name": "Platinum",  "required": 35_000_000,  "bonus": 8_000_000,  "color": 0xE5E4E2, "emoji": "💠"},
    {"name": "Diamond",   "required": 50_000_000,  "bonus": 12_000_000, "color": 0xB9F2FF, "emoji": "💎"},
    {"name": "Emerald",   "required": 100_000_000, "bonus": 18_000_000, "color": 0x50C878, "emoji": "💚"},
    {"name": "Ruby",      "required": 200_000_000, "bonus": 25_000_000, "color": 0xE0115F, "emoji": "❤️"},
    {"name": "Netherite", "required": 350_000_000, "bonus": 35_000_000, "color": 0x2F2F2F, "emoji": "⬛"},
]

BOMB_WIRES = 5
BOMB_PAYOUTS = [1.3, 1.8, 2.5, 3.5, 10.0]
ITEMS_PER_PAGE = 10
DAILY_COOLDOWN_HOURS = 3
OWNER_ID = int(os.environ.get("OWNER_ID", 0))

def get_rank_for_wager(total_wagered: int):
    current = None
    for rank in RANKS:
        if total_wagered >= rank["required"]:
            current = rank
    return current

def fmt(n: int) -> str:
    return f"{n:,}"

def fmt_short(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.1f}K"
    return str(n)

def parse_bet(bet_str: str, balance: int):
    bet_str = bet_str.lower().strip()
    if bet_str in ("all", "max"): return balance
    if bet_str.endswith("%"):
        try: return int(balance * float(bet_str[:-1]) / 100)
        except ValueError: return None
    multiplier = 1
    if bet_str.endswith("b"):   multiplier = 1_000_000_000; bet_str = bet_str[:-1]
    elif bet_str.endswith("m"): multiplier = 1_000_000;     bet_str = bet_str[:-1]
    elif bet_str.endswith("k"): multiplier = 1_000;         bet_str = bet_str[:-1]
    try: return int(float(bet_str) * multiplier)
    except ValueError: return None

def win_embed(title, description, amount=None, extra_fields=None):
    embed = discord.Embed(title=f"✅ {title}", description=description, color=COLOR_GREEN)
    if amount is not None: embed.add_field(name="Won", value=f"🪙 **{fmt(amount)}**", inline=True)
    if extra_fields:
        for name, value, inline in extra_fields: embed.add_field(name=name, value=value, inline=inline)
    return embed

def lose_embed(title, description, amount=None, extra_fields=None):
    embed = discord.Embed(title=f"❌ {title}", description=description, color=COLOR_RED)
    if amount is not None: embed.add_field(name="Lost", value=f"🪙 **{fmt(amount)}**", inline=True)
    if extra_fields:
        for name, value, inline in extra_fields: embed.add_field(name=name, value=value, inline=inline)
    return embed

def info_embed(title, description="", color=COLOR_BLUE):
    return discord.Embed(title=title, description=description, color=color)

def error_embed(msg):
    return discord.Embed(title="⚠️ Error", description=msg, color=COLOR_RED)

def validate_bet(bet_str, balance, min_bet=1000):
    parsed = parse_bet(bet_str, balance)
    if not parsed or parsed <= 0: return None, "Invalid bet amount."
    if parsed < min_bet:          return None, f"Minimum bet is 🪙 **{fmt(min_bet)}**."
    if parsed > balance:          return None, f"You only have 🪙 **{fmt(balance)}**."
    return parsed, None

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        await self.create_tables()
        print("Database connected.")

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance BIGINT DEFAULT 500000,
                    total_wagered BIGINT DEFAULT 0,
                    rank TEXT DEFAULT 'Unranked',
                    last_daily TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    type TEXT,
                    amount BIGINT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS stock (
                    id SERIAL PRIMARY KEY,
                    item_name TEXT NOT NULL,
                    item_emoji TEXT,
                    price BIGINT NOT NULL,
                    added_by BIGINT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    log_channel_id BIGINT
                );
                CREATE TABLE IF NOT EXISTS whitelist (
                    user_id BIGINT PRIMARY KEY,
                    added_by BIGINT,
                    added_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS rank_claims (
                    user_id BIGINT,
                    rank TEXT,
                    claimed_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (user_id, rank)
                );
            """)

    async def get_user(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            if not row:
                await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
                row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            return dict(row)

    async def update_balance(self, user_id, amount):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

    async def set_balance(self, user_id, amount):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", amount, user_id)

    async def add_wager(self, user_id, amount):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET total_wagered = total_wagered + $1 WHERE user_id = $2", amount, user_id)

    async def set_rank(self, user_id, rank):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET rank = $1 WHERE user_id = $2", rank, user_id)

    async def set_last_daily(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_daily = NOW() WHERE user_id = $1", user_id)

    async def get_last_daily(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT last_daily FROM users WHERE user_id = $1", user_id)
            return row["last_daily"] if row else None

    async def log_transaction(self, user_id, type_, amount, description):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description) VALUES ($1, $2, $3, $4)",
                user_id, type_, amount, description
            )

    async def get_leaderboard(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT user_id, balance, rank FROM users ORDER BY balance DESC LIMIT $1", limit)

    async def get_guild_settings(self, guild_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
            if not row:
                await conn.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id)
                row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
            return dict(row)

    async def set_log_channel(self, guild_id, channel_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO guild_settings (guild_id, log_channel_id) VALUES ($1, $2) "
                "ON CONFLICT (guild_id) DO UPDATE SET log_channel_id = $2",
                guild_id, channel_id
            )

    async def get_stock(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM stock ORDER BY price ASC")

    async def add_stock(self, item_name, emoji, price, added_by):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO stock (item_name, item_emoji, price, added_by) VALUES ($1, $2, $3, $4)",
                item_name, emoji, price, added_by
            )

    async def remove_stock(self, item_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM stock WHERE id = $1", item_id)

    async def get_stock_item(self, item_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM stock WHERE id = $1", item_id)

    async def is_whitelisted(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM whitelist WHERE user_id = $1", user_id)
            return row is not None

    async def add_whitelist(self, user_id, added_by):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO whitelist (user_id, added_by) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, added_by
            )

    async def remove_whitelist(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM whitelist WHERE user_id = $1", user_id)

    async def has_claimed_rank(self, user_id, rank):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM rank_claims WHERE user_id = $1 AND rank = $2", user_id, rank
            )
            return row is not None

    async def claim_rank(self, user_id, rank):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO rank_claims (user_id, rank) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, rank
            )

# ══════════════════════════════════════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class ShillingsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database()

    async def setup_hook(self):
        await self.db.connect()
        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        await self.change_presence(activity=discord.Game(name="🎰 Shillings Casino"))

bot = ShillingsBot()

# ══════════════════════════════════════════════════════════════════════════════
#  SHARED GAME LOGIC
# ══════════════════════════════════════════════════════════════════════════════

async def process_game(interaction, bet, won, multiplier=2.0):
    user_id = interaction.user.id
    await bot.db.get_user(user_id)
    if won:
        profit = int(bet * multiplier) - bet
        await bot.db.update_balance(user_id, profit)
        net = profit
    else:
        await bot.db.update_balance(user_id, -bet)
        net = -bet
    await bot.db.add_wager(user_id, bet)
    data = await bot.db.get_user(user_id)
    await check_and_assign_rank(interaction.guild, interaction.guild.get_member(user_id) if interaction.guild else None, data["total_wagered"])
    return net

async def check_and_assign_rank(guild, member, total_wagered):
    if not guild or not member:
        return
    rank_data = get_rank_for_wager(total_wagered)
    if not rank_data:
        return
    db_user = await bot.db.get_user(member.id)
    if db_user["rank"] == rank_data["name"]:
        return
    await bot.db.set_rank(member.id, rank_data["name"])
    role = discord.utils.get(guild.roles, name=rank_data["name"])
    if role:
        try:
            old_roles = [r for r in member.roles if r.name in [rk["name"] for rk in RANKS]]
            if old_roles:
                await member.remove_roles(*old_roles, reason="Rank update")
            await member.add_roles(role, reason=f"Reached {rank_data['name']}")
        except discord.Forbidden:
            pass

async def log_to_channel(guild, description, color=COLOR_TEAL):
    try:
        settings = await bot.db.get_guild_settings(guild.id)
        if settings.get("log_channel_id"):
            ch = guild.get_channel(settings["log_channel_id"])
            if ch:
                embed = discord.Embed(description=description, color=color, timestamp=datetime.now(timezone.utc))
                await ch.send(embed=embed)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
#  ECONOMY COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="balance", description="Check your or another user's balance.")
@app_commands.describe(user="User to check")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer()
    target = user or interaction.user
    data = await bot.db.get_user(target.id)
    embed = info_embed(f"🪙 {target.display_name}'s Balance", color=COLOR_GOLD)
    embed.add_field(name="Balance",       value=f"🪙 **{fmt(data['balance'])}**",       inline=True)
    embed.add_field(name="Total Wagered", value=f"🪙 **{fmt(data['total_wagered'])}**", inline=True)
    embed.add_field(name="Rank",          value=f"**{data['rank']}**",                  inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="daily", description="Claim your daily Shillings reward (every 3 hours).")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = interaction.user.id
    last = await bot.db.get_last_daily(user_id)
    now = datetime.now(timezone.utc)
    if last:
        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        diff = now - last
        cooldown = timedelta(hours=DAILY_COOLDOWN_HOURS)
        if diff < cooldown:
            remaining = cooldown - diff
            hours, rem = divmod(int(remaining.total_seconds()), 3600)
            minutes = rem // 60
            await interaction.followup.send(embed=error_embed(f"⏰ Come back in **{hours}h {minutes}m**."), ephemeral=True)
            return
    amount = random.randint(100_000, 5_000_000)
    await bot.db.get_user(user_id)
    await bot.db.update_balance(user_id, amount)
    await bot.db.set_last_daily(user_id)
    await bot.db.log_transaction(user_id, "daily", amount, "Daily reward")
    embed = win_embed("Daily Claimed!", f"**{interaction.user.display_name}** claimed their daily!", amount=amount)
    embed.set_footer(text=f"Next claim in {DAILY_COOLDOWN_HOURS} hours.")
    await interaction.followup.send(embed=embed)
    await log_to_channel(interaction.guild, f"💰 {interaction.user.mention} claimed daily: **{fmt(amount)}** 🪙")

@bot.tree.command(name="tip", description="Send Shillings to another user.")
@app_commands.describe(user="User to tip", amount="Amount (e.g. 1m, 500k)")
async def tip(interaction: discord.Interaction, user: discord.Member, amount: str):
    await interaction.response.defer()
    if user.id == interaction.user.id or user.bot:
        await interaction.followup.send(embed=error_embed("Invalid target."), ephemeral=True); return
    sender = await bot.db.get_user(interaction.user.id)
    parsed = parse_bet(amount, sender["balance"])
    if not parsed or parsed <= 0 or parsed > sender["balance"]:
        await interaction.followup.send(embed=error_embed("Invalid amount or insufficient balance."), ephemeral=True); return
    await bot.db.update_balance(interaction.user.id, -parsed)
    await bot.db.get_user(user.id)
    await bot.db.update_balance(user.id, parsed)
    await bot.db.log_transaction(interaction.user.id, "tip_out", -parsed, f"Tipped {user.id}")
    await bot.db.log_transaction(user.id, "tip_in", parsed, f"From {interaction.user.id}")
    embed = info_embed("💸 Tip Sent!", color=COLOR_TEAL)
    embed.add_field(name="From",   value=interaction.user.mention, inline=True)
    embed.add_field(name="To",     value=user.mention,             inline=True)
    embed.add_field(name="Amount", value=f"🪙 **{fmt(parsed)}**",  inline=True)
    await interaction.followup.send(embed=embed)
    await log_to_channel(interaction.guild, f"💸 {interaction.user.mention} tipped {user.mention} **{fmt(parsed)}** 🪙")

@bot.tree.command(name="leaderboard", description="Top 10 richest players.")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await bot.db.get_leaderboard(10)
    embed = info_embed("🏆 Shillings Leaderboard", color=COLOR_GOLD)
    medals = ["🥇", "🥈", "🥉"]
    desc = ""
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        try:    user = await bot.fetch_user(row["user_id"]); name = user.display_name
        except: name = f"User {row['user_id']}"
        desc += f"{medal} **{name}** — 🪙 {fmt(row['balance'])} | {row['rank']}\n"
    embed.description = desc or "No players yet."
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  RANK COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="rank", description="View your rank progress and claim bonuses.")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    wagered = data["total_wagered"]
    embed = discord.Embed(title=f"🏅 {interaction.user.display_name}'s Rank", color=COLOR_GOLD)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="Current Rank",  value=f"**{data['rank']}**",        inline=True)
    embed.add_field(name="Total Wagered", value=f"🪙 **{fmt(wagered)}**",      inline=True)
    next_rank = next((r for r in RANKS if wagered < r["required"]), None)
    if next_rank:
        needed = next_rank["required"] - wagered
        embed.add_field(name=f"Next: {next_rank['emoji']} {next_rank['name']}", value=f"🪙 **{fmt(needed)}** more to wager", inline=False)
    else:
        embed.add_field(name="Status", value="👑 **MAX RANK ACHIEVED**", inline=False)
    claimable = []
    for r in RANKS:
        if wagered >= r["required"] and not await bot.db.has_claimed_rank(interaction.user.id, r["name"]):
            claimable.append(r)
    if claimable:
        embed.add_field(name="🎁 Claimable", value=", ".join(f"{r['emoji']} {r['name']}" for r in claimable), inline=False)
        embed.set_footer(text="Use /claimrank to claim!")
    table = "".join(
        f"{'✅' if wagered >= r['required'] else '🔒'} {r['emoji']} **{r['name']}** — Wager {fmt_short(r['required'])} → Bonus {fmt_short(r['bonus'])}\n"
        for r in RANKS
    )
    embed.add_field(name="📊 Rank Table", value=table, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="claimrank", description="Claim your rank bonus reward.")
@app_commands.describe(rank="Rank to claim")
@app_commands.choices(rank=[app_commands.Choice(name=r["name"], value=r["name"]) for r in RANKS])
async def claimrank(interaction: discord.Interaction, rank: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    rank_data = next((r for r in RANKS if r["name"] == rank), None)
    if not rank_data: await interaction.followup.send(embed=error_embed("Invalid rank."), ephemeral=True); return
    if data["total_wagered"] < rank_data["required"]:
        needed = rank_data["required"] - data["total_wagered"]
        await interaction.followup.send(embed=error_embed(f"Need **{fmt(needed)}** more wagered."), ephemeral=True); return
    if await bot.db.has_claimed_rank(interaction.user.id, rank):
        await interaction.followup.send(embed=error_embed(f"Already claimed **{rank}** bonus."), ephemeral=True); return
    bonus = rank_data["bonus"]
    await bot.db.update_balance(interaction.user.id, bonus)
    await bot.db.claim_rank(interaction.user.id, rank)
    await bot.db.log_transaction(interaction.user.id, "rank_bonus", bonus, f"Rank bonus: {rank}")
    await interaction.followup.send(embed=win_embed(f"{rank_data['emoji']} Rank Bonus Claimed!", f"Claimed **{rank}** bonus!", amount=bonus))

# ══════════════════════════════════════════════════════════════════════════════
#  BASIC GAMES
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="coinflip", description="Flip a coin. Heads or tails?")
@app_commands.describe(bet="Amount to bet", choice="heads or tails")
@app_commands.choices(choice=[app_commands.Choice(name="Heads", value="heads"), app_commands.Choice(name="Tails", value="tails")])
async def coinflip(interaction: discord.Interaction, bet: str, choice: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    result = random.choice(["heads", "tails"])
    won = result == choice
    net = await process_game(interaction, parsed, won)
    coin = "🪙 Heads" if result == "heads" else "🟫 Tails"
    embed = (win_embed if won else lose_embed)(f"Coinflip {'Win' if won else 'Loss'}!", f"**{coin}**!", amount=abs(net))
    embed.add_field(name="Your Guess", value=choice.capitalize(), inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="roulette", description="Bet on roulette — number, color, or range.")
@app_commands.describe(bet="Amount to bet", choice="red/black/green, odd/even, 1-18/19-36, or 0–36")
async def roulette(interaction: discord.Interaction, bet: str, choice: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    spin = random.randint(0, 36)
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color = "🟢 Green" if spin == 0 else ("🔴 Red" if spin in reds else "⚫ Black")
    c = choice.strip().lower()
    won = False; multiplier = 2.0
    if c in ("red","black","green"):
        target = {"red": "🔴 Red", "black": "⚫ Black", "green": "🟢 Green"}[c]
        won = color == target; multiplier = 14.0 if c == "green" else 2.0
    elif c == "odd":  won = spin != 0 and spin % 2 == 1
    elif c == "even": won = spin != 0 and spin % 2 == 0
    elif c == "1-18": won = 1 <= spin <= 18
    elif c == "19-36": won = 19 <= spin <= 36
    else:
        try:
            num = int(c)
            if 0 <= num <= 36: won = spin == num; multiplier = 35.0
            else: await interaction.followup.send(embed=error_embed("Number must be 0–36."), ephemeral=True); return
        except ValueError:
            await interaction.followup.send(embed=error_embed("Invalid choice."), ephemeral=True); return
    net = await process_game(interaction, parsed, won, multiplier)
    embed = (win_embed if won else lose_embed)(f"🎡 Roulette {'Win' if won else 'Loss'}!", f"Ball landed on **{spin}** ({color})", amount=abs(net))
    embed.add_field(name="Multiplier", value=f"**{multiplier}x**", inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="numguess", description="Guess a number 1–100. Closer = bigger reward!")
@app_commands.describe(bet="Amount to bet", guess="Your guess (1–100)")
async def numguess(interaction: discord.Interaction, bet: str, guess: int):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    if not 1 <= guess <= 100: await interaction.followup.send(embed=error_embed("Guess must be 1–100."), ephemeral=True); return
    number = random.randint(1, 100)
    diff = abs(guess - number)
    if diff == 0:    mult = 10.0; result = "🎯 PERFECT!"
    elif diff <= 5:  mult = 3.0;  result = "🔥 Very Close!"
    elif diff <= 15: mult = 1.5;  result = "👌 Close!"
    elif diff <= 30: mult = 0.5;  result = "😐 Far..."
    else:            mult = 0.0;  result = "❌ Way off!"
    won = mult > 1.0
    net = await process_game(interaction, parsed, won, max(mult, 1.0))
    embed = (win_embed if won else lose_embed)(f"NumGuess — {result}", f"Number was **{number}**, you guessed **{guess}**.", amount=abs(net))
    embed.add_field(name="Difference", value=f"**{diff}**", inline=True)
    embed.add_field(name="Multiplier", value=f"**{mult}x**", inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="scratch", description="Scratch a 3x3 card for instant prizes!")
@app_commands.describe(bet="Amount to bet")
async def scratch(interaction: discord.Interaction, bet: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    symbols = ["🍒","🍋","🍊","⭐","💎","🎰","🃏","🔔"]
    grid = [random.choice(symbols) for _ in range(9)]
    counts = Counter(grid)
    sym, count = counts.most_common(1)[0]
    mult_table = {3:1.5, 4:2.0, 5:3.0, 6:5.0, 7:8.0, 8:12.0, 9:25.0}
    mult = mult_table.get(count, 0)
    won = mult > 1.0
    net = await process_game(interaction, parsed, won, mult if mult > 0 else 1.0)
    display = "\n".join(" ".join(grid[i:i+3]) for i in range(0,9,3))
    embed = (win_embed if won else lose_embed)("🎟️ Scratch Card!", f"{display}\n{'**' + str(count) + 'x ' + sym + '!**' if won else 'No match.'}", amount=abs(net))
    embed.add_field(name="Multiplier", value=f"**{mult}x**", inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="horserace", description="Pick a horse and hope it wins!")
@app_commands.describe(bet="Amount to bet", horse="Horse number 1–6")
async def horserace(interaction: discord.Interaction, bet: str, horse: int):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    if not 1 <= horse <= 6: await interaction.followup.send(embed=error_embed("Pick horse 1–6."), ephemeral=True); return
    horses = ["🐴","🏇","🦄","🐎","🎠","🏆"]
    odds = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
    random.shuffle(odds)
    winner = random.randint(1, 6)
    won = winner == horse
    mult = odds[horse-1]
    net = await process_game(interaction, parsed, won, mult)
    race = ""
    for i in range(1, 7):
        pos = "🏁" if i == winner else "🏃"
        pick = " ← Your pick" if i == horse else ""
        race += f"{i}. {horses[i-1]} {pos} (odds: **{odds[i-1]}x**){pick}\n"
    embed = (win_embed if won else lose_embed)(f"🏇 Horse Race {'Win' if won else 'Loss'}!", f"Horse **#{winner}** wins!\n{race}", amount=abs(net))
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="limbo", description="Set a multiplier target. Hit it or higher to win!")
@app_commands.describe(bet="Amount to bet", target="Target multiplier (e.g. 2.5)")
async def limbo(interaction: discord.Interaction, bet: str, target: float):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    if not 1.01 <= target <= 1000: await interaction.followup.send(embed=error_embed("Target must be 1.01–1000."), ephemeral=True); return
    result = round(random.uniform(1.0, 1000.0), 2)
    won = result >= target
    net = await process_game(interaction, parsed, won, target)
    embed = (win_embed if won else lose_embed)(f"🎯 Limbo {'Win' if won else 'Loss'}!", f"Result: **{result}x** (target: **{target}x**)", amount=abs(net))
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  CARD GAMES
# ══════════════════════════════════════════════════════════════════════════════

SUITS = ["♠️","♥️","♦️","♣️"]
FACES = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]

def new_deck():
    deck = [(f, s) for s in SUITS for f in FACES]; random.shuffle(deck); return deck
def card_value(card): f,_ = card; return 10 if f in ("J","Q","K") else (11 if f=="A" else int(f))
def hand_value(hand):
    val = sum(card_value(c) for c in hand); aces = sum(1 for c in hand if c[0]=="A")
    while val > 21 and aces: val -= 10; aces -= 1
    return val
def fmt_hand(hand): return " ".join(f"`{f}{s}`" for f,s in hand)
def card_rank_index(card): f,_ = card; return FACES.index(f)

bj_games = {}
hl_games = {}

class BlackjackView(discord.ui.View):
    def __init__(self, user_id, game):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.game = game

    async def interaction_check(self, interaction): return interaction.user.id == self.user_id

    async def finish(self, interaction, result):
        game = self.game
        dealer_hand = game["dealer"]
        while hand_value(dealer_hand) < 17: dealer_hand.append(game["deck"].pop())
        pv = hand_value(game["player"]); dv = hand_value(dealer_hand)
        if result == "stand":
            if dv > 21 or pv > dv:   won, mult, title = True, 2.0, "Blackjack Win! 🎉"
            elif pv == dv:            won, mult, title = True, 1.0, "Blackjack Push!"
            else:                     won, mult, title = False, 1.0, "Blackjack Loss!"
        else:                         won, mult, title = False, 1.0, "Blackjack Loss! (Bust)"
        net = await process_game(interaction, game["bet"], won, mult)
        bj_games.pop(self.user_id, None); self.stop()
        for item in self.children: item.disabled = True
        embed = (win_embed if won else lose_embed)(title, f"**Your hand:** {fmt_hand(game['player'])} = **{pv}**\n**Dealer:** {fmt_hand(dealer_hand)} = **{dv}**", amount=abs(net))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.green, emoji="🃏")
    async def hit(self, interaction, button):
        self.game["player"].append(self.game["deck"].pop())
        val = hand_value(self.game["player"])
        if val > 21: await self.finish(interaction, "bust")
        else:
            embed = info_embed("🃏 Blackjack", color=COLOR_BLUE)
            embed.add_field(name="Your Hand", value=f"{fmt_hand(self.game['player'])} = **{val}**", inline=False)
            embed.add_field(name="Dealer Shows", value=fmt_hand([self.game["dealer"][0]]), inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.red, emoji="✋")
    async def stand(self, interaction, button): await self.finish(interaction, "stand")

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.blurple, emoji="💰")
    async def double_down(self, interaction, button):
        data = await bot.db.get_user(interaction.user.id)
        if data["balance"] < self.game["bet"]:
            await interaction.response.send_message(embed=error_embed("Not enough to double down."), ephemeral=True); return
        self.game["bet"] *= 2
        self.game["player"].append(self.game["deck"].pop())
        val = hand_value(self.game["player"])
        await self.finish(interaction, "bust" if val > 21 else "stand")

@bot.tree.command(name="blackjack", description="Beat the dealer to 21!")
@app_commands.describe(bet="Amount to bet")
async def blackjack(interaction: discord.Interaction, bet: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    deck = new_deck()
    player = [deck.pop(), deck.pop()]; dealer = [deck.pop(), deck.pop()]
    game = {"player": player, "dealer": dealer, "deck": deck, "bet": parsed}
    bj_games[interaction.user.id] = game
    pv = hand_value(player)
    if pv == 21:
        dv = hand_value(dealer)
        won, mult, title = (True, 1.0, "Push!") if dv == 21 else (True, 2.5, "🎉 Natural Blackjack!")
        net = await process_game(interaction, parsed, won, mult)
        await interaction.followup.send(embed=(win_embed if won else lose_embed)(title, fmt_hand(player), amount=abs(net))); return
    embed = info_embed("🃏 Blackjack", color=COLOR_BLUE)
    embed.add_field(name="Your Hand", value=f"{fmt_hand(player)} = **{pv}**", inline=False)
    embed.add_field(name="Dealer Shows", value=fmt_hand([dealer[0]]), inline=False)
    embed.add_field(name="Bet", value=f"🪙 **{fmt(parsed)}**", inline=True)
    await interaction.followup.send(embed=embed, view=BlackjackView(interaction.user.id, game))

@bot.tree.command(name="war", description="Card duel vs the dealer. Higher card wins!")
@app_commands.describe(bet="Amount to bet")
async def war(interaction: discord.Interaction, bet: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    deck = new_deck()
    pc = deck.pop(); dc = deck.pop()
    pr = card_rank_index(pc); dr = card_rank_index(dc)
    if pr > dr:   won = True;  result = "You win!"
    elif dr > pr: won = False; result = "Dealer wins!"
    else:         won = True;  result = "Tie — push!"; parsed = 0
    net = await process_game(interaction, parsed, won)
    pf,ps = pc; df,ds = dc
    embed = (win_embed if won else lose_embed)("⚔️ War", f"Your: `{pf}{ps}` vs Dealer: `{df}{ds}`\n**{result}**", amount=abs(net))
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="higherlower", description="Guess if the next card is higher or lower!")
@app_commands.describe(bet="Amount to bet", choice="higher or lower")
@app_commands.choices(choice=[app_commands.Choice(name="Higher", value="higher"), app_commands.Choice(name="Lower", value="lower")])
async def higherlower(interaction: discord.Interaction, bet: str, choice: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    deck = new_deck()
    current = deck.pop(); next_card = deck.pop()
    cr = card_rank_index(current); nr = card_rank_index(next_card)
    won = (nr > cr) if choice == "higher" else (nr < cr)
    if cr == nr: won = False
    uid = interaction.user.id
    streak = hl_games.get(uid, {}).get("streak", 0)
    streak = streak + 1 if won else 0
    hl_games[uid] = {"streak": streak}
    mult = min(2.0 + (streak-1)*0.5, 10.0) if won else 1.0
    net = await process_game(interaction, parsed, won, mult)
    cf,cs = current; nf,ns = next_card
    desc = f"Current: `{cf}{cs}` → Next: `{nf}{ns}`"
    embed = (win_embed if won else lose_embed)(f"🃏 Higher/Lower {'Win' if won else 'Loss'}!", desc, amount=abs(net))
    if won: embed.add_field(name="Streak 🔥", value=f"**{streak}**", inline=True); embed.add_field(name="Multiplier", value=f"**{mult}x**", inline=True)
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
#  ADVANCED GAMES
# ══════════════════════════════════════════════════════════════════════════════

mines_games = {}
crash_games = {}
bomb_games = {}

def mines_multiplier(revealed, bombs, total=16):
    safe = total - bombs
    if revealed == 0: return 1.0
    mult = 1.0
    for i in range(revealed):
        mult *= (safe-i)/(total-i) if (total-i) > 0 else 1
    return round(1/mult, 2) if mult > 0 else 1.0

class MinesView(discord.ui.View):
    def __init__(self, user_id, game):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.game = game
        self._build()

    def _build(self):
        self.clear_items()
        game = self.game
        for i in range(16):
            is_rev = i in game["revealed"]
            is_bomb = i in game["bombs"]
            if is_rev:
                btn = discord.ui.Button(label="💎" if not is_bomb else "💣", style=discord.ButtonStyle.green if not is_bomb else discord.ButtonStyle.red, disabled=True, row=i//4)
            else:
                btn = discord.ui.Button(label="❓", style=discord.ButtonStyle.grey, row=i//4, custom_id=f"mine_{i}")
                btn.callback = self._make_cb(i)
            self.add_item(btn)
        mult = mines_multiplier(len(game["revealed"]), len(game["bombs"]))
        co = discord.ui.Button(label=f"Cash Out ({mult}x)", style=discord.ButtonStyle.blurple, row=4)
        co.callback = self.cashout
        self.add_item(co)

    def _make_cb(self, index):
        async def cb(interaction):
            if interaction.user.id != self.user_id: await interaction.response.send_message("Not your game!", ephemeral=True); return
            game = self.game
            game["revealed"].add(index)
            if index in game["bombs"]:
                net = await process_game(interaction, game["bet"], False)
                mines_games.pop(self.user_id, None); self.stop()
                for item in self.children: item.disabled = True
                await interaction.response.edit_message(embed=lose_embed("💣 BOOM!", f"Hit mine at tile {index+1}.", amount=abs(net)), view=self)
            else:
                mult = mines_multiplier(len(game["revealed"]), len(game["bombs"]))
                self._build()
                embed = info_embed("⛏️ Mines", color=COLOR_BLUE)
                embed.add_field(name="Gems Found", value=f"**{len(game['revealed'])}**", inline=True)
                embed.add_field(name="Multiplier",  value=f"**{mult}x**",               inline=True)
                await interaction.response.edit_message(embed=embed, view=self)
        return cb

    async def cashout(self, interaction):
        if interaction.user.id != self.user_id: await interaction.response.send_message("Not your game!", ephemeral=True); return
        if not self.game["revealed"]: await interaction.response.send_message(embed=error_embed("Reveal at least one tile!"), ephemeral=True); return
        mult = mines_multiplier(len(self.game["revealed"]), len(self.game["bombs"]))
        net = await process_game(interaction, self.game["bet"], True, mult)
        mines_games.pop(self.user_id, None); self.stop()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(embed=win_embed("⛏️ Cashed Out!", f"Cashed out at **{mult}x**!", amount=abs(net)), view=self)

    async def interaction_check(self, interaction): return interaction.user.id == self.user_id

@bot.tree.command(name="mines", description="4x4 grid — reveal gems, avoid bombs!")
@app_commands.describe(bet="Amount to bet", bombs="Number of bombs (1–10)")
async def mines(interaction: discord.Interaction, bet: str, bombs: int = 3):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    if not 1 <= bombs <= 10: await interaction.followup.send(embed=error_embed("Bombs must be 1–10."), ephemeral=True); return
    game = {"bet": parsed, "bombs": set(random.sample(range(16), bombs)), "revealed": set()}
    mines_games[interaction.user.id] = game
    view = MinesView(interaction.user.id, game)
    embed = info_embed("⛏️ Mines", f"**{bombs}** bombs hidden. Reveal gems safely!", color=COLOR_BLUE)
    embed.add_field(name="Bet", value=f"🪙 **{fmt(parsed)}**", inline=True)
    await interaction.followup.send(embed=embed, view=view)

class CrashView(discord.ui.View):
    def __init__(self, user_id, game):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.game = game

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.green, emoji="💸")
    async def cashout(self, interaction, button):
        if interaction.user.id != self.user_id: await interaction.response.send_message("Not your game!", ephemeral=True); return
        game = self.game
        mult = game.get("current_mult", 1.0)
        won = mult < game["crash_point"]
        net = await process_game(interaction, game["bet"], won, mult)
        crash_games.pop(self.user_id, None); button.disabled = True; self.stop()
        embed = win_embed("🚀 Cashed Out!", f"Cashed at **{mult}x** (crashed at **{game['crash_point']}x**)", amount=abs(net))
        await interaction.response.edit_message(embed=embed, view=self)

@bot.tree.command(name="crash", description="Multiplier rises — cash out before it crashes!")
@app_commands.describe(bet="Amount to bet", auto_cashout="Auto cash out at this multiplier")
async def crash(interaction: discord.Interaction, bet: str, auto_cashout: float = 0):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    crash_point = round(random.choices(
        [random.uniform(1.0,1.5), random.uniform(1.5,5.0), random.uniform(5.0,50.0)],
        weights=[45,40,15]
    )[0], 2)
    game = {"bet": parsed, "crash_point": crash_point, "current_mult": 1.0}
    crash_games[interaction.user.id] = game
    view = CrashView(interaction.user.id, game)
    embed = info_embed("🚀 Crash", "Multiplier is rising! Cash out before it crashes!", color=COLOR_GREEN)
    embed.add_field(name="Bet", value=f"🪙 **{fmt(parsed)}**", inline=True)
    msg = await interaction.followup.send(embed=embed, view=view)
    mult = 1.0
    while mult < crash_point:
        await asyncio.sleep(0.8); mult = round(mult + 0.1, 2)
        game["current_mult"] = mult
        if auto_cashout > 1.0 and mult >= auto_cashout:
            net = await process_game(interaction, parsed, True, mult)
            crash_games.pop(interaction.user.id, None); view.stop()
            for item in view.children: item.disabled = True
            await msg.edit(embed=win_embed("🚀 Auto Cash Out!", f"Auto cashed at **{mult}x** (crashed at **{crash_point}x**)", amount=abs(net)), view=view); return
        embed = info_embed("🚀 Crash", f"**{mult}x** 🚀", color=COLOR_GREEN)
        try: await msg.edit(embed=embed, view=view)
        except Exception: break
    if interaction.user.id in crash_games:
        crash_games.pop(interaction.user.id, None)
        net = await process_game(interaction, parsed, False)
        view.stop()
        for item in view.children: item.disabled = True
        try: await msg.edit(embed=lose_embed("🚀 CRASHED!", f"Crashed at **{crash_point}x**. Too slow!", amount=abs(net)), view=view)
        except Exception: pass

class BombView(discord.ui.View):
    def __init__(self, user_id, game):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.game = game
        self._build()

    def _build(self):
        self.clear_items()
        game = self.game
        for i in range(BOMB_WIRES):
            cut = i in game["cut"]; is_bomb = i == game["bomb_wire"]
            if cut:
                btn = discord.ui.Button(label="💣" if is_bomb else "✅", style=discord.ButtonStyle.red if is_bomb else discord.ButtonStyle.green, disabled=True)
            else:
                btn = discord.ui.Button(label=f"Wire {i+1}", style=discord.ButtonStyle.grey)
                btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, index):
        async def cb(interaction):
            if interaction.user.id != self.user_id: await interaction.response.send_message("Not your game!", ephemeral=True); return
            game = self.game; game["cut"].add(index)
            if index == game["bomb_wire"]:
                net = await process_game(interaction, game["bet"], False)
                bomb_games.pop(self.user_id, None); self.stop()
                for item in self.children: item.disabled = True
                await interaction.response.edit_message(embed=lose_embed("💣 BOOM! Wrong Wire!", f"Wire {index+1} was the bomb!", amount=abs(net)), view=self)
            else:
                stage = len(game["cut"]) - 1
                mult = BOMB_PAYOUTS[min(stage, len(BOMB_PAYOUTS)-1)]
                if len(game["cut"]) >= BOMB_WIRES - 1:
                    net = await process_game(interaction, game["bet"], True, BOMB_PAYOUTS[-1])
                    bomb_games.pop(self.user_id, None); self.stop()
                    await interaction.response.edit_message(embed=win_embed("💣 Bomb Defused!", f"All safe wires cut! **{BOMB_PAYOUTS[-1]}x**!", amount=abs(net)), view=self); return
                self._build()
                embed = info_embed("💣 Bomb", color=COLOR_GOLD)
                embed.add_field(name="Wires Cut",      value=f"**{len(game['cut'])}**", inline=True)
                embed.add_field(name="Current Payout", value=f"**{mult}x**",            inline=True)
                await interaction.response.edit_message(embed=embed, view=self)
        return cb

    async def interaction_check(self, interaction): return interaction.user.id == self.user_id

@bot.tree.command(name="bomb", description="Cut wires to increase payout — avoid the bomb wire!")
@app_commands.describe(bet="Amount to bet")
async def bomb(interaction: discord.Interaction, bet: str):
    await interaction.response.defer()
    data = await bot.db.get_user(interaction.user.id)
    parsed, err = validate_bet(bet, data["balance"])
    if err: await interaction.followup.send(embed=error_embed(err), ephemeral=True); return
    game = {"bet": parsed, "bomb_wire": random.randint(0, BOMB_WIRES-1), "cut": set()}
    bomb_games[interaction.user.id] = game
    view = BombView(interaction.user.id, game)
    embed = info_embed("💣 Bomb", f"**{BOMB_WIRES}** wires. One is the bomb. Cut safely!", color=COLOR_GOLD)
    embed.add_field(name="Payouts", value=" → ".join(f"**{p}x**" for p in BOMB_PAYOUTS), inline=False)
    embed.add_field(name="Bet", value=f"🪙 **{fmt(parsed)}**", inline=True)
    await interaction.followup.send(embed=embed, view=view)

# ══════════════════════════════════════════════════════════════════════════════
#  DUEL
# ══════════════════════════════════════════════════════════════════════════════

pending_duels = {}

class DuelView(discord.ui.View):
    def __init__(self, challenger, opponent, bet):
        super().__init__(timeout=60)
        self.challenger = challenger; self.opponent = opponent; self.bet = bet

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, emoji="⚔️")
    async def accept(self, interaction, button):
        if interaction.user.id != self.opponent.id: await interaction.response.send_message("Not your duel!", ephemeral=True); return
        data = await bot.db.get_user(self.opponent.id)
        if data["balance"] < self.bet:
            await interaction.response.send_message(embed=error_embed(f"Need 🪙 **{fmt(self.bet)}** to accept."), ephemeral=True); return
        winner = random.choice([self.challenger, self.opponent])
        loser = self.opponent if winner == self.challenger else self.challenger
        await bot.db.update_balance(winner.id, self.bet)
        await bot.db.update_balance(loser.id, -self.bet)
        await bot.db.add_wager(self.challenger.id, self.bet)
        await bot.db.add_wager(self.opponent.id, self.bet)
        await bot.db.log_transaction(winner.id, "duel_win",  self.bet,  f"Won duel")
        await bot.db.log_transaction(loser.id,  "duel_loss", -self.bet, f"Lost duel")
        pending_duels.pop(self.challenger.id, None); self.stop()
        for item in self.children: item.disabled = True
        embed = win_embed("⚔️ Duel Result!", f"🏆 **{winner.display_name}** defeats **{loser.display_name}**!", amount=self.bet)
        embed.add_field(name="Winner", value=winner.mention, inline=True)
        embed.add_field(name="Loser",  value=loser.mention,  inline=True)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, emoji="🚫")
    async def decline(self, interaction, button):
        if interaction.user.id not in (self.opponent.id, self.challenger.id): await interaction.response.send_message("Not your duel!", ephemeral=True); return
        pending_duels.pop(self.challenger.id, None); self.stop()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(embed=error_embed(f"Duel declined."), view=self)

@bot.tree.command(name="duel", description="Challenge another player — winner takes all!")
@app_commands.describe(opponent="User to challenge", bet="Amount to bet")
async def duel(interaction: discord.Interaction, opponent: discord.Member, bet: str):
    await interaction.response.defer()
    if opponent.id == interaction.user.id or opponent.bot:
        await interaction.followup.send(embed=error_embed("Invalid opponent."), ephemeral=True); return
    data = await bot.db.get_user(interaction.user.id)
    parsed = parse_bet(bet, data["balance"])
    if not parsed or parsed <= 0 or parsed > data["balance"]:
        await interaction.followup.send(embed=error_embed("Invalid bet or insufficient balance."), ephemeral=True); return
    if interaction.user.id in pending_duels:
        await interaction.followup.send(embed=error_embed("You already have a pending duel."), ephemeral=True); return
    pending_duels[interaction.user.id] = True
    embed = info_embed("⚔️ Duel Challenge!", color=COLOR_PURPLE)
    embed.add_field(name="Challenger", value=interaction.user.mention, inline=True)
    embed.add_field(name="Opponent",   value=opponent.mention,         inline=True)
    embed.add_field(name="Bet",        value=f"🪙 **{fmt(parsed)}**",  inline=True)
    embed.set_footer(text="60 seconds to accept or decline.")
    await interaction.followup.send(content=opponent.mention, embed=embed, view=DuelView(interaction.user, opponent, parsed))

# ══════════════════════════════════════════════════════════════════════════════
#  STOCK & WITHDRAW
# ══════════════════════════════════════════════════════════════════════════════

class StockPaginator(discord.ui.View):
    def __init__(self, items, page=0):
        super().__init__(timeout=60)
        self.items = items; self.page = page
        self.max_page = max(0, math.ceil(len(items)/ITEMS_PER_PAGE)-1)

    def build_embed(self):
        start = self.page * ITEMS_PER_PAGE
        page_items = self.items[start:start+ITEMS_PER_PAGE]
        embed = info_embed(f"🏪 Stock ({len(self.items)} items)", color=COLOR_GOLD)
        if not page_items: embed.description = "No items in stock."; return embed
        embed.description = "".join(f"**#{i['id']}** {i['item_emoji'] or '📦'} {i['item_name']} — 🪙 **{fmt(i['price'])}**\n" for i in page_items)
        embed.set_footer(text=f"Page {self.page+1}/{self.max_page+1} • /withdraw <id> to claim")
        return embed

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.grey)
    async def first(self, interaction, button): self.page = 0; await interaction.response.edit_message(embed=self.build_embed(), view=self)
    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.grey)
    async def prev(self, interaction, button):
        if self.page > 0: self.page -= 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.grey)
    async def next(self, interaction, button):
        if self.page < self.max_page: self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.grey)
    async def last(self, interaction, button): self.page = self.max_page; await interaction.response.edit_message(embed=self.build_embed(), view=self)

@bot.tree.command(name="stock", description="Browse items available for purchase.")
async def stock(interaction: discord.Interaction):
    await interaction.response.defer()
    items = list(await bot.db.get_stock())
    view = StockPaginator(items)
    await interaction.followup.send(embed=view.build_embed(), view=view)

@bot.tree.command(name="withdraw", description="Claim a stock item with your Shillings.")
@app_commands.describe(item_id="Item ID from /stock")
async def withdraw(interaction: discord.Interaction, item_id: int):
    await interaction.response.defer(ephemeral=True)
    item = await bot.db.get_stock_item(item_id)
    if not item: await interaction.followup.send(embed=error_embed("Item not found."), ephemeral=True); return
    data = await bot.db.get_user(interaction.user.id)
    if data["balance"] < item["price"]:
        await interaction.followup.send(embed=error_embed(f"Need 🪙 **{fmt(item['price'])}**, you have 🪙 **{fmt(data['balance'])}**."), ephemeral=True); return
    await bot.db.update_balance(interaction.user.id, -item["price"])
    await bot.db.remove_stock(item_id)
    await bot.db.log_transaction(interaction.user.id, "withdraw", -item["price"], f"Withdrew: {item['item_name']}")
    emoji = item["item_emoji"] or "📦"
    embed = info_embed("✅ Item Claimed!", color=COLOR_TEAL)
    embed.add_field(name="Item", value=f"{emoji} **{item['item_name']}**", inline=True)
    embed.add_field(name="Cost", value=f"🪙 **{fmt(item['price'])}**",      inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)
    await log_to_channel(interaction.guild, f"🛒 {interaction.user.mention} withdrew {emoji} **{item['item_name']}** for 🪙 {fmt(item['price'])}")

@bot.tree.command(name="addtostock", description="[Staff] Add an item to stock.")
@app_commands.describe(name="Item name", price="Price in Shillings", emoji="Item emoji")
async def addtostock(interaction: discord.Interaction, name: str, price: int, emoji: str = "📦"):
    await interaction.response.defer(ephemeral=True)
    if not await bot.db.is_whitelisted(interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(embed=error_embed("No permission."), ephemeral=True); return
    await bot.db.add_stock(name, emoji, price, interaction.user.id)
    await interaction.followup.send(embed=info_embed(f"✅ Added {emoji} **{name}** at 🪙 **{fmt(price)}**", color=COLOR_TEAL), ephemeral=True)

@bot.tree.command(name="removestock", description="[Staff] Remove a stock item by ID.")
@app_commands.describe(item_id="Item ID to remove")
async def removestock(interaction: discord.Interaction, item_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await bot.db.is_whitelisted(interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(embed=error_embed("No permission."), ephemeral=True); return
    item = await bot.db.get_stock_item(item_id)
    if not item: await interaction.followup.send(embed=error_embed("Item not found."), ephemeral=True); return
    await bot.db.remove_stock(item_id)
    await interaction.followup.send(embed=info_embed(f"🗑️ Removed **{item['item_name']}**.", color=COLOR_TEAL), ephemeral=True)

@bot.tree.command(name="deposit", description="[Staff] Log an item deposit from a user.")
@app_commands.describe(user="User depositing", item="Item description", value="Shillings value")
async def deposit(interaction: discord.Interaction, user: discord.Member, item: str, value: int):
    await interaction.response.defer(ephemeral=True)
    if not await bot.db.is_whitelisted(interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(embed=error_embed("No permission."), ephemeral=True); return
    await bot.db.update_balance(user.id, value)
    await bot.db.log_transaction(user.id, "deposit", value, f"Item deposit: {item}")
    embed = info_embed("📥 Deposit Logged", color=COLOR_TEAL)
    embed.add_field(name="User",  value=user.mention,       inline=True)
    embed.add_field(name="Item",  value=item,               inline=True)
    embed.add_field(name="Value", value=f"🪙 **{fmt(value)}**", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)
    await log_to_channel(interaction.guild, f"📥 {user.mention} deposited **{item}** (🪙 {fmt(value)})")

@bot.tree.command(name="depositshillings", description="[Staff] Manually deposit Shillings for a user.")
@app_commands.describe(user="Target user", amount="Amount to add")
async def depositshillings(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if not await bot.db.is_whitelisted(interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(embed=error_embed("No permission."), ephemeral=True); return
    await bot.db.get_user(user.id)
    await bot.db.update_balance(user.id, amount)
    await bot.db.log_transaction(user.id, "deposit_shillings", amount, f"Manual deposit by {interaction.user.id}")
    await interaction.followup.send(embed=info_embed(f"💰 Added 🪙 **{fmt(amount)}** to {user.display_name}.", color=COLOR_TEAL), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  STAFF / OWNER COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def owner_only():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID and not interaction.user.guild_permissions.administrator:
            raise app_commands.CheckFailure("Owner only.")
        return True
    return app_commands.check(predicate)

@bot.tree.command(name="setlogs", description="[Owner] Set the transaction log channel.")
@app_commands.describe(channel="Channel to log to")
@owner_only()
async def setlogs(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    await bot.db.set_log_channel(interaction.guild.id, channel.id)
    await interaction.followup.send(embed=info_embed(f"✅ Log channel set to {channel.mention}", color=COLOR_TEAL), ephemeral=True)

@bot.tree.command(name="whitelist", description="[Owner] Add or remove a staff member.")
@app_commands.describe(user="User", action="add or remove")
@app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
@owner_only()
async def whitelist(interaction: discord.Interaction, user: discord.Member, action: str):
    await interaction.response.defer(ephemeral=True)
    if action == "add":
        await bot.db.add_whitelist(user.id, interaction.user.id)
        await interaction.followup.send(embed=info_embed(f"✅ {user.display_name} whitelisted.", color=COLOR_TEAL), ephemeral=True)
    else:
        await bot.db.remove_whitelist(user.id)
        await interaction.followup.send(embed=info_embed(f"🗑️ {user.display_name} removed from whitelist.", color=COLOR_RED), ephemeral=True)

@bot.tree.command(name="addshillings", description="[Owner] Add Shillings to a user.")
@app_commands.describe(user="Target user", amount="Amount")
@owner_only()
async def addshillings(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    await bot.db.get_user(user.id); await bot.db.update_balance(user.id, amount)
    await bot.db.log_transaction(user.id, "admin_add", amount, f"Added by {interaction.user.id}")
    await interaction.followup.send(embed=info_embed(f"✅ Added 🪙 **{fmt(amount)}** to {user.display_name}.", color=COLOR_TEAL), ephemeral=True)

@bot.tree.command(name="removeshillings", description="[Owner] Remove Shillings from a user.")
@app_commands.describe(user="Target user", amount="Amount")
@owner_only()
async def removeshillings(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    data = await bot.db.get_user(user.id)
    if data["balance"] < amount: await interaction.followup.send(embed=error_embed(f"User only has 🪙 **{fmt(data['balance'])}**."), ephemeral=True); return
    await bot.db.update_balance(user.id, -amount)
    await bot.db.log_transaction(user.id, "admin_remove", -amount, f"Removed by {interaction.user.id}")
    await interaction.followup.send(embed=info_embed(f"🗑️ Removed 🪙 **{fmt(amount)}** from {user.display_name}.", color=COLOR_RED), ephemeral=True)

@bot.tree.command(name="setbalance", description="[Owner] Set a user's balance.")
@app_commands.describe(user="Target user", amount="New balance")
@owner_only()
async def setbalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    await bot.db.get_user(user.id); await bot.db.set_balance(user.id, amount)
    await bot.db.log_transaction(user.id, "admin_set", amount, f"Set by {interaction.user.id}")
    await interaction.followup.send(embed=info_embed(f"✅ Set {user.display_name}'s balance to 🪙 **{fmt(amount)}**.", color=COLOR_TEAL), ephemeral=True)

@bot.tree.command(name="clearwager", description="[Owner] Reset a user's wagered amount.")
@app_commands.describe(user="Target user")
@owner_only()
async def clearwager(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with bot.db.pool.acquire() as conn:
        await conn.execute("UPDATE users SET total_wagered = 0 WHERE user_id = $1", user.id)
    await interaction.followup.send(embed=info_embed(f"✅ Cleared wager for {user.display_name}.", color=COLOR_TEAL), ephemeral=True)

@bot.tree.command(name="userinfo", description="[Staff] View full info on a user.")
@app_commands.describe(user="Target user")
async def userinfo(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not await bot.db.is_whitelisted(interaction.user.id) and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(embed=error_embed("No permission."), ephemeral=True); return
    data = await bot.db.get_user(user.id)
    embed = info_embed(f"👤 {user.display_name}", color=COLOR_TEAL)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Balance",       value=f"🪙 **{fmt(data['balance'])}**",       inline=True)
    embed.add_field(name="Total Wagered", value=f"🪙 **{fmt(data['total_wagered'])}**", inline=True)
    embed.add_field(name="Rank",          value=f"**{data['rank']}**",                  inline=True)
    embed.add_field(name="Last Daily",    value=str(data.get("last_daily","Never")),    inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed("You don't have permission."), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
#  WEB SERVER + ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def run_webserver():
    async def handle(request): return web.Response(text="Shillings Bot is alive!")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()
    print("Web server running.")

async def main():
    async with bot:
        await asyncio.gather(run_webserver(), bot.start(os.environ["DISCORD_TOKEN"]))

if __name__ == "__main__":
    asyncio.run(main())
