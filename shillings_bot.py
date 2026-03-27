"""
╔══════════════════════════════════════════════════════════╗
║           SHILLINGS CASINO — Discord Gambling Bot        ║
║                  Single-file edition                     ║
╚══════════════════════════════════════════════════════════╝
ENV VARS REQUIRED:
  DISCORD_TOKEN   — your bot token
  OWNER_ID        — your Discord user ID (int)
  DATABASE_URL    — postgresql://... from Render
  RENDER_EXTERNAL_URL — https://your-app.onrender.com (optional, for UptimeRobot)
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncpg, asyncio, os, random, time
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════
PREFIX = "!"
DAILY_COOLDOWN_HOURS = 3
DAILY_TIERS = [
    ("🪨 Small",   1,        100_000,   60),   # (name, min, max, weight)
    ("🥉 Decent",  100_001,  1_000_000, 25),
    ("🥈 Nice",    1_000_001,5_000_000, 10),
    ("💎 Rare",    5_000_001,15_000_000, 4),
    ("👑 Jackpot", 15_000_001,20_000_000,1),
]

# Embed colours
C_GOLD    = 0xF5C518
C_GREEN   = 0x2ECC71
C_RED     = 0xE74C3C
C_BLUE    = 0x3498DB
C_PURPLE  = 0x9B59B6
C_DARK    = 0x1A1A2E
C_ORANGE  = 0xE67E22

SHILLING  = "🪙"   # currency emoji

# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════
class DB:
    pool: asyncpg.Pool = None

    @classmethod
    async def connect(cls):
        import ssl as _ssl
        url = os.environ["DATABASE_URL"]
        # Render appends ?sslmode=require — strip it so asyncpg doesn't get confused
        url = url.replace("?sslmode=require", "").replace("&sslmode=require", "")
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        cls.pool = await asyncpg.create_pool(url, ssl=ctx, min_size=1, max_size=5)
        await cls.init_tables()

    @classmethod
    async def init_tables(cls):
        async with cls.pool.acquire() as c:
            await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT,
                balance     BIGINT  DEFAULT 1000,
                wager_req   BIGINT  DEFAULT 0,
                wager_done  BIGINT  DEFAULT 0,
                total_won   BIGINT  DEFAULT 0,
                total_lost  BIGINT  DEFAULT 0,
                games_played INT    DEFAULT 0,
                last_daily  BIGINT  DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id   BIGINT PRIMARY KEY,
                added_by  BIGINT,
                added_at  TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id   BIGINT PRIMARY KEY,
                log_channel BIGINT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS gamble_tickets (
                channel_id  BIGINT PRIMARY KEY,
                guild_id    BIGINT,
                creator_id  BIGINT,
                opponent_id BIGINT,
                pot         BIGINT DEFAULT 0,
                status      TEXT   DEFAULT 'open',
                created_at  TIMESTAMP DEFAULT NOW()
            );
            """)

    # ── helpers ────────────────────────────────────────────
    @classmethod
    async def ensure(cls, uid: int, uname: str):
        async with cls.pool.acquire() as c:
            await c.execute("""
                INSERT INTO users(user_id,username) VALUES($1,$2)
                ON CONFLICT(user_id) DO UPDATE SET username=$2
            """, uid, uname)

    @classmethod
    async def get(cls, uid: int):
        async with cls.pool.acquire() as c:
            return await c.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

    @classmethod
    async def bal(cls, uid: int) -> int:
        r = await cls.get(uid)
        return r["balance"] if r else 0

    @classmethod
    async def update_bal(cls, uid: int, delta: int):
        """Add/subtract, track won/lost, track wager progress"""
        async with cls.pool.acquire() as c:
            if delta > 0:
                await c.execute("""UPDATE users SET balance=balance+$1,
                    total_won=total_won+$1 WHERE user_id=$2""", delta, uid)
            else:
                lost = abs(delta)
                await c.execute("""UPDATE users SET balance=GREATEST(0,balance-$1),
                    total_lost=total_lost+$1,
                    wager_done=wager_done+$1
                    WHERE user_id=$2""", lost, uid)
                # also count wagers on wins
            # wager progress on bets regardless of outcome
            if delta < 0:
                pass  # already counted above
            else:
                await c.execute("""UPDATE users SET wager_done=wager_done+$1
                    WHERE user_id=$2 AND wager_req > wager_done""", delta, uid)

    @classmethod
    async def add_wager(cls, uid: int, wager: int):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET wager_req=wager_req+$1 WHERE user_id=$2", wager, uid)

    @classmethod
    async def set_wager(cls, uid: int, amount: int):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET wager_req=$1, wager_done=0 WHERE user_id=$2", amount, uid)

    @classmethod
    async def set_balance(cls, uid: int, amount: int):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET balance=$1 WHERE user_id=$2", amount, uid)

    @classmethod
    async def clear_balance(cls, uid: int):
        await cls.set_balance(uid, 0)

    @classmethod
    async def add_raw(cls, uid: int, amount: int):
        """Add balance without touching won/lost stats (for admin/deposit)"""
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", amount, uid)

    @classmethod
    async def remove_raw(cls, uid: int, amount: int):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET balance=GREATEST(0,balance-$1) WHERE user_id=$2", amount, uid)

    @classmethod
    async def inc_games(cls, uid: int):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id=$1", uid)

    @classmethod
    async def get_daily_ts(cls, uid: int) -> int:
        r = await cls.get(uid)
        return r["last_daily"] if r else 0

    @classmethod
    async def set_daily_ts(cls, uid: int, ts: int):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE users SET last_daily=$1 WHERE user_id=$2", ts, uid)

    @classmethod
    async def leaderboard(cls, n=10):
        async with cls.pool.acquire() as c:
            return await c.fetch("SELECT * FROM users ORDER BY balance DESC LIMIT $1", n)

    # whitelist
    @classmethod
    async def is_staff(cls, uid: int) -> bool:
        async with cls.pool.acquire() as c:
            return bool(await c.fetchrow("SELECT 1 FROM whitelist WHERE user_id=$1", uid))

    @classmethod
    async def wl_add(cls, uid: int, by: int):
        async with cls.pool.acquire() as c:
            await c.execute("INSERT INTO whitelist(user_id,added_by) VALUES($1,$2) ON CONFLICT DO NOTHING", uid, by)

    @classmethod
    async def wl_remove(cls, uid: int):
        async with cls.pool.acquire() as c:
            await c.execute("DELETE FROM whitelist WHERE user_id=$1", uid)

    @classmethod
    async def wl_list(cls):
        async with cls.pool.acquire() as c:
            return await c.fetch("SELECT * FROM whitelist")

    # guild settings / logs
    @classmethod
    async def get_log_channel(cls, guild_id: int):
        async with cls.pool.acquire() as c:
            r = await c.fetchrow("SELECT log_channel FROM guild_settings WHERE guild_id=$1", guild_id)
            return r["log_channel"] if r else None

    @classmethod
    async def set_log_channel(cls, guild_id: int, channel_id: int):
        async with cls.pool.acquire() as c:
            await c.execute("""
                INSERT INTO guild_settings(guild_id, log_channel) VALUES($1,$2)
                ON CONFLICT(guild_id) DO UPDATE SET log_channel=$2
            """, guild_id, channel_id)

    # gamble tickets
    @classmethod
    async def create_ticket(cls, channel_id, guild_id, creator_id, opponent_id):
        async with cls.pool.acquire() as c:
            await c.execute("""INSERT INTO gamble_tickets
                (channel_id,guild_id,creator_id,opponent_id)
                VALUES($1,$2,$3,$4)""", channel_id, guild_id, creator_id, opponent_id)

    @classmethod
    async def get_ticket(cls, channel_id):
        async with cls.pool.acquire() as c:
            return await c.fetchrow("SELECT * FROM gamble_tickets WHERE channel_id=$1", channel_id)

    @classmethod
    async def update_ticket(cls, channel_id, **kw):
        async with cls.pool.acquire() as c:
            sets = ", ".join(f"{k}=${i+2}" for i,k in enumerate(kw))
            await c.execute(f"UPDATE gamble_tickets SET {sets} WHERE channel_id=$1", channel_id, *kw.values())

    @classmethod
    async def delete_ticket(cls, channel_id):
        async with cls.pool.acquire() as c:
            await c.execute("DELETE FROM gamble_tickets WHERE channel_id=$1", channel_id)

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════
def owner_id() -> int:
    return int(os.environ.get("OWNER_ID", 0))

def is_owner(uid: int) -> bool:
    return uid == owner_id()

async def is_staff(uid: int) -> bool:
    return is_owner(uid) or await DB.is_staff(uid)

def fmt(n: int) -> str:
    """Format number with commas"""
    return f"{n:,}"

def fmt_bal(n: int) -> str:
    return f"{SHILLING} **{fmt(n)}**"

def parse_bet(s: str, bal: int):
    s = s.lower().strip()
    if s in ("all","allin","max"): return bal
    if s == "half": return bal // 2
    if s.endswith("k"): 
        try: return int(float(s[:-1]) * 1_000)
        except: return None
    if s.endswith("m"):
        try: return int(float(s[:-1]) * 1_000_000)
        except: return None
    try:
        v = int(s.replace(",",""))
        return v if v > 0 else None
    except: return None

def progress_bar(done: int, total: int, length: int = 12) -> str:
    if total <= 0: return "▓" * length + " ✅"
    pct = min(done / total, 1.0)
    filled = int(pct * length)
    bar = "▓" * filled + "░" * (length - filled)
    return f"`{bar}` {int(pct*100)}%"

async def log(guild: discord.Guild, embed: discord.Embed):
    """Send to log channel if configured"""
    if not guild: return
    ch_id = await DB.get_log_channel(guild.id)
    if not ch_id: return
    ch = guild.get_channel(ch_id)
    if ch:
        try: await ch.send(embed=embed)
        except: pass

def log_embed(title: str, color: int, **fields) -> discord.Embed:
    e = discord.Embed(title=title, color=color, timestamp=datetime.utcnow())
    for k, v in fields.items():
        e.add_field(name=k, value=str(v), inline=True)
    return e

async def staff_guard(interaction: discord.Interaction) -> bool:
    if await is_staff(interaction.user.id): return True
    await interaction.response.send_message(
        embed=discord.Embed(description="❌ Staff only.", color=C_RED), ephemeral=True)
    return False

async def owner_guard(interaction: discord.Interaction) -> bool:
    if is_owner(interaction.user.id): return True
    await interaction.response.send_message(
        embed=discord.Embed(description="❌ Owner only.", color=C_RED), ephemeral=True)
    return False

async def db_ready(interaction: discord.Interaction) -> bool:
    """Check DB is connected before any command that needs it."""
    if DB.pool is None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Bot Starting Up",
                description="The database is still connecting. Please wait 10 seconds and try again.",
                color=C_ORANGE),
            ephemeral=True)
        return False
    return True

async def get_bet(interaction: discord.Interaction, bet_str: str):
    if not await db_ready(interaction): return None
    uid = interaction.user.id
    await DB.ensure(uid, str(interaction.user))
    bal = await DB.bal(uid)
    bet = parse_bet(bet_str, bal)
    if bet is None:
        await interaction.response.send_message(
            embed=discord.Embed(description="❌ Invalid bet. Use a number, `all`, `half`, `5k`, `1m`.", color=C_RED),
            ephemeral=True)
        return None
    if bet > bal:
        await interaction.response.send_message(
            embed=discord.Embed(description=f"❌ Insufficient funds.\nYou have {fmt_bal(bal)}.", color=C_RED),
            ephemeral=True)
        return None
    if bet < 1:
        await interaction.response.send_message(
            embed=discord.Embed(description="❌ Minimum bet is 1 Shilling.", color=C_RED), ephemeral=True)
        return None
    return bet

# ═══════════════════════════════════════════════════════════
#  BOT SETUP
# ═══════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

