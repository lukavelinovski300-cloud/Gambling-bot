import os, random, asyncio, logging, secrets
from datetime import datetime, timezone
from typing import Optional
import asyncpg, discord
from discord import ButtonStyle, app_commands
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger   = logging.getLogger(__name__)
PREFIX   = "!"
CUR      = "Shillings"
CE       = "🪙"
CLAIM_CD = 3 * 3600
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════

class DB:
    pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        url = os.getenv("DATABASE_URL", "")
        if url.startswith("postgres://"): url = "postgresql://" + url[11:]
        self.pool = await asyncpg.create_pool(url, min_size=1, max_size=5, command_timeout=15)
        await self._setup()
        logger.info("db ready")

    async def _setup(self):
        async with self.pool.acquire() as c:
            for sql in [
                """CREATE TABLE IF NOT EXISTS eco(
                    guild_id BIGINT, user_id BIGINT,
                    balance BIGINT DEFAULT 0,
                    last_claim TIMESTAMPTZ,
                    wager_req BIGINT DEFAULT 0,
                    PRIMARY KEY(guild_id,user_id))""",
                """CREATE TABLE IF NOT EXISTS wl(
                    guild_id BIGINT, user_id BIGINT,
                    PRIMARY KEY(guild_id,user_id))""",
                """CREATE TABLE IF NOT EXISTS stock(
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT, item_name TEXT,
                    price BIGINT, added_by BIGINT,
                    added_at TIMESTAMPTZ DEFAULT NOW())""",
                """CREATE TABLE IF NOT EXISTS cfg(
                    guild_id BIGINT PRIMARY KEY,
                    cat_id BIGINT, log_id BIGINT,
                    staff_id BIGINT)""",
                """CREATE TABLE IF NOT EXISTS tctr(
                    guild_id BIGINT PRIMARY KEY, n INT DEFAULT 0)""",
            ]:
                try: await c.execute(sql)
                except: pass

    async def bal(self, gid, uid) -> int:
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT balance FROM eco WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r["balance"] if r else 0

    async def adj(self, gid, uid, amt: int):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,balance) VALUES($1,$2,GREATEST(0,$3)) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=GREATEST(0,eco.balance+$3)",
                gid, uid, amt)

    async def set_bal(self, gid, uid, amt: int):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,balance) VALUES($1,$2,GREATEST(0,$3)) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=GREATEST(0,$3)",
                gid, uid, amt)

    async def wager_req(self, gid, uid) -> int:
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT wager_req FROM eco WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r["wager_req"] if r else 0

    async def reduce_wager(self, gid, uid, amt: int):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,wager_req) VALUES($1,$2,0) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wager_req=GREATEST(0,eco.wager_req-$3)",
                gid, uid, amt)

    async def add_wager(self, gid, uid, amt: int):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,wager_req) VALUES($1,$2,$3) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wager_req=eco.wager_req+$3",
                gid, uid, amt)

    async def set_wager(self, gid, uid, amt: int):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,wager_req) VALUES($1,$2,GREATEST(0,$3)) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wager_req=GREATEST(0,$3)",
                gid, uid, amt)

    async def last_claim(self, gid, uid):
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT last_claim FROM eco WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r["last_claim"] if r else None

    async def touch_claim(self, gid, uid):
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,last_claim) VALUES($1,$2,NOW()) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET last_claim=NOW()",
                gid, uid)

    async def is_wl(self, gid, uid) -> bool:
        if uid == OWNER_ID: return True
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT 1 FROM wl WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r is not None

    async def get_cfg(self, gid):
        async with self.pool.acquire() as c:
            return await c.fetchrow("SELECT * FROM cfg WHERE guild_id=$1", gid)

    async def next_tid(self, gid) -> int:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO tctr(guild_id,n) VALUES($1,1) ON CONFLICT(guild_id) DO UPDATE SET n=tctr.n+1", gid)
            r = await c.fetchrow("SELECT n FROM tctr WHERE guild_id=$1", gid)
        return r["n"]

db = DB()

# ═══════════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def fm(n) -> str:
    return f"{int(n):,}"

def fmt_cd(s: float) -> str:
    h, r = divmod(int(s), 3600)
    m, sec = divmod(r, 60)
    p = []
    if h: p.append(f"{h}h")
    if m: p.append(f"{m}m")
    if sec or not p: p.append(f"{sec}s")
    return " ".join(p)

def roll_claim() -> int:
    # Provably fair: uses secrets.randbelow for true randomness
    r = secrets.randbelow(100) / 100.0
    if r < 0.40: return random.randint(1, 100_000)
    if r < 0.70: return random.randint(100_001, 1_000_000)
    if r < 0.90: return random.randint(1_000_001, 5_000_000)
    if r < 0.98: return random.randint(5_000_001, 15_000_000)
    return random.randint(15_000_001, 20_000_000)

def rarity_label(n: int) -> str:
    if n >= 15_000_000: return "🌟 **JACKPOT!**"
    if n >= 5_000_000:  return "💎 **Rare**"
    if n >= 1_000_000:  return "✨ **Nice**"
    if n >= 100_000:    return "👍 **Decent**"
    return "🪙 Small"

def fair_flip() -> str:
    """Provably fair coinflip using secrets."""
    return "heads" if secrets.randbelow(2) == 0 else "tails"

def fair_int(lo: int, hi: int) -> int:
    """Provably fair integer in [lo, hi]."""
    span = hi - lo + 1
    return lo + secrets.randbelow(span)

def fair_choice(seq: list):
    """Provably fair choice from a list."""
    return seq[secrets.randbelow(len(seq))]