@bot.event
async def on_ready():
    print(f"🔄 Logged in as {bot.user} — connecting to DB...")
    try:
        await DB.connect()
        print("✅ Database connected and tables ready.")
    except Exception as e:
        print(f"❌ DATABASE CONNECTION FAILED: {e}")
        return  # Can't proceed without DB

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"❌ FAILED TO SYNC COMMANDS: {e}")

    print(f"✅ {bot.user} is online in {len(bot.guilds)} guild(s)!")
    asyncio.create_task(_keep_alive())

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global slash command error handler — shows errors in Discord instead of silently failing."""
    msg = f"❌ Something went wrong: `{error}`"
    if isinstance(error, app_commands.CommandInvokeError):
        msg = f"❌ Error: `{error.original}`"
        print(f"[CMD ERROR] /{interaction.command.name if interaction.command else '?'}: {error.original}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

async def _keep_alive():
    import aiohttp
    url = os.environ.get("RENDER_EXTERNAL_URL","")
    if not url: return
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url)
        except: pass
        await asyncio.sleep(840)

# ═══════════════════════════════════════════════════════════
#  ──────────────  ECONOMY COMMANDS  ──────────────
# ═══════════════════════════════════════════════════════════

# ── /daily ───────────────────────────────────────────────
@bot.tree.command(name="daily", description="Claim your daily Shillings every 3 hours")
async def cmd_daily(interaction: discord.Interaction):
    if not await db_ready(interaction): return
    uid = interaction.user.id
    await DB.ensure(uid, str(interaction.user))
    now = int(time.time())
    last = await DB.get_daily_ts(uid)
    cooldown = DAILY_COOLDOWN_HOURS * 3600
    if now - last < cooldown:
        rem = cooldown - (now - last)
        h, r = divmod(rem, 3600); m = r // 60
        e = discord.Embed(
            title="⏰  Daily Not Ready",
            description=f"Come back in **{h}h {m}m**.",
            color=C_ORANGE)
        return await interaction.response.send_message(embed=e, ephemeral=True)

    tier = random.choices(DAILY_TIERS, weights=[t[3] for t in DAILY_TIERS])[0]
    name, lo, hi, _ = tier
    amount = random.randint(lo, hi)
    await DB.add_raw(uid, amount)
    await DB.set_daily_ts(uid, now)
    bal = await DB.bal(uid)

    e = discord.Embed(title="🎁  Daily Claimed!", color=C_GOLD)
    e.add_field(name="Rarity", value=name)
    e.add_field(name="Earned", value=fmt_bal(amount))
    e.add_field(name="Balance", value=fmt_bal(bal))
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text=f"Next daily in {DAILY_COOLDOWN_HOURS}h")
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("📅 Daily Claimed", C_GOLD,
        User=interaction.user.mention, Rarity=name, Amount=fmt(amount), Balance=fmt(bal)))

# ── /balance ─────────────────────────────────────────────
@bot.tree.command(name="balance", description="Check your balance or someone else's")
@app_commands.describe(user="User to check (defaults to you)")
async def cmd_balance(interaction: discord.Interaction, user: discord.Member = None):
    if not await db_ready(interaction): return
    target = user or interaction.user
    await DB.ensure(target.id, str(target))
    row = await DB.get(target.id)
    bal   = row["balance"]
    wr    = row["wager_req"]
    wd    = row["wager_done"]
    w_left = max(0, wr - wd)

    e = discord.Embed(title=f"💰  {target.display_name}'s Wallet", color=C_GOLD)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name=f"{SHILLING} Balance",   value=fmt(bal))
    e.add_field(name="🎮 Games",              value=fmt(row["games_played"]))
    e.add_field(name="📈 Won",                value=fmt(row["total_won"]))
    e.add_field(name="📉 Lost",               value=fmt(row["total_lost"]))
    if wr > 0:
        e.add_field(name="⚠️ Wager Requirement",
            value=f"{fmt(wd)} / {fmt(wr)} ({fmt(w_left)} left)\n{progress_bar(wd, wr)}",
            inline=False)
    else:
        e.add_field(name="✅ Wager", value="No requirement", inline=False)
    await interaction.response.send_message(embed=e)

# ── /leaderboard ─────────────────────────────────────────
@bot.tree.command(name="leaderboard", description="Top 10 richest players")
async def cmd_lb(interaction: discord.Interaction):
    if not await db_ready(interaction): return
    rows = await DB.leaderboard(10)
    if not rows:
        return await interaction.response.send_message("No data yet.", ephemeral=True)
    medals = ["🥇","🥈","🥉"]
    lines = []
    for i, row in enumerate(rows):
        m = medals[i] if i < 3 else f"`{i+1}.`"
        u = interaction.guild.get_member(row["user_id"])
        name = u.display_name if u else (row["username"] or "Unknown")
        lines.append(f"{m} **{name}** — {SHILLING} {fmt(row['balance'])}")
    e = discord.Embed(title="🏆  Richest Players", description="\n".join(lines), color=C_GOLD)
    await interaction.response.send_message(embed=e)

# ── /tip ─────────────────────────────────────────────────
@bot.tree.command(name="tip", description="Tip another user some Shillings")
@app_commands.describe(user="Who to tip", amount="How much")
async def cmd_tip(interaction: discord.Interaction, user: discord.Member, amount: str):
    if user.id == interaction.user.id:
        return await interaction.response.send_message(
            embed=discord.Embed(description="❌ Can't tip yourself.", color=C_RED), ephemeral=True)
    if user.bot:
        return await interaction.response.send_message(
            embed=discord.Embed(description="❌ Can't tip bots.", color=C_RED), ephemeral=True)
    bet = await get_bet(interaction, amount)
    if bet is None: return
    await DB.remove_raw(interaction.user.id, bet)
    await DB.ensure(user.id, str(user))
    await DB.add_raw(user.id, bet)
    e = discord.Embed(title="💸  Tip Sent!", color=C_GREEN)
    e.add_field(name="From", value=interaction.user.mention)
    e.add_field(name="To",   value=user.mention)
    e.add_field(name="Amount", value=fmt_bal(bet))
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("💸 Tip", C_GREEN,
        From=interaction.user.mention, To=user.mention, Amount=fmt(bet)))

# ── /roll ─────────────────────────────────────────────────
@bot.tree.command(name="roll", description="Roll a random number 1–100 (useful in gamble tickets)")
async def cmd_roll(interaction: discord.Interaction):
    n = random.randint(1, 100)
    e = discord.Embed(title="🎲  Roll", description=f"{interaction.user.mention} rolled **{n}**", color=C_BLUE)
    await interaction.response.send_message(embed=e)

# ═══════════════════════════════════════════════════════════
#  ──────────────  GAME HELPERS  ──────────────
# ═══════════════════════════════════════════════════════════
SUITS = ["♠️","♥️","♦️","♣️"]
RANKS = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
SLOT_SYMS  = ["🍒","🍋","🍊","🍇","⭐","🔔","💎","7️⃣"]
SLOT_W     = [30,25,20,15,6,4,2,1]  # weights
SLOT_PAY   = {"7️⃣":50,"💎":30,"🔔":15,"⭐":10,"🍇":7,"🍊":5,"🍋":4,"🍒":3}

def new_deck():
    d = [(r,s) for r in RANKS for s in SUITS]
    random.shuffle(d)
    return d

def card_val(r):
    if r in ("J","Q","K"): return 10
    if r == "A": return 11
    return int(r)

def hand_total(hand):
    t = sum(card_val(r) for r,_ in hand)
    aces = sum(1 for r,_ in hand if r=="A")
    while t > 21 and aces:
        t -= 10; aces -= 1
    return t

def hand_str(hand):
    return "  ".join(f"`{r}{s}`" for r,s in hand)

def card_rank_int(r):
    order = {"A":14,"K":13,"Q":12,"J":11,"10":10,"9":9,"8":8,"7":7,
             "6":6,"5":5,"4":4,"3":3,"2":2}
    return order.get(r, 0)

# ═══════════════════════════════════════════════════════════
#  ──────────────  GAMES  ──────────────
# ═══════════════════════════════════════════════════════════

# ── /coinflip ────────────────────────────────────────────
@bot.tree.command(name="coinflip", description="50/50 coin flip — doubles your bet")
@app_commands.describe(bet="Amount to bet", choice="heads or tails")
@app_commands.choices(choice=[
    app_commands.Choice(name="Heads",  value="heads"),
    app_commands.Choice(name="Tails",  value="tails"),
])
async def cmd_coinflip(interaction: discord.Interaction, bet: str, choice: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    flip = random.choice(["heads","tails"])
    won  = flip == choice
    net  = amount if won else -amount
    if won: await DB.update_bal(uid, amount)
    else:   await DB.update_bal(uid, -amount)
    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    coin = "🟡" if flip == "heads" else "⚫"
    e = discord.Embed(
        title=f"{'🎉 You Won!' if won else '😞 You Lost'}",
        color=C_GREEN if won else C_RED)
    e.add_field(name="Result", value=f"{coin} **{flip.capitalize()}**")
    e.add_field(name="Net",    value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance",value=fmt_bal(bal))
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("🪙 Coinflip", C_GREEN if won else C_RED,
        User=interaction.user.mention, Bet=fmt(amount),
        Result=flip, Net=f"{'+'if net>=0 else ''}{fmt(net)}"))

# ── /slots ────────────────────────────────────────────────
@bot.tree.command(name="slots", description="Spin the slot machine")
@app_commands.describe(bet="Amount to bet")
async def cmd_slots(interaction: discord.Interaction, bet: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    await interaction.response.defer()

    # Deduct bet upfront
    await DB.remove_raw(uid, amount)

    reels = random.choices(SLOT_SYMS, weights=SLOT_W, k=3)
    jackpot = reels[0] == reels[1] == reels[2]
    two     = not jackpot and (reels[0]==reels[1] or reels[1]==reels[2])

    if jackpot:
        multiplier = SLOT_PAY.get(reels[0], 3)
        payout = amount * multiplier
        net = payout - amount
        await DB.add_raw(uid, payout)          # return full payout
        result = f"🎉 **JACKPOT! {multiplier}x**"
        color = C_GOLD
    elif two:
        net = 0
        await DB.add_raw(uid, amount)           # return stake (push)
        result = "✨ **Two of a kind — push!**"
        color = C_BLUE
    else:
        net = -amount
        result = "😞 No match"
        color = C_RED

    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    e = discord.Embed(title="🎰  Slots", color=color)
    e.description = f"**｜ {' ｜ '.join(reels)} ｜**\n\n{result}"
    e.add_field(name="Bet",     value=fmt(amount))
    e.add_field(name="Net",     value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance", value=fmt_bal(bal))
    await interaction.followup.send(embed=e)

# ── /roulette ─────────────────────────────────────────────
ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

@bot.tree.command(name="roulette", description="Bet on red/black/odd/even/third/exact number")
@app_commands.describe(bet="Amount to bet", choice="red/black/odd/even/1st/2nd/3rd or 0-36")
async def cmd_roulette(interaction: discord.Interaction, bet: str, choice: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    num = random.randint(0, 36)
    c = choice.lower().strip()
    color_icon = "🟢" if num==0 else ("🔴" if num in ROULETTE_RED else "⚫")

    multi = 0
    if c=="red"   and num in ROULETTE_RED and num!=0: multi=2
    elif c=="black" and num not in ROULETTE_RED and num!=0: multi=2
    elif c=="odd"   and num%2==1:  multi=2
    elif c=="even"  and num%2==0 and num!=0: multi=2
    elif c=="green" and num==0:    multi=14
    elif c=="1st"   and 1<=num<=12: multi=3
    elif c=="2nd"   and 13<=num<=24: multi=3
    elif c=="3rd"   and 25<=num<=36: multi=3
    else:
        try:
            if int(c)==num: multi=36
        except: pass

    if multi > 0:
        payout = amount * multi
        net = payout - amount
        await DB.update_bal(uid, net)
        color = C_GREEN
    else:
        net = -amount
        await DB.update_bal(uid, -amount)
        color = C_RED

    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    e = discord.Embed(title="🎡  Roulette", color=color)
    e.description = f"{color_icon} Ball landed on **{num}**"
    e.add_field(name="Your Bet", value=choice.upper())
    e.add_field(name="Multi",    value=f"{multi}x" if multi else "❌ Miss")
    e.add_field(name="Net",      value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance",  value=fmt_bal(bal))
    e.set_footer(text="Choices: red black odd even green 1st 2nd 3rd 0-36")
    await interaction.response.send_message(embed=e)

# ── /blackjack ────────────────────────────────────────────
active_bj = {}  # uid -> {bet, player, dealer, deck, doubled}

class BJView(discord.ui.View):
    def __init__(self, uid, bet):
        super().__init__(timeout=60)
        self.uid = uid
        self.bet = bet

    async def end_game(self, interaction, reason=None):
        game = active_bj.pop(self.uid, None)
        if not game: return
        player, dealer, deck, bet = game["player"], game["dealer"], game["deck"], game["bet"]
        doubled = game.get("doubled", False)

        while hand_total(dealer) < 17:
            dealer.append(deck.pop())

        pv, dv = hand_total(player), hand_total(dealer)

        # Bet was already deducted when game started (and doubled if doubled).
        # So we only need to ADD back winnings — never deduct again.
        if reason == "bust" or pv > 21:
            net = -bet; outcome = "💥 Bust! You lost."
            # nothing to add — bet already gone
        elif reason == "bj" and len(player) == 2 and pv == 21:
            bonus = int(bet * 0.5)
            net = bet + bonus
            outcome = "🃏 Blackjack! 1.5x payout!"
            await DB.add_raw(self.uid, bet + bonus)   # return stake + 50% bonus
        elif dv > 21 or pv > dv:
            net = bet
            await DB.add_raw(self.uid, bet * 2)       # return stake + equal profit
            outcome = "🎉 You win!"
        elif pv == dv:
            net = 0
            await DB.add_raw(self.uid, bet)            # return stake only
            outcome = "🤝 Push — bet returned."
        else:
            net = -bet; outcome = "😞 Dealer wins."
            # nothing to add — bet already gone

        await DB.inc_games(self.uid)
        bal = await DB.bal(self.uid)
        e = discord.Embed(title="🃏  Blackjack — Result",
                          color=C_GREEN if net >= 0 else C_RED)
        e.add_field(name="Your Hand",   value=f"{hand_str(player)} = **{pv}**", inline=False)
        e.add_field(name="Dealer Hand", value=f"{hand_str(dealer)} = **{dv}**", inline=False)
        e.add_field(name="Result",      value=outcome)
        e.add_field(name="Net",         value=f"{'+'if net>=0 else ''}{fmt(net)}")
        e.add_field(name="Balance",     value=fmt_bal(bal))
        for item in self.children: item.disabled = True
        try: await interaction.response.edit_message(embed=e, view=self)
        except: await interaction.message.edit(embed=e, view=self)
        await log(interaction.guild, log_embed("🃏 Blackjack", C_GREEN if net>=0 else C_RED,
            User=f"<@{self.uid}>", Bet=fmt(bet), Net=f"{'+'if net>=0 else ''}{fmt(net)}",
            Result=outcome))

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: return await interaction.response.defer()
        game = active_bj.get(self.uid)
        if not game: return
        game["player"].append(game["deck"].pop())
        pv = hand_total(game["player"])
        e = discord.Embed(title="🃏  Blackjack", color=C_BLUE)
        e.add_field(name="Your Hand",   value=f"{hand_str(game['player'])} = **{pv}**")
        e.add_field(name="Dealer Shows", value=f"`{game['dealer'][0][0]}{game['dealer'][0][1]}`  ?")
        e.add_field(name="Bet",         value=fmt(game["bet"]))
        if pv > 21:
            return await self.end_game(interaction, "bust")
        if pv == 21:
            return await self.end_game(interaction, "stand")
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="🖐️")
    async def stand_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: return await interaction.response.defer()
        await self.end_game(interaction, "stand")

    @discord.ui.button(label="Double", style=discord.ButtonStyle.danger, emoji="⚡")
    async def double_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid: return await interaction.response.defer()
        game = active_bj.get(self.uid)
        if not game: return
        bal = await DB.bal(self.uid)
        if bal < game["bet"]:
            return await interaction.response.send_message("❌ Not enough balance to double.", ephemeral=True)
        await DB.update_bal(self.uid, -game["bet"])
        game["bet"] *= 2
        game["doubled"] = True
        game["player"].append(game["deck"].pop())
        await self.end_game(interaction, "stand")

    async def on_timeout(self):
        game = active_bj.pop(self.uid, None)
        if game:
            await DB.add_raw(self.uid, game["bet"])  # refund on timeout

@bot.tree.command(name="blackjack", description="Classic blackjack vs the dealer")
@app_commands.describe(bet="Amount to bet")
async def cmd_blackjack(interaction: discord.Interaction, bet: str):
    uid = interaction.user.id
    if uid in active_bj:
        return await interaction.response.send_message("❌ You have an active game!", ephemeral=True)
    amount = await get_bet(interaction, bet)
    if amount is None: return
    await DB.update_bal(uid, -amount)
    deck = new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    active_bj[uid] = {"bet": amount, "player": player, "dealer": dealer, "deck": deck, "doubled": False}
    pv = hand_total(player)
    view = BJView(uid, amount)
    e = discord.Embed(title="🃏  Blackjack", color=C_BLUE)
    e.add_field(name="Your Hand",    value=f"{hand_str(player)} = **{pv}**")
    e.add_field(name="Dealer Shows", value=f"`{dealer[0][0]}{dealer[0][1]}`  ?")
    e.add_field(name="Bet",          value=fmt(amount))
    e.set_footer(text="Hit / Stand / Double Down")
    if pv == 21:
        await interaction.response.send_message(embed=e)
        await view.end_game(interaction, "bj")
    else:
        await interaction.response.send_message(embed=e, view=view)

# ── /limbo ────────────────────────────────────────────────
@bot.tree.command(name="limbo", description="Set your own target multiplier — higher = riskier")
@app_commands.describe(bet="Amount to bet", target="Multiplier target e.g. 2.5")
async def cmd_limbo(interaction: discord.Interaction, bet: str, target: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    try:
        t = float(target)
        if t < 1.01 or t > 1000:
            raise ValueError
    except:
        return await interaction.response.send_message("❌ Target must be 1.01–1000.", ephemeral=True)
    uid = interaction.user.id
    win_chance = 0.96 / t
    won = random.random() < win_chance
    result_multi = round(random.uniform(1.0, t * 3), 2) if not won else t

    if won:
        payout = int(amount * t)
        net = payout - amount
        await DB.update_bal(uid, net)
        color = C_GREEN
        desc = f"✅ Result: **{result_multi:.2f}x** — Hit target **{t}x**!"
    else:
        net = -amount
        await DB.update_bal(uid, -amount)
        color = C_RED
        desc = f"❌ Result: **{result_multi:.2f}x** — Missed **{t}x**"

    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    e = discord.Embed(title="🎯  Limbo", description=desc, color=color)
    e.add_field(name="Target",  value=f"{t}x")
    e.add_field(name="Win %",   value=f"{win_chance*100:.1f}%")
    e.add_field(name="Net",     value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance", value=fmt_bal(bal))
    await interaction.response.send_message(embed=e)

# ── /crash ────────────────────────────────────────────────
@bot.tree.command(name="crash", description="Multiplier climbs until it crashes — cash out in time!")
@app_commands.describe(bet="Amount to bet")
async def cmd_crash(interaction: discord.Interaction, bet: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    await DB.update_bal(uid, -amount)
    crash_at = round(max(1.0, random.expovariate(0.5) + 1), 2)

    class CrashView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.cashed = False
            self.cashout_multi = None
        @discord.ui.button(label="💰  Cash Out", style=discord.ButtonStyle.success)
        async def cashout(self, intr: discord.Interaction, btn: discord.ui.Button):
            if intr.user.id != uid: return await intr.response.defer()
            self.cashed = True
            self.cashout_multi = current[0]
            btn.disabled = True
            self.stop()
            await intr.response.defer()

    view = CrashView()
    current = [1.00]

    e = discord.Embed(title="🚀  Crash", description="**1.00x** 🟢 climbing…", color=C_GREEN)
    e.add_field(name="Bet", value=fmt(amount))
    e.set_footer(text=f"Crash point hidden | Click Cash Out!")
    await interaction.response.send_message(embed=e, view=view)
    msg = await interaction.original_response()

    while current[0] < crash_at and not view.cashed:
        await asyncio.sleep(1.5)
        current[0] = round(current[0] + random.uniform(0.1, 0.6), 2)
        if current[0] >= crash_at: break
        pct = min(current[0] / 10, 1.0)
        bar = "🟢" * int(pct * 10) or "🟡"
        upd = discord.Embed(title="🚀  Crash", color=C_GREEN,
            description=f"**{current[0]:.2f}x** {bar}")
        upd.add_field(name="Bet", value=fmt(amount))
        upd.set_footer(text="Cash out before it crashes!")
        try: await msg.edit(embed=upd, view=view)
        except: break

    if view.cashed and view.cashout_multi:
        m = view.cashout_multi
        payout = int(amount * m)
        net = payout - amount
        await DB.add_raw(uid, payout)          # return stake + profit (bet already deducted)
        color = C_GREEN
        result = f"✅ Cashed out at **{m:.2f}x** — +{fmt(net)}"
    else:
        net = -amount
        color = C_RED
        result = f"💥 Crashed at **{crash_at:.2f}x** — lost everything"

    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    final = discord.Embed(title="🚀  Crash — Done", description=result, color=color)
    final.add_field(name="Crash Point", value=f"{crash_at:.2f}x")
    final.add_field(name="Net",         value=f"{'+'if net>=0 else ''}{fmt(net)}")
    final.add_field(name="Balance",     value=fmt_bal(bal))
    for item in view.children: item.disabled = True
    try: await msg.edit(embed=final, view=view)
    except: pass
    await log(interaction.guild, log_embed("🚀 Crash", color,
        User=interaction.user.mention, Bet=fmt(amount),
        Result=result, Net=f"{'+'if net>=0 else ''}{fmt(net)}"))

# ── /higherlower ──────────────────────────────────────────
active_hl = {}  # uid -> {bet, card, streak, total_payout}

class HLView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=60)
        self.uid = uid

    async def on_timeout(self):
        game = active_hl.pop(self.uid, None)
        if game:
            await DB.add_raw(self.uid, game["bet"])  # refund on timeout

    async def resolve(self, interaction, choice):
        if interaction.user.id != self.uid: return await interaction.response.defer()
        game = active_hl.get(self.uid)
        if not game: return
        old_card = game["card"]
        new_card  = (random.choice(RANKS), random.choice(SUITS))
        game["card"] = new_card
        old_v = card_rank_int(old_card[0])
        new_v = card_rank_int(new_card[0])
        correct = (choice=="higher" and new_v > old_v) or (choice=="lower" and new_v < old_v)
        if old_v == new_v: correct = False  # tie = loss

        if correct:
            game["streak"] += 1
            game["total_payout"] = round(game["total_payout"] * 1.5, 2)
            e = discord.Embed(title="🃏  Higher or Lower", color=C_GREEN)
            e.description = (f"Old: `{old_card[0]}{old_card[1]}`  →  New: `{new_card[0]}{new_card[1]}`\n"
                             f"✅ **Correct!** Streak: {game['streak']} | "
                             f"Multi: **{game['total_payout']:.2f}x**")
            e.set_footer(text="Keep going or Cash Out!")
            await interaction.response.edit_message(embed=e, view=self)
        else:
            active_hl.pop(self.uid, None)
            bet = game["bet"]
            # bet already deducted at game start — nothing more to remove
            await DB.inc_games(self.uid)
            bal = await DB.bal(self.uid)
            for item in self.children: item.disabled = True
            e = discord.Embed(title="🃏  Higher or Lower", color=C_RED)
            e.description = (f"Old: `{old_card[0]}{old_card[1]}`  →  New: `{new_card[0]}{new_card[1]}`\n"
                             f"❌ **Wrong!** Lost {fmt(bet)}")
            e.add_field(name="Balance", value=fmt_bal(bal))
            await interaction.response.edit_message(embed=e, view=self)
            await log(interaction.guild, log_embed("🃏 HiLo", C_RED,
                User=f"<@{self.uid}>", Bet=fmt(bet), Result="Lost"))

    @discord.ui.button(label="Higher ⬆️", style=discord.ButtonStyle.primary)
    async def higher(self, interaction, btn): await self.resolve(interaction, "higher")

    @discord.ui.button(label="Lower ⬇️",  style=discord.ButtonStyle.primary)
    async def lower(self, interaction, btn):  await self.resolve(interaction, "lower")

    @discord.ui.button(label="💰 Cash Out", style=discord.ButtonStyle.success)
    async def cashout(self, interaction, btn):
        if interaction.user.id != self.uid: return await interaction.response.defer()
        game = active_hl.pop(self.uid, None)
        if not game: return
        bet = game["bet"]
        payout = int(bet * game["total_payout"])
        net = payout - bet
        await DB.add_raw(self.uid, payout)     # return stake + profit (bet already deducted)
        await DB.inc_games(self.uid)
        bal = await DB.bal(self.uid)
        for item in self.children: item.disabled = True
        e = discord.Embed(title="🃏  Higher or Lower — Cashed Out!", color=C_GOLD)
        e.add_field(name="Streak",  value=game["streak"])
        e.add_field(name="Multi",   value=f"{game['total_payout']:.2f}x")
        e.add_field(name="Payout",  value=fmt_bal(payout))
        e.add_field(name="Balance", value=fmt_bal(bal))
        await interaction.response.edit_message(embed=e, view=self)
        await log(interaction.guild, log_embed("🃏 HiLo", C_GOLD,
            User=f"<@{self.uid}>", Bet=fmt(bet), Net=f"+{fmt(net)}"))

@bot.tree.command(name="higherlower", description="Is the next card higher or lower? Build a streak!")
@app_commands.describe(bet="Amount to bet")
async def cmd_hl(interaction: discord.Interaction, bet: str):
    uid = interaction.user.id
    if uid in active_hl:
        return await interaction.response.send_message("❌ Already in a game!", ephemeral=True)
    amount = await get_bet(interaction, bet)
    if amount is None: return
    await DB.remove_raw(uid, amount)           # deduct bet upfront
    card = (random.choice(RANKS), random.choice(SUITS))
    active_hl[uid] = {"bet": amount, "card": card, "streak": 0, "total_payout": 1.0}
    view = HLView(uid)
    e = discord.Embed(title="🃏  Higher or Lower", color=C_BLUE)
    e.description = f"Current card: **`{card[0]}{card[1]}`**\nWill the next card be higher or lower?"
    e.add_field(name="Bet",  value=fmt(amount))
    e.set_footer(text="Win = 1.5x per correct guess | Cash out anytime")
    await interaction.response.send_message(embed=e, view=view)

# ── /mines ────────────────────────────────────────────────
active_mines = {}  # uid -> {bet, grid, revealed, mine_count, multi}

def mines_multi(revealed: int, total_safe: int) -> float:
    if total_safe <= 0: return 1.0
    m = 1.0
    total = 16
    safe = total - (total - total_safe)  # hmm, recalc cleanly below
    return m

def calc_mines_multi(revealed: int, mine_count: int) -> float:
    safe = 16 - mine_count
    if safe <= 0: return 0
    multi = 1.0
    for i in range(revealed):
        remaining = 16 - i
        safe_remaining = safe - i
        multi *= remaining / safe_remaining
    return round(multi * 0.97, 3)

class MinesView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=120)
        self.uid = uid
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        game = active_mines.get(self.uid)
        if not game: return
        for i in range(16):
            revealed_set = game["revealed"]
            is_revealed = i in revealed_set
            label = "💎" if is_revealed else "·"
            btn = discord.ui.Button(
                label=label, row=i//4,
                style=discord.ButtonStyle.success if is_revealed else discord.ButtonStyle.secondary,
                custom_id=f"mine_{i}", disabled=is_revealed)
            btn.callback = self._make_cb(i)
            self.add_item(btn)
        # Cash out
        co = discord.ui.Button(label="💰 Cash Out", style=discord.ButtonStyle.success,
                                row=4, custom_id="mines_cashout")
        co.callback = self._cashout_cb
        self.add_item(co)

    def _make_cb(self, idx):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.uid: return await interaction.response.defer()
            game = active_mines.get(self.uid)
            if not game: return
            if game["grid"][idx] == "💣":
                # Hit mine
                game["revealed"].add(idx)
                active_mines.pop(self.uid, None)
                bet = game["bet"]
                await DB.update_bal(self.uid, -bet)
                await DB.inc_games(self.uid)
                bal = await DB.bal(self.uid)
                # Reveal all mines
                for i in range(16):
                    game["grid"][i]  # just show
                for item in self.children: item.disabled = True
                e = discord.Embed(title="💣  Mines — BOOM!", color=C_RED)
                grid_str = ""
                for i in range(16):
                    if i in game["revealed"]: grid_str += "💣 "
                    elif game["grid"][i]=="💣": grid_str += "💣 "
                    else: grid_str += "💎 "
                    if (i+1)%4==0: grid_str += "\n"
                e.description = grid_str
                e.add_field(name="Lost", value=fmt_bal(bet))
                e.add_field(name="Balance", value=fmt_bal(bal))
                await interaction.response.edit_message(embed=e, view=self)
                await log(interaction.guild, log_embed("💣 Mines", C_RED,
                    User=f"<@{self.uid}>", Bet=fmt(bet), Result="Hit mine"))
            else:
                game["revealed"].add(idx)
                multi = calc_mines_multi(len(game["revealed"]), game["mine_count"])
                self._update_buttons()
                e = discord.Embed(title="💎  Mines", color=C_BLUE)
                grid_str = ""
                for i in range(16):
                    if i in game["revealed"]: grid_str += "💎 "
                    else: grid_str += "⬛ "
                    if (i+1)%4==0: grid_str += "\n"
                e.description = grid_str
                e.add_field(name="Gems Found",  value=len(game["revealed"]))
                e.add_field(name="Multiplier",  value=f"{multi:.3f}x")
                e.add_field(name="Payout",      value=fmt(int(game["bet"]*multi)))
                e.set_footer(text="Click more tiles or Cash Out")
                await interaction.response.edit_message(embed=e, view=self)
        return callback

    async def _cashout_cb(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid: return await interaction.response.defer()
        game = active_mines.pop(self.uid, None)
        if not game: return
        bet = game["bet"]
        revealed = len(game["revealed"])
        if revealed == 0:
            await DB.add_raw(self.uid, bet)
            return await interaction.response.send_message("↩️ No tiles revealed. Bet returned.", ephemeral=True)
        multi = calc_mines_multi(revealed, game["mine_count"])
        payout = int(bet * multi)
        net = payout - bet
        await DB.update_bal(self.uid, net)
        await DB.inc_games(self.uid)
        bal = await DB.bal(self.uid)
        for item in self.children: item.disabled = True
        e = discord.Embed(title="💰  Mines — Cashed Out!", color=C_GOLD)
        e.add_field(name="Gems",    value=revealed)
        e.add_field(name="Multi",   value=f"{multi:.3f}x")
        e.add_field(name="Payout",  value=fmt_bal(payout))
        e.add_field(name="Balance", value=fmt_bal(bal))
        await interaction.response.edit_message(embed=e, view=self)
        await log(interaction.guild, log_embed("💣 Mines", C_GOLD,
            User=f"<@{self.uid}>", Bet=fmt(bet), Net=f"+{fmt(net)}"))

@bot.tree.command(name="mines", description="Click tiles to find gems — avoid the bombs!")
@app_commands.describe(bet="Amount to bet",
    mines="Number of mines: 1, 3, 5, or 10")
@app_commands.choices(mines=[
    app_commands.Choice(name="1 mine (easy)", value=1),
    app_commands.Choice(name="3 mines",        value=3),
    app_commands.Choice(name="5 mines",        value=5),
    app_commands.Choice(name="10 mines (hard)",value=10),
])
async def cmd_mines(interaction: discord.Interaction, bet: str, mines: int = 3):
    uid = interaction.user.id
    if uid in active_mines:
        return await interaction.response.send_message("❌ Already in a game!", ephemeral=True)
    amount = await get_bet(interaction, bet)
    if amount is None: return
    await DB.remove_raw(uid, amount)           # deduct bet upfront
    grid = ["💣"]*mines + ["💎"]*(16-mines)
    random.shuffle(grid)
    active_mines[uid] = {"bet": amount, "grid": grid, "revealed": set(), "mine_count": mines}
    view = MinesView(uid)
    e = discord.Embed(title="💎  Mines", color=C_BLUE,
        description="⬛ ⬛ ⬛ ⬛\n⬛ ⬛ ⬛ ⬛\n⬛ ⬛ ⬛ ⬛\n⬛ ⬛ ⬛ ⬛")
    e.add_field(name="Bet",   value=fmt(amount))
    e.add_field(name="Mines", value=mines)
    e.set_footer(text="Click tiles to reveal gems. Cash out before hitting a bomb!")
    await interaction.response.send_message(embed=e, view=view)

# ── /bomb ─────────────────────────────────────────────────
@bot.tree.command(name="bomb", description="5 wires — 2 are bombs. Cut safe ones for 1.5x/2.5x/5x")
@app_commands.describe(bet="Amount to bet")
async def cmd_bomb(interaction: discord.Interaction, bet: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    PAYOUTS = [1.5, 2.5, 5.0]
    wires = ["💣","💣","✅","✅","✅"]
    random.shuffle(wires)
    cuts = []
    color_map = {"✅": "🟢", "💣": "🔴"}

    class BombView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.done = False
            for i in range(5):
                btn = discord.ui.Button(label=f"Wire {i+1}", style=discord.ButtonStyle.primary,
                                        custom_id=f"wire_{i}", row=0 if i < 3 else 1)
                btn.callback = self._make_cb(i)
                self.add_item(btn)
            co = discord.ui.Button(label="💰 Cash Out", style=discord.ButtonStyle.success,
                                   custom_id="bomb_co", row=1)
            co.callback = self._cashout
            self.add_item(co)

        def _make_cb(self, idx):
            async def cb(intr: discord.Interaction):
                if intr.user.id != uid or self.done: return await intr.response.defer()
                for item in self.children:
                    if hasattr(item,'custom_id') and item.custom_id==f"wire_{idx}":
                        item.disabled = True
                cuts.append(idx)
                if wires[idx] == "💣":
                    self.done = True
                    await DB.update_bal(uid, -amount)
                    await DB.inc_games(uid)
                    bal = await DB.bal(uid)
                    for item in self.children: item.disabled = True
                    revealed = "  ".join(f"W{i+1}:{color_map[wires[i]]}" for i in range(5))
                    e = discord.Embed(title="💥  Bomb — Boom!", color=C_RED,
                        description=f"Wire {idx+1} was a **BOMB**!\n{revealed}")
                    e.add_field(name="Lost",    value=fmt_bal(amount))
                    e.add_field(name="Balance", value=fmt_bal(bal))
                    await intr.response.edit_message(embed=e, view=self)
                    self.stop()
                    await log(intr.guild, log_embed("💣 Bomb", C_RED,
                        User=f"<@{uid}>", Bet=fmt(amount), Result="Bomb"))
                else:
                    n = len(cuts)
                    if n >= 3:
                        # Auto cash out at max
                        self.done = True
                        multi = PAYOUTS[-1]
                        payout = int(amount*multi); net = payout - amount
                        await DB.update_bal(uid, net)
                        await DB.inc_games(uid); bal = await DB.bal(uid)
                        for item in self.children: item.disabled=True
                        e = discord.Embed(title="🏆  Bomb — Max Payout!", color=C_GOLD,
                            description=f"All 3 safe wires cut! **{multi}x**")
                        e.add_field(name="Payout",  value=fmt_bal(payout))
                        e.add_field(name="Balance", value=fmt_bal(bal))
                        await intr.response.edit_message(embed=e, view=self); self.stop()
                    else:
                        multi = PAYOUTS[n-1]
                        e = discord.Embed(title="✅  Bomb — Safe!", color=C_GREEN,
                            description=f"Wire {idx+1} was safe!\n**{n}/3 safe cuts** — current: **{multi}x**")
                        e.add_field(name="Bet",     value=fmt(amount))
                        e.add_field(name="If cashout now", value=fmt(int(amount*multi)))
                        await intr.response.edit_message(embed=e, view=self)

            return cb

        async def _cashout(self, intr: discord.Interaction):
            if intr.user.id != uid or self.done: return await intr.response.defer()
            self.done = True
            n = len(cuts)
            if n == 0:
                await DB.add_raw(uid, 0)
                return await intr.response.send_message("❌ Cut at least one wire first!", ephemeral=True)
            multi = PAYOUTS[n-1]
            payout = int(amount*multi); net = payout - amount
            await DB.update_bal(uid, net)
            await DB.inc_games(uid); bal = await DB.bal(uid)
            for item in self.children: item.disabled=True
            e = discord.Embed(title="💰  Bomb — Cashed Out!", color=C_GOLD)
            e.add_field(name="Cuts",    value=n)
            e.add_field(name="Multi",   value=f"{multi}x")
            e.add_field(name="Payout",  value=fmt_bal(payout))
            e.add_field(name="Balance", value=fmt_bal(bal))
            await intr.response.edit_message(embed=e, view=self); self.stop()
            await log(intr.guild, log_embed("💣 Bomb", C_GOLD,
                User=f"<@{uid}>", Bet=fmt(amount), Net=f"+{fmt(net)}"))

    e = discord.Embed(title="💣  Bomb — 5 Wires", color=C_PURPLE,
        description="**2 wires are bombs. 3 are safe.**\nCut safe wires for **1.5x → 2.5x → 5x**\nCash out any time!")
    e.add_field(name="Bet", value=fmt(amount))
    view = BombView()
    await interaction.response.send_message(embed=e, view=view)

# ── /war ──────────────────────────────────────────────────
@bot.tree.command(name="war", description="Draw a card vs the dealer — highest card wins 2x")
@app_commands.describe(bet="Amount to bet")
async def cmd_war(interaction: discord.Interaction, bet: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    rounds = 0
    p_card = (random.choice(RANKS), random.choice(SUITS))
    d_card = (random.choice(RANKS), random.choice(SUITS))
    pv, dv = card_rank_int(p_card[0]), card_rank_int(d_card[0])
    history = [f"R1: You `{p_card[0]}{p_card[1]}` vs Dealer `{d_card[0]}{d_card[1]}`"]
    rounds = 1
    while pv == dv and rounds < 5:
        rounds += 1
        p_card = (random.choice(RANKS), random.choice(SUITS))
        d_card = (random.choice(RANKS), random.choice(SUITS))
        pv, dv = card_rank_int(p_card[0]), card_rank_int(d_card[0])
        history.append(f"R{rounds}: You `{p_card[0]}{p_card[1]}` vs Dealer `{d_card[0]}{d_card[1]}`")

    won = pv > dv
    if pv == dv: won = bool(random.getrandbits(1))  # ultimate tie: coin flip
    net = amount if won else -amount
    if won: await DB.update_bal(uid, amount)
    else:   await DB.update_bal(uid, -amount)
    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    e = discord.Embed(title=f"⚔️  War — {'You Win!' if won else 'Dealer Wins'}", color=C_GREEN if won else C_RED)
    e.description = "\n".join(history)
    if rounds > 1: e.description += f"\n💥 _{rounds-1} tie round(s)!_"
    e.add_field(name="Net",     value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance", value=fmt_bal(bal))
    await interaction.response.send_message(embed=e)

# ── /horserace ────────────────────────────────────────────
HORSES = [
    ("🐎 Thunderbolt",   2,  40),
    ("🐴 Midnight Star", 4,  25),
    ("🏇 Lucky Dash",    6,  18),
    ("🦄 Silver Wind",   10, 10),
    ("🐆 Iron Hooves",   15,  5),
    ("🐉 Dragon Fury",   20,  2),
]

@bot.tree.command(name="horserace", description="Pick a horse — higher payout = lower chance!")
@app_commands.describe(bet="Amount to bet", horse="Pick horse 1-6")
@app_commands.choices(horse=[app_commands.Choice(name=f"{i+1}. {h[0]} ({h[1]}x)", value=i+1) for i,h in enumerate(HORSES)])
async def cmd_horserace(interaction: discord.Interaction, bet: str, horse: int):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    winner_idx = random.choices(range(6), weights=[h[2] for h in HORSES])[0]
    winner = HORSES[winner_idx]
    pick = HORSES[horse-1]
    won = horse-1 == winner_idx
    net = (amount * pick[1] - amount) if won else -amount
    if won: await DB.update_bal(uid, amount * pick[1] - amount)
    else:   await DB.update_bal(uid, -amount)
    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    e = discord.Embed(title=f"🏁  Horse Race — {'You Win!' if won else 'Loss'}", color=C_GREEN if won else C_RED)
    e.add_field(name="Winner",  value=winner[0])
    e.add_field(name="Your Pick", value=f"{pick[0]} ({pick[1]}x)")
    e.add_field(name="Net",     value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance", value=fmt_bal(bal))
    await interaction.response.send_message(embed=e)

# ── /numguess ─────────────────────────────────────────────
@bot.tree.command(name="numguess", description="Guess 1-100 — exact=90x, within 3=10x, within 10=3x")
@app_commands.describe(bet="Amount to bet", guess="Your guess 1-100")
async def cmd_numguess(interaction: discord.Interaction, bet: str, guess: int):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    if not 1 <= guess <= 100:
        return await interaction.response.send_message("❌ Guess must be 1–100.", ephemeral=True)
    uid = interaction.user.id
    num = random.randint(1, 100)
    diff = abs(guess - num)
    if diff == 0:   multi = 90;  result = "🎯 **EXACT! 90x!**"
    elif diff <= 3:  multi = 10;  result = f"🔥 **Within 3! 10x** (was {num})"
    elif diff <= 10: multi = 3;   result = f"✅ **Within 10! 3x** (was {num})"
    else:            multi = 0;   result = f"❌ **Miss** (was {num}, off by {diff})"

    if multi > 0:
        payout = amount * multi; net = payout - amount
        await DB.update_bal(uid, net)
    else:
        net = -amount
        await DB.update_bal(uid, -amount)
    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    e = discord.Embed(title="🔢  Number Guess", description=result, color=C_GREEN if multi>0 else C_RED)
    e.add_field(name="Your Guess", value=guess)
    e.add_field(name="Net",        value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance",    value=fmt_bal(bal))
    await interaction.response.send_message(embed=e)

# ── /scratch ──────────────────────────────────────────────
SCRATCH_SYMS = ["💰","⭐","🍒","💎","💣","🍋","🔔","🍇"]

@bot.tree.command(name="scratch", description="3×3 scratch card — match 3 in a row to win!")
@app_commands.describe(bet="Amount to bet")
async def cmd_scratch(interaction: discord.Interaction, bet: str):
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    grid = [random.choice(SCRATCH_SYMS) for _ in range(9)]
    # Check win conditions
    lines = [
        [0,1,2],[3,4,5],[6,7,8],  # rows
        [0,3,6],[1,4,7],[2,5,8],  # cols
        [0,4,8],[2,4,6]           # diags
    ]
    payouts = {"💎":50,"💰":20,"⭐":10,"🔔":7,"🍇":5,"🍒":4,"🍋":3}
    won_lines = []
    for line in lines:
        syms = [grid[i] for i in line]
        if syms[0]==syms[1]==syms[2] and syms[0]!="💣":
            won_lines.append((syms[0], payouts.get(syms[0],2)))

    hit_bomb = "💣" in grid
    if hit_bomb:
        net = -amount
        await DB.update_bal(uid, -amount)
        result = "💣 **Bomb tile! Lost everything.**"
        color = C_RED
    elif won_lines:
        best_multi = max(m for _,m in won_lines)
        payout = amount * best_multi; net = payout - amount
        await DB.update_bal(uid, net)
        result = f"{'  '.join(f'{s} {m}x' for s,m in won_lines)}\n🎉 Best: **{best_multi}x**"
        color = C_GOLD
    else:
        net = -amount
        await DB.update_bal(uid, -amount)
        result = "😞 No matches"
        color = C_RED

    await DB.inc_games(uid)
    bal = await DB.bal(uid)
    grid_str = ""
    for i, s in enumerate(grid):
        grid_str += s + " "
        if (i+1) % 3 == 0: grid_str += "\n"
    e = discord.Embed(title="🎟️  Scratch Card", color=color)
    e.description = grid_str + "\n" + result
    e.add_field(name="Net",     value=f"{'+'if net>=0 else ''}{fmt(net)}")
    e.add_field(name="Balance", value=fmt_bal(bal))
    await interaction.response.send_message(embed=e)

# ── /duel ─────────────────────────────────────────────────
pending_duels = {}  # challenger_id -> {opponent_id, bet, expires}

@bot.tree.command(name="duel", description="Challenge someone — random winner takes both bets!")
@app_commands.describe(user="Who to duel", bet="Amount to wager")
async def cmd_duel(interaction: discord.Interaction, user: discord.Member, bet: str):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("❌ Can't duel yourself.", ephemeral=True)
    if user.bot:
        return await interaction.response.send_message("❌ Can't duel bots.", ephemeral=True)
    amount = await get_bet(interaction, bet)
    if amount is None: return
    uid = interaction.user.id
    await DB.ensure(user.id, str(user))
    opp_bal = await DB.bal(user.id)
    if opp_bal < amount:
        return await interaction.response.send_message(
            f"❌ {user.display_name} doesn't have enough Shillings ({fmt_bal(opp_bal)}).", ephemeral=True)

    pending_duels[uid] = {"opponent": user.id, "bet": amount, "expires": time.time()+60}
    await DB.update_bal(uid, -amount)  # hold challenger bet

    class DuelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
        @discord.ui.button(label="⚔️ Accept Duel", style=discord.ButtonStyle.danger)
        async def accept(self, intr: discord.Interaction, btn: discord.ui.Button):
            if intr.user.id != user.id:
                return await intr.response.send_message("❌ This duel isn't for you.", ephemeral=True)
            duel = pending_duels.pop(uid, None)
            if not duel:
                return await intr.response.send_message("❌ Duel expired.", ephemeral=True)
            await DB.update_bal(user.id, -amount)
            winner = random.choice([uid, user.id])
            loser  = user.id if winner==uid else uid
            prize  = amount * 2
            await DB.update_bal(winner, prize)
            await DB.inc_games(uid); await DB.inc_games(user.id)
            winner_u = interaction.guild.get_member(winner)
            for item in self.children: item.disabled=True
            e = discord.Embed(title="⚔️  Duel — Result!", color=C_GOLD)
            e.description = (f"🏆 **{winner_u.display_name if winner_u else winner}** wins!\n"
                             f"Prize: {fmt_bal(prize)}")
            e.add_field(name=f"{interaction.user.display_name} bal",
                        value=fmt(await DB.bal(uid)))
            e.add_field(name=f"{user.display_name} bal",
                        value=fmt(await DB.bal(user.id)))
            await intr.response.edit_message(embed=e, view=self)
            await log(intr.guild, log_embed("⚔️ Duel", C_GOLD,
                Winner=f"<@{winner}>", Loser=f"<@{loser}>", Prize=fmt(prize)))
        @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.secondary)
        async def decline(self, intr: discord.Interaction, btn: discord.ui.Button):
            if intr.user.id not in (uid, user.id): return await intr.response.defer()
            pending_duels.pop(uid, None)
            await DB.add_raw(uid, amount)  # refund
            for item in self.children: item.disabled=True
            e = discord.Embed(description="❌ Duel declined. Bet refunded.", color=C_RED)
            await intr.response.edit_message(embed=e, view=self)

    e = discord.Embed(title="⚔️  Duel Challenge!", color=C_PURPLE)
    e.description = (f"{interaction.user.mention} challenges {user.mention}!\n"
                     f"Bet: {fmt_bal(amount)} each\n"
                     f"Winner takes **{fmt(amount*2)}** 🪙\n\n"
                     f"{user.mention} — you have **60 seconds** to respond.")
    await interaction.response.send_message(embed=e, view=DuelView())

# ═══════════════════════════════════════════════════════════
#  ──────────────  GAMBLE TICKET  ──────────────
# ═══════════════════════════════════════════════════════════
class TicketView(discord.ui.View):
    """Persistent view for ticket channel controls"""
    def __init__(self, creator_id, channel_id):
        super().__init__(timeout=None)
        self.creator_id  = creator_id
        self.channel_id  = channel_id

    @discord.ui.button(label="➕ Add User", style=discord.ButtonStyle.primary, custom_id="ticket_add")
    async def add_user(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await is_staff(interaction.user.id):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(AddUserModal(interaction.channel))

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await is_staff(interaction.user.id):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing in 5 seconds…")
        await DB.delete_ticket(interaction.channel.id)
        await asyncio.sleep(5)
        try: await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except: pass

class AddUserModal(discord.ui.Modal, title="Add User to Ticket"):
    username = discord.ui.TextInput(label="User ID or @mention", placeholder="123456789")
    def __init__(self, channel): super().__init__(); self.channel = channel
    async def on_submit(self, interaction: discord.Interaction):
        raw = self.username.value.strip().replace("<@","").replace(">","").replace("!","")
        try:
            uid = int(raw)
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
        except:
            return await interaction.response.send_message("❌ Couldn't find that user.", ephemeral=True)
        await self.channel.set_permissions(member, read_messages=True, send_messages=True)
        await interaction.response.send_message(f"✅ {member.mention} added to ticket.")

@bot.tree.command(name="gambleticket", description="Open a private gamble room with an opponent (staff middleman)")
@app_commands.describe(opponent="The other player")
async def cmd_gambleticket(interaction: discord.Interaction, opponent: discord.Member):
    if not await db_ready(interaction): return
    if not await is_staff(interaction.user.id):
        return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
    if opponent.bot or opponent.id == interaction.user.id:
        return await interaction.response.send_message("❌ Invalid opponent.", ephemeral=True)

    guild = interaction.guild
    cat = discord.utils.get(guild.categories, name="🎰 Gamble Tickets")
    if not cat:
        cat = await guild.create_category("🎰 Gamble Tickets", overwrites={
            guild.default_role: discord.PermissionOverwrite(read_messages=False)})

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        opponent: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    ch = await guild.create_text_channel(
        f"🎰┃{interaction.user.display_name}-vs-{opponent.display_name}",
        category=cat, overwrites=overwrites,
        topic=f"Gamble Ticket | Middleman: {interaction.user} | {interaction.user} vs {opponent}")

    await DB.create_ticket(ch.id, guild.id, interaction.user.id, opponent.id)
    view = TicketView(interaction.user.id, ch.id)
    e = discord.Embed(title="🎰  Gamble Ticket", color=C_PURPLE)
    e.description = (
        f"**Middleman / Staff:** {interaction.user.mention}\n"
        f"**Player 1:** {interaction.user.mention}\n"
        f"**Player 2:** {opponent.mention}\n\n"
        f"Both players deposit their items/bets. Staff declares the winner using `/payout`.\n"
        f"Use `/roll` to decide the outcome if needed.")
    e.add_field(name="Staff Commands", value=(
        "`/payout @winner amount` — Award pot\n"
        "`/addshillings @user amount` — Add balance\n"
        "`/roll` — Roll 1-100"))
    e.set_footer(text="This channel is private. Only tagged users can see it.")
    await ch.send(f"{interaction.user.mention} {opponent.mention}", embed=e, view=view)
    await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)
    await log(guild, log_embed("🎰 Gamble Ticket", C_PURPLE,
        Staff=interaction.user.mention, P1=interaction.user.mention, P2=opponent.mention,
        Channel=ch.mention))

@bot.tree.command(name="payout", description="Staff: award coins to the winner of a gamble ticket")
@app_commands.describe(user="Winner", amount="Amount to give")
async def cmd_payout(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await db_ready(interaction): return
    if not await is_staff(interaction.user.id):
        return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
    await DB.ensure(user.id, str(user))
    await DB.add_raw(user.id, amount)
    bal = await DB.bal(user.id)
    e = discord.Embed(title="🏆  Payout!", color=C_GOLD)
    e.add_field(name="Winner",   value=user.mention)
    e.add_field(name="Prize",    value=fmt_bal(amount))
    e.add_field(name="Balance",  value=fmt_bal(bal))
    e.set_footer(text=f"Paid by {interaction.user}")
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("🏆 Payout", C_GOLD,
        Staff=interaction.user.mention, Winner=user.mention, Amount=fmt(amount)))

# ── /deposit ──────────────────────────────────────────────
@bot.tree.command(name="deposit", description="Open a deposit ticket to trade real items for Shillings")
@app_commands.describe(item="Describe what you're depositing")
async def cmd_deposit(interaction: discord.Interaction, item: str):
    if not await db_ready(interaction): return
    guild = interaction.guild
    cat = discord.utils.get(guild.categories, name="📥 Deposits")
    if not cat:
        cat = await guild.create_category("📥 Deposits", overwrites={
            guild.default_role: discord.PermissionOverwrite(read_messages=False)})

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    # Add all staff
    for row in await DB.wl_list():
        m = guild.get_member(row["user_id"])
        if m: overwrites[m] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    owner = guild.get_member(owner_id())
    if owner: overwrites[owner] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    ch = await guild.create_text_channel(
        f"📥┃deposit-{interaction.user.display_name}",
        category=cat, overwrites=overwrites)

    class DepositView(discord.ui.View):
        def __init__(self): super().__init__(timeout=None)
        @discord.ui.button(label="✅ Approve + Set Amount", style=discord.ButtonStyle.success)
        async def approve(self, intr: discord.Interaction, btn: discord.ui.Button):
            if not await is_staff(intr.user.id):
                return await intr.response.send_message("❌ Staff only.", ephemeral=True)
            await intr.response.send_modal(DepositApproveModal(interaction.user, ch))
        @discord.ui.button(label="❌ Reject + Close", style=discord.ButtonStyle.danger)
        async def reject(self, intr: discord.Interaction, btn: discord.ui.Button):
            if not await is_staff(intr.user.id):
                return await intr.response.send_message("❌ Staff only.", ephemeral=True)
            await intr.response.send_message("❌ Deposit rejected. Closing in 5s…")
            await asyncio.sleep(5)
            await ch.delete()

    e = discord.Embed(title="📥  Deposit Request", color=C_BLUE)
    e.add_field(name="User",  value=interaction.user.mention)
    e.add_field(name="Item",  value=item)
    e.set_footer(text="Staff: approve and set Shilling value + wager requirement")
    await ch.send(embed=e, view=DepositView())
    await interaction.response.send_message(f"✅ Deposit ticket created: {ch.mention}", ephemeral=True)
    await log(guild, log_embed("📥 Deposit Request", C_BLUE,
        User=interaction.user.mention, Item=item, Channel=ch.mention))

class DepositApproveModal(discord.ui.Modal, title="Approve Deposit"):
    amount = discord.ui.TextInput(label="Shillings to award")
    wager  = discord.ui.TextInput(label="Wager requirement (0 = none)", default="0")
    def __init__(self, target_user, channel):
        super().__init__()
        self.target_user = target_user
        self.channel = channel
    async def on_submit(self, interaction: discord.Interaction):
        try: amt = int(self.amount.value.replace(",",""))
        except: return await interaction.response.send_message("❌ Invalid amount.", ephemeral=True)
        try: wager = int(self.wager.value.replace(",",""))
        except: wager = 0
        await DB.ensure(self.target_user.id, str(self.target_user))
        await DB.add_raw(self.target_user.id, amt)
        if wager > 0: await DB.add_wager(self.target_user.id, wager)
        bal = await DB.bal(self.target_user.id)
        e = discord.Embed(title="✅  Deposit Approved!", color=C_GREEN)
        e.add_field(name="User",    value=self.target_user.mention)
        e.add_field(name="Awarded", value=fmt_bal(amt))
        e.add_field(name="Wager",   value=f"{fmt(wager)} required" if wager else "None")
        e.add_field(name="Balance", value=fmt_bal(bal))
        await interaction.response.send_message(embed=e)
        await log(interaction.guild, log_embed("📥 Deposit Approved", C_GREEN,
            Staff=interaction.user.mention, User=self.target_user.mention,
            Amount=fmt(amt), Wager=fmt(wager)))
        await asyncio.sleep(10)
        await self.channel.delete()

# ── /depositshillings ─────────────────────────────────────
@bot.tree.command(name="depositshillings", description="Staff: give Shillings to a user with a wager requirement")
@app_commands.describe(user="Who to give Shillings to", amount="Amount to deposit")
async def cmd_deposit_shillings(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await db_ready(interaction): return
    if not await staff_guard(interaction): return
    if amount <= 0:
        return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
    await DB.ensure(user.id, str(user))
    await DB.add_raw(user.id, amount)
    await DB.add_wager(user.id, amount)  # 1x wager requirement
    bal = await DB.bal(user.id)
    e = discord.Embed(title="💰  Shillings Deposited", color=C_GREEN)
    e.add_field(name="User",    value=user.mention)
    e.add_field(name="Amount",  value=fmt_bal(amount))
    e.add_field(name="Wager",   value=f"{fmt(amount)} (1x)")
    e.add_field(name="Balance", value=fmt_bal(bal))
    e.set_footer(text=f"By {interaction.user}")
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("💸 Deposit Shillings", C_GREEN,
        Staff=interaction.user.mention, User=user.mention, Amount=fmt(amount)))

# ═══════════════════════════════════════════════════════════
#  ──────────────  STAFF / ADMIN COMMANDS  ──────────────
# ═══════════════════════════════════════════════════════════

@bot.tree.command(name="addshillings", description="Staff: add Shillings to a user")
@app_commands.describe(user="Target user", amount="Amount to add")
async def cmd_addshillings(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await db_ready(interaction): return
    if not await staff_guard(interaction): return
    if amount <= 0: return await interaction.response.send_message("❌ Must be positive.", ephemeral=True)
    await DB.ensure(user.id, str(user))
    await DB.add_raw(user.id, amount)
    bal = await DB.bal(user.id)
    e = discord.Embed(title="✅  Added Shillings", color=C_GREEN)
    e.add_field(name="User",        value=user.mention)
    e.add_field(name="Added",       value=f"+{fmt(amount)}")
    e.add_field(name="New Balance", value=fmt_bal(bal))
    e.set_footer(text=f"By {interaction.user}")
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("➕ Add Shillings", C_GREEN,
        Staff=interaction.user.mention, User=user.mention,
        Amount=f"+{fmt(amount)}", Balance=fmt(bal)))

@bot.tree.command(name="removeshillings", description="Staff: remove Shillings from a user")
@app_commands.describe(user="Target user", amount="Amount to remove")
async def cmd_removeshillings(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await db_ready(interaction): return
    if not await staff_guard(interaction): return
    if amount <= 0: return await interaction.response.send_message("❌ Must be positive.", ephemeral=True)
    await DB.ensure(user.id, str(user))
    await DB.remove_raw(user.id, amount)
    bal = await DB.bal(user.id)
    e = discord.Embed(title="✅  Removed Shillings", color=C_ORANGE)
    e.add_field(name="User",        value=user.mention)
    e.add_field(name="Removed",     value=f"-{fmt(amount)}")
    e.add_field(name="New Balance", value=fmt_bal(bal))
    e.set_footer(text=f"By {interaction.user}")
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("➖ Remove Shillings", C_ORANGE,
        Staff=interaction.user.mention, User=user.mention,
        Amount=f"-{fmt(amount)}", Balance=fmt(bal)))

@bot.tree.command(name="clearbalance", description="Staff: reset a user's balance to 0")
@app_commands.describe(user="Target user")
async def cmd_clearbalance(interaction: discord.Interaction, user: discord.Member):
    if not await db_ready(interaction): return
    if not await staff_guard(interaction): return
    await DB.ensure(user.id, str(user))
    await DB.clear_balance(user.id)
    e = discord.Embed(title="🗑️  Balance Cleared", color=C_RED)
    e.add_field(name="User",        value=user.mention)
    e.add_field(name="New Balance", value=fmt_bal(0))
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("🗑️ Clear Balance", C_RED,
        Staff=interaction.user.mention, User=user.mention))

@bot.tree.command(name="setwager", description="Staff: manually set or clear a user's wager requirement")
@app_commands.describe(user="Target user", amount="0 to clear")
async def cmd_setwager(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await db_ready(interaction): return
    if not await staff_guard(interaction): return
    await DB.ensure(user.id, str(user))
    await DB.set_wager(user.id, max(0, amount))
    e = discord.Embed(title="⚙️  Wager Set", color=C_BLUE)
    e.add_field(name="User",   value=user.mention)
    e.add_field(name="Wager",  value=fmt(amount) if amount > 0 else "Cleared ✅")
    await interaction.response.send_message(embed=e)
    await log(interaction.guild, log_embed("⚙️ Set Wager", C_BLUE,
        Staff=interaction.user.mention, User=user.mention, Wager=fmt(amount)))

@bot.tree.command(name="setlogs", description="Owner/Staff: set the channel for transaction logs")
@app_commands.describe(channel="Channel to send logs to")
async def cmd_setlogs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await db_ready(interaction): return
    if not await staff_guard(interaction): return
    await DB.set_log_channel(interaction.guild.id, channel.id)
    e = discord.Embed(title="📋  Log Channel Set", color=C_BLUE,
        description=f"All transactions will now be logged to {channel.mention}")
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="whitelist", description="Owner only: manage staff whitelist")
@app_commands.describe(action="add or remove", user="Target user")
@app_commands.choices(action=[
    app_commands.Choice(name="add",    value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list",   value="list"),
])
async def cmd_whitelist(interaction: discord.Interaction, action: str, user: discord.Member = None):
    if not await db_ready(interaction): return
    if not await owner_guard(interaction): return
    if action == "list":
        rows = await DB.wl_list()
        if not rows:
            return await interaction.response.send_message("📋 Whitelist is empty.", ephemeral=True)
        lines = []
        for r in rows:
            m = interaction.guild.get_member(r["user_id"])
            lines.append(f"• {m.mention if m else r['user_id']}")
        e = discord.Embed(title="📋  Staff Whitelist", description="\n".join(lines), color=C_GOLD)
        return await interaction.response.send_message(embed=e)
    if not user:
        return await interaction.response.send_message("❌ Provide a user.", ephemeral=True)
    if action == "add":
        await DB.wl_add(user.id, interaction.user.id)
        e = discord.Embed(description=f"✅ {user.mention} added to staff whitelist.", color=C_GREEN)
    else:
        await DB.wl_remove(user.id)
        e = discord.Embed(description=f"✅ {user.mention} removed from whitelist.", color=C_RED)
    await interaction.response.send_message(embed=e)

# ═══════════════════════════════════════════════════════════
#  GAME PAGES — one full page per game
# ═══════════════════════════════════════════════════════════
GAME_PAGES = [
    {
        "title": "🎰  Game List — 14 Games",
        "color": C_PURPLE,
        "description": (
            "Use the arrows below to browse each game in detail.\n\n"
            "**All games:**\n"
            "1️⃣  🪙 Coin Flip\n"
            "2️⃣  🎰 Slots\n"
            "3️⃣  🎡 Roulette\n"
            "4️⃣  🃏 Blackjack\n"
            "5️⃣  🎯 Limbo\n"
            "6️⃣  🚀 Crash\n"
            "7️⃣  🃏 Higher or Lower\n"
            "8️⃣  💎 Mines\n"
            "9️⃣  💣 Bomb\n"
            "🔟  ⚔️ War\n"
            "1️⃣1️⃣  🏇 Horse Race\n"
            "1️⃣2️⃣  🔢 Number Guess\n"
            "1️⃣3️⃣  🎟️ Scratch Card\n"
            "1️⃣4️⃣  ⚔️ Duel"
        ),
        "fields": [
            ("💡 Bet Shortcuts", "`all` — all-in   ·   `half` — half your balance\n`5k` — 5,000   ·   `1m` — 1,000,000", False),
        ],
    },
    {
        "title": "🪙  Coin Flip",
        "color": C_GOLD,
        "description": "The classic 50/50. Pick heads or tails — if you're right, you double your bet. Simple and clean.",
        "fields": [
            ("📝 Command", "`/coinflip <bet> <heads/tails>`", False),
            ("💰 Payout", "Win → **2x** your bet\nLose → lose your bet", False),
            ("🎲 Odds", "50% win · 50% lose", False),
            ("📌 Example", (
                "`/coinflip 10000 heads`\n"
                "→ Coin lands **heads** ✅\n"
                "→ You win **+10,000** 🪙\n\n"
                "`/coinflip 50k tails`\n"
                "→ Coin lands **heads** ❌\n"
                "→ You lose **-50,000** 🪙"
            ), False),
            ("💡 Tips", "Best for quick doubles. Use `half` to protect your bag while still getting a big flip.", False),
        ],
    },
    {
        "title": "🎰  Slots",
        "color": C_GOLD,
        "description": "Spin 3 reels. Match all 3 symbols for a jackpot. Match 2 and your bet is returned. No match and it's gone.",
        "fields": [
            ("📝 Command", "`/slots <bet>`", False),
            ("💰 Payouts (3 of a kind)", (
                "7️⃣ 7️⃣ 7️⃣ → **50x**\n"
                "💎 💎 💎 → **30x**\n"
                "🔔 🔔 🔔 → **15x**\n"
                "⭐ ⭐ ⭐ → **10x**\n"
                "🍇 🍇 🍇 → **7x**\n"
                "🍊 🍊 🍊 → **5x**\n"
                "🍋 🍋 🍋 → **4x**\n"
                "🍒 🍒 🍒 → **3x**"
            ), False),
            ("🤝 Two of a kind", "Any 2 matching → **bet returned** (push)", False),
            ("📌 Example", (
                "`/slots 100k`\n"
                "→ `｜ 💎 ｜ 💎 ｜ 💎 ｜` 🎉\n"
                "→ **JACKPOT 30x** — you win **3,000,000** 🪙\n\n"
                "`/slots 50k`\n"
                "→ `｜ 🍒 ｜ 🍒 ｜ 🍋 ｜`\n"
                "→ Two of a kind — **push**, bet returned"
            ), False),
            ("💡 Tips", "The 7️⃣ jackpot is extremely rare (weight 1). Chase it with small bets to keep playing longer.", False),
        ],
    },
    {
        "title": "🎡  Roulette",
        "color": 0xC0392B,
        "description": "A ball is dropped on a wheel of numbers 0–36. Bet on where it lands — the riskier your bet, the bigger the payout.",
        "fields": [
            ("📝 Command", "`/roulette <bet> <choice>`", False),
            ("💰 Payout Table", (
                "`red` / `black` → **2x**\n"
                "`odd` / `even` → **2x**\n"
                "`1st` (1–12) / `2nd` (13–24) / `3rd` (25–36) → **3x**\n"
                "`green` (0 only) → **14x**\n"
                "Exact number `0`–`36` → **36x**"
            ), False),
            ("📌 Examples", (
                "`/roulette 20k red`\n"
                "→ Ball lands on **7 🔴** → win **+20,000** 🪙\n\n"
                "`/roulette 10k 17`\n"
                "→ Ball lands on **17** 🎯 → win **+350,000** 🪙\n\n"
                "`/roulette 5k green`\n"
                "→ Ball lands on **0 🟢** → win **+65,000** 🪙"
            ), False),
            ("💡 Tips", "Red/Black is safest. Exact number is high risk high reward — try it with small bets for big flips.", False),
        ],
    },
    {
        "title": "🃏  Blackjack",
        "color": C_DARK,
        "description": "Classic blackjack against the dealer. Get closer to 21 than the dealer without going over. Has interactive Hit, Stand, and Double Down buttons.",
        "fields": [
            ("📝 Command", "`/blackjack <bet>`", False),
            ("🎮 Buttons", (
                "**Hit** 🃏 — Draw another card\n"
                "**Stand** 🖐️ — Keep your hand, let dealer play\n"
                "**Double** ⚡ — Double your bet, take exactly 1 more card"
            ), False),
            ("💰 Payouts", (
                "Beat dealer → **2x** your bet\n"
                "Natural Blackjack (A + 10/J/Q/K on deal) → **1.5x**\n"
                "Tie / Push → **bet returned**\n"
                "Bust (over 21) or dealer wins → lose bet"
            ), False),
            ("📌 Example", (
                "`/blackjack 50k`\n"
                "→ You get `A♠` `K♥` = **21** 🎉 Natural Blackjack!\n"
                "→ Win **+75,000** 🪙 (1.5x)\n\n"
                "→ You get `9♦` `7♣` = 16\n"
                "→ Hit → draw `5♠` = **21** ✅\n"
                "→ Dealer has 18 → you win **+50,000** 🪙"
            ), False),
            ("💡 Tips", "Always hit on 11 or less. Stand on 17+. Double when you have 10 or 11 and dealer shows a low card.", False),
        ],
    },
    {
        "title": "🎯  Limbo",
        "color": C_BLUE,
        "description": "Set your own target multiplier. The game generates a random result — if it hits your target or higher, you win that multiplier. Higher targets = bigger payouts but lower chance of winning.",
        "fields": [
            ("📝 Command", "`/limbo <bet> <target>`", False),
            ("🎲 Win Chance Formula", "Win % = **96 ÷ target**\ne.g. target `2` = 48% · target `10` = 9.6% · target `100` = 0.96%", False),
            ("💰 Payout", "Win → **target multiplier × bet**\nLose → lose your bet", False),
            ("📌 Examples", (
                "`/limbo 100k 2`\n"
                "→ 48% chance → win **+100,000** 🪙\n\n"
                "`/limbo 50k 10`\n"
                "→ 9.6% chance → win **+450,000** 🪙\n\n"
                "`/limbo 10k 1000`\n"
                "→ 0.096% chance → win **+9,990,000** 🪙 🤯"
            ), False),
            ("💡 Tips", "Target 1.5–3 is a good balance of risk/reward. Going above 50x is a long shot — only do it with small bets.", False),
        ],
    },
    {
        "title": "🚀  Crash",
        "color": C_GREEN,
        "description": "A multiplier starts at 1x and climbs. It can crash at any moment. Hit **Cash Out** before it crashes to lock in your winnings. Wait too long and you lose everything.",
        "fields": [
            ("📝 Command", "`/crash <bet>`", False),
            ("🎮 How it works", (
                "1. Multiplier starts at **1.00x** and climbs\n"
                "2. A **💰 Cash Out** button appears\n"
                "3. Press it before the crash to win\n"
                "4. If it crashes before you cash out → you lose your bet"
            ), False),
            ("💰 Payout", "Cashout multiplier × bet (minus original bet = net profit)", False),
            ("📌 Example", (
                "`/crash 100k`\n"
                "→ Multiplier climbing: 1.2x… 1.8x… 2.4x…\n"
                "→ You cash out at **2.4x** ✅\n"
                "→ Win **+140,000** 🪙\n\n"
                "→ Didn't cash out in time 💥\n"
                "→ Crashed at **1.6x** → lose **-100,000** 🪙"
            ), False),
            ("💡 Tips", "Cashing out at 1.5–2x is a solid safe play. The crash point is random — don't get greedy!", False),
        ],
    },
    {
        "title": "🃏  Higher or Lower",
        "color": C_BLUE,
        "description": "A card is shown. Guess if the next card will be higher or lower. Each correct guess multiplies your payout by 1.5x. Build a streak and cash out at any time.",
        "fields": [
            ("📝 Command", "`/higherlower <bet>`", False),
            ("🎮 Buttons", (
                "**Higher ⬆️** — Next card will be higher\n"
                "**Lower ⬇️** — Next card will be lower\n"
                "**💰 Cash Out** — Lock in your current multiplier"
            ), False),
            ("💰 Multiplier per correct guess", "Each correct guess: current payout **× 1.5**\nTie = loss", False),
            ("📌 Example", (
                "`/higherlower 50k`\n"
                "→ Card: **7♠** → guess Higher\n"
                "→ Next: **J♦** ✅ → 1.5x\n"
                "→ Card: **J♦** → guess Lower\n"
                "→ Next: **3♣** ✅ → 2.25x\n"
                "→ Cash out → win **+62,500** 🪙\n\n"
                "→ Keep going and guess wrong → lose everything"
            ), False),
            ("💡 Tips", "Cash out after 2–3 correct guesses. The longer you go, the more likely you'll slip up. Ties count as a loss!", False),
        ],
    },
    {
        "title": "💎  Mines",
        "color": C_PURPLE,
        "description": "A 4×4 grid of 16 tiles hides gems and bombs. Click tiles to reveal gems — each one increases your multiplier. Hit a bomb and you lose everything. Cash out any time to lock in your winnings.",
        "fields": [
            ("📝 Command", "`/mines <bet> [mines]`\nMine options: `1` `3` `5` `10`", False),
            ("💰 Multiplier", (
                "Increases each safe tile you reveal.\n"
                "Formula: `(total tiles / safe tiles remaining) × 0.97` per reveal\n"
                "More mines = multiplier grows faster = more risk"
            ), False),
            ("📌 Example", (
                "`/mines 100k 3`  _(3 bombs, 13 safe)_\n"
                "→ Click tile 1 → 💎 safe! Multi: **1.2x**\n"
                "→ Click tile 2 → 💎 safe! Multi: **1.5x**\n"
                "→ Click tile 3 → 💎 safe! Multi: **1.9x**\n"
                "→ Cash out → win **+90,000** 🪙\n\n"
                "`/mines 50k 10`  _(10 bombs, risky!)_\n"
                "→ Click tile 1 → 💣 BOOM! Lose **-50,000** 🪙"
            ), False),
            ("🎮 Mine Options", (
                "`1` mine — slow multiplier, safer\n"
                "`3` mines — balanced (recommended)\n"
                "`5` mines — faster multiplier, riskier\n"
                "`10` mines — extreme risk, extreme reward"
            ), False),
            ("💡 Tips", "With 3 mines, cashing out after 3–4 gems is solid. With 10 mines, even 1 gem is already a great multiplier.", False),
        ],
    },
    {
        "title": "💣  Bomb",
        "color": C_RED,
        "description": "5 wires are in front of you. 2 are bombs, 3 are safe. Cut safe wires one by one for increasing payouts. Cut a bomb and you lose. Cash out after any safe cut.",
        "fields": [
            ("📝 Command", "`/bomb <bet>`", False),
            ("🎮 Buttons", "5 wire buttons + a **💰 Cash Out** button", False),
            ("💰 Payout per safe cut", (
                "1st safe wire → **1.5x**\n"
                "2nd safe wire → **2.5x**\n"
                "3rd safe wire → **5x** (max — auto cash out)"
            ), False),
            ("📌 Example", (
                "`/bomb 100k`\n"
                "→ Cut Wire 2 → ✅ Safe! Payout if cashout: **150,000**\n"
                "→ Cut Wire 4 → ✅ Safe! Payout if cashout: **250,000**\n"
                "→ Cash out → win **+150,000** 🪙\n\n"
                "→ Cut Wire 1 → 💣 BOOM! Lose **-100,000** 🪙"
            ), False),
            ("💡 Tips", "Cashing out after the 1st safe cut (1.5x) is low risk. Going for all 3 is thrilling but the odds of hitting both safes are ~13%.", False),
        ],
    },
    {
        "title": "⚔️  War",
        "color": C_RED,
        "description": "Draw a random card. The dealer draws one too. Highest card wins. Ties trigger sudden death — you both draw again until someone wins.",
        "fields": [
            ("📝 Command", "`/war <bet>`", False),
            ("💰 Payout", "Win → **2x** your bet\nLose → lose your bet", False),
            ("🎲 Card Ranks (low → high)", "`2 3 4 5 6 7 8 9 10 J Q K A`", False),
            ("📌 Example", (
                "`/war 75k`\n"
                "→ You draw **K♠** · Dealer draws **9♦**\n"
                "→ You win! **+75,000** 🪙\n\n"
                "`/war 50k`\n"
                "→ You draw **7♥** · Dealer draws **7♣** — TIE!\n"
                "→ Sudden death → You: **A♠** · Dealer: **Q♦**\n"
                "→ You win! **+50,000** 🪙"
            ), False),
            ("💡 Tips", "Pure luck — no strategy needed. Great for quick coin flips with higher implied value. Up to 5 sudden death rounds on ties.", False),
        ],
    },
    {
        "title": "🏇  Horse Race",
        "color": C_ORANGE,
        "description": "6 horses race. Each has different odds — lower numbered horses win more often, higher numbered horses pay out way more if they win. Pick your horse and pray.",
        "fields": [
            ("📝 Command", "`/horserace <bet> <1–6>`", False),
            ("🐎 Horses & Odds", (
                "1. 🐎 Thunderbolt — **2x** (most common)\n"
                "2. 🐴 Midnight Star — **4x**\n"
                "3. 🏇 Lucky Dash — **6x**\n"
                "4. 🦄 Silver Wind — **10x**\n"
                "5. 🐆 Iron Hooves — **15x**\n"
                "6. 🐉 Dragon Fury — **20x** (rarest)"
            ), False),
            ("📌 Example", (
                "`/horserace 50k 1`\n"
                "→ 🐎 Thunderbolt wins! → **+50,000** 🪙\n\n"
                "`/horserace 20k 6`\n"
                "→ 🐉 Dragon Fury wins! 🎉 → **+380,000** 🪙\n\n"
                "`/horserace 20k 6`\n"
                "→ 🐎 Thunderbolt wins ❌ → **-20,000** 🪙"
            ), False),
            ("💡 Tips", "Horse 1 is basically a coin flip with bad odds. Horse 6 is the jackpot pick — small bet, massive upside.", False),
        ],
    },
    {
        "title": "🔢  Number Guess",
        "color": C_BLUE,
        "description": "Guess a number between 1 and 100. The closer you are to the actual number, the bigger your reward. Exact hit is a massive 90x payout.",
        "fields": [
            ("📝 Command", "`/numguess <bet> <1–100>`", False),
            ("💰 Payout Tiers", (
                "🎯 Exact match → **90x**\n"
                "🔥 Within 3 (±3) → **10x**\n"
                "✅ Within 10 (±10) → **3x**\n"
                "❌ Miss (off by more than 10) → lose bet"
            ), False),
            ("📌 Example", (
                "`/numguess 10k 50`\n"
                "→ Number was **50** 🎯 EXACT! → **+890,000** 🪙\n\n"
                "`/numguess 50k 33`\n"
                "→ Number was **35** (off by 2) 🔥 → **+450,000** 🪙\n\n"
                "`/numguess 20k 70`\n"
                "→ Number was **78** (off by 8) ✅ → **+40,000** 🪙\n\n"
                "`/numguess 30k 1`\n"
                "→ Number was **88** ❌ → **-30,000** 🪙"
            ), False),
            ("💡 Tips", "Pick numbers in the middle (40–60) to statistically reduce your average miss distance. 90x exact is life-changing — worth a small bet.", False),
        ],
    },
    {
        "title": "🎟️  Scratch Card",
        "color": C_GOLD,
        "description": "A 3×3 grid of 9 tiles is instantly revealed. Match 3 identical symbols in a row, column, or diagonal to win. Hit a 💣 bomb tile anywhere on the grid and lose instantly.",
        "fields": [
            ("📝 Command", "`/scratch <bet>`", False),
            ("💰 Match 3 Payouts", (
                "💎 💎 💎 → **50x**\n"
                "💰 💰 💰 → **20x**\n"
                "⭐ ⭐ ⭐ → **10x**\n"
                "🔔 🔔 🔔 → **7x**\n"
                "🍇 🍇 🍇 → **5x**\n"
                "🍒 🍒 🍒 → **4x**\n"
                "🍋 🍋 🍋 → **3x**"
            ), False),
            ("⚠️ Bomb Rule", "💣 appearing **anywhere** in the grid = instant loss, no payout", False),
            ("📌 Example", (
                "`/scratch 100k`\n"
                "→ Grid:\n"
                "💰 ⭐ 💰\n"
                "🍒 💰 🍋\n"
                "🍇 🔔 💰\n"
                "→ 💰 column match! **20x** → **+1,900,000** 🪙\n\n"
                "→ Grid contains 💣 → lose **-100k** 🪙"
            ), False),
            ("💡 Tips", "The bomb makes this high variance. Use moderate bets — the occasional 20x or 50x hit more than makes up for losses.", False),
        ],
    },
    {
        "title": "⚔️  Duel",
        "color": C_PURPLE,
        "description": "Challenge another server member to a duel. You both put up the same bet. A random winner is picked and takes the entire pot. The opponent has 60 seconds to accept or decline.",
        "fields": [
            ("📝 Command", "`/duel @user <bet>`", False),
            ("🎮 How it works", (
                "1. You challenge `@user` with a bet\n"
                "2. Your bet is **held** immediately\n"
                "3. They have **60 seconds** to accept\n"
                "4. If accepted, both bets are locked in\n"
                "5. Random winner takes **both bets**\n"
                "6. If declined/expired, your bet is refunded"
            ), False),
            ("💰 Payout", "Winner takes **2x** the bet amount", False),
            ("📌 Example", (
                "`/duel @John 500k`\n"
                "→ John accepts within 60s\n"
                "→ Both put in **500,000** 🪙\n"
                "→ Random winner: **you** 🎉\n"
                "→ You win **+500,000** 🪙 (net)\n\n"
                "→ John wins instead ❌\n"
                "→ You lose **-500,000** 🪙"
            ), False),
            ("💡 Tips", "Great for settling beef or making big bets between friends. Opponent must have enough balance to match your bet before the duel starts.", False),
        ],
    },
]

def build_game_embed(page_idx: int) -> discord.Embed:
    page = GAME_PAGES[page_idx]
    e = discord.Embed(
        title=page["title"],
        description=page.get("description", ""),
        color=page["color"]
    )
    for name, value, inline in page.get("fields", []):
        e.add_field(name=name, value=value, inline=inline)
    total = len(GAME_PAGES)
    e.set_footer(text=f"Page {page_idx + 1} of {total}  ·  🎰 Shillings Casino")
    return e

class GamesView(discord.ui.View):
    def __init__(self, uid: int, page: int = 0):
        super().__init__(timeout=120)
        self.uid  = uid
        self.page = page
        self._update_buttons()

    def _update_buttons(self):
        total = len(GAME_PAGES)
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == total - 1
        self.page_btn.label    = f"{self.page + 1} / {total}"

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="games_prev")
    async def prev_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            return await interaction.response.defer()
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=build_game_embed(self.page), view=self)

    @discord.ui.button(label="1 / 15", style=discord.ButtonStyle.secondary, disabled=True, custom_id="games_page")
    async def page_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, custom_id="games_next")
    async def next_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            return await interaction.response.defer()
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=build_game_embed(self.page), view=self)

    async def on_timeout(self):
        pass

# ── /games ────────────────────────────────────────────────
@bot.tree.command(name="games", description="Browse all games — one page per game with examples and payouts")
async def cmd_games(interaction: discord.Interaction):
    view = GamesView(interaction.user.id, page=0)
    await interaction.response.send_message(embed=build_game_embed(0), view=view)

# ═══════════════════════════════════════════════════════════
#  HELP PAGES
# ═══════════════════════════════════════════════════════════
HELP_PAGES = [
    {
        "title": "🎰  Shillings Casino — Help",
        "color": C_GOLD,
        "description": "Welcome to Shillings Casino! Use the arrows to browse all commands.\n\nUse `/games` to browse every game with full details and examples.",
        "fields": [
            ("📖 Sections", (
                "1️⃣  💰 Economy\n"
                "2️⃣  🚪 Gamble Tickets & Deposits\n"
                "3️⃣  🛠️ Staff Commands\n"
                "4️⃣  👑 Owner Commands"
            ), False),
            ("💡 Bet Shortcuts", (
                "`all` — go all-in\n"
                "`half` — bet half your balance\n"
                "`5k` — 5,000 · `1m` — 1,000,000"
            ), False),
        ],
    },
    {
        "title": "💰  Economy Commands",
        "color": C_GOLD,
        "description": "Commands for managing your Shillings balance and interacting with other players.",
        "fields": [
            ("/daily", (
                "Claim free Shillings every **3 hours**.\n"
                "Amount is random with weighted rarities:\n"
                "🪨 Small — up to 100K\n"
                "🥉 Decent — 100K–1M\n"
                "🥈 Nice — 1M–5M\n"
                "💎 Rare — 5M–15M\n"
                "👑 Jackpot — 15M–20M"
            ), False),
            ("/balance [@user]", (
                "Shows your balance (or someone else's).\n"
                "Also displays wager requirement progress bar if you have one active."
            ), False),
            ("/leaderboard", "Top 10 richest players in the server.", False),
            ("/tip @user <amount>", (
                "Send Shillings directly to another player.\n"
                "Example: `/tip @John 50k`"
            ), False),
            ("/roll", (
                "Rolls a random number **1–100**.\n"
                "Useful inside gamble ticket channels to decide a winner fairly."
            ), False),
        ],
    },
    {
        "title": "🚪  Gamble Tickets & Deposits",
        "color": C_PURPLE,
        "description": "Private channels for real-item gambling and depositing items in exchange for Shillings.",
        "fields": [
            ("/gambleticket @opponent", (
                "Opens a **private channel** visible only to you, the opponent, and staff.\n"
                "Inside the ticket:\n"
                "• **Add User** button — add extra viewers\n"
                "• **Close Ticket** button — deletes the channel\n"
                "• Use `/roll` to decide a winner\n"
                "• Use `/payout` to award winnings"
            ), False),
            ("/payout @winner <amount>", (
                "**Staff only.** Awards Shillings directly to the winner.\n"
                "Example: `/payout @John 500000`\n"
                "Logged to the server's log channel automatically."
            ), False),
            ("/deposit <item>", (
                "Opens a private **deposit ticket** where you describe your item.\n"
                "Staff will approve it and set the Shilling value + wager requirement.\n"
                "Example: `/deposit Rare sword skin`"
            ), False),
            ("/depositshillings @user <amount>", (
                "**Staff only.** Gives a user Shillings with an automatic **1x wager requirement**.\n"
                "They must wager the full amount before it's considered withdrawable.\n"
                "Example: `/depositshillings @John 1000000`"
            ), False),
        ],
    },
    {
        "title": "🛠️  Staff Commands",
        "color": C_ORANGE,
        "description": "Available to server owner and anyone on the `/whitelist`. Used to manage player balances and server settings.",
        "fields": [
            ("/addshillings @user <amount>", "Add Shillings to a user's balance.\nExample: `/addshillings @John 500k`", False),
            ("/removeshillings @user <amount>", "Remove Shillings from a user's balance.\nExample: `/removeshillings @John 100k`", False),
            ("/clearbalance @user", "Reset a user's balance to **0**.\nExample: `/clearbalance @John`", False),
            ("/setwager @user <amount>", (
                "Manually set or clear a user's wager requirement.\n"
                "Set to `0` to clear it entirely.\n"
                "Example: `/setwager @John 1000000`"
            ), False),
            ("/setlogs #channel", (
                "Set the channel where all transactions are logged.\n"
                "Logs include: game wins/losses, tips, deposits, payouts, balance changes.\n"
                "Example: `/setlogs #casino-logs`"
            ), False),
        ],
    },
    {
        "title": "👑  Owner Commands",
        "color": C_GOLD,
        "description": "Only the bot owner (set via `OWNER_ID` env var) can use these commands.",
        "fields": [
            ("/whitelist add @user", "Grant a user **staff access** — they can use all staff commands.", False),
            ("/whitelist remove @user", "Remove a user's staff access.", False),
            ("/whitelist list", "Show all currently whitelisted staff members.", False),
            ("⚠️ Note", (
                "The owner always has full access regardless of the whitelist.\n"
                "Be careful who you whitelist — staff can add/remove balances freely."
            ), False),
        ],
    },
]

def build_help_embed(page_idx: int) -> discord.Embed:
    page = HELP_PAGES[page_idx]
    e = discord.Embed(
        title=page["title"],
        description=page.get("description", ""),
        color=page["color"]
    )
    for name, value, inline in page.get("fields", []):
        e.add_field(name=name, value=value, inline=inline)
    e.set_footer(text=f"Page {page_idx + 1} of {len(HELP_PAGES)}  ·  🎰 Shillings Casino")
    return e

class HelpView(discord.ui.View):
    def __init__(self, uid: int, page: int = 0):
        super().__init__(timeout=120)
        self.uid  = uid
        self.page = page
        self._update_buttons()

    def _update_buttons(self):
        total = len(HELP_PAGES)
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == total - 1
        self.page_btn.label    = f"{self.page + 1} / {total}"

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="help_prev")
    async def prev_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            return await interaction.response.defer()
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=build_help_embed(self.page), view=self)

    @discord.ui.button(label="1 / 5", style=discord.ButtonStyle.secondary, disabled=True, custom_id="help_page")
    async def page_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, custom_id="help_next")
    async def next_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.uid:
            return await interaction.response.defer()
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=build_help_embed(self.page), view=self)

    async def on_timeout(self):
        pass

# ── /help ─────────────────────────────────────────────────
@bot.tree.command(name="help", description="Browse all commands with descriptions and examples")
async def cmd_help(interaction: discord.Interaction):
    view = HelpView(interaction.user.id, page=0)
    await interaction.response.send_message(embed=build_help_embed(0), view=view)

# ═══════════════════════════════════════════════════════════
#  KEEP-ALIVE WEB SERVER (required for Render Web Service)
#  Binds to PORT env var (Render sets this automatically).
#  UptimeRobot can ping it to prevent sleeping.
# ═══════════════════════════════════════════════════════════
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Shillings Casino is alive!")
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
    def log_message(self, *args):
        pass  # silence access logs

def _start_webserver():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _PingHandler)
    server.serve_forever()

threading.Thread(target=_start_webserver, daemon=True).start()
print("🌐 Web server started")

# ═══════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════
bot.run(os.environ["DISCORD_TOKEN"])