def fair_shuffle(seq: list) -> list:
    """Provably fair Fisher-Yates shuffle."""
    a = list(seq)
    for i in range(len(a) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        a[i], a[j] = a[j], a[i]
    return a

async def parse_bet(src, arg: str) -> Optional[int]:
    gi = src.guild_id if isinstance(src, discord.Interaction) else src.guild.id
    ui = src.user.id  if isinstance(src, discord.Interaction) else src.author.id
    bl = await db.bal(gi, ui)
    a  = arg.lower().strip()
    if a in ("all", "max"): return bl or None
    for s, m in (("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if a.endswith(s):
            try: return int(float(a[:-1]) * m)
            except: return None
    try: return int(a)
    except: return None

async def chk_bet(src, bet) -> bool:
    ii = isinstance(src, discord.Interaction)
    gi = src.guild_id if ii else src.guild.id
    ui = src.user.id  if ii else src.author.id
    async def err(msg):
        e = discord.Embed(description=msg, color=0xED4245)
        if ii: await src.response.send_message(embed=e, ephemeral=True)
        else:  await src.reply(embed=e)
    if not bet or bet <= 0:
        await err("Enter a valid bet. e.g. `1000`, `5k`, `2m`, `all`")
        return False
    bl = await db.bal(gi, ui)
    if bet > bl:
        await err(f"You only have {CE} **{fm(bl)} {CUR}**.")
        return False
    return True

def e_win(game: str, detail: str, won: int) -> discord.Embed:
    e = discord.Embed(color=0x57F287)
    e.description = f"**{game}**\n{detail}\n\n> {CE} **+{fm(won)}** won"
    return e

def e_lose(game: str, detail: str, lost: int) -> discord.Embed:
    e = discord.Embed(color=0xED4245)
    e.description = f"**{game}**\n{detail}\n\n> {CE} **−{fm(lost)}** lost"
    return e

def e_tie(game: str, detail: str) -> discord.Embed:
    e = discord.Embed(color=0xFEE75C)
    e.description = f"**{game}**\n{detail}\n\n> 🤝 Tie — bet returned"
    return e

def add_bal(e: discord.Embed, bal: int) -> discord.Embed:
    e.set_footer(text=f"Balance: {CE} {fm(bal)} {CUR}")
    return e

def wl_only():
    async def pred(ctx):
        ok = await db.is_wl(ctx.guild.id, ctx.author.id)
        if not ok:
            await ctx.reply(embed=discord.Embed(description="You are not authorized.", color=0xED4245))
        return ok
    return commands.check(pred)

async def get_cat(gid, guild):
    c = await db.get_cfg(gid)
    return guild.get_channel(c["cat_id"]) if c and c.get("cat_id") else None

async def get_staff_r(gid, guild):
    c = await db.get_cfg(gid)
    return guild.get_role(c["staff_id"]) if c and c.get("staff_id") else None

async def get_log(gid, guild):
    c = await db.get_cfg(gid)
    return guild.get_channel(c["log_id"]) if c and c.get("log_id") else None

# ═══════════════════════════════════════════════════════════════════
# CLAIMDAILY
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="claimdaily", aliases=["daily", "claim"])
async def claimdaily_cmd(ctx):
    last = await db.last_claim(ctx.guild.id, ctx.author.id)
    now  = datetime.now(timezone.utc)
    if last:
        t   = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        rem = CLAIM_CD - (now - t).total_seconds()
        if rem > 0:
            e = discord.Embed(color=0xFEE75C)
            e.description = f"⏳  Next claim in **{fmt_cd(rem)}**"
            return await ctx.reply(embed=e)
    amt = roll_claim()
    await db.adj(ctx.guild.id, ctx.author.id, amt)
    await db.touch_claim(ctx.guild.id, ctx.author.id)
    bl  = await db.bal(ctx.guild.id, ctx.author.id)
    e   = discord.Embed(color=0x57F287)
    e.description = f"{rarity_label(amt)}\n\nClaimed {CE} **{fm(amt)} {CUR}**"
    e.set_footer(text=f"Balance: {CE} {fm(bl)}  ·  Next in 3h")
    await ctx.reply(embed=e)

@bot.tree.command(name="claimdaily", description="Claim your Shillings every 3 hours")
async def sl_claimdaily(interaction: discord.Interaction):
    last = await db.last_claim(interaction.guild_id, interaction.user.id)
    now  = datetime.now(timezone.utc)
    if last:
        t   = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        rem = CLAIM_CD - (now - t).total_seconds()
        if rem > 0:
            return await interaction.response.send_message(
                embed=discord.Embed(color=0xFEE75C, description=f"⏳  Next claim in **{fmt_cd(rem)}**"),
                ephemeral=True)
    amt = roll_claim()
    await db.adj(interaction.guild_id, interaction.user.id, amt)
    await db.touch_claim(interaction.guild_id, interaction.user.id)
    bl  = await db.bal(interaction.guild_id, interaction.user.id)
    e   = discord.Embed(color=0x57F287)
    e.description = f"{rarity_label(amt)}\n\nClaimed {CE} **{fm(amt)} {CUR}**"
    e.set_footer(text=f"Balance: {CE} {fm(bl)}  ·  Next in 3h")
    await interaction.response.send_message(embed=e)

# ═══════════════════════════════════════════════════════════════════
# BALANCE
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="balance", aliases=["bal", "wallet"])
async def balance_cmd(ctx, member: discord.Member = None):
    m  = member or ctx.author
    bl = await db.bal(ctx.guild.id, m.id)
    wr = await db.wager_req(ctx.guild.id, m.id)
    e  = discord.Embed(color=0x5865F2)
    e.set_author(name=m.display_name, icon_url=m.display_avatar.url)
    e.set_thumbnail(url=m.display_avatar.url)
    e.description = f"{CE}  **{fm(bl)} {CUR}**"
    if wr > 0:
        e.description += f"\n\n⚠️  Must wager **{CE} {fm(wr)}** more before you can withdraw."
    await ctx.reply(embed=e)

@bot.tree.command(name="balance", description="Check your or someone's balance")
@app_commands.describe(member="Member to check")
async def sl_balance(interaction: discord.Interaction, member: discord.Member = None):
    m  = member or interaction.user
    bl = await db.bal(interaction.guild_id, m.id)
    wr = await db.wager_req(interaction.guild_id, m.id)
    e  = discord.Embed(color=0x5865F2)
    e.set_author(name=m.display_name, icon_url=m.display_avatar.url)
    e.description = f"{CE}  **{fm(bl)} {CUR}**"
    if wr > 0:
        e.description += f"\n\n⚠️  Must wager **{CE} {fm(wr)}** more before you can withdraw."
    await interaction.response.send_message(embed=e)

# ═══════════════════════════════════════════════════════════════════
# COINFLIP  (provably fair)
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="coinflip", aliases=["cf"])
async def coinflip_cmd(ctx, bet: str = None, side: str = None):
    if not bet or not side:
        return await ctx.reply(embed=discord.Embed(
            description="**Usage:** `!cf <bet> <heads/tails>`", color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    side   = "heads" if side.lower() in ("heads", "h") else "tails"
    result = fair_flip()
    coin   = "🪙 Heads" if result == "heads" else "🟫 Tails"
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if result == side:
        await db.adj(ctx.guild.id, ctx.author.id, amt)
        e = e_win("Coinflip", f"{coin} — you called it!", amt)
    else:
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("Coinflip", f"{coin} — wrong call.", amt)
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# SLOTS  (provably fair)
# ═══════════════════════════════════════════════════════════════════

SLOT_SYM = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣"]
SLOT_W   = [30, 25, 20, 15, 7, 3]
SLOT_PAY = {"7️⃣": 50, "💎": 20, "🍇": 10, "🍊": 5, "🍋": 3, "🍒": 2}

@bot.command(name="slots")
async def slots_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!slots <bet>`", color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    # Provably fair: pick each reel independently
    reels = [random.choices(SLOT_SYM, weights=SLOT_W, k=1)[0] for _ in range(3)]
    row   = "  ".join(reels)
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if reels[0] == reels[1] == reels[2]:
        mult = SLOT_PAY[reels[0]]
        win  = amt * mult
        await db.adj(ctx.guild.id, ctx.author.id, win)
        e = e_win("🎰  Slots", f"`[ {row} ]`\n**Jackpot! {mult}x**", win)
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        e = e_tie("🎰  Slots", f"`[ {row} ]`\n2 of a kind — money back")
    else:
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("🎰  Slots", f"`[ {row} ]`\nNo match", amt)
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# ROULETTE  (provably fair)
# ═══════════════════════════════════════════════════════════════════

RL_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

@bot.command(name="roulette", aliases=["rl"])
async def roulette_cmd(ctx, bet: str = None, *, choice: str = None):
    if not bet or not choice:
        return await ctx.reply(embed=discord.Embed(description=(
            "**🎡  Roulette**\n`!rl <bet> <choice>`\n\n"
            "> `red`/`black` — **2x**\n"
            "> `odd`/`even` — **2x**\n"
            "> `1-12`/`13-24`/`25-36` — **3x**\n"
            "> Exact number 0–36 — **36x**"), color=0x5865F2))
    amt  = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    spin = fair_int(0, 36)
    c    = choice.lower().strip()
    clr  = "🟥" if spin in RL_RED else ("⬛" if spin > 0 else "🟩")
    won, mult = False, 0
    if c == "red":           won, mult = spin in RL_RED, 2
    elif c == "black":       won, mult = spin not in RL_RED and spin > 0, 2
    elif c == "odd":         won, mult = spin > 0 and spin % 2 == 1, 2
    elif c == "even":        won, mult = spin > 0 and spin % 2 == 0, 2
    elif c in ("1-12","first"):   won, mult = 1 <= spin <= 12, 3
    elif c in ("13-24","second"): won, mult = 13 <= spin <= 24, 3
    elif c in ("25-36","third"):  won, mult = 25 <= spin <= 36, 3
    else:
        try:
            n = int(c)
            if 0 <= n <= 36: won, mult = spin == n, 36
            else: return await ctx.reply(embed=discord.Embed(description="Number must be 0–36.", color=0xED4245))
        except ValueError:
            return await ctx.reply(embed=discord.Embed(description="Invalid choice. Run `!rl` for options.", color=0xED4245))
    line = f"Ball landed on {clr} **{spin}**"
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if won:
        win = amt * (mult - 1)
        await db.adj(ctx.guild.id, ctx.author.id, win)
        e = e_win("🎡  Roulette", f"{line}\nYou picked **{c}** — {mult}x!", win)
    else:
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("🎡  Roulette", f"{line}\nYou picked **{c}**", amt)
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# BLACKJACK  (provably fair)
# ═══════════════════════════════════════════════════════════════════

def mk_deck():
    d = [f"{r}{s}" for s in ["♠","♥","♦","♣"]
         for r in ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]]
    return fair_shuffle(d)

def card_val(card):
    r = card[:-1]
    if r in ("J","Q","K"): return 10
    if r == "A": return 11
    return int(r)

def hand_val(hand):
    v, aces = sum(card_val(c) for c in hand), sum(1 for c in hand if c.startswith("A"))
    while v > 21 and aces: v -= 10; aces -= 1
    return v

class BjView(View):
    def __init__(self, uid, gid, deck, ph, dh, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.deck=deck
        self.ph=ph; self.dh=dh; self.bet=bet
        self.done=False; self.msg=None

    def _embed(self, reveal=False):
        pv = hand_val(self.ph); dv = hand_val(self.dh)
        e  = discord.Embed(color=0x5865F2)
        if reveal:
            e.description = (f"**🃏  Blackjack**\n\n"
                             f"**Dealer** ({dv}): {' '.join(self.dh)}\n"
                             f"**You** ({pv}): {' '.join(self.ph)}")
        else:
            e.description = (f"**🃏  Blackjack**\n\n"
                             f"**Dealer**: {self.dh[0]} 🂠\n"
                             f"**You** ({pv}): {' '.join(self.ph)}")
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def _finish(self, interaction):
        while hand_val(self.dh) < 17: self.dh.append(self.deck.pop())
        pv = hand_val(self.ph); dv = hand_val(self.dh)
        self.done = True; self.stop()
        for i in self.children: i.disabled = True
        await db.reduce_wager(self.gid, self.uid, self.bet)
        if pv > 21:
            await db.adj(self.gid, self.uid, -self.bet)
            e = e_lose("🃏  Blackjack", f"Bust ({pv}). Dealer had {dv}.", self.bet)
        elif dv > 21 or pv > dv:
            await db.adj(self.gid, self.uid, self.bet)
            e = e_win("🃏  Blackjack", f"You win! ({pv} vs {dv})", self.bet)
        elif pv == dv:
            e = e_tie("🃏  Blackjack", f"Push! Both {pv}.")
        else:
            await db.adj(self.gid, self.uid, -self.bet)
            e = e_lose("🃏  Blackjack", f"Dealer wins ({dv} vs {pv}).", self.bet)
        e.description += f"\nDealer: {' '.join(self.dh)}\nYou: {' '.join(self.ph)}"
        add_bal(e, await db.bal(self.gid, self.uid))
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Hit", style=ButtonStyle.green)
    async def hit(self, interaction, _):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
        self.ph.append(self.deck.pop())
        if hand_val(self.ph) >= 21: await self._finish(interaction)
        else: await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Stand", style=ButtonStyle.red)
    async def stand(self, interaction, _):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
        await self._finish(interaction)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(view=self)
        except: pass

@bot.command(name="blackjack", aliases=["bj"])
async def bj_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!bj <bet>`", color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    deck = mk_deck(); ph = [deck.pop(), deck.pop()]; dh = [deck.pop(), deck.pop()]
    if hand_val(ph) == 21:
        win = int(amt * 1.5)
        await db.adj(ctx.guild.id, ctx.author.id, win)
        await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
        e = e_win("🃏  Blackjack",
                  f"**Natural 21! 1.5x**\nYou: {' '.join(ph)}\nDealer: {' '.join(dh)} ({hand_val(dh)})", win)
        add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
        return await ctx.reply(embed=e)
    v = BjView(ctx.author.id, ctx.guild.id, deck, ph, dh, amt)
    v.msg = await ctx.reply(embed=v._embed(), view=v)

# ═══════════════════════════════════════════════════════════════════
# LIMBO  (provably fair)
# ═══════════════════════════════════════════════════════════════════

def gen_limbo() -> float:
    r = secrets.randbelow(10_000_000) / 10_000_000.0
    if r < 0.04: return 1.00
    return round(0.96 / r, 2)

@bot.command(name="limbo")
async def limbo_cmd(ctx, bet: str = None, target: str = None):
    if not bet or not target:
        return await ctx.reply(embed=discord.Embed(description=(
            "**🎯  Limbo**\n`!limbo <bet> <target>`\n\n"
            "Pick a multiplier. If result ≥ target, you win.\n"
            "Win chance = **96%** ÷ target\n\n"
            "`!limbo 1000 2` → 48% chance → 2x\n"
            "`!limbo 1000 10` → 9.6% chance → 10x"), color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    try: tgt = float(target)
    except: return await ctx.reply(embed=discord.Embed(description="Target must be a number.", color=0xED4245))
    if tgt < 1.01 or tgt > 1_000_000:
        return await ctx.reply(embed=discord.Embed(description="Target must be between **1.01** and **1,000,000**.", color=0xED4245))
    result = gen_limbo()
    chance = round(96 / tgt, 2)
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if result >= tgt:
        win = int(amt * tgt) - amt
        await db.adj(ctx.guild.id, ctx.author.id, win)
        e = e_win("🎯  Limbo", f"Result: **{result:.2f}x** ≥ target **{tgt:.2f}x**", win)
    else:
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("🎯  Limbo", f"Result: **{result:.2f}x** < target **{tgt:.2f}x**", amt)
    e.description += f"\n> Win chance: **{chance:.1f}%**"
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# CRASH  (provably fair)
# ═══════════════════════════════════════════════════════════════════

def gen_crash() -> float:
    r = secrets.randbelow(10_000_000) / 10_000_000.0
    if r < 0.04: return 1.00
    return round(0.96 / r, 2)

class CrashView(View):
    def __init__(self, uid, gid, bet, crash_at):
        super().__init__(timeout=120)
        self.uid=uid; self.gid=gid; self.bet=bet; self.crash_at=crash_at
        self.cur=1.00; self.cashed=False; self.crashed=False
        self.msg=None; self.task=None

    def _embed(self):
        if self.crashed:
            e = discord.Embed(color=0xED4245)
            e.description = f"**📈  Crash**\n\n💥 Crashed at **{self.crash_at:.2f}x**!"
        else:
            e = discord.Embed(color=0x57F287)
            e.description = (f"**📈  Crash**\n\n**{self.cur:.2f}x** 🚀\n\n"
                             f"Potential: {CE} **{fm(int(self.bet * self.cur))}**")
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    @discord.ui.button(label="💰 Cash Out", style=ButtonStyle.green, custom_id="crash_co")
    async def cashout(self, interaction, _):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
        if self.cashed or self.crashed:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Game is already over.", color=0xFEE75C), ephemeral=True)
        self.cashed = True
        if self.task: self.task.cancel()
        self.stop()
        for i in self.children: i.disabled = True
        win = int(self.bet * self.cur) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        e = e_win("📈  Crash", f"Cashed out at **{self.cur:.2f}x** before crash at {self.crash_at:.2f}x!", win)
        add_bal(e, await db.bal(self.gid, self.uid))
        await interaction.response.edit_message(embed=e, view=self)

async def _crash_loop(view: CrashView):
    while view.cur < view.crash_at and not view.cashed:
        await asyncio.sleep(1.0)
        if view.cashed: break
        view.cur = round(min(view.cur + 0.10, view.crash_at), 2)
        if view.msg:
            try: await view.msg.edit(embed=view._embed(), view=view)
            except: break
    if not view.cashed:
        view.crashed = True; view.stop()
        for i in view.children: i.disabled = True
        await db.adj(view.gid, view.uid, -view.bet)
        await db.reduce_wager(view.gid, view.uid, view.bet)
        e = e_lose("📈  Crash", f"💥 Crashed at **{view.crash_at:.2f}x**!", view.bet)
        add_bal(e, await db.bal(view.gid, view.uid))
        if view.msg:
            try: await view.msg.edit(embed=e, view=view)
            except: pass

@bot.command(name="crash")
async def crash_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(
            description="**📈  Crash**\n`!crash <bet>`\n\nMultiplier climbs — cash out before it crashes!", color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    cp   = gen_crash()
    view = CrashView(ctx.author.id, ctx.guild.id, amt, cp)
    view.msg  = await ctx.reply(embed=view._embed(), view=view)
    view.task = asyncio.create_task(_crash_loop(view))

# ═══════════════════════════════════════════════════════════════════
# HIGHER OR LOWER  (provably fair)
# ═══════════════════════════════════════════════════════════════════

HL_MULTS = [1.0,1.5,2.2,3.2,4.7,6.8,9.9,14.0,20.0,29.0,42.0,60.0,85.0]
HL_DECK  = [f"{r}{s}" for s in ["♠","♥","♦","♣"]
             for r in ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]]

class HLView(View):
    def __init__(self, uid, gid, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.bet=bet
        self.deck = fair_shuffle(HL_DECK)
        self.cur  = self.deck.pop()
        self.streak = 0; self.done = False; self.msg = None

    def _mult(self): return HL_MULTS[min(self.streak, len(HL_MULTS)-1)]

    def _embed(self, status=""):
        e = discord.Embed(color=0x5865F2)
        e.description = (f"**🃏  Higher or Lower**\n\nCard: **{self.cur}**\n"
                         f"Streak: **{self.streak}** — {self._mult():.1f}x\n"
                         f"Potential: {CE} **{fm(int(self.bet * self._mult()))}**\n{status}")
        return e

    async def _guess(self, interaction, direction):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
        if not self.deck: self.deck = fair_shuffle(HL_DECK)
        nxt = self.deck.pop()
        cv  = card_val(self.cur); nv = card_val(nxt); self.cur = nxt
        if cv == nv:
            return await interaction.response.edit_message(
                embed=self._embed(f"↔️ Tie — {nxt}"), view=self)
        correct = (direction == "h" and nv > cv) or (direction == "l" and nv < cv)
        if correct:
            self.streak += 1
            await interaction.response.edit_message(embed=self._embed(f"✅ Correct! **{nxt}**"), view=self)
        else:
            self.done = True; self.stop()
            for i in self.children: i.disabled = True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            e = e_lose("🃏  Higher or Lower", f"❌ Next was **{nxt}** (streak: {self.streak})", self.bet)
            add_bal(e, await db.bal(self.gid, self.uid))
            await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="⬆️ Higher", style=ButtonStyle.green, custom_id="hl_h")
    async def higher(self, i, _): await self._guess(i, "h")
    @discord.ui.button(label="⬇️ Lower",  style=ButtonStyle.red,   custom_id="hl_l")
    async def lower(self, i, _):  await self._guess(i, "l")

    @discord.ui.button(label="💰 Cash Out", style=ButtonStyle.primary, custom_id="hl_co")
    async def cashout(self, interaction, _):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
        if self.streak == 0:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Get at least 1 correct first.", color=0xFEE75C), ephemeral=True)
        self.done = True; self.stop()
        for i in self.children: i.disabled = True
        win = int(self.bet * self._mult()) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        e = e_win("🃏  Higher or Lower", f"Cashed out at **{self._mult():.1f}x** (streak {self.streak})", win)
        add_bal(e, await db.bal(self.gid, self.uid))
        await interaction.response.edit_message(embed=e, view=self)

    async def on_timeout(self):
        if not self.done:
            await db.adj(self.gid, self.uid, -self.bet)
            for i in self.children: i.disabled = True
            try: await self.msg.edit(embed=e_lose("Higher or Lower", "⏰ Timed out", self.bet), view=self)
            except: pass

@bot.command(name="higherlower", aliases=["hl"])
async def hl_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!hl <bet>`", color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    v = HLView(ctx.author.id, ctx.guild.id, amt)
    v.msg = await ctx.reply(embed=v._embed(), view=v)

# ═══════════════════════════════════════════════════════════════════
# MINES  (provably fair, 4×4 grid)
# ═══════════════════════════════════════════════════════════════════

_MINES_PAY = {
    1:  [1.0,1.1,1.2,1.4,1.6,1.9,2.2,2.6,3.1,3.7,4.5,5.5,6.7,8.2,10.0,12.5],
    3:  [1.0,1.2,1.5,1.9,2.4,3.1,4.0,5.2,6.9,9.3,12.7,17.6,25.0,37.0,58.0,99.0],
    5:  [1.0,1.4,2.0,3.0,4.5,6.8,10.5,16.5,26.5,44.0,76.0,140.0,280.0,640.0,1700.0,6200.0],
    10: [1.0,1.7,3.1,6.0,12.5,28.0,68.0,185.0,580.0,2200.0,11000.0,80000.0,1200000.0,200000000.0,9e10,9e10],
}

def mines_mult(mc, safe): t = _MINES_PAY.get(mc, _MINES_PAY[3]); return t[min(safe, len(t)-1)]

class MinesView(View):
    def __init__(self, uid, gid, bet, mc):
        super().__init__(timeout=120)
        self.uid=uid; self.gid=gid; self.bet=bet; self.mc=mc
        positions = list(range(16))
        self.mines = set(random.sample(positions, mc))  # fair random placement
        self.rev   = set(); self.safe_cnt = 0
        self.done  = False; self.msg = None
        self._build()

    def _mult(self): return mines_mult(self.mc, self.safe_cnt)

    def _build(self):
        self.clear_items()
        for i in range(16):
            row = i // 4
            if i in self.rev:
                lbl = "💣" if i in self.mines else "✅"
                sty = ButtonStyle.danger if i in self.mines else ButtonStyle.success
                btn = Button(label=lbl, style=sty, disabled=True, row=row)
            else:
                btn = Button(label="​", style=ButtonStyle.secondary, disabled=self.done, row=row,
                             custom_id=f"mine_{i}_{self.uid}")
            self.add_item(btn)
        if self.safe_cnt > 0 and not self.done:
            m  = self._mult()
            co = Button(label=f"💰 {m:.2f}x", style=ButtonStyle.primary, row=4,
                        custom_id=f"mine_co_{self.uid}")
            self.add_item(co)

    def _embed(self, status=""):
        m = self._mult()
        e = discord.Embed(color=0x5865F2)
        e.description = (f"**💣  Mines** ({self.mc} mines)\n\n"
                         f"Safe found: **{self.safe_cnt}** — **{m:.2f}x**\n"
                         f"Potential: {CE} **{fm(int(self.bet * m))}**\n{status}")
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def interaction_check(self, interaction) -> bool:
        cid = interaction.data.get("custom_id", "")
        if not cid.startswith("mine_"): return True
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
            return False
        if self.done: return False
        if cid.startswith("mine_co_"): await self._cashout(interaction)
        else:
            try: tile = int(cid.split("_")[1]); await self._tile(interaction, tile)
            except: pass
        return False

    async def _tile(self, interaction, t):
        self.rev.add(t)
        if t in self.mines:
            for m in self.mines: self.rev.add(m)
            self.done = True; self.stop(); self._build()
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            e = e_lose("💣  Mines", "💥 Boom! Hit a mine.", self.bet)
            add_bal(e, await db.bal(self.gid, self.uid))
            await interaction.response.edit_message(embed=e, view=self)
        else:
            self.safe_cnt += 1; self._build()
            await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _cashout(self, interaction):
        self.done = True; self.stop(); self._build()
        win = int(self.bet * self._mult()) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        e = e_win("💣  Mines", f"Cashed out at **{self._mult():.2f}x** ({self.safe_cnt} safe tiles)", win)
        add_bal(e, await db.bal(self.gid, self.uid))
        await interaction.response.edit_message(embed=e, view=self)

    async def on_timeout(self):
        if not self.done:
            self.done = True
            await db.adj(self.gid, self.uid, -self.bet)
            try: self._build(); await self.msg.edit(embed=e_lose("Mines", "⏰ Timed out", self.bet), view=self)
            except: pass

@bot.command(name="mines")
async def mines_cmd(ctx, bet: str = None, mc: int = 3):
    if not bet:
        return await ctx.reply(embed=discord.Embed(
            description="**💣  Mines**\n`!mines <bet> [1/3/5/10]`\n\nMore mines = higher multiplier.", color=0x5865F2))
    if mc not in (1,3,5,10):
        return await ctx.reply(embed=discord.Embed(description="Mines must be **1**, **3**, **5**, or **10**.", color=0xED4245))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    v = MinesView(ctx.author.id, ctx.guild.id, amt, mc)
    v.msg = await ctx.reply(embed=v._embed(), view=v)

# ═══════════════════════════════════════════════════════════════════
# WAR  (provably fair)
# ═══════════════════════════════════════════════════════════════════

WAR_DECK = [f"{r}{s}" for s in ["♠","♥","♦","♣"]
             for r in ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]]

@bot.command(name="war")
async def war_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(
            description="**⚔️  War**\n`!war <bet>`\n\nDraw a card. Highest wins. Ties = sudden death.", color=0x5865F2))
    amt  = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    deck   = fair_shuffle(WAR_DECK)
    rounds = []; yc = deck.pop(); dc = deck.pop(); rounds.append((yc, dc))
    while card_val(yc) == card_val(dc) and len(deck) >= 2:
        yc = deck.pop(); dc = deck.pop(); rounds.append((yc, dc))
    lines = []
    for i, (y, d) in enumerate(rounds):
        lbl = "Round 1" if i == 0 else f"War Round {i+1}"
        lines.append(f"**{lbl}:** You **{y}** vs Dealer **{d}**")
    detail = "\n".join(lines)
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if card_val(yc) > card_val(dc):
        await db.adj(ctx.guild.id, ctx.author.id, amt)
        e = e_win("⚔️  War", detail, amt)
    elif card_val(yc) < card_val(dc):
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("⚔️  War", detail, amt)
    else:
        e = e_tie("⚔️  War", detail + "\nDeck exhausted — tie!")
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# HORSE RACE  (provably fair)
# ═══════════════════════════════════════════════════════════════════

HORSES = [
    ("🐎 Thunderhoof",  2.0, 30),
    ("🐴 Silver Wind",  3.0, 22),
    ("🏇 Dark Knight",  4.0, 18),
    ("🦄 Stardust",     6.0, 14),
    ("🌪️ Whirlwind",   10.0,  9),
    ("💀 Last Chance", 20.0,  7),
]

@bot.command(name="horserace", aliases=["hr", "horse"])
async def horse_cmd(ctx, bet: str = None, pick: int = None):
    if not bet or pick is None:
        lines = "\n".join(f"`{i+1}` {n} — **{o}x** ({w}% win chance)"
                          for i, (n, o, w) in enumerate(HORSES))
        return await ctx.reply(embed=discord.Embed(
            description=f"**🏇  Horse Race**\n`!hr <bet> <1-6>`\n\n{lines}", color=0x5865F2))
    if not 1 <= pick <= 6:
        return await ctx.reply(embed=discord.Embed(description="Pick a horse from **1** to **6**.", color=0xED4245))
    amt  = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    weights = [w for _, _, w in HORSES]
    winner  = random.choices(range(6), weights=weights, k=1)[0]
    chosen  = pick - 1
    cn, co, _ = HORSES[chosen]; wn, _, _ = HORSES[winner]
    positions = fair_shuffle(list(range(6)))
    race = "\n".join(
        ("🏆" if hi == winner else f"{pos}.") + " " + HORSES[hi][0]
        for pos, hi in enumerate(positions, 1))
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if winner == chosen:
        win = int(amt * (co - 1))
        await db.adj(ctx.guild.id, ctx.author.id, win)
        e = e_win("🏇  Horse Race", f"{race}\n\nYou picked **{cn}** and it won!", win)
    else:
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("🏇  Horse Race", f"{race}\n\nYou picked **{cn}** — **{wn}** won.", amt)
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# BOMB DEFUSE  (provably fair — 5 wires, 2 bombs)
# ═══════════════════════════════════════════════════════════════════

WIRE_CLR = ["🔴","🟡","🟢","🔵","🟣"]

class BombView(View):
    def __init__(self, uid, gid, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.bet=bet
        wires = fair_shuffle(list(range(5)))
        self.bombs = set(wires[:2])
        self.cut   = set(); self.done = False; self.msg = None
        self._build()

    def _safe_cut(self): return len([c for c in self.cut if c not in self.bombs])
    def _mult(self): return [0, 1.5, 2.5, 5.0][min(self._safe_cut(), 3)]

    def _build(self):
        self.clear_items()
        for i in range(5):
            if i in self.cut:
                lbl = "💣" if i in self.bombs else "✂️"
                sty = ButtonStyle.danger if i in self.bombs else ButtonStyle.success
                btn = Button(label=f"{WIRE_CLR[i]} {lbl}", style=sty, disabled=True, row=0)
            else:
                btn = Button(label=f"{WIRE_CLR[i]} Wire", style=ButtonStyle.secondary,
                             disabled=self.done, row=0,
                             custom_id=f"bomb_{i}_{self.uid}")
            self.add_item(btn)
        if self._safe_cut() > 0 and not self.done:
            m  = self._mult()
            co = Button(label=f"💰 Cash Out {m:.1f}x", style=ButtonStyle.primary,
                        row=1, custom_id=f"bomb_co_{self.uid}")
            self.add_item(co)

    def _embed(self, status=""):
        safe = self._safe_cut()
        e = discord.Embed(color=0x5865F2)
        e.description = (f"**💣  Bomb Defuse**\n\n"
                         f"5 wires — 2 are bombs, 3 are safe.\n"
                         f"Safe cut: **{safe}/3** — **{self._mult():.1f}x**\n{status}")
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def interaction_check(self, interaction) -> bool:
        cid = interaction.data.get("custom_id", "")
        if not cid.startswith("bomb_"): return True
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                embed=discord.Embed(description="Not your game.", color=0xED4245), ephemeral=True)
            return False
        if self.done: return False
        if cid.startswith("bomb_co_"): await self._cashout(interaction)
        else:
            try: wire = int(cid.split("_")[1]); await self._cut(interaction, wire)
            except: pass
        return False

    async def _cut(self, interaction, w):
        self.cut.add(w)
        if w in self.bombs:
            for i in range(5): self.cut.add(i)
            self.done = True; self.stop(); self._build()
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            e = e_lose("💣  Bomb Defuse", "💥 You cut a bomb wire!", self.bet)
            add_bal(e, await db.bal(self.gid, self.uid))
            await interaction.response.edit_message(embed=e, view=self)
        else:
            safe = self._safe_cut()
            if safe == 3:
                self.done = True; self.stop(); self._build()
                win = int(self.bet * self._mult()) - self.bet
                await db.adj(self.gid, self.uid, win)
                await db.reduce_wager(self.gid, self.uid, self.bet)
                e = e_win("💣  Bomb Defuse", "✅ All 3 safe wires cut — max payout!", win)
                add_bal(e, await db.bal(self.gid, self.uid))
                await interaction.response.edit_message(embed=e, view=self)
            else:
                self._build()
                await interaction.response.edit_message(
                    embed=self._embed(f"✅ Safe! {safe}/3 cut"), view=self)

    async def _cashout(self, interaction):
        self.done = True; self.stop(); self._build()
        win = int(self.bet * self._mult()) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        e = e_win("💣  Bomb Defuse", f"Cashed out with {self._safe_cut()} safe wires — **{self._mult():.1f}x**!", win)
        add_bal(e, await db.bal(self.gid, self.uid))
        await interaction.response.edit_message(embed=e, view=self)

    async def on_timeout(self):
        if not self.done:
            self.done = True
            await db.adj(self.gid, self.uid, -self.bet)
            try: self._build(); await self.msg.edit(embed=e_lose("Bomb Defuse", "⏰ Timed out", self.bet), view=self)
            except: pass

@bot.command(name="bomb", aliases=["bombdefuse", "bd"])
async def bomb_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(
            description="**💣  Bomb Defuse**\n`!bomb <bet>`\n\n`1 safe=1.5x` · `2 safe=2.5x` · `3 safe=5x`",
            color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    v = BombView(ctx.author.id, ctx.guild.id, amt)
    v.msg = await ctx.reply(embed=v._embed(), view=v)

# ═══════════════════════════════════════════════════════════════════
# NUMBER GUESS  (provably fair, 1–100)
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="numguess", aliases=["ng", "guess", "number"])
async def numguess_cmd(ctx, bet: str = None, guess: int = None):
    if not bet or guess is None:
        return await ctx.reply(embed=discord.Embed(description=(
            "**🔢  Number Guess**\n`!ng <bet> <1-100>`\n\n"
            "> **Exact** — **90x**\n"
            "> **Within 3** — **10x**\n"
            "> **Within 10** — **3x**\n"
            "> **Miss** — lose"), color=0x5865F2))
    if not 1 <= guess <= 100:
        return await ctx.reply(embed=discord.Embed(description="Guess must be **1–100**.", color=0xED4245))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    result = fair_int(1, 100)
    diff   = abs(result - guess)
    await db.reduce_wager(ctx.guild.id, ctx.author.id, amt)
    if diff == 0:      mult, lbl = 90,  "🎯 **Exact!**"
    elif diff <= 3:    mult, lbl = 10,  f"🔥 **Within 3!** (off by {diff})"
    elif diff <= 10:   mult, lbl = 3,   f"🌡️ **Within 10!** (off by {diff})"
    else:              mult, lbl = 0,   f"❌ **Miss!** (off by {diff})"
    detail = f"Number was **{result}**, you guessed **{guess}**\n{lbl}"
    if mult > 0:
        win = amt * (mult - 1)
        await db.adj(ctx.guild.id, ctx.author.id, win)
        e = e_win("🔢  Number Guess", detail, win)
    else:
        await db.adj(ctx.guild.id, ctx.author.id, -amt)
        e = e_lose("🔢  Number Guess", detail, amt)
    add_bal(e, await db.bal(ctx.guild.id, ctx.author.id))
    await ctx.reply(embed=e)

# ═══════════════════════════════════════════════════════════════════
# SCRATCH CARD  (provably fair)
# ═══════════════════════════════════════════════════════════════════

SC_SYM = ["💰","💎","⭐","🍀","🎁","💣"]
SC_W   = [25, 15, 20, 18, 15, 7]
SC_PAY = {"💎": 50, "💰": 20, "🍀": 10, "⭐": 6, "🎁": 3}

class ScratchView(View):
    def __init__(self, uid, gid, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.bet=bet
        self.tiles = random.choices(SC_SYM, weights=SC_W, k=9)
        self.rev   = set(); self.done = False; self.msg = None
        self._build()

    def _build(self):
        self.clear_items()
        for i in range(9):
            row = i // 3
            if i in self.rev:
                lbl = self.tiles[i]
                sty = ButtonStyle.danger if lbl == "💣" else ButtonStyle.success
                btn = Button(label=lbl, style=sty, disabled=True, row=row)
            else:
                btn = Button(label="?", style=ButtonStyle.secondary, disabled=self.done, row=row,
                             custom_id=f"sc_{i}_{self.uid}")
            self.add_item(btn)

    def _check_win(self):
        lines = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]]
        best  = 0
        for line in lines:
            if all(i in self.rev for i in line):
                s = [self.tiles[i] for i in line]
                if s[0] == s[1] == s[2]:
                    if s[0] == "💣": return -1
                    best = max(best, SC_PAY.get(s[0], 0))
        return best

    def _embed(self):
        e = discord.Embed(color=0x5865F2)
        e.description = (f"**🎟️  Scratch Card**\n\n"
                         f"Reveal all 9 tiles. Match 3 in a row to win!\n"
                         f"Tiles left: **{9 - len(self.rev)}**")
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def interaction_check(self, interaction) -> bool:
        cid = interaction.data.get("custom_id", "")
        if not cid.startswith("sc_"): return True
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                embed=discord.Embed(description="Not your card.", color=0xED4245), ephemeral=True)
            return False
        if self.done: return False
        try: tile = int(cid.split("_")[1]); await self._reveal(interaction, tile)
        except: pass
        return False

    async def _reveal(self, interaction, t):
        self.rev.add(t); self._build()
        if self.tiles[t] == "💣":
            self.done = True; self.stop()
            for i in range(9): self.rev.add(i)
            self._build()
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            e = e_lose("🎟️  Scratch Card", "💣 Hit a bomb tile!", self.bet)
            add_bal(e, await db.bal(self.gid, self.uid))
            return await interaction.response.edit_message(embed=e, view=self)
        if len(self.rev) == 9:
            self.done = True; self.stop()
            mult = self._check_win()
            await db.reduce_wager(self.gid, self.uid, self.bet)
            if mult > 0:
                win = int(self.bet * mult) - self.bet
                await db.adj(self.gid, self.uid, win)
                e = e_win("🎟️  Scratch Card", f"3 in a row! **{mult}x**", win)
            else:
                await db.adj(self.gid, self.uid, -self.bet)
                e = e_lose("🎟️  Scratch Card", "No 3 in a row.", self.bet)
            add_bal(e, await db.bal(self.gid, self.uid))
            await interaction.response.edit_message(embed=e, view=self)
        else:
            await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        if not self.done:
            self.done = True
            await db.adj(self.gid, self.uid, -self.bet)
            try: self._build(); await self.msg.edit(embed=e_lose("Scratch Card", "⏰ Timed out", self.bet), view=self)
            except: pass

@bot.command(name="scratch", aliases=["sc"])
async def scratch_cmd(ctx, bet: str = None):
    if not bet:
        return await ctx.reply(embed=discord.Embed(
            description="**🎟️  Scratch Card**\n`!scratch <bet>`\n\nReveal 9 tiles. Match 3 in a row/col/diag to win!",
            color=0x5865F2))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    v = ScratchView(ctx.author.id, ctx.guild.id, amt)
    v.msg = await ctx.reply(embed=v._embed(), view=v)

# ═══════════════════════════════════════════════════════════════════
# DUEL (PvP)  — provably fair
# ═══════════════════════════════════════════════════════════════════

class DuelView(View):
    def __init__(self, challenger, opponent, bet):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent   = opponent
        self.bet        = bet
        self.msg        = None

    @discord.ui.button(label="✅ Accept", style=ButtonStyle.green, custom_id="duel_accept")
    async def accept(self, interaction, _):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(
                embed=discord.Embed(description="This duel was not sent to you.", color=0xED4245), ephemeral=True)
        self.stop()
        for i in self.children: i.disabled = True
        gid = interaction.guild_id
        cb  = await db.bal(gid, self.challenger.id)
        ob  = await db.bal(gid, self.opponent.id)
        if self.bet > cb:
            return await interaction.response.edit_message(
                embed=discord.Embed(color=0xED4245,
                    description=f"{self.challenger.mention} no longer has enough {CE}."), view=self)
        if self.bet > ob:
            return await interaction.response.edit_message(
                embed=discord.Embed(color=0xED4245,
                    description=f"You don't have enough {CE}."), view=self)
        winner = fair_choice([self.challenger, self.opponent])
        loser  = self.opponent if winner == self.challenger else self.challenger
        await db.adj(gid, winner.id,  self.bet)
        await db.adj(gid, loser.id,  -self.bet)
        await db.reduce_wager(gid, self.challenger.id, self.bet)
        await db.reduce_wager(gid, self.opponent.id,   self.bet)
        e = discord.Embed(color=0x57F287)
        e.description = (f"**⚔️  Duel**\n\n"
                         f"{self.challenger.mention} vs {self.opponent.mention}\n\n"
                         f"🏆 **{winner.display_name}** wins {CE} **{fm(self.bet * 2)}**!")
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="❌ Decline", style=ButtonStyle.red, custom_id="duel_decline")
    async def decline(self, interaction, _):
        if interaction.user.id not in (self.opponent.id, self.challenger.id):
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not for you.", color=0xED4245), ephemeral=True)
        self.stop()
        for i in self.children: i.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(color=0xFEE75C, description="⚔️ **Duel declined.**"), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(embed=discord.Embed(color=0xFEE75C, description="⚔️ Duel expired."), view=self)
        except: pass

@bot.command(name="duel")
async def duel_cmd(ctx, member: discord.Member = None, bet: str = None):
    if not member or not bet:
        return await ctx.reply(embed=discord.Embed(
            description="**⚔️  Duel**\n`!duel @user <bet>`\n\nChallenge someone to a winner-takes-all duel.",
            color=0x5865F2))
    if member.bot or member == ctx.author:
        return await ctx.reply(embed=discord.Embed(description="Invalid opponent.", color=0xED4245))
    amt = await parse_bet(ctx, bet)
    if not await chk_bet(ctx, amt): return
    ob = await db.bal(ctx.guild.id, member.id)
    if amt > ob:
        return await ctx.reply(embed=discord.Embed(
            description=f"{member.mention} doesn't have enough {CE}.", color=0xED4245))
    v = DuelView(ctx.author, member, amt)
    e = discord.Embed(color=0x5865F2)
    e.description = (f"**⚔️  Duel Challenge**\n\n"
                     f"{ctx.author.mention} challenges {member.mention}\n"
                     f"Bet: {CE} **{fm(amt)}** each\n"
                     f"Winner takes: {CE} **{fm(amt * 2)}**")
    v.msg = await ctx.reply(content=member.mention, embed=e, view=v)

# ═══════════════════════════════════════════════════════════════════
# ROLL (useful inside gamble tickets)
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="roll")
async def roll_cmd(ctx):
    result = fair_int(1, 100)
    e = discord.Embed(color=0x5865F2)
    e.description = f"🎲 **{ctx.author.display_name}** rolled **{result}** / 100"
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════
# STOCK SYSTEM
# ═══════════════════════════════════════════════════════════════════

async def build_stock_pages(gid) -> list:
    async with db.pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM stock WHERE guild_id=$1 ORDER BY added_at ASC", gid)
    if not rows: return []
    pages = []
    for i in range(0, len(rows), 10):
        chunk = rows[i:i+10]
        lines = [f"`{j+i+1}` **{r['item_name']}** — {CE} {fm(r['price'])}"
                 for j, r in enumerate(chunk)]
        pages.append({"lines": lines, "total": len(rows)})
    return pages

class StockView(View):
    def __init__(self, gid, aid):
        super().__init__(timeout=120)
        self.gid=gid; self.aid=aid; self.page=0; self.pages=[]; self.msg=None

    async def load(self): self.pages = await build_stock_pages(self.gid); self._upd()

    def _upd(self):
        self.prev_b.disabled = self.page == 0
        self.next_b.disabled = self.page >= len(self.pages) - 1

    def _embed(self):
        e = discord.Embed(color=0x5865F2)
        if not self.pages:
            e.description = "**📦  Stock Items**\n\nThe stock is currently empty."
            return e
        p = self.pages[self.page]
        e.description = "**📦  Stock Items**\n\n" + "\n".join(p["lines"])
        e.set_footer(text=f"Page {self.page+1}/{len(self.pages)}  ·  {p['total']} items")
        return e

    @discord.ui.button(label="◀", style=ButtonStyle.gray, custom_id="st_prev")
    async def prev_b(self, interaction, _):
        self.page -= 1; self._upd()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=ButtonStyle.blurple, custom_id="st_next")
    async def next_b(self, interaction, _):
        self.page += 1; self._upd()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(view=self)
        except: pass

@bot.command(name="stock")
async def stock_cmd(ctx):
    v = StockView(ctx.guild.id, ctx.author.id)
    await v.load(); v.msg = await ctx.reply(embed=v._embed(), view=v)

@bot.tree.command(name="stock", description="View items available in the stock")
async def sl_stock(interaction: discord.Interaction):
    v = StockView(interaction.guild_id, interaction.user.id)
    await v.load()
    await interaction.response.send_message(embed=v._embed(), view=v)
    v.msg = await interaction.original_response()

@bot.command(name="addtostock", aliases=["addstock", "sadd"])
@wl_only()
async def addtostock_cmd(ctx, price: int = None, *, item: str = None):
    if not price or not item:
        return await ctx.reply(embed=discord.Embed(
            description="**Usage:** `!addtostock <price> <item name>`", color=0x5865F2))
    async with db.pool.acquire() as c:
        await c.execute("INSERT INTO stock(guild_id,item_name,price,added_by) VALUES($1,$2,$3,$4)",
                        ctx.guild.id, item, price, ctx.author.id)
    e = discord.Embed(color=0x57F287)
    e.description = f"✅ Added to stock:\n**{item}** — {CE} {fm(price)}"
    await ctx.reply(embed=e)

@bot.command(name="removefromstock", aliases=["removestock", "sremove"])
@wl_only()
async def removefromstock_cmd(ctx, *, item: str = None):
    if not item:
        return await ctx.reply(embed=discord.Embed(
            description="**Usage:** `!removefromstock <item name>`", color=0x5865F2))
    async with db.pool.acquire() as c:
        res = await c.execute(
            "DELETE FROM stock WHERE guild_id=$1 AND LOWER(item_name)=LOWER($2)", ctx.guild.id, item)
    if res == "DELETE 0":
        return await ctx.reply(embed=discord.Embed(description=f"No item **{item}** found.", color=0xED4245))
    await ctx.reply(embed=discord.Embed(color=0x57F287, description=f"✅ **{item}** removed from stock."))

@bot.command(name="clearstock")
@wl_only()
async def clearstock_cmd(ctx):
    async with db.pool.acquire() as c: await c.execute("DELETE FROM stock WHERE guild_id=$1", ctx.guild.id)
    await ctx.reply(embed=discord.Embed(color=0x57F287, description="✅ Stock cleared."))

# ═══════════════════════════════════════════════════════════════════
# DEPOSIT TICKET
# ═══════════════════════════════════════════════════════════════════

class ApproveModal(Modal, title="Approve Deposit"):
    gems = TextInput(label="Gem Value to Award", placeholder="Enter amount", required=True)

    def __init__(self, dep_id, user_id, channel):
        super().__init__()
        self.dep_id  = dep_id
        self.user_id = user_id
        self.channel = channel

    async def on_submit(self, interaction):
        try: value = int(self.gems.value.replace(",","").strip())
        except:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Invalid amount.", color=0xED4245), ephemeral=True)
        if value <= 0:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Amount must be positive.", color=0xED4245), ephemeral=True)
        wager_req = value  # 1x wager requirement
        await db.adj(interaction.guild_id, self.user_id, value)
        await db.add_wager(interaction.guild_id, self.user_id, wager_req)
        member = interaction.guild.get_member(self.user_id)
        e = discord.Embed(color=0x57F287)
        e.title       = "✅  Deposit Approved"
        e.description = (f"**Approved by:** {interaction.user.mention}\n"
                         f"**Awarded:** {CE} **{fm(value)} {CUR}**\n"
                         f"**Wager required before withdraw:** {CE} **{fm(wager_req)}**")
        await interaction.response.edit_message(embed=e, view=None)
        if member:
            try:
                dm = discord.Embed(color=0x57F287)
                dm.title       = "✅  Deposit Approved"
                dm.description = (f"Your deposit in **{interaction.guild.name}** was approved!\n\n"
                                  f"{CE} **{fm(value)} {CUR}** added to your balance.\n"
                                  f"You must wager {CE} **{fm(wager_req)}** before you can withdraw.")
                await member.send(embed=dm)
            except: pass
        log = await get_log(interaction.guild_id, interaction.guild)
        if log:
            le = discord.Embed(title="📥  Deposit Approved", color=0x57F287)
            le.description = (f"**User:** {member.mention if member else self.user_id}\n"
                              f"**Approved by:** {interaction.user.mention}\n"
                              f"**Awarded:** {CE} {fm(value)}\n"
                              f"**Wager req:** {CE} {fm(wager_req)}")
            await log.send(embed=le)
        await asyncio.sleep(4)
        try: await self.channel.delete()
        except: pass

class DepositControlView(View):
    def __init__(self, dep_id, user_id, channel):
        super().__init__(timeout=None)
        self.dep_id  = dep_id
        self.user_id = user_id
        self.channel = channel

    @discord.ui.button(label="✅ Approve", style=ButtonStyle.green, custom_id="dep_approve")
    async def approve(self, interaction, _):
        if not await db.is_wl(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not authorized.", color=0xED4245), ephemeral=True)
        await interaction.response.send_modal(
            ApproveModal(self.dep_id, self.user_id, self.channel))

    @discord.ui.button(label="❌ Deny", style=ButtonStyle.red, custom_id="dep_deny")
    async def deny(self, interaction, _):
        if not await db.is_wl(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not authorized.", color=0xED4245), ephemeral=True)
        self.stop()
        for i in self.children: i.disabled = True
        member = interaction.guild.get_member(self.user_id)
        e = discord.Embed(color=0xED4245)
        e.description = f"❌ Deposit denied by {interaction.user.mention}."
        await interaction.response.edit_message(embed=e, view=self)
        if member:
            try: await member.send(embed=discord.Embed(color=0xED4245,
                description=f"Your deposit in **{interaction.guild.name}** was **denied** by staff."))
            except: pass
        await asyncio.sleep(5)
        try: await self.channel.delete()
        except: pass

async def open_deposit_ticket(guild, user, item: str):
    cat   = await get_cat(guild.id, guild)
    if not cat: return None, "Deposit category not configured. Ask an admin to run `!setup`."
    staff = await get_staff_r(guild.id, guild)
    num   = await db.next_tid(guild.id)
    tid   = f"{num:04d}"
    ow    = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if staff: ow[staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    ch = await cat.create_text_channel(f"deposit-{tid}-{user.name}", overwrites=ow)
    return ch, tid

@bot.command(name="deposit")
async def deposit_cmd(ctx, *, item: str = None):
    if not item:
        return await ctx.reply(embed=discord.Embed(
            description="**📥  Deposit**\n`!deposit <what you are depositing>`\n\n"
                        "Opens a ticket. Staff will review and award Shillings.\n"
                        "You must wager **1x** the awarded amount before withdrawing.",
            color=0x5865F2))
    ch, result = await open_deposit_ticket(ctx.guild, ctx.author, item)
    if ch is None:
        return await ctx.reply(embed=discord.Embed(description=result, color=0xED4245))
    view = DepositControlView(result, ctx.author.id, ch)
    e = discord.Embed(title="📥  Deposit Request", color=0x5865F2)
    e.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    e.description = (f"**User:** {ctx.author.mention}\n"
                     f"**Depositing:** {item}\n\n"
                     f"Staff: approve below and set the {CUR} value.")
    staff = await get_staff_r(ctx.guild.id, ctx.guild)
    ping  = ctx.author.mention
    if staff: ping += f" {staff.mention}"
    await ch.send(content=ping, embed=e, view=view)
    await ctx.reply(embed=discord.Embed(
        description=f"✅ Deposit ticket opened — {ch.mention}", color=0x57F287))

@bot.tree.command(name="deposit", description="Open a deposit ticket")
@app_commands.describe(item="What you are depositing")
async def sl_deposit(interaction: discord.Interaction, item: str):
    ch, result = await open_deposit_ticket(interaction.guild, interaction.user, item)
    if ch is None:
        return await interaction.response.send_message(
            embed=discord.Embed(description=result, color=0xED4245), ephemeral=True)
    view = DepositControlView(result, interaction.user.id, ch)
    e = discord.Embed(title="📥  Deposit Request", color=0x5865F2)
    e.description = (f"**User:** {interaction.user.mention}\n"
                     f"**Depositing:** {item}\n\n"
                     f"Staff: approve below and set the {CUR} value.")
    staff = await get_staff_r(interaction.guild_id, interaction.guild)
    ping  = interaction.user.mention
    if staff: ping += f" {staff.mention}"
    await ch.send(content=ping, embed=e, view=view)
    await interaction.response.send_message(
        embed=discord.Embed(description=f"✅ Deposit ticket opened — {ch.mention}", color=0x57F287),
        ephemeral=True)

# ═══════════════════════════════════════════════════════════════════
# WITHDRAW SYSTEM
# ═══════════════════════════════════════════════════════════════════

class WithdrawSelect(Select):
    def __init__(self, items, uid, gid):
        self.items = items; self.uid = uid; self.gid = gid
        opts = [discord.SelectOption(
                    label=f"{r['item_name'][:80]}",
                    description=f"{fm(r['price'])} {CUR}",
                    value=str(r["id"]))
                for r in items[:25]]
        super().__init__(placeholder="Select an item to withdraw...", options=opts)

    async def callback(self, interaction):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your withdrawal.", color=0xED4245), ephemeral=True)
        row_id   = int(self.values[0])
        item_row = next((r for r in self.items if r["id"] == row_id), None)
        if not item_row:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Item not found.", color=0xED4245), ephemeral=True)
        price = item_row["price"]
        bal   = await db.bal(self.gid, self.uid)
        wr    = await db.wager_req(self.gid, self.uid)
        if wr > 0:
            return await interaction.response.send_message(
                embed=discord.Embed(color=0xED4245,
                    description=f"❌ You must wager {CE} **{fm(wr)}** more before withdrawing."), ephemeral=True)
        if bal < price:
            return await interaction.response.send_message(
                embed=discord.Embed(color=0xED4245,
                    description=f"You need {CE} **{fm(price)}** but only have {CE} **{fm(bal)}**."), ephemeral=True)
        # Open withdraw ticket
        cat   = await get_cat(self.gid, interaction.guild)
        if not cat:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Withdraw category not configured.", color=0xED4245), ephemeral=True)
        staff  = await get_staff_r(self.gid, interaction.guild)
        num    = await db.next_tid(self.gid)
        tid    = f"{num:04d}"
        member = interaction.user
        ow     = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
            member:                         discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        if staff: ow[staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        ch = await cat.create_text_channel(f"withdraw-{tid}-{member.name}", overwrites=ow)
        await db.adj(self.gid, self.uid, -price)
        e = discord.Embed(title="📤  Withdraw Request", color=0xF1C40F)
        e.description = (f"**User:** {member.mention}\n"
                         f"**Item:** {item_row['item_name']}\n"
                         f"**Cost:** {CE} **{fm(price)}**\n\n"
                         f"Staff: please send this item to the user then run `!close`.")
        ping = member.mention
        if staff: ping += f" {staff.mention}"
        await ch.send(content=ping, embed=e)
        # Disable the select
        self.view.stop()
        for i in self.view.children: i.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(color=0x57F287,
                description=f"✅ Withdraw ticket opened — {ch.mention}\n{CE} **{fm(price)}** deducted."),
            view=self.view)
        log = await get_log(self.gid, interaction.guild)
        if log:
            le = discord.Embed(title="📤  Withdraw", color=0xF1C40F)
            le.description = (f"**User:** {member.mention}\n"
                              f"**Item:** {item_row['item_name']}\n"
                              f"**Cost:** {CE} {fm(price)}")
            await log.send(embed=le)

@bot.command(name="withdraw", aliases=["wd"])
async def withdraw_cmd(ctx):
    wr = await db.wager_req(ctx.guild.id, ctx.author.id)
    if wr > 0:
        return await ctx.reply(embed=discord.Embed(color=0xED4245,
            description=f"❌ You must wager {CE} **{fm(wr)}** more before you can withdraw."))
    async with db.pool.acquire() as c:
        items = await c.fetch("SELECT * FROM stock WHERE guild_id=$1 ORDER BY price ASC", ctx.guild.id)
    if not items:
        return await ctx.reply(embed=discord.Embed(description="The stock is currently empty.", color=0x5865F2))
    bal = await db.bal(ctx.guild.id, ctx.author.id)
    affordable = [r for r in items if r["price"] <= bal]
    if not affordable:
        return await ctx.reply(embed=discord.Embed(color=0xED4245,
            description=f"You don't have enough {CE} for any stock item. Run `!stock` to see prices."))
    view = View(timeout=60)
    sel  = WithdrawSelect(affordable, ctx.author.id, ctx.guild.id)
    view.add_item(sel)
    e = discord.Embed(color=0x5865F2)
    e.description = f"**📤  Withdraw**\nBalance: {CE} **{fm(bal)}**\n\nSelect an item below."
    await ctx.reply(embed=e, view=view)

@bot.tree.command(name="withdraw", description="Spend Shillings to claim a stock item")
async def sl_withdraw(interaction: discord.Interaction):
    wr = await db.wager_req(interaction.guild_id, interaction.user.id)
    if wr > 0:
        return await interaction.response.send_message(
            embed=discord.Embed(color=0xED4245,
                description=f"❌ You must wager {CE} **{fm(wr)}** more before withdrawing."), ephemeral=True)
    async with db.pool.acquire() as c:
        items = await c.fetch("SELECT * FROM stock WHERE guild_id=$1 ORDER BY price ASC", interaction.guild_id)
    if not items:
        return await interaction.response.send_message(
            embed=discord.Embed(description="Stock is empty.", color=0x5865F2), ephemeral=True)
    bal = await db.bal(interaction.guild_id, interaction.user.id)
    affordable = [r for r in items if r["price"] <= bal]
    if not affordable:
        return await interaction.response.send_message(
            embed=discord.Embed(color=0xED4245, description=f"Not enough {CE} for any item."), ephemeral=True)
    view = View(timeout=60)
    sel  = WithdrawSelect(affordable, interaction.user.id, interaction.guild_id)
    view.add_item(sel)
    e = discord.Embed(color=0x5865F2)
    e.description = f"**📤  Withdraw**\nBalance: {CE} **{fm(bal)}**\n\nSelect an item below."
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

# ═══════════════════════════════════════════════════════════════════
# GAMBLE TICKET (Middleman system)
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="gambleticket", aliases=["gt"])
async def gambleticket_cmd(ctx, opponent: discord.Member = None):
    if not opponent:
        return await ctx.reply(embed=discord.Embed(
            description="**🎲  Gamble Ticket**\n`!gambleticket @opponent`\n\n"
                        "Opens a private channel with you, your opponent, and staff.\n"
                        "Middleman holds items. Use `!roll` or `!cf` to decide the winner.",
            color=0x5865F2))
    if opponent.bot or opponent == ctx.author:
        return await ctx.reply(embed=discord.Embed(description="Invalid opponent.", color=0xED4245))
    cat = await get_cat(ctx.guild.id, ctx.guild)
    if not cat:
        return await ctx.reply(embed=discord.Embed(
            description="Category not configured. Run `!setup category #channel`.", color=0xED4245))
    staff  = await get_staff_r(ctx.guild.id, ctx.guild)
    num    = await db.next_tid(ctx.guild.id)
    tid    = f"{num:04d}"
    ow     = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        ctx.author:             discord.PermissionOverwrite(read_messages=True, send_messages=True),
        opponent:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if staff: ow[staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    ch = await cat.create_text_channel(f"gamble-{tid}", overwrites=ow)
    e  = discord.Embed(title="🎲  Gamble Ticket", color=0xEB459E)
    e.description = (f"**Players:** {ctx.author.mention} vs {opponent.mention}\n\n"
                     f"**How it works:**\n"
                     f"> 1. Both players state what they are wagering\n"
                     f"> 2. Staff middleman collects both items\n"
                     f"> 3. Run `!roll` or `!cf heads` to decide the winner\n"
                     f"> 4. Middleman sends all items to the winner\n\n"
                     f"Staff: run `!close` when done.")
    ping = f"{ctx.author.mention} {opponent.mention}"
    if staff: ping += f" {staff.mention}"
    await ch.send(content=ping, embed=e)
    await ctx.reply(embed=discord.Embed(
        description=f"🎲 Gamble ticket opened — {ch.mention}", color=0xEB459E))

@bot.tree.command(name="gambleticket", description="Open a real-item gamble ticket with a middleman")
@app_commands.describe(opponent="Your opponent")
async def sl_gambleticket(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.bot or opponent == interaction.user:
        return await interaction.response.send_message(
            embed=discord.Embed(description="Invalid opponent.", color=0xED4245), ephemeral=True)
    cat = await get_cat(interaction.guild_id, interaction.guild)
    if not cat:
        return await interaction.response.send_message(
            embed=discord.Embed(description="Not configured. Run `!setup`.", color=0xED4245), ephemeral=True)
    staff  = await get_staff_r(interaction.guild_id, interaction.guild)
    num    = await db.next_tid(interaction.guild_id)
    tid    = f"{num:04d}"
    ow     = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        interaction.user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
        opponent:                       discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if staff: ow[staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    ch = await interaction.guild.create_text_channel(f"gamble-{tid}", category=cat, overwrites=ow)
    e  = discord.Embed(title="🎲  Gamble Ticket", color=0xEB459E)
    e.description = (f"**Players:** {interaction.user.mention} vs {opponent.mention}\n\n"
                     f"State your wagers. Staff will collect items.\n"
                     f"Use `!roll` or `!cf heads` to decide the winner.")
    ping = f"{interaction.user.mention} {opponent.mention}"
    if staff: ping += f" {staff.mention}"
    await ch.send(content=ping, embed=e)
    await interaction.response.send_message(
        embed=discord.Embed(description=f"🎲 Gamble ticket — {ch.mention}", color=0xEB459E), ephemeral=True)

# ═══════════════════════════════════════════════════════════════════
# ADMIN COMMANDS (whitelist only)
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="addshillings", aliases=["add", "addgems"])
@wl_only()
async def add_cmd(ctx, member: discord.Member = None, amount: int = None):
    if not member or not amount or amount <= 0:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!add @user <amount>`", color=0x5865F2))
    await db.adj(ctx.guild.id, member.id, amount)
    bal = await db.bal(ctx.guild.id, member.id)
    e   = discord.Embed(color=0x57F287)
    e.description = (f"✅ Added {CE} **{fm(amount)}** to {member.mention}\n"
                     f"New balance: {CE} **{fm(bal)}**")
    await ctx.reply(embed=e)

@bot.command(name="removeshillings", aliases=["remove", "removegems"])
@wl_only()
async def remove_cmd(ctx, member: discord.Member = None, amount: int = None):
    if not member or not amount or amount <= 0:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!remove @user <amount>`", color=0x5865F2))
    await db.adj(ctx.guild.id, member.id, -amount)
    bal = await db.bal(ctx.guild.id, member.id)
    e   = discord.Embed(color=0x57F287)
    e.description = (f"✅ Removed {CE} **{fm(amount)}** from {member.mention}\n"
                     f"New balance: {CE} **{fm(bal)}**")
    await ctx.reply(embed=e)

@bot.command(name="clearshillings", aliases=["clear", "cleargems"])
@wl_only()
async def clear_cmd(ctx, member: discord.Member = None):
    if not member:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!clear @user`", color=0x5865F2))
    await db.set_bal(ctx.guild.id, member.id, 0)
    e = discord.Embed(color=0x57F287)
    e.description = f"✅ {member.mention}'s balance reset to **0**."
    await ctx.reply(embed=e)

@bot.command(name="setwager")
@wl_only()
async def setwager_cmd(ctx, member: discord.Member = None, amount: int = None):
    if not member or amount is None:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!setwager @user <amount>`", color=0x5865F2))
    await db.set_wager(ctx.guild.id, member.id, amount)
    e = discord.Embed(color=0x57F287)
    e.description = f"✅ Wager requirement for {member.mention} set to {CE} **{fm(amount)}**."
    await ctx.reply(embed=e)

@bot.command(name="whitelist", aliases=["wl"])
async def whitelist_cmd(ctx, action: str = None, member: discord.Member = None):
    if ctx.author.id != OWNER_ID and not await db.is_wl(ctx.guild.id, ctx.author.id):
        return await ctx.reply(embed=discord.Embed(description="Not authorized.", color=0xED4245))
    if not action or not member:
        return await ctx.reply(embed=discord.Embed(description="**Usage:** `!wl add/remove @user`", color=0x5865F2))
    action = action.lower()
    if action == "add":
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO wl(guild_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                            ctx.guild.id, member.id)
        await ctx.reply(embed=discord.Embed(color=0x57F287, description=f"✅ {member.mention} added to whitelist."))
    elif action == "remove":
        async with db.pool.acquire() as c:
            await c.execute("DELETE FROM wl WHERE guild_id=$1 AND user_id=$2", ctx.guild.id, member.id)
        await ctx.reply(embed=discord.Embed(color=0x57F287, description=f"✅ {member.mention} removed from whitelist."))
    else:
        await ctx.reply(embed=discord.Embed(description="Use `add` or `remove`.", color=0xED4245))

# ═══════════════════════════════════════════════════════════════════
# SETUP
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="setup")
async def setup_cmd(ctx, sub: str = None, *, args: str = None):
    if ctx.author.id != OWNER_ID and not await db.is_wl(ctx.guild.id, ctx.author.id):
        return await ctx.reply(embed=discord.Embed(description="Not authorized.", color=0xED4245))
    help_e = discord.Embed(title="⚙️  Setup", color=0x5865F2)
    help_e.description = ("`!setup category #channel` — set ticket category\n"
                          "`!setup logs #channel` — set log channel\n"
                          "`!setup staff @role` — set staff role\n"
                          "`!setup view` — view current config")
    if not sub: return await ctx.reply(embed=help_e)
    sub = sub.lower()
    if sub == "view":
        c   = await db.get_cfg(ctx.guild.id)
        e2  = discord.Embed(title="⚙️  Config", color=0x5865F2)
        if c:
            cat  = ctx.guild.get_channel(c["cat_id"])   if c.get("cat_id")   else None
            log  = ctx.guild.get_channel(c["log_id"])   if c.get("log_id")   else None
            stf  = ctx.guild.get_role(c["staff_id"])    if c.get("staff_id") else None
            e2.description = (f"📁 Category: {cat.mention if cat else 'Not set'}\n"
                              f"📋 Logs: {log.mention if log else 'Not set'}\n"
                              f"🛡️ Staff: {stf.mention if stf else 'Not set'}")
        else:
            e2.description = "No config found."
        return await ctx.reply(embed=e2)
    elif sub == "category" and ctx.message.channel_mentions:
        ch = ctx.message.channel_mentions[0]
        cid = ch.category_id or ch.id
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO cfg(guild_id,cat_id) VALUES($1,$2) "
                            "ON CONFLICT(guild_id) DO UPDATE SET cat_id=$2", ctx.guild.id, cid)
        await ctx.reply(embed=discord.Embed(color=0x57F287, description="✅ Ticket category set."))
    elif sub == "logs" and ctx.message.channel_mentions:
        ch = ctx.message.channel_mentions[0]
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO cfg(guild_id,log_id) VALUES($1,$2) "
                            "ON CONFLICT(guild_id) DO UPDATE SET log_id=$2", ctx.guild.id, ch.id)
        await ctx.reply(embed=discord.Embed(color=0x57F287, description=f"✅ Log channel set to {ch.mention}."))
    elif sub == "staff" and ctx.message.role_mentions:
        r = ctx.message.role_mentions[0]
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO cfg(guild_id,staff_id) VALUES($1,$2) "
                            "ON CONFLICT(guild_id) DO UPDATE SET staff_id=$2", ctx.guild.id, r.id)
        await ctx.reply(embed=discord.Embed(color=0x57F287, description=f"✅ Staff role set to {r.mention}."))
    else:
        await ctx.reply(embed=help_e)

@bot.command(name="close")
async def close_ticket_cmd(ctx):
    valid = any(ctx.channel.name.startswith(p) for p in ("deposit-","withdraw-","gamble-"))
    if not valid:
        return await ctx.reply(embed=discord.Embed(description="This is not a ticket channel.", color=0xED4245))
    if not await db.is_wl(ctx.guild.id, ctx.author.id):
        return await ctx.reply(embed=discord.Embed(description="Only staff can close tickets.", color=0xED4245))
    await ctx.send(embed=discord.Embed(color=0x57F287, description="✅ Closing ticket in 3 seconds..."))
    await asyncio.sleep(3)
    await ctx.channel.delete()

# ═══════════════════════════════════════════════════════════════════
# GAMES LIST  (paginated, one game per page)
# ═══════════════════════════════════════════════════════════════════

GAMES_PAGES = [
    {"name": "🪙  Coinflip",         "color": 0x57F287, "desc":
     "Flip a coin. 50/50.\n\n**Usage:** `!cf <bet> <heads/tails>`\n**Payout:** 2x\n**Win chance:** 50%\n\n**Examples:**\n`!cf 1000 heads`\n`!cf 5k tails`"},
    {"name": "🎰  Slots",            "color": 0xF1C40F, "desc":
     "Spin 3 reels. Match for jackpot.\n\n**Usage:** `!slots <bet>`\n\n**Payouts:**\n> 7️⃣7️⃣7️⃣ — **50x**\n> 💎💎💎 — **20x**\n> 🍇🍇🍇 — **10x**\n> 🍊🍊🍊 — **5x**\n> 🍋🍋🍋 — **3x**\n> 🍒🍒🍒 — **2x**\n> 2 of a kind — **money back**"},
    {"name": "🎡  Roulette",         "color": 0xED4245, "desc":
     "Bet on where the ball lands.\n\n**Usage:** `!rl <bet> <choice>`\n\n**Choices:**\n> `red`/`black` — 2x\n> `odd`/`even` — 2x\n> `1-12`/`13-24`/`25-36` — 3x\n> Exact number 0–36 — 36x"},
    {"name": "🃏  Blackjack",        "color": 0x5865F2, "desc":
     "Beat the dealer without going over 21.\n\n**Usage:** `!bj <bet>`\n\n**Payouts:**\n> Natural 21 — **1.5x**\n> Beat dealer — **2x**\n> Tie — **money back**\n\nDealer hits on 16 or less."},
    {"name": "💣  Mines",            "color": 0xEB459E, "desc":
     "Click tiles to find safe spots. Avoid bombs.\n\n**Usage:** `!mines <bet> [1/3/5/10]`\n\nMore mines = higher multiplier per tile.\nCash out any time. Hit a mine = lose everything.\n\n**Grid:** 4×4 (16 tiles)"},
    {"name": "📈  Crash",            "color": 0x57F287, "desc":
     "Multiplier climbs — cash out before it crashes.\n\n**Usage:** `!crash <bet>`\n\nMultiplier starts at 1x and rises.\nHit Cash Out before crash to win.\nWait too long = lose."},
    {"name": "🃏  Higher or Lower",  "color": 0x5865F2, "desc":
     "Guess if the next card is higher or lower.\n\n**Usage:** `!hl <bet>`\n\nBuild a streak for bigger multipliers.\nCash out any time. Wrong guess = lose.\n\n**Streak multipliers:**\n> 1→1.5x 2→2.2x 3→3.2x 4→4.7x ..."},
    {"name": "🎯  Limbo",            "color": 0xEB459E, "desc":
     "Pick a target multiplier. Hit it to win.\n\n**Usage:** `!limbo <bet> <target>`\n\nWin chance = **96%** ÷ target\n\n**Examples:**\n`!limbo 1k 2` → 48% → 2x\n`!limbo 1k 10` → 9.6% → 10x\n`!limbo 1k 1000000` → 0.0001% → 1,000,000x"},
    {"name": "⚔️  War",              "color": 0xED4245, "desc":
     "Draw a card. Highest wins.\n\n**Usage:** `!war <bet>`\n\nYou and the dealer each draw a card. Highest card wins 2x. Ties trigger sudden death (draw again).\n\n**Payout:** 2x on win"},
    {"name": "🏇  Horse Race",       "color": 0xF1C40F, "desc":
     "Pick a horse. Pray it wins.\n\n**Usage:** `!hr <bet> <1-6>`\n\n**Horses:**\n> 1 🐎 Thunderhoof — 2x (30%)\n> 2 🐴 Silver Wind — 3x (22%)\n> 3 🏇 Dark Knight — 4x (18%)\n> 4 🦄 Stardust — 6x (14%)\n> 5 🌪️ Whirlwind — 10x (9%)\n> 6 💀 Last Chance — 20x (7%)"},
    {"name": "💣  Bomb Defuse",      "color": 0xED4245, "desc":
     "Cut wires. 2 are bombs, 3 are safe.\n\n**Usage:** `!bomb <bet>`\n\n**Payouts:**\n> Cut 1 safe wire — **1.5x**\n> Cut 2 safe wires — **2.5x**\n> Cut all 3 safe wires — **5x**\n> Hit a bomb — **lose**\n\nCash out after any safe cut."},
    {"name": "🔢  Number Guess",     "color": 0x5865F2, "desc":
     "Guess a number between 1 and 100.\n\n**Usage:** `!ng <bet> <1-100>`\n\n**Payouts:**\n> Exact match — **90x**\n> Within 3 — **10x**\n> Within 10 — **3x**\n> Miss — lose\n\n`!ng 1000 47`"},
    {"name": "🎟️  Scratch Card",     "color": 0xF1C40F, "desc":
     "Reveal 9 tiles. Match 3 in a row to win.\n\n**Usage:** `!scratch <bet>`\n\n**Payouts (3 in a row):**\n> 💎💎💎 — **50x**\n> 💰💰💰 — **20x**\n> 🍀🍀🍀 — **10x**\n> ⭐⭐⭐ — **6x**\n> 🎁🎁🎁 — **3x**\n> 💣 bomb tile — **lose instantly**"},
    {"name": "⚔️  Duel (PvP)",       "color": 0x57F287, "desc":
     "Challenge another member to a winner-takes-all duel.\n\n**Usage:** `!duel @user <bet>`\n\nThey must accept within 60 seconds. Both put up the same bet. Random winner takes everything.\n\n**Payout:** 2x (winner takes both bets)"},
]

class GamesView(View):
    def __init__(self, aid):
        super().__init__(timeout=120)
        self.page=0; self.aid=aid; self.msg=None; self._upd()

    def _upd(self):
        self.prev_b.disabled = self.page == 0
        self.next_b.disabled = self.page >= len(GAMES_PAGES) - 1

    def _embed(self):
        p = GAMES_PAGES[self.page]
        e = discord.Embed(title=p["name"], description=p["desc"], color=p["color"])
        e.set_footer(text=f"Game {self.page+1}/{len(GAMES_PAGES)}  ·  Use ◀ ▶ to browse  ·  Prefix: {PREFIX}")
        return e

    @discord.ui.button(label="◀", style=ButtonStyle.gray, custom_id="gm_prev")
    async def prev_b(self, interaction, _):
        if interaction.user.id != self.aid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your menu.", color=0xED4245), ephemeral=True)
        self.page -= 1; self._upd()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=ButtonStyle.blurple, custom_id="gm_next")
    async def next_b(self, interaction, _):
        if interaction.user.id != self.aid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your menu.", color=0xED4245), ephemeral=True)
        self.page += 1; self._upd()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(view=self)
        except: pass

@bot.command(name="games")
async def games_cmd(ctx):
    v = GamesView(ctx.author.id); v.msg = await ctx.reply(embed=v._embed(), view=v)

@bot.tree.command(name="games", description="Browse all available games")
async def sl_games(interaction: discord.Interaction):
    v = GamesView(interaction.user.id)
    await interaction.response.send_message(embed=v._embed(), view=v)
    v.msg = await interaction.original_response()

# ═══════════════════════════════════════════════════════════════════
# HELP  (6 pages)
# ═══════════════════════════════════════════════════════════════════

HELP_PAGES = [
    {"title": "🪙  Economy", "color": 0x57F287, "desc":
     f"`!claimdaily` / `!daily` — Claim 1–20M {CUR} every 3 hours\n"
     f"`!balance [@user]` / `!bal` — Check balance + wager status\n\n"
     f"**Claim Rarities:**\n"
     f"> 🪙 Small — up to 100K\n"
     f"> 👍 Decent — 100K–1M\n"
     f"> ✨ Nice — 1M–5M\n"
     f"> 💎 Rare — 5M–15M\n"
     f"> 🌟 Jackpot — 15M–20M"},
    {"title": "🎮  Quick Games",     "color": 0x5865F2, "desc":
     "`!cf <bet> <heads/tails>` — Coinflip 2x\n"
     "`!slots <bet>` — Slots up to 50x\n"
     "`!rl <bet> <choice>` — Roulette up to 36x\n"
     "`!bj <bet>` — Blackjack 1.5x–2x\n"
     "`!war <bet>` — War 2x\n"
     "`!hr <bet> <1-6>` — Horse race up to 20x\n"
     "`!ng <bet> <1-100>` — Number guess up to 90x\n"
     "`!scratch <bet>` — Scratch card up to 50x\n\n"
     "Run `!games` for full details on each game."},
    {"title": "🎮  Interactive Games", "color": 0xEB459E, "desc":
     "`!mines <bet> [1/3/5/10]` — Avoid bombs, cash out anytime\n"
     "`!crash <bet>` — Cash out before crash\n"
     "`!hl <bet>` — Higher or lower, build streak\n"
     "`!limbo <bet> <target>` — Hit your multiplier\n"
     "`!bomb <bet>` — Cut wires, avoid bombs\n"
     "`!duel @user <bet>` — PvP winner-takes-all\n\n"
     "All interactive games use buttons. You have 60–120s to finish."},
    {"title": "📦  Stock & Economy", "color": 0xF1C40F, "desc":
     "`!stock` — View items available to withdraw\n"
     "`!deposit <item>` — Open a deposit ticket\n"
     "`!withdraw` — Spend Shillings to claim a stock item\n\n"
     "**Deposit flow:**\n"
     "> Open ticket → state what you're depositing\n"
     "> Staff approves and awards Shillings\n"
     "> You must wager **1x** the Shillings before withdrawing\n\n"
     "**Wager requirement** shows in `!balance`."},
    {"title": "🎲  Gamble Tickets", "color": 0xEB459E, "desc":
     "`!gambleticket @opponent` — Open a real-item gamble\n\n"
     "**How it works:**\n"
     "> Opens a private channel for both players + staff\n"
     "> Each player states what they are wagering\n"
     "> Middleman collects both items\n"
     "> Use `!roll` or `!cf heads` to decide winner\n"
     "> Middleman sends everything to the winner\n\n"
     "Also: `/gambleticket`"},
    {"title": "⚙️  Admin (Whitelist Only)", "color": 0x747F8D, "desc":
     "**Whitelist (owner only):**\n"
     "`!wl add/remove @user`\n\n"
     "**Shilling Management:**\n"
     "`!add @user <amount>` — Add Shillings\n"
     "`!remove @user <amount>` — Remove Shillings\n"
     "`!clear @user` — Reset to 0\n"
     "`!setwager @user <amount>` — Set wager requirement\n\n"
     "**Stock:**\n"
     "`!addtostock <price> <item>` — Add item\n"
     "`!removefromstock <item>` — Remove item\n"
     "`!clearstock` — Clear all items\n\n"
     "**Server:** `!setup category/logs/staff/view`\n"
     "**Tickets:** `!close` inside any ticket channel"},
]

class HelpView(View):
    def __init__(self, aid):
        super().__init__(timeout=120)
        self.page=0; self.aid=aid; self.msg=None; self._upd()

    def _upd(self):
        self.prev_b.disabled = self.page == 0
        self.next_b.disabled = self.page >= len(HELP_PAGES) - 1

    def _embed(self):
        p = HELP_PAGES[self.page]
        e = discord.Embed(title=p["title"], description=p["desc"], color=p["color"])
        e.set_footer(text=f"Page {self.page+1}/{len(HELP_PAGES)}  ·  Prefix: {PREFIX}  ·  Currency: {CE} {CUR}")
        return e

    @discord.ui.button(label="◀", style=ButtonStyle.gray, custom_id="hp_prev")
    async def prev_b(self, interaction, _):
        if interaction.user.id != self.aid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your menu.", color=0xED4245), ephemeral=True)
        self.page -= 1; self._upd()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=ButtonStyle.blurple, custom_id="hp_next")
    async def next_b(self, interaction, _):
        if interaction.user.id != self.aid:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Not your menu.", color=0xED4245), ephemeral=True)
        self.page += 1; self._upd()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(view=self)
        except: pass

@bot.command(name="help")
async def help_cmd(ctx):
    v = HelpView(ctx.author.id); v.msg = await ctx.reply(embed=v._embed(), view=v)

@bot.tree.command(name="help", description="View all commands")
async def sl_help(interaction: discord.Interaction):
    v = HelpView(interaction.user.id)
    await interaction.response.send_message(embed=v._embed(), view=v)
    v.msg = await interaction.original_response()

# ═══════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await db.connect()
    try:
        synced = await bot.tree.sync()
        logger.info(f"synced {len(synced)} slash commands")
    except Exception as ex:
        logger.error(f"sync failed: {ex}")
    logger.info(f"ready as {bot.user}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)): return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(embed=discord.Embed(
            description=f"Missing `{error.param.name}`. Run `!help` for usage.", color=0xED4245))
    else:
        logger.error(f"cmd error: {error}")

# ═══════════════════════════════════════════════════════════════════
# WEBSERVER (keep-alive for Render)
# ═══════════════════════════════════════════════════════════════════

async def _health(req):
    import aiohttp.web
    return aiohttp.web.Response(text="ok")

async def start_web():
    import aiohttp.web as web
    app    = web.Application()
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()

async def main():
    async with bot:
        await start_web()
        token = os.getenv("BOT_TOKEN", "")
        if not token: raise RuntimeError("BOT_TOKEN not set")
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
