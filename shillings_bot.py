"""
Shillings Bot — Slash Commands Only
No stock/withdraw system · Gamble tickets support adding users · Render/PostgreSQL/UptimeRobot ready
"""

import os, asyncio, logging, secrets, random
from datetime import datetime, timezone
from typing import Optional
import asyncpg, discord
from discord import ButtonStyle, app_commands
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CUR      = "Shillings"
CE       = "\U0001fa99"
CLAIM_CD = 3 * 3600
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

C_GREEN  = 0x2ECC71
C_RED    = 0xE74C3C
C_GOLD   = 0xF1C40F
C_BLUE   = 0x5865F2
C_PINK   = 0xEB459E
C_GREY   = 0x95A5A6

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

class DB:
    pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if self.pool is not None:
            return  # already connected — don't create a second pool on reconnect
        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL is not set!")
        if url.startswith("postgres://"):
            url = "postgresql://" + url[11:]
        self.pool = await asyncpg.create_pool(url, min_size=1, max_size=5, command_timeout=15)
        await self._setup()
        log.info("DB connected")

    async def _setup(self):
        async with self.pool.acquire() as c:
            for sql in [
                """CREATE TABLE IF NOT EXISTS eco(
                    guild_id BIGINT, user_id BIGINT,
                    balance BIGINT DEFAULT 0,
                    last_claim TIMESTAMPTZ,
                    wager_req BIGINT DEFAULT 0,
                    PRIMARY KEY(guild_id,user_id))""",
                """CREATE TABLE IF NOT EXISTS whitelist(
                    guild_id BIGINT, user_id BIGINT,
                    PRIMARY KEY(guild_id,user_id))""",
                """CREATE TABLE IF NOT EXISTS cfg(
                    guild_id BIGINT PRIMARY KEY,
                    cat_id BIGINT, log_id BIGINT, staff_id BIGINT)""",
                """CREATE TABLE IF NOT EXISTS ticket_ctr(
                    guild_id BIGINT PRIMARY KEY, n INT DEFAULT 0)""",
            ]:
                try: await c.execute(sql)
                except Exception as e: log.warning(f"Schema warning: {e}")

    def _guard(self):
        if self.pool is None:
            raise RuntimeError("DB_NOT_READY")

    async def bal(self, gid, uid):
        self._guard()
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT balance FROM eco WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r["balance"] if r else 0

    async def adj(self, gid, uid, amt):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,balance) VALUES($1,$2,GREATEST(0,$3)) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=GREATEST(0,eco.balance+$3)",
                gid, uid, amt)

    async def set_bal(self, gid, uid, amt):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,balance) VALUES($1,$2,GREATEST(0,$3)) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=GREATEST(0,$3)",
                gid, uid, amt)

    async def wager_req(self, gid, uid):
        self._guard()
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT wager_req FROM eco WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r["wager_req"] if r else 0

    async def reduce_wager(self, gid, uid, amt):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,wager_req) VALUES($1,$2,0) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wager_req=GREATEST(0,eco.wager_req-$3)",
                gid, uid, amt)

    async def add_wager(self, gid, uid, amt):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,wager_req) VALUES($1,$2,$3) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wager_req=eco.wager_req+$3",
                gid, uid, amt)

    async def set_wager(self, gid, uid, amt):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,wager_req) VALUES($1,$2,GREATEST(0,$3)) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET wager_req=GREATEST(0,$3)",
                gid, uid, amt)

    async def last_claim(self, gid, uid):
        self._guard()
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT last_claim FROM eco WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r["last_claim"] if r else None

    async def touch_claim(self, gid, uid):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO eco(guild_id,user_id,last_claim) VALUES($1,$2,NOW()) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET last_claim=NOW()",
                gid, uid)

    async def is_wl(self, gid, uid):
        if uid == OWNER_ID: return True
        self._guard()
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT 1 FROM whitelist WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return r is not None

    async def get_cfg(self, gid):
        self._guard()
        async with self.pool.acquire() as c:
            return await c.fetchrow("SELECT * FROM cfg WHERE guild_id=$1", gid)

    async def next_tid(self, gid):
        self._guard()
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO ticket_ctr(guild_id,n) VALUES($1,1) "
                "ON CONFLICT(guild_id) DO UPDATE SET n=ticket_ctr.n+1", gid)
            r = await c.fetchrow("SELECT n FROM ticket_ctr WHERE guild_id=$1", gid)
        return r["n"]

db = DB()

# ══════════════════════════════════════════════════════════════════════════════
# BOT SETUP
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="\xA7", intents=intents, help_command=None)

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fm(n): return f"{int(n):,}"

def fmt_cd(s):
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if sec or not parts: parts.append(f"{sec}s")
    return " ".join(parts)

def fair_flip():        return "heads" if secrets.randbelow(2) == 0 else "tails"
def fair_int(lo, hi):  return lo + secrets.randbelow(hi - lo + 1)
def fair_choice(seq):  return seq[secrets.randbelow(len(seq))]
def fair_shuffle(seq):
    a = list(seq)
    for i in range(len(a) - 1, 0, -1):
        j = secrets.randbelow(i + 1); a[i], a[j] = a[j], a[i]
    return a

def gen_mult():
    r = secrets.randbelow(10_000_000) / 10_000_000.0
    return 1.00 if r < 0.04 else round(0.96 / r, 2)

async def parse_bet(ix, arg):
    bl = await db.bal(ix.guild_id, ix.user.id)
    a  = arg.lower().strip()
    if a in ("all", "max"): return bl or None
    for s, m in (("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if a.endswith(s):
            try: return int(float(a[:-1]) * m)
            except: return None
    try: return int(a)
    except: return None

async def chk_bet(ix, bet):
    async def err(msg):
        e = discord.Embed(description=f"\u274c {msg}", color=C_RED)
        try:    await ix.followup.send(embed=e, ephemeral=True)
        except: await ix.response.send_message(embed=e, ephemeral=True)
    if not bet or bet <= 0:
        await err("Enter a valid bet — e.g. `1000`, `5k`, `2m`, `all`"); return False
    bl = await db.bal(ix.guild_id, ix.user.id)
    if bet > bl:
        await err(f"You only have {CE} **{fm(bl)} {CUR}**."); return False
    return True

def e_win(title, desc, won, bal):
    e = discord.Embed(color=C_GREEN, title=f"\u2705  {title}")
    e.description = f"{desc}\n\n> {CE} **+{fm(won)}** won"
    e.set_footer(text=f"Balance: {CE} {fm(bal)} {CUR}")
    return e

def e_lose(title, desc, lost, bal):
    e = discord.Embed(color=C_RED, title=f"\u274c  {title}")
    e.description = f"{desc}\n\n> {CE} **\u2212{fm(lost)}** lost"
    e.set_footer(text=f"Balance: {CE} {fm(bal)} {CUR}")
    return e

def e_tie(title, desc, bal):
    e = discord.Embed(color=C_GOLD, title=f"\U0001f91d  {title}")
    e.description = f"{desc}\n\n> Bet returned"
    e.set_footer(text=f"Balance: {CE} {fm(bal)} {CUR}")
    return e

async def get_cat(gid, guild):
    c = await db.get_cfg(gid)
    return guild.get_channel(c["cat_id"]) if c and c.get("cat_id") else None

async def get_staff_role(gid, guild):
    c = await db.get_cfg(gid)
    return guild.get_role(c["staff_id"]) if c and c.get("staff_id") else None

async def get_log_ch(gid, guild):
    c = await db.get_cfg(gid)
    return guild.get_channel(c["log_id"]) if c and c.get("log_id") else None

def roll_claim():
    r = secrets.randbelow(100)
    if r < 40: return random.randint(1,         100_000)
    if r < 70: return random.randint(100_001,   1_000_000)
    if r < 90: return random.randint(1_000_001, 5_000_000)
    if r < 98: return random.randint(5_000_001, 15_000_000)
    return random.randint(15_000_001, 20_000_000)

def rarity_label(n):
    if n >= 15_000_000: return "\U0001f31f **JACKPOT!**"
    if n >=  5_000_000: return "\U0001f48e **Rare**"
    if n >=  1_000_000: return "\u2728 **Nice**"
    if n >=    100_000: return "\U0001f44d **Decent**"
    return f"{CE} **Small**"

def wl_check():
    async def pred(ix):
        if db.pool is None:
            await ix.response.send_message(
                embed=discord.Embed(description="\u26a0\ufe0f Bot is still starting up, try again in a moment.", color=C_GOLD),
                ephemeral=True)
            return False
        ok = await db.is_wl(ix.guild_id, ix.user.id)
        if not ok:
            await ix.response.send_message(
                embed=discord.Embed(description="\u274c Not authorized.", color=C_RED), ephemeral=True)
        return ok
    return app_commands.check(pred)

# ══════════════════════════════════════════════════════════════════════════════
# ECONOMY
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="daily", description="Claim 1\u201320M Shillings every 3 hours")
async def cmd_daily(ix: discord.Interaction):
    await ix.response.defer()
    gid, uid = ix.guild_id, ix.user.id
    last = await db.last_claim(gid, uid)
    now  = datetime.now(timezone.utc)
    if last:
        t   = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        rem = CLAIM_CD - (now - t).total_seconds()
        if rem > 0:
            e = discord.Embed(title="\u23f3  On Cooldown",
                              description=f"Next claim in **{fmt_cd(rem)}**", color=C_GOLD)
            return await ix.followup.send(embed=e)
    amt = roll_claim()
    await db.adj(gid, uid, amt); await db.touch_claim(gid, uid)
    bal = await db.bal(gid, uid)
    e = discord.Embed(title=f"{CE}  Daily Claim", color=C_GREEN)
    e.description = f"{rarity_label(amt)}\n\nYou claimed {CE} **{fm(amt)} {CUR}**!"
    e.set_thumbnail(url=ix.user.display_avatar.url)
    e.set_footer(text=f"Balance: {CE} {fm(bal)}  \u00b7  Next in 3h")
    await ix.followup.send(embed=e)


@bot.tree.command(name="balance", description="Check your or someone else's balance")
@app_commands.describe(member="User to check (leave empty for yourself)")
async def cmd_balance(ix: discord.Interaction, member: discord.Member = None):
    await ix.response.defer()
    m   = member or ix.user
    bal = await db.bal(ix.guild_id, m.id)
    wr  = await db.wager_req(ix.guild_id, m.id)
    e   = discord.Embed(color=C_BLUE)
    e.set_author(name=m.display_name, icon_url=m.display_avatar.url)
    e.set_thumbnail(url=m.display_avatar.url)
    e.add_field(name="\U0001f4b0 Balance", value=f"{CE} **{fm(bal)} {CUR}**", inline=False)
    if wr > 0:
        pct = max(0, min(100, int((1 - wr / max(bal, 1)) * 100)))
        bar = "\u2588" * (pct // 10) + "\u2591" * (10 - pct // 10)
        e.add_field(name="\u26a0\ufe0f Wager Requirement",
                    value=f"Must wager {CE} **{fm(wr)}** more\n`{bar}` {pct}%", inline=False)
    else:
        e.add_field(name="\u2705 Status", value="No wager requirement!", inline=False)
    await ix.followup.send(embed=e)

# ══════════════════════════════════════════════════════════════════════════════
# COINFLIP
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="coinflip", description="Flip a coin \u2014 2x payout on correct call")
@app_commands.describe(bet="Amount to bet", side="heads or tails")
@app_commands.choices(side=[
    app_commands.Choice(name="Heads", value="heads"),
    app_commands.Choice(name="Tails", value="tails"),
])
async def cmd_cf(ix: discord.Interaction, bet: str, side: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    result = fair_flip()
    coin   = "\U0001fa99 Heads" if result == "heads" else "\U0001f7eb Tails"
    gid, uid = ix.guild_id, ix.user.id
    await db.reduce_wager(gid, uid, amt)
    if result == side:
        await db.adj(gid, uid, amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("Coinflip", f"{coin}\nYou called **{side}** \u2014 nailed it!", amt, bal))
    else:
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("Coinflip", f"{coin}\nYou called **{side}** \u2014 wrong call.", amt, bal))

# ══════════════════════════════════════════════════════════════════════════════
# SLOTS
# ══════════════════════════════════════════════════════════════════════════════

SLOT_SYM = ["\U0001f352", "\U0001f34b", "\U0001f34a", "\U0001f347", "\U0001f48e", "7\ufe0f\u20e3"]
SLOT_W   = [30, 25, 20, 15, 7, 3]
SLOT_PAY = {"7\ufe0f\u20e3": 50, "\U0001f48e": 20, "\U0001f347": 10,
            "\U0001f34a": 5, "\U0001f34b": 3, "\U0001f352": 2}

@bot.tree.command(name="slots", description="Spin the slots \u2014 up to 50x payout")
@app_commands.describe(bet="Amount to bet")
async def cmd_slots(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    reels = [random.choices(SLOT_SYM, weights=SLOT_W, k=1)[0] for _ in range(3)]
    row   = " \u2502 ".join(reels)
    gid, uid = ix.guild_id, ix.user.id
    await db.reduce_wager(gid, uid, amt)
    if reels[0] == reels[1] == reels[2]:
        mult = SLOT_PAY[reels[0]]; win = amt * mult - amt
        await db.adj(gid, uid, win); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("Slots \U0001f3b0", f"\u27e3 {row} \u27e2\n**JACKPOT! {mult}x**", win, bal))
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_tie("Slots \U0001f3b0", f"\u27e3 {row} \u27e2\n2 of a kind \u2014 bet returned", bal))
    else:
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("Slots \U0001f3b0", f"\u27e3 {row} \u27e2\nNo match.", amt, bal))

# ══════════════════════════════════════════════════════════════════════════════
# ROULETTE
# ══════════════════════════════════════════════════════════════════════════════

RL_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

@bot.tree.command(name="roulette", description="Bet on where the ball lands \u2014 up to 36x")
@app_commands.describe(bet="Amount to bet", choice="red/black/odd/even/1-12/13-24/25-36 or a number 0\u201336")
async def cmd_roulette(ix: discord.Interaction, bet: str, choice: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    spin = fair_int(0, 36)
    c    = choice.lower().strip()
    clr  = "\U0001f7e5" if spin in RL_RED else ("\u2b1b" if spin > 0 else "\U0001f7e9")
    gid, uid = ix.guild_id, ix.user.id
    won, mult = False, 0
    if   c == "red":              won, mult = spin in RL_RED, 2
    elif c == "black":            won, mult = (spin not in RL_RED and spin > 0), 2
    elif c == "odd":              won, mult = (spin > 0 and spin % 2 == 1), 2
    elif c == "even":             won, mult = (spin > 0 and spin % 2 == 0), 2
    elif c in ("1-12","first"):   won, mult = (1 <= spin <= 12), 3
    elif c in ("13-24","second"): won, mult = (13 <= spin <= 24), 3
    elif c in ("25-36","third"):  won, mult = (25 <= spin <= 36), 3
    else:
        try:
            n = int(c)
            if 0 <= n <= 36: won, mult = spin == n, 36
            else: return await ix.followup.send(
                embed=discord.Embed(description="\u274c Number must be 0\u201336.", color=C_RED), ephemeral=True)
        except ValueError:
            return await ix.followup.send(
                embed=discord.Embed(description="\u274c Invalid choice. Use red/black/odd/even/1-12/13-24/25-36 or 0\u201336.", color=C_RED), ephemeral=True)
    detail = f"Ball landed on {clr} **{spin}** \u2014 you bet **{choice}**"
    await db.reduce_wager(gid, uid, amt)
    if won:
        win = amt * (mult - 1); await db.adj(gid, uid, win); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("Roulette \U0001f3a1", f"{detail} \u2014 **{mult}x!**", win, bal))
    else:
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("Roulette \U0001f3a1", detail, amt, bal))

# ══════════════════════════════════════════════════════════════════════════════
# BLACKJACK
# ══════════════════════════════════════════════════════════════════════════════

def mk_deck():
    d = [f"{r}{s}" for s in ["\u2660","\u2665","\u2666","\u2663"]
         for r in ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]]
    return fair_shuffle(d)

def card_val(card):
    r = card[:-1]
    if r in ("J","Q","K"): return 10
    if r == "A":           return 11
    return int(r)

def hand_val(hand):
    v = sum(card_val(c) for c in hand)
    aces = sum(1 for c in hand if c.startswith("A"))
    while v > 21 and aces: v -= 10; aces -= 1
    return v

class BjView(View):
    def __init__(self, uid, gid, deck, ph, dh, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.deck=deck
        self.ph=ph; self.dh=dh; self.bet=bet
        self.done=False; self.msg=None

    def _embed(self):
        pv = hand_val(self.ph); dv = hand_val(self.dh)
        e  = discord.Embed(color=C_BLUE, title="\U0001f0cf  Blackjack")
        e.add_field(name="Dealer", value=f"{self.dh[0]} \U0001f0a0", inline=False)
        e.add_field(name=f"You ({pv})", value=" ".join(self.ph), inline=False)
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def _finish(self, ix):
        while hand_val(self.dh) < 17: self.dh.append(self.deck.pop())
        pv = hand_val(self.ph); dv = hand_val(self.dh)
        self.done = True; self.stop()
        for i in self.children: i.disabled = True
        await db.reduce_wager(self.gid, self.uid, self.bet)
        dealer_str = f"Dealer: {' '.join(self.dh)} ({dv})"
        if pv > 21:
            await db.adj(self.gid, self.uid, -self.bet); bal = await db.bal(self.gid, self.uid)
            em = e_lose("Blackjack", f"Bust! You had {pv}.\n{dealer_str}", self.bet, bal)
        elif dv > 21 or pv > dv:
            await db.adj(self.gid, self.uid, self.bet); bal = await db.bal(self.gid, self.uid)
            em = e_win("Blackjack", f"You win! {pv} vs {dv}.\n{dealer_str}", self.bet, bal)
        elif pv == dv:
            bal = await db.bal(self.gid, self.uid)
            em = e_tie("Blackjack", f"Push \u2014 both {pv}.\n{dealer_str}", bal)
        else:
            await db.adj(self.gid, self.uid, -self.bet); bal = await db.bal(self.gid, self.uid)
            em = e_lose("Blackjack", f"Dealer wins \u2014 {dv} vs {pv}.\n{dealer_str}", self.bet, bal)
        await ix.response.edit_message(embed=em, view=self)

    @discord.ui.button(label="Hit", style=ButtonStyle.green, emoji="\U0001f44a")
    async def hit(self, ix, _):
        if ix.user.id != self.uid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
        if self.done: return await ix.response.defer()
        self.ph.append(self.deck.pop())
        if hand_val(self.ph) >= 21: await self._finish(ix)
        else: await ix.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Stand", style=ButtonStyle.red, emoji="\u270b")
    async def stand(self, ix, _):
        if ix.user.id != self.uid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
        if self.done: return await ix.response.defer()
        await self._finish(ix)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try:
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            await self.msg.edit(embed=discord.Embed(title="\U0001f0cf Blackjack",
                description="\u23f0 Timed out \u2014 bet lost.", color=C_RED), view=self)
        except: pass

@bot.tree.command(name="blackjack", description="Play blackjack against the dealer")
@app_commands.describe(bet="Amount to bet")
async def cmd_bj(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    deck = mk_deck(); ph = [deck.pop(), deck.pop()]; dh = [deck.pop(), deck.pop()]
    if hand_val(ph) == 21:
        win = int(amt * 1.5)
        await db.adj(ix.guild_id, ix.user.id, win)
        await db.reduce_wager(ix.guild_id, ix.user.id, amt)
        bal = await db.bal(ix.guild_id, ix.user.id)
        return await ix.followup.send(embed=e_win("Blackjack", f"\U0001f31f Natural 21! You: {' '.join(ph)}", win, bal))
    v = BjView(ix.user.id, ix.guild_id, deck, ph, dh, amt)
    v.msg = await ix.followup.send(embed=v._embed(), view=v)

# ══════════════════════════════════════════════════════════════════════════════
# LIMBO
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="limbo", description="Set a target multiplier and hit it to win")
@app_commands.describe(bet="Amount to bet", target="Target multiplier e.g. 2.5")
async def cmd_limbo(ix: discord.Interaction, bet: str, target: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    try: tgt = float(target)
    except:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Target must be a number.", color=C_RED), ephemeral=True)
    if tgt < 1.01 or tgt > 1_000_000:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Target must be between 1.01 and 1,000,000.", color=C_RED), ephemeral=True)
    result = gen_mult(); chance = round(96 / tgt, 2)
    gid, uid = ix.guild_id, ix.user.id
    await db.reduce_wager(gid, uid, amt)
    if result >= tgt:
        win = int(amt * tgt) - amt; await db.adj(gid, uid, win); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("Limbo \U0001f3af",
            f"Result: **{result:.2f}x** \u2265 target **{tgt:.2f}x**\nWin chance: {chance:.1f}%", win, bal))
    else:
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("Limbo \U0001f3af",
            f"Result: **{result:.2f}x** < target **{tgt:.2f}x**\nWin chance: {chance:.1f}%", amt, bal))

# ══════════════════════════════════════════════════════════════════════════════
# CRASH
# ══════════════════════════════════════════════════════════════════════════════

class CrashView(View):
    def __init__(self, uid, gid, bet, crash_at):
        super().__init__(timeout=120)
        self.uid=uid; self.gid=gid; self.bet=bet; self.crash_at=crash_at
        self.cur=1.00; self.cashed=False; self.crashed=False
        self.msg=None; self.task=None

    def _embed(self):
        if self.crashed:
            e = discord.Embed(title="\U0001f4c8  Crash", color=C_RED)
            e.description = f"\U0001f4a5 Crashed at **{self.crash_at:.2f}x**!"
        else:
            e = discord.Embed(title="\U0001f4c8  Crash", color=C_GREEN)
            e.description = f"\U0001f680 **{self.cur:.2f}x**\n\nCash out for {CE} **{fm(int(self.bet * self.cur))}**"
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    @discord.ui.button(label="Cash Out", style=ButtonStyle.green, emoji="\U0001f4b0")
    async def cashout(self, ix, _):
        if ix.user.id != self.uid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
        if self.cashed or self.crashed:
            return await ix.response.send_message(embed=discord.Embed(description="Game already over.", color=C_GOLD), ephemeral=True)
        self.cashed = True
        if self.task: self.task.cancel()
        self.stop()
        for i in self.children: i.disabled = True
        win = int(self.bet * self.cur) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        bal = await db.bal(self.gid, self.uid)
        await ix.response.edit_message(embed=e_win("Crash",
            f"Cashed out at **{self.cur:.2f}x** (crashed at {self.crash_at:.2f}x)", win, bal), view=self)

    async def on_timeout(self):
        if not self.cashed and not self.crashed:
            self.crashed = True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            for i in self.children: i.disabled = True
            try: await self.msg.edit(embed=discord.Embed(title="\U0001f4c8 Crash",
                description="\u23f0 Timed out \u2014 bet lost.", color=C_RED), view=self)
            except: pass

async def _crash_loop(view: CrashView):
    try:
        while view.cur < view.crash_at and not view.cashed and not view.crashed:
            await asyncio.sleep(1.5)
            if view.cashed or view.crashed: break
            view.cur = round(min(view.cur + random.uniform(0.08, 0.18), view.crash_at), 2)
            if view.msg:
                try: await view.msg.edit(embed=view._embed(), view=view)
                except: break
        if not view.cashed and not view.crashed:
            view.crashed = True; view.stop()
            for i in view.children: i.disabled = True
            await db.adj(view.gid, view.uid, -view.bet)
            await db.reduce_wager(view.gid, view.uid, view.bet)
            bal = await db.bal(view.gid, view.uid)
            if view.msg:
                try: await view.msg.edit(embed=e_lose("Crash",
                    f"\U0001f4a5 Crashed at **{view.crash_at:.2f}x**!", view.bet, bal), view=view)
                except: pass
    except asyncio.CancelledError: pass

@bot.tree.command(name="crash", description="Ride the multiplier and cash out before it crashes!")
@app_commands.describe(bet="Amount to bet")
async def cmd_crash(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    view = CrashView(ix.user.id, ix.guild_id, amt, gen_mult())
    view.msg  = await ix.followup.send(embed=view._embed(), view=view)
    view.task = asyncio.create_task(_crash_loop(view))

# ══════════════════════════════════════════════════════════════════════════════
# HIGHER OR LOWER
# ══════════════════════════════════════════════════════════════════════════════

HL_MULTS = [1.0,1.5,2.2,3.2,4.7,6.8,9.9,14.0,20.0,29.0,42.0,60.0,85.0]
HL_DECK  = [f"{r}{s}" for s in ["\u2660","\u2665","\u2666","\u2663"]
             for r in ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]]

class HLView(View):
    def __init__(self, uid, gid, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.bet=bet
        self.deck=fair_shuffle(HL_DECK); self.cur=self.deck.pop()
        self.streak=0; self.done=False; self.msg=None

    def _mult(self): return HL_MULTS[min(self.streak, len(HL_MULTS)-1)]

    def _embed(self, status=""):
        e = discord.Embed(title="\U0001f0cf  Higher or Lower", color=C_BLUE)
        e.add_field(name="Current Card", value=f"**{self.cur}**", inline=True)
        e.add_field(name="Streak", value=f"**{self.streak}** \u2014 {self._mult():.1f}x", inline=True)
        e.add_field(name="Potential", value=f"{CE} **{fm(int(self.bet*self._mult()))}**", inline=True)
        if status: e.description = status
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def _guess(self, ix, direction):
        if ix.user.id != self.uid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
        if self.done: return await ix.response.defer()
        if not self.deck: self.deck = fair_shuffle(HL_DECK)
        nxt = self.deck.pop(); cv = card_val(self.cur); nv = card_val(nxt); self.cur = nxt
        if cv == nv:
            return await ix.response.edit_message(embed=self._embed(f"\u2194\ufe0f Tie \u2014 **{nxt}** (no change)"), view=self)
        correct = (direction == "h" and nv > cv) or (direction == "l" and nv < cv)
        if correct:
            self.streak += 1
            await ix.response.edit_message(embed=self._embed(f"\u2705 Correct! Next was **{nxt}**"), view=self)
        else:
            self.done = True; self.stop()
            for i in self.children: i.disabled = True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            bal = await db.bal(self.gid, self.uid)
            await ix.response.edit_message(embed=e_lose("Higher or Lower",
                f"\u274c Next was **{nxt}** \u2014 streak of {self.streak}", self.bet, bal), view=self)

    @discord.ui.button(label="Higher", style=ButtonStyle.green, emoji="\u2b06\ufe0f", custom_id="hl_h")
    async def higher(self, i, _): await self._guess(i, "h")

    @discord.ui.button(label="Lower", style=ButtonStyle.red, emoji="\u2b07\ufe0f", custom_id="hl_l")
    async def lower(self, i, _): await self._guess(i, "l")

    @discord.ui.button(label="Cash Out", style=ButtonStyle.blurple, emoji="\U0001f4b0", custom_id="hl_co")
    async def cashout(self, ix, _):
        if ix.user.id != self.uid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
        if self.streak == 0:
            return await ix.response.send_message(embed=discord.Embed(description="Get at least 1 correct first!", color=C_GOLD), ephemeral=True)
        if self.done: return await ix.response.defer()
        self.done = True; self.stop()
        for i in self.children: i.disabled = True
        win = int(self.bet * self._mult()) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        bal = await db.bal(self.gid, self.uid)
        await ix.response.edit_message(embed=e_win("Higher or Lower",
            f"Cashed out at **{self._mult():.1f}x** (streak {self.streak})", win, bal), view=self)

    async def on_timeout(self):
        if not self.done:
            self.done = True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            for i in self.children: i.disabled = True
            try: await self.msg.edit(embed=discord.Embed(title="\U0001f0cf Higher or Lower",
                description="\u23f0 Timed out \u2014 bet lost.", color=C_RED), view=self)
            except: pass

@bot.tree.command(name="higherlower", description="Guess if the next card is higher or lower \u2014 build a streak!")
@app_commands.describe(bet="Amount to bet")
async def cmd_hl(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    v = HLView(ix.user.id, ix.guild_id, amt)
    v.msg = await ix.followup.send(embed=v._embed(), view=v)

# ══════════════════════════════════════════════════════════════════════════════
# MINES
# ══════════════════════════════════════════════════════════════════════════════

_MINES_PAY = {
    1:  [1.0,1.1,1.2,1.4,1.6,1.9,2.2,2.6,3.1,3.7,4.5,5.5,6.7,8.2,10.0,12.5],
    3:  [1.0,1.2,1.5,1.9,2.4,3.1,4.0,5.2,6.9,9.3,12.7,17.6,25.0,37.0,58.0,99.0],
    5:  [1.0,1.4,2.0,3.0,4.5,6.8,10.5,16.5,26.5,44.0,76.0,140.0,280.0,640.0,1700.0,6200.0],
    10: [1.0,1.7,3.1,6.0,12.5,28.0,68.0,185.0,580.0,2200.0,11000.0,80000.0,1.2e6,2e8,9e10,9e10],
}

def mines_mult(mc, safe):
    t = _MINES_PAY.get(mc, _MINES_PAY[3]); return t[min(safe, len(t)-1)]

class MinesView(View):
    def __init__(self, uid, gid, bet, mc):
        super().__init__(timeout=120)
        self.uid=uid; self.gid=gid; self.bet=bet; self.mc=mc
        self.mines = set(random.sample(list(range(16)), mc))
        self.rev=set(); self.safe_cnt=0; self.done=False; self.msg=None
        self._build()

    def _mult(self): return mines_mult(self.mc, self.safe_cnt)

    def _build(self):
        self.clear_items()
        for i in range(16):
            row = i // 4
            if i in self.rev:
                lbl = "\U0001f4a3" if i in self.mines else "\U0001f48e"
                sty = ButtonStyle.danger if i in self.mines else ButtonStyle.success
                btn = Button(label=lbl, style=sty, disabled=True, row=row)
            else:
                btn = Button(label="?", style=ButtonStyle.secondary,
                             disabled=self.done, row=row, custom_id=f"mine_{i}_{self.uid}")
            self.add_item(btn)
        if self.safe_cnt > 0 and not self.done:
            co = Button(label=f"Cash Out {self._mult():.2f}x \U0001f4b0",
                        style=ButtonStyle.primary, row=4, custom_id=f"mine_co_{self.uid}")
            self.add_item(co)

    def _embed(self, status=""):
        m = self._mult()
        e = discord.Embed(title=f"\U0001f4a3  Mines ({self.mc} bombs)", color=C_BLUE)
        e.add_field(name="Safe Found", value=f"**{self.safe_cnt}**", inline=True)
        e.add_field(name="Multiplier", value=f"**{m:.2f}x**", inline=True)
        e.add_field(name="Potential Win", value=f"{CE} **{fm(int(self.bet*m))}**", inline=True)
        if status: e.description = status
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def interaction_check(self, ix):
        cid = ix.data.get("custom_id","")
        if not cid.startswith("mine_"): return True
        if ix.user.id != self.uid:
            await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
            return False
        if self.done: await ix.response.defer(); return False
        if cid.startswith("mine_co_"): await self._cashout(ix)
        else:
            try: tile = int(cid.split("_")[1]); await self._tile(ix, tile)
            except: await ix.response.defer()
        return False

    async def _tile(self, ix, t):
        self.rev.add(t)
        if t in self.mines:
            for m in self.mines: self.rev.add(m)
            self.done=True; self.stop(); self._build()
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            bal = await db.bal(self.gid, self.uid)
            await ix.response.edit_message(embed=e_lose("Mines", "\U0001f4a5 Boom! You hit a mine!", self.bet, bal), view=self)
        else:
            self.safe_cnt += 1
            if self.safe_cnt >= 16 - self.mc:
                self.done=True; self.stop(); self._build()
                win = int(self.bet * self._mult()) - self.bet
                await db.adj(self.gid, self.uid, win)
                await db.reduce_wager(self.gid, self.uid, self.bet)
                bal = await db.bal(self.gid, self.uid)
                await ix.response.edit_message(embed=e_win("Mines", "\u2705 All safe tiles found!", win, bal), view=self)
            else:
                self._build(); await ix.response.edit_message(embed=self._embed(), view=self)

    async def _cashout(self, ix):
        self.done=True; self.stop(); self._build()
        win = int(self.bet * self._mult()) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        bal = await db.bal(self.gid, self.uid)
        await ix.response.edit_message(embed=e_win("Mines",
            f"Cashed out at **{self._mult():.2f}x** ({self.safe_cnt} safe tiles)", win, bal), view=self)

    async def on_timeout(self):
        if not self.done:
            self.done=True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            for i in self.children: i.disabled = True
            try: await self.msg.edit(embed=discord.Embed(title="\U0001f4a3 Mines",
                description="\u23f0 Timed out \u2014 bet lost.", color=C_RED), view=self)
            except: pass

@bot.tree.command(name="mines", description="Click tiles to find gems \u2014 avoid the bombs!")
@app_commands.describe(bet="Amount to bet", mines="Number of mines")
@app_commands.choices(mines=[
    app_commands.Choice(name="1 mine (low risk)",    value=1),
    app_commands.Choice(name="3 mines (medium)",     value=3),
    app_commands.Choice(name="5 mines (high risk)",  value=5),
    app_commands.Choice(name="10 mines (extreme)",   value=10),
])
async def cmd_mines(ix: discord.Interaction, bet: str, mines: int = 3):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    v = MinesView(ix.user.id, ix.guild_id, amt, mines)
    v.msg = await ix.followup.send(embed=v._embed(), view=v)

# ══════════════════════════════════════════════════════════════════════════════
# BOMB DEFUSE
# ══════════════════════════════════════════════════════════════════════════════

WIRE_CLR = ["\U0001f534 Red","\U0001f7e1 Yellow","\U0001f7e2 Green","\U0001f535 Blue","\U0001f7e3 Purple"]

class BombView(View):
    def __init__(self, uid, gid, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.bet=bet
        wires = fair_shuffle(list(range(5))); self.bombs = set(wires[:2])
        self.cut=set(); self.done=False; self.msg=None; self._build()

    def _safe_cnt(self): return len([c for c in self.cut if c not in self.bombs])
    def _mult(self):     return [0, 1.5, 2.5, 5.0][min(self._safe_cnt(), 3)]

    def _build(self):
        self.clear_items()
        for i in range(5):
            if i in self.cut:
                lbl = ("\U0001f4a3" if i in self.bombs else "\u2702\ufe0f") + " " + WIRE_CLR[i]
                sty = ButtonStyle.danger if i in self.bombs else ButtonStyle.success
                btn = Button(label=lbl[:40], style=sty, disabled=True, row=0)
            else:
                btn = Button(label=WIRE_CLR[i], style=ButtonStyle.secondary,
                             disabled=self.done, row=0, custom_id=f"bomb_{i}_{self.uid}")
            self.add_item(btn)
        if self._safe_cnt() > 0 and not self.done:
            co = Button(label=f"Cash Out {self._mult():.1f}x \U0001f4b0",
                        style=ButtonStyle.primary, row=1, custom_id=f"bomb_co_{self.uid}")
            self.add_item(co)

    def _embed(self, status=""):
        e = discord.Embed(title="\U0001f4a3  Bomb Defuse", color=C_BLUE)
        e.description = "5 wires \u2014 2 are bombs, 3 are safe.\n" + (status or "")
        e.add_field(name="Safe Cut",   value=f"**{self._safe_cnt()}/3**", inline=True)
        e.add_field(name="Multiplier", value=f"**{self._mult():.1f}x**", inline=True)
        e.add_field(name="Potential",  value=f"{CE} **{fm(int(self.bet*self._mult()))}**", inline=True)
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}")
        return e

    async def interaction_check(self, ix):
        cid = ix.data.get("custom_id","")
        if not cid.startswith("bomb_"): return True
        if ix.user.id != self.uid:
            await ix.response.send_message(embed=discord.Embed(description="Not your game.", color=C_RED), ephemeral=True)
            return False
        if self.done: await ix.response.defer(); return False
        if cid.startswith("bomb_co_"): await self._cashout(ix)
        else:
            try: wire = int(cid.split("_")[1]); await self._cut(ix, wire)
            except: await ix.response.defer()
        return False

    async def _cut(self, ix, w):
        self.cut.add(w)
        if w in self.bombs:
            for i in range(5): self.cut.add(i)
            self.done=True; self.stop(); self._build()
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            bal = await db.bal(self.gid, self.uid)
            await ix.response.edit_message(embed=e_lose("Bomb Defuse",
                f"\U0001f4a5 You cut the **{WIRE_CLR[w]}** wire \u2014 it was a bomb!", self.bet, bal), view=self)
        else:
            safe = self._safe_cnt()
            if safe == 3:
                self.done=True; self.stop(); self._build()
                win = int(self.bet * self._mult()) - self.bet
                await db.adj(self.gid, self.uid, win)
                await db.reduce_wager(self.gid, self.uid, self.bet)
                bal = await db.bal(self.gid, self.uid)
                await ix.response.edit_message(embed=e_win("Bomb Defuse",
                    "\u2705 All 3 safe wires cut \u2014 max payout!", win, bal), view=self)
            else:
                self._build()
                await ix.response.edit_message(embed=self._embed(
                    f"\u2705 **{WIRE_CLR[w]}** safely defused! {safe}/3 done."), view=self)

    async def _cashout(self, ix):
        self.done=True; self.stop(); self._build()
        win = int(self.bet * self._mult()) - self.bet
        await db.adj(self.gid, self.uid, win)
        await db.reduce_wager(self.gid, self.uid, self.bet)
        bal = await db.bal(self.gid, self.uid)
        await ix.response.edit_message(embed=e_win("Bomb Defuse",
            f"Cashed out with {self._safe_cnt()} safe wires \u2014 **{self._mult():.1f}x**!", win, bal), view=self)

    async def on_timeout(self):
        if not self.done:
            self.done=True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            for i in self.children: i.disabled = True
            try: await self.msg.edit(embed=discord.Embed(title="\U0001f4a3 Bomb Defuse",
                description="\u23f0 Timed out \u2014 bet lost.", color=C_RED), view=self)
            except: pass

@bot.tree.command(name="bomb", description="Cut 3 safe wires, avoid 2 bombs \u2014 cash out anytime!")
@app_commands.describe(bet="Amount to bet")
async def cmd_bomb(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    v = BombView(ix.user.id, ix.guild_id, amt)
    v.msg = await ix.followup.send(embed=v._embed(), view=v)

# ══════════════════════════════════════════════════════════════════════════════
# WAR
# ══════════════════════════════════════════════════════════════════════════════

WAR_DECK = [f"{r}{s}" for s in ["\u2660","\u2665","\u2666","\u2663"]
             for r in ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]]

@bot.tree.command(name="war", description="Draw a card \u2014 highest wins! Ties = sudden death.")
@app_commands.describe(bet="Amount to bet")
async def cmd_war(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    deck = fair_shuffle(WAR_DECK); rounds=[]
    yc = deck.pop(); dc = deck.pop(); rounds.append((yc, dc))
    while card_val(yc) == card_val(dc) and len(deck) >= 2:
        yc = deck.pop(); dc = deck.pop(); rounds.append((yc, dc))
    lines = [("Round 1" if i==0 else f"\u2694\ufe0f War Round {i+1}") +
             f": You **{y}** vs Dealer **{d}**" for i,(y,d) in enumerate(rounds)]
    gid, uid = ix.guild_id, ix.user.id
    await db.reduce_wager(gid, uid, amt)
    detail = "\n".join(lines)
    if card_val(yc) > card_val(dc):
        await db.adj(gid, uid, amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("War \u2694\ufe0f", detail, amt, bal))
    elif card_val(yc) < card_val(dc):
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("War \u2694\ufe0f", detail, amt, bal))
    else:
        bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_tie("War \u2694\ufe0f", detail+"\nDeck exhausted \u2014 tie!", bal))

# ══════════════════════════════════════════════════════════════════════════════
# HORSE RACE
# ══════════════════════════════════════════════════════════════════════════════

HORSES = [
    ("\U0001f40e Thunderhoof",  2.0, 30),
    ("\U0001f434 Silver Wind",  3.0, 22),
    ("\U0001f3c7 Dark Knight",  4.0, 18),
    ("\U0001f984 Stardust",     6.0, 14),
    ("\U0001f32a\ufe0f Whirlwind", 10.0, 9),
    ("\U0001f480 Last Chance", 20.0,  7),
]

@bot.tree.command(name="horserace", description="Pick a horse \u2014 up to 20x payout!")
@app_commands.describe(bet="Amount to bet", horse="Pick your horse")
@app_commands.choices(horse=[
    app_commands.Choice(name="1 \u2014 Thunderhoof (2x, 30%)",    value=1),
    app_commands.Choice(name="2 \u2014 Silver Wind (3x, 22%)",    value=2),
    app_commands.Choice(name="3 \u2014 Dark Knight (4x, 18%)",    value=3),
    app_commands.Choice(name="4 \u2014 Stardust (6x, 14%)",       value=4),
    app_commands.Choice(name="5 \u2014 Whirlwind (10x, 9%)",      value=5),
    app_commands.Choice(name="6 \u2014 Last Chance (20x, 7%)",    value=6),
])
async def cmd_horse(ix: discord.Interaction, bet: str, horse: int):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    weights = [w for _,_,w in HORSES]
    winner  = random.choices(range(6), weights=weights, k=1)[0]; chosen = horse - 1
    cn, co, _ = HORSES[chosen]; wn, _, _ = HORSES[winner]
    positions = fair_shuffle(list(range(6)))
    race = "\n".join(("\U0001f3c6" if hi==winner else f"{pos}.") + f" {HORSES[hi][0]}"
                     for pos, hi in enumerate(positions, 1))
    gid, uid = ix.guild_id, ix.user.id
    await db.reduce_wager(gid, uid, amt)
    if winner == chosen:
        win = int(amt * (co - 1)); await db.adj(gid, uid, win); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("Horse Race \U0001f3c7", f"{race}\n\nYou picked **{cn}** \u2014 winner!", win, bal))
    else:
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("Horse Race \U0001f3c7", f"{race}\n\nYou picked **{cn}** \u2014 **{wn}** won.", amt, bal))

# ══════════════════════════════════════════════════════════════════════════════
# NUMBER GUESS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="numguess", description="Guess a number 1\u2013100 \u2014 exact hit = 90x!")
@app_commands.describe(bet="Amount to bet", guess="Your guess (1\u2013100)")
async def cmd_ng(ix: discord.Interaction, bet: str, guess: int):
    await ix.response.defer()
    if not 1 <= guess <= 100:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Guess must be 1\u2013100.", color=C_RED), ephemeral=True)
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    result = fair_int(1,100); diff = abs(result - guess)
    gid, uid = ix.guild_id, ix.user.id
    await db.reduce_wager(gid, uid, amt)
    if diff == 0:    mult, lbl = 90, "\U0001f3af **EXACT HIT!**"
    elif diff <= 3:  mult, lbl = 10, f"\U0001f525 **Within 3!** (off by {diff})"
    elif diff <= 10: mult, lbl =  3, f"\U0001f321\ufe0f **Within 10!** (off by {diff})"
    else:            mult, lbl =  0, f"\u274c **Miss!** (off by {diff})"
    detail = f"Number was **{result}**, you guessed **{guess}**\n{lbl}"
    if mult > 0:
        win = amt * (mult - 1); await db.adj(gid, uid, win); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_win("Number Guess \U0001f522", detail, win, bal))
    else:
        await db.adj(gid, uid, -amt); bal = await db.bal(gid, uid)
        await ix.followup.send(embed=e_lose("Number Guess \U0001f522", detail, amt, bal))

# ══════════════════════════════════════════════════════════════════════════════
# SCRATCH CARD
# ══════════════════════════════════════════════════════════════════════════════

SC_SYM = ["\U0001f4b0","\U0001f48e","\u2b50","\U0001f340","\U0001f381","\U0001f4a3"]
SC_W   = [25, 15, 20, 18, 15, 7]
SC_PAY = {"\U0001f48e":50,"\U0001f4b0":20,"\U0001f340":10,"\u2b50":6,"\U0001f381":3}

class ScratchView(View):
    def __init__(self, uid, gid, bet):
        super().__init__(timeout=60)
        self.uid=uid; self.gid=gid; self.bet=bet
        self.tiles=random.choices(SC_SYM, weights=SC_W, k=9)
        self.rev=set(); self.done=False; self.msg=None; self._build()

    def _build(self):
        self.clear_items()
        for i in range(9):
            row = i // 3
            if i in self.rev:
                lbl = self.tiles[i]
                sty = ButtonStyle.danger if lbl == "\U0001f4a3" else ButtonStyle.success
                btn = Button(label=lbl, style=sty, disabled=True, row=row)
            else:
                btn = Button(label="?", style=ButtonStyle.secondary,
                             disabled=self.done, row=row, custom_id=f"sc_{i}_{self.uid}")
            self.add_item(btn)

    def _check_win(self):
        lines = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]]
        best = 0
        for line in lines:
            if all(i in self.rev for i in line):
                s = [self.tiles[i] for i in line]
                if s[0] == s[1] == s[2]:
                    if s[0] == "\U0001f4a3": return -1
                    best = max(best, SC_PAY.get(s[0], 0))
        return best

    def _embed(self):
        e = discord.Embed(title="\U0001f39f\ufe0f  Scratch Card", color=C_BLUE)
        e.description = "Reveal all 9 tiles \u2014 match 3 in a row/col/diagonal to win!"
        e.add_field(name="Remaining", value=f"**{9-len(self.rev)}** tiles", inline=True)
        e.set_footer(text=f"Bet: {CE} {fm(self.bet)}  \u00b7  \U0001f48e50x \U0001f4b020x \U0001f34010x \u2b506x \U0001f3813x")
        return e

    async def interaction_check(self, ix):
        cid = ix.data.get("custom_id","")
        if not cid.startswith("sc_"): return True
        if ix.user.id != self.uid:
            await ix.response.send_message(embed=discord.Embed(description="Not your card.", color=C_RED), ephemeral=True)
            return False
        if self.done: await ix.response.defer(); return False
        try: tile = int(cid.split("_")[1]); await self._reveal(ix, tile)
        except: await ix.response.defer()
        return False

    async def _reveal(self, ix, t):
        self.rev.add(t); self._build()
        if self.tiles[t] == "\U0001f4a3":
            self.done=True; self.stop()
            for i in range(9): self.rev.add(i)
            self._build()
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            bal = await db.bal(self.gid, self.uid)
            return await ix.response.edit_message(embed=e_lose("Scratch Card", "\U0001f4a3 Bomb tile \u2014 instant loss!", self.bet, bal), view=self)
        if len(self.rev) == 9:
            self.done=True; self.stop(); mult = self._check_win()
            await db.reduce_wager(self.gid, self.uid, self.bet)
            if mult > 0:
                win = int(self.bet * mult) - self.bet; await db.adj(self.gid, self.uid, win); bal = await db.bal(self.gid, self.uid)
                await ix.response.edit_message(embed=e_win("Scratch Card", f"3 in a row! **{mult}x**", win, bal), view=self)
            else:
                await db.adj(self.gid, self.uid, -self.bet); bal = await db.bal(self.gid, self.uid)
                await ix.response.edit_message(embed=e_lose("Scratch Card", "No 3 in a row.", self.bet, bal), view=self)
        else:
            await ix.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        if not self.done:
            self.done=True
            await db.adj(self.gid, self.uid, -self.bet)
            await db.reduce_wager(self.gid, self.uid, self.bet)
            for i in self.children: i.disabled = True
            try: await self.msg.edit(embed=discord.Embed(title="\U0001f39f\ufe0f Scratch Card",
                description="\u23f0 Timed out \u2014 bet lost.", color=C_RED), view=self)
            except: pass

@bot.tree.command(name="scratch", description="Reveal 9 tiles \u2014 match 3 in a row to win!")
@app_commands.describe(bet="Amount to bet")
async def cmd_scratch(ix: discord.Interaction, bet: str):
    await ix.response.defer()
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    v = ScratchView(ix.user.id, ix.guild_id, amt)
    v.msg = await ix.followup.send(embed=v._embed(), view=v)

# ══════════════════════════════════════════════════════════════════════════════
# DUEL (PvP)
# ══════════════════════════════════════════════════════════════════════════════

class DuelView(View):
    def __init__(self, challenger, opponent, bet):
        super().__init__(timeout=60)
        self.challenger=challenger; self.opponent=opponent; self.bet=bet; self.msg=None

    @discord.ui.button(label="Accept", style=ButtonStyle.green, emoji="\u2705")
    async def accept(self, ix, _):
        if ix.user.id != self.opponent.id:
            return await ix.response.send_message(embed=discord.Embed(description="Not your duel.", color=C_RED), ephemeral=True)
        self.stop()
        for i in self.children: i.disabled = True
        gid = ix.guild_id
        cb = await db.bal(gid, self.challenger.id); ob = await db.bal(gid, self.opponent.id)
        if self.bet > cb:
            return await ix.response.edit_message(embed=discord.Embed(color=C_RED,
                description=f"{self.challenger.mention} no longer has enough {CE}."), view=self)
        if self.bet > ob:
            return await ix.response.edit_message(embed=discord.Embed(color=C_RED,
                description=f"You don't have enough {CE}."), view=self)
        winner = fair_choice([self.challenger, self.opponent])
        loser  = self.opponent if winner == self.challenger else self.challenger
        await db.adj(gid, winner.id, self.bet); await db.adj(gid, loser.id, -self.bet)
        await db.reduce_wager(gid, self.challenger.id, self.bet)
        await db.reduce_wager(gid, self.opponent.id, self.bet)
        bal = await db.bal(gid, winner.id)
        e = discord.Embed(title="\u2694\ufe0f  Duel Result", color=C_GREEN)
        e.description = (f"{self.challenger.mention} vs {self.opponent.mention}\n\n"
                         f"\U0001f3c6 **{winner.display_name}** wins {CE} **{fm(self.bet*2)}**!")
        e.set_footer(text=f"Winner balance: {CE} {fm(bal)}")
        await ix.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Decline", style=ButtonStyle.red, emoji="\u274c")
    async def decline(self, ix, _):
        if ix.user.id not in (self.opponent.id, self.challenger.id):
            return await ix.response.send_message(embed=discord.Embed(description="Not for you.", color=C_RED), ephemeral=True)
        self.stop()
        for i in self.children: i.disabled = True
        await ix.response.edit_message(embed=discord.Embed(title="\u2694\ufe0f Duel",
            description="Duel was declined.", color=C_GOLD), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(embed=discord.Embed(title="\u2694\ufe0f Duel",
            description="Duel expired.", color=C_GREY), view=self)
        except: pass

@bot.tree.command(name="duel", description="Challenge someone to a winner-takes-all duel!")
@app_commands.describe(opponent="The user to challenge", bet="Amount each player wagers")
async def cmd_duel(ix: discord.Interaction, opponent: discord.Member, bet: str):
    await ix.response.defer()
    if opponent.bot or opponent == ix.user:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Invalid opponent.", color=C_RED), ephemeral=True)
    amt = await parse_bet(ix, bet)
    if not await chk_bet(ix, amt): return
    ob = await db.bal(ix.guild_id, opponent.id)
    if amt > ob:
        return await ix.followup.send(embed=discord.Embed(description=f"\u274c {opponent.mention} doesn't have enough {CE}.", color=C_RED), ephemeral=True)
    e = discord.Embed(title="\u2694\ufe0f  Duel Challenge", color=C_BLUE)
    e.description = (f"{ix.user.mention} challenges {opponent.mention}!\n\n"
                     f"**Bet:** {CE} **{fm(amt)}** each\n"
                     f"**Winner takes:** {CE} **{fm(amt*2)}**")
    e.set_footer(text=f"{opponent.display_name} \u2014 you have 60s to respond!")
    v = DuelView(ix.user, opponent, amt)
    v.msg = await ix.followup.send(content=opponent.mention, embed=e, view=v)

# ══════════════════════════════════════════════════════════════════════════════
# ROLL
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="roll", description="Roll a random number 1\u2013100 (useful in gamble tickets)")
async def cmd_roll(ix: discord.Interaction):
    result = fair_int(1, 100)
    e = discord.Embed(title="\U0001f3b2  Dice Roll", color=C_BLUE)
    e.description = f"**{ix.user.display_name}** rolled **{result}** / 100"
    await ix.response.send_message(embed=e)

# ══════════════════════════════════════════════════════════════════════════════
# GAMES MENU
# ══════════════════════════════════════════════════════════════════════════════

_GAMES_PAGES = [
    {
        "title": "\U0001f3ae  Quick Games",
        "color": C_BLUE,
        "desc": (
            "These resolve instantly — no buttons needed.\n\n"
            f"`/coinflip <bet> <heads|tails>`\n> Correct call pays **2x**\n\n"
            f"`/slots <bet>`\n> 3 reels, match to win up to **50x**\n\n"
            f"`/roulette <bet> <red|black|odd|even|1-12|0-36>`\n> Up to **36x** on a single number\n\n"
            f"`/blackjack <bet>`\n> Beat the dealer — **1.5x** natural, **2x** regular win\n\n"
            f"`/war <bet>`\n> High card wins **2x** — ties go to sudden death\n\n"
            f"`/horserace <bet> <horse 1\u20136>`\n> Pick a horse, up to **20x** payout\n\n"
            f"`/numguess <bet> <1\u2013100>`\n> Exact hit = **90x**, within 3 = **10x**, within 10 = **3x**\n\n"
            f"`/limbo <bet> <multiplier>`\n> Set your own target — higher = riskier"
        ),
    },
    {
        "title": "\U0001f579\ufe0f  Interactive Games",
        "color": C_PINK,
        "desc": (
            "These use buttons — you keep playing until you stop or time out.\n\n"
            f"`/mines <bet> [1|3|5|10 mines]`\n> Click tiles to find gems, avoid bombs. Cash out anytime.\n\n"
            f"`/crash <bet>`\n> Watch the multiplier rise and cash out before it crashes!\n\n"
            f"`/higherlower <bet>`\n> Guess the next card, build a streak up to **85x**\n\n"
            f"`/bomb <bet>`\n> Cut 3 safe wires out of 5, avoid 2 bombs — up to **5x**\n\n"
            f"`/scratch <bet>`\n> Reveal 9 tiles, match 3 in a row/col/diagonal — up to **50x**\n\n"
            f"`/duel @user <bet>`\n> PvP — winner takes both bets. Opponent must accept.\n\n"
            "\u23f1\ufe0f All interactive games have a **60\u2013120 second** timeout."
        ),
    },
]

class GamesView(View):
    def __init__(self, aid):
        super().__init__(timeout=120)
        self.page = 0; self.aid = aid; self.msg = None; self._upd()

    def _upd(self):
        self.quick_b.disabled  = self.page == 0
        self.inter_b.disabled  = self.page == 1

    def _embed(self):
        p = _GAMES_PAGES[self.page]
        e = discord.Embed(title=p["title"], description=p["desc"], color=p["color"])
        e.set_footer(text=f"Page {self.page+1}/2  \u00b7  Shillings Bot")
        return e

    @discord.ui.button(label="\U0001f3ae Quick Games", style=ButtonStyle.blurple, custom_id="gm_quick")
    async def quick_b(self, ix, _):
        if ix.user.id != self.aid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your menu.", color=C_RED), ephemeral=True)
        self.page = 0; self._upd()
        await ix.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="\U0001f579\ufe0f Interactive", style=ButtonStyle.grey, custom_id="gm_inter")
    async def inter_b(self, ix, _):
        if ix.user.id != self.aid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your menu.", color=C_RED), ephemeral=True)
        self.page = 1; self._upd()
        await ix.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(view=self)
        except: pass

@bot.tree.command(name="games", description="Browse all available games and how to play them")
async def cmd_games(ix: discord.Interaction):
    v = GamesView(ix.user.id)
    await ix.response.send_message(embed=v._embed(), view=v)
    v.msg = await ix.original_response()

class ApproveModal(Modal, title="Approve Deposit"):
    amount = TextInput(label="Shillings to Award", placeholder="e.g. 500000", required=True)

    def __init__(self, user_id, channel):
        super().__init__(); self.user_id=user_id; self.channel=channel

    async def on_submit(self, ix: discord.Interaction):
        try: value = int(self.amount.value.replace(",","").strip())
        except:
            return await ix.response.send_message(embed=discord.Embed(description="\u274c Invalid amount.", color=C_RED), ephemeral=True)
        if value <= 0:
            return await ix.response.send_message(embed=discord.Embed(description="\u274c Must be positive.", color=C_RED), ephemeral=True)
        await db.adj(ix.guild_id, self.user_id, value)
        await db.add_wager(ix.guild_id, self.user_id, value)
        member = ix.guild.get_member(self.user_id)
        e = discord.Embed(title="\u2705  Deposit Approved", color=C_GREEN)
        e.description = (f"**Approved by:** {ix.user.mention}\n"
                         f"**Awarded:** {CE} **{fm(value)} {CUR}**\n"
                         f"**Wager required:** {CE} **{fm(value)}** (1x)")
        await ix.response.edit_message(embed=e, view=None)
        if member:
            try:
                dm = discord.Embed(title="\u2705 Deposit Approved!", color=C_GREEN)
                dm.description = (f"Your deposit in **{ix.guild.name}** was approved!\n\n"
                                  f"{CE} **{fm(value)} {CUR}** added.\n"
                                  f"Wager {CE} **{fm(value)}** before withdrawing.")
                await member.send(embed=dm)
            except: pass
        log_ch = await get_log_ch(ix.guild_id, ix.guild)
        if log_ch:
            le = discord.Embed(title="\U0001f4e5 Deposit Approved", color=C_GREEN)
            le.description = (f"**User:** {member.mention if member else self.user_id}\n"
                              f"**By:** {ix.user.mention}\n**Awarded:** {CE} {fm(value)}")
            await log_ch.send(embed=le)
        await asyncio.sleep(4)
        try: await self.channel.delete()
        except: pass

class DepositControlView(View):
    def __init__(self, user_id, channel):
        super().__init__(timeout=None); self.user_id=user_id; self.channel=channel

    @discord.ui.button(label="Approve", style=ButtonStyle.green, emoji="\u2705")
    async def approve(self, ix, _):
        if not await db.is_wl(ix.guild_id, ix.user.id):
            return await ix.response.send_message(embed=discord.Embed(description="\u274c Not authorized.", color=C_RED), ephemeral=True)
        await ix.response.send_modal(ApproveModal(self.user_id, self.channel))

    @discord.ui.button(label="Deny", style=ButtonStyle.red, emoji="\u274c")
    async def deny(self, ix, _):
        if not await db.is_wl(ix.guild_id, ix.user.id):
            return await ix.response.send_message(embed=discord.Embed(description="\u274c Not authorized.", color=C_RED), ephemeral=True)
        self.stop()
        for i in self.children: i.disabled = True
        member = ix.guild.get_member(self.user_id)
        await ix.response.edit_message(embed=discord.Embed(title="\u274c Deposit Denied",
            description=f"Denied by {ix.user.mention}.", color=C_RED), view=self)
        if member:
            try: await member.send(embed=discord.Embed(color=C_RED,
                description=f"Your deposit in **{ix.guild.name}** was **denied**."))
            except: pass
        await asyncio.sleep(5)
        try: await self.channel.delete()
        except: pass

@bot.tree.command(name="deposit", description="Open a deposit ticket for staff to award Shillings")
@app_commands.describe(item="What you are depositing")
async def cmd_deposit(ix: discord.Interaction, item: str):
    await ix.response.defer(ephemeral=True)
    cat = await get_cat(ix.guild_id, ix.guild)
    if not cat:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Category not configured. Ask admin to run /setup.", color=C_RED), ephemeral=True)
    staff = await get_staff_role(ix.guild_id, ix.guild)
    num = await db.next_tid(ix.guild_id); tid = f"{num:04d}"
    ow = {
        ix.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ix.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        ix.user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if staff: ow[staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    ch = await cat.create_text_channel(f"deposit-{tid}-{ix.user.name[:15]}", overwrites=ow)
    view = DepositControlView(ix.user.id, ch)
    e = discord.Embed(title="\U0001f4e5  Deposit Request", color=C_BLUE)
    e.set_author(name=ix.user.display_name, icon_url=ix.user.display_avatar.url)
    e.description = (f"**User:** {ix.user.mention}\n**Depositing:** {item}\n\n"
                     f"Staff: approve and set the Shillings value.")
    ping = ix.user.mention + (f" {staff.mention}" if staff else "")
    await ch.send(content=ping, embed=e, view=view)
    await ix.followup.send(embed=discord.Embed(description=f"\u2705 Deposit ticket opened \u2014 {ch.mention}", color=C_GREEN), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# GAMBLE TICKET — with Add User button
# ══════════════════════════════════════════════════════════════════════════════

class AddUserModal(Modal, title="Add User to Gamble Room"):
    user_id_input = TextInput(label="User ID to add", placeholder="Right-click user > Copy ID", required=True)

    def __init__(self, channel: discord.TextChannel, staff_role):
        super().__init__()
        self.channel = channel
        self.staff_role = staff_role

    async def on_submit(self, ix: discord.Interaction):
        try:
            uid = int(self.user_id_input.value.strip())
        except:
            return await ix.response.send_message(
                embed=discord.Embed(description="\u274c Invalid user ID. Right-click a user and copy their ID.", color=C_RED), ephemeral=True)
        member = ix.guild.get_member(uid)
        if not member:
            return await ix.response.send_message(
                embed=discord.Embed(description="\u274c User not found in this server.", color=C_RED), ephemeral=True)
        await self.channel.set_permissions(member, read_messages=True, send_messages=True)
        e = discord.Embed(title="\u2705 User Added", color=C_GREEN)
        e.description = f"{member.mention} has been added to this gamble room."
        await ix.response.send_message(embed=e)

class GambleTicketView(View):
    def __init__(self, channel: discord.TextChannel, staff_role, opener_id: int):
        super().__init__(timeout=None)
        self.channel = channel
        self.staff_role = staff_role
        self.opener_id = opener_id

    async def _is_authorized(self, ix) -> bool:
        return await db.is_wl(ix.guild_id, ix.user.id) or ix.user.id == self.opener_id

    @discord.ui.button(label="Add User", style=ButtonStyle.blurple, emoji="\U0001f464", row=0)
    async def add_user(self, ix, _):
        if not await self._is_authorized(ix):
            return await ix.response.send_message(
                embed=discord.Embed(description="\u274c Only the ticket opener or staff can add users.", color=C_RED), ephemeral=True)
        await ix.response.send_modal(AddUserModal(self.channel, self.staff_role))

    @discord.ui.button(label="Close Ticket", style=ButtonStyle.red, emoji="\U0001f512", row=0)
    async def close_ticket(self, ix, _):
        if not await db.is_wl(ix.guild_id, ix.user.id):
            return await ix.response.send_message(
                embed=discord.Embed(description="\u274c Only staff can close tickets.", color=C_RED), ephemeral=True)
        await ix.response.send_message(embed=discord.Embed(description="\u2705 Closing in 3 seconds...", color=C_GREEN))
        await asyncio.sleep(3)
        try: await self.channel.delete()
        except: pass

@bot.tree.command(name="gambleticket", description="Open a real-item gamble ticket with a staff middleman")
@app_commands.describe(opponent="Your opponent")
async def cmd_gambleticket(ix: discord.Interaction, opponent: discord.Member):
    await ix.response.defer(ephemeral=True)
    if opponent.bot or opponent == ix.user:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Invalid opponent.", color=C_RED), ephemeral=True)
    cat = await get_cat(ix.guild_id, ix.guild)
    if not cat:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Category not configured. Run /setup.", color=C_RED), ephemeral=True)
    staff = await get_staff_role(ix.guild_id, ix.guild)
    num = await db.next_tid(ix.guild_id); tid = f"{num:04d}"
    ow = {
        ix.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ix.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        ix.user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
        opponent:              discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if staff: ow[staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    ch = await ix.guild.create_text_channel(f"gamble-{tid}", category=cat, overwrites=ow)
    view = GambleTicketView(ch, staff, ix.user.id)
    e = discord.Embed(title="\U0001f3b2  Gamble Ticket", color=C_PINK)
    e.description = (
        f"**{ix.user.mention}** vs **{opponent.mention}**\n\n"
        f"**How it works:**\n"
        f"> 1\ufe0f\u20e3 Both players state what they are wagering\n"
        f"> 2\ufe0f\u20e3 Staff middleman collects both items\n"
        f"> 3\ufe0f\u20e3 Use `/roll` or `/coinflip` to decide winner\n"
        f"> 4\ufe0f\u20e3 Middleman sends everything to the winner\n\n"
        f"Use **Add User** to add extra players or spectators.\n"
        f"Staff: use **Close Ticket** when done."
    )
    ping = f"{ix.user.mention} {opponent.mention}" + (f" {staff.mention}" if staff else "")
    await ch.send(content=ping, embed=e, view=view)
    await ix.followup.send(embed=discord.Embed(description=f"\U0001f3b2 Gamble ticket opened \u2014 {ch.mention}", color=C_PINK), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="addshillings", description="[Staff] Add Shillings to a user")
@app_commands.describe(member="Target user", amount="Amount to add")
@wl_check()
async def cmd_add(ix: discord.Interaction, member: discord.Member, amount: int):
    await ix.response.defer(ephemeral=True)
    if amount <= 0:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Must be positive.", color=C_RED), ephemeral=True)
    await db.adj(ix.guild_id, member.id, amount)
    bal = await db.bal(ix.guild_id, member.id)
    e = discord.Embed(title="\u2705 Shillings Added", color=C_GREEN)
    e.description = f"**User:** {member.mention}\n**Added:** {CE} **{fm(amount)}**\n**New Balance:** {CE} **{fm(bal)}**"
    await ix.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="removeshillings", description="[Staff] Remove Shillings from a user")
@app_commands.describe(member="Target user", amount="Amount to remove")
@wl_check()
async def cmd_remove(ix: discord.Interaction, member: discord.Member, amount: int):
    await ix.response.defer(ephemeral=True)
    if amount <= 0:
        return await ix.followup.send(embed=discord.Embed(description="\u274c Must be positive.", color=C_RED), ephemeral=True)
    await db.adj(ix.guild_id, member.id, -amount)
    bal = await db.bal(ix.guild_id, member.id)
    e = discord.Embed(title="\u2705 Shillings Removed", color=C_GREEN)
    e.description = f"**User:** {member.mention}\n**Removed:** {CE} **{fm(amount)}**\n**New Balance:** {CE} **{fm(bal)}**"
    await ix.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="clearbalance", description="[Staff] Reset a user's balance to 0")
@app_commands.describe(member="Target user")
@wl_check()
async def cmd_clear(ix: discord.Interaction, member: discord.Member):
    await ix.response.defer(ephemeral=True)
    await db.set_bal(ix.guild_id, member.id, 0)
    await ix.followup.send(embed=discord.Embed(title="\u2705 Balance Cleared",
        description=f"{member.mention}'s balance reset to 0.", color=C_GREEN), ephemeral=True)

@bot.tree.command(name="setwager", description="[Staff] Set a user's wager requirement")
@app_commands.describe(member="Target user", amount="Amount (0 to clear)")
@wl_check()
async def cmd_setwager(ix: discord.Interaction, member: discord.Member, amount: int):
    await ix.response.defer(ephemeral=True)
    await db.set_wager(ix.guild_id, member.id, amount)
    desc = (f"{member.mention}'s wager set to {CE} **{fm(amount)}**."
            if amount > 0 else f"{member.mention}'s wager cleared.")
    await ix.followup.send(embed=discord.Embed(title="\u2705 Wager Set", description=desc, color=C_GREEN), ephemeral=True)

@bot.tree.command(name="whitelist", description="[Owner] Add or remove a user from the staff whitelist")
@app_commands.describe(action="add or remove", member="Target user")
@app_commands.choices(action=[
    app_commands.Choice(name="Add",    value="add"),
    app_commands.Choice(name="Remove", value="remove"),
])
async def cmd_wl(ix: discord.Interaction, action: str, member: discord.Member):
    if ix.user.id != OWNER_ID and not await db.is_wl(ix.guild_id, ix.user.id):
        return await ix.response.send_message(
            embed=discord.Embed(description="\u274c Owner-only command.", color=C_RED), ephemeral=True)
    await ix.response.defer(ephemeral=True)
    if action == "add":
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO whitelist(guild_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                            ix.guild_id, member.id)
        await ix.followup.send(embed=discord.Embed(description=f"\u2705 {member.mention} added to whitelist.", color=C_GREEN), ephemeral=True)
    else:
        async with db.pool.acquire() as c:
            await c.execute("DELETE FROM whitelist WHERE guild_id=$1 AND user_id=$2", ix.guild_id, member.id)
        await ix.followup.send(embed=discord.Embed(description=f"\u2705 {member.mention} removed from whitelist.", color=C_GREEN), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# SETUP & CLOSE
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup", description="[Staff] Configure the bot for this server")
@app_commands.describe(setting="What to configure", channel="Channel to use", role="Role to use")
@app_commands.choices(setting=[
    app_commands.Choice(name="Ticket Category", value="category"),
    app_commands.Choice(name="Log Channel",     value="logs"),
    app_commands.Choice(name="Staff Role",      value="staff"),
    app_commands.Choice(name="View Config",     value="view"),
])
@wl_check()
async def cmd_setup(ix: discord.Interaction, setting: str,
                    channel: discord.TextChannel = None, role: discord.Role = None):
    await ix.response.defer(ephemeral=True); gid = ix.guild_id
    if setting == "view":
        c = await db.get_cfg(gid)
        e = discord.Embed(title="\u2699\ufe0f  Server Config", color=C_BLUE)
        if c:
            cat = ix.guild.get_channel(c["cat_id"]) if c.get("cat_id") else None
            lch = ix.guild.get_channel(c["log_id"]) if c.get("log_id") else None
            stf = ix.guild.get_role(c["staff_id"])  if c.get("staff_id") else None
            e.add_field(name="\U0001f4c1 Category", value=cat.mention if cat else "Not set", inline=False)
            e.add_field(name="\U0001f4cb Logs",     value=lch.mention if lch else "Not set", inline=False)
            e.add_field(name="\U0001f6e1\ufe0f Staff", value=stf.mention if stf else "Not set", inline=False)
        else: e.description = "No config found."
        return await ix.followup.send(embed=e, ephemeral=True)
    elif setting == "category":
        if not channel:
            return await ix.followup.send(embed=discord.Embed(description="\u274c Select a channel.", color=C_RED), ephemeral=True)
        cid = channel.category_id or channel.id
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO cfg(guild_id,cat_id) VALUES($1,$2) ON CONFLICT(guild_id) DO UPDATE SET cat_id=$2", gid, cid)
        await ix.followup.send(embed=discord.Embed(description="\u2705 Ticket category set.", color=C_GREEN), ephemeral=True)
    elif setting == "logs":
        if not channel:
            return await ix.followup.send(embed=discord.Embed(description="\u274c Select a channel.", color=C_RED), ephemeral=True)
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO cfg(guild_id,log_id) VALUES($1,$2) ON CONFLICT(guild_id) DO UPDATE SET log_id=$2", gid, channel.id)
        await ix.followup.send(embed=discord.Embed(description=f"\u2705 Log channel set to {channel.mention}.", color=C_GREEN), ephemeral=True)
    elif setting == "staff":
        if not role:
            return await ix.followup.send(embed=discord.Embed(description="\u274c Select a role.", color=C_RED), ephemeral=True)
        async with db.pool.acquire() as c:
            await c.execute("INSERT INTO cfg(guild_id,staff_id) VALUES($1,$2) ON CONFLICT(guild_id) DO UPDATE SET staff_id=$2", gid, role.id)
        await ix.followup.send(embed=discord.Embed(description=f"\u2705 Staff role set to {role.mention}.", color=C_GREEN), ephemeral=True)

@bot.tree.command(name="close", description="[Staff] Close a ticket channel")
@wl_check()
async def cmd_close(ix: discord.Interaction):
    ch = ix.channel
    if not any(ch.name.startswith(p) for p in ("deposit-","gamble-")):
        return await ix.response.send_message(
            embed=discord.Embed(description="\u274c Not a ticket channel.", color=C_RED), ephemeral=True)
    await ix.response.send_message(embed=discord.Embed(description="\u2705 Closing in 3 seconds...", color=C_GREEN))
    await asyncio.sleep(3)
    try: await ch.delete()
    except: pass

# ══════════════════════════════════════════════════════════════════════════════
# HELP
# ══════════════════════════════════════════════════════════════════════════════

HELP_PAGES = [
    {"title": f"{CE}  Economy", "color": C_GREEN, "desc":
     f"`/daily` \u2014 Claim 1\u201320M {CUR} every 3 hours\n"
     f"`/balance [@user]` \u2014 Check balance & wager status\n\n"
     f"**Claim Rarities:**\n"
     f"> {CE} Small \u2014 up to 100K\n"
     f"> \U0001f44d Decent \u2014 100K\u20131M\n"
     f"> \u2728 Nice \u2014 1M\u20135M\n"
     f"> \U0001f48e Rare \u2014 5M\u201315M\n"
     f"> \U0001f31f Jackpot \u2014 15M\u201320M\n\n"
     f"\U0001f3ae Use `/games` to browse all available games!"},
    {"title": "\U0001f3b2  Gamble Tickets", "color": C_PINK, "desc":
     "`/gambleticket @opponent` \u2014 Open a real-item gamble\n\n"
     "**How it works:**\n"
     "> Opens a private channel for both players + staff\n"
     "> Use **Add User** button to add extra people\n"
     "> State what you\u2019re wagering\n"
     "> Middleman collects both items\n"
     "> Use `/roll` or `/coinflip` to decide winner\n"
     "> Staff closes with the **Close Ticket** button"},
    {"title": "\U0001f4e5  Deposit", "color": C_GOLD, "desc":
     "`/deposit <item>` \u2014 Open a deposit ticket\n\n"
     "**Deposit flow:**\n"
     "> Open ticket \u2192 state what you\u2019re depositing\n"
     "> Staff approves and awards Shillings\n"
     "> You must wager **1x** the amount before the wager\n   requirement clears\n\n"
     "Your wager progress shows in `/balance`."},
    {"title": "\u2699\ufe0f  Staff Commands", "color": C_GREY, "desc":
     "**Whitelist (owner only):** `/whitelist add/remove @user`\n\n"
     "**Balance:** `/addshillings` \u00b7 `/removeshillings` \u00b7 `/clearbalance`\n"
     "**Wager:** `/setwager @user <amount>`\n\n"
     "**Server setup:** `/setup category|logs|staff|view`\n"
     "**Tickets:** `/close` (staff only, inside ticket channels)"},
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
        e.set_footer(text=f"Page {self.page+1}/{len(HELP_PAGES)}  \u00b7  Shillings Bot")
        return e

    @discord.ui.button(label="\u25c4 Back", style=ButtonStyle.grey, custom_id="hp_prev")
    async def prev_b(self, ix, _):
        if ix.user.id != self.aid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your menu.", color=C_RED), ephemeral=True)
        self.page -= 1; self._upd()
        await ix.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next \u25ba", style=ButtonStyle.blurple, custom_id="hp_next")
    async def next_b(self, ix, _):
        if ix.user.id != self.aid:
            return await ix.response.send_message(embed=discord.Embed(description="Not your menu.", color=C_RED), ephemeral=True)
        self.page += 1; self._upd()
        await ix.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        for i in self.children: i.disabled = True
        try: await self.msg.edit(view=self)
        except: pass

@bot.tree.command(name="help", description="View all bot commands and how to use them")
async def cmd_help(ix: discord.Interaction):
    v = HelpView(ix.user.id)
    await ix.response.send_message(embed=v._embed(), view=v)
    v.msg = await ix.original_response()

# ══════════════════════════════════════════════════════════════════════════════
# SYNC (owner-only utility)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="sync", description="[Owner] Force re-sync slash commands")
async def cmd_sync(ix: discord.Interaction):
    if ix.user.id != OWNER_ID:
        return await ix.response.send_message(
            embed=discord.Embed(description="\u274c Owner-only command.", color=C_RED), ephemeral=True)
    await ix.response.defer(ephemeral=True)
    try:
        guild_id = int(os.getenv("GUILD_ID", "0"))
        if guild_id:
            guild_obj = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            desc = f"\u2705 Synced **{len(synced)}** commands to this guild (instant)."
        else:
            synced = await bot.tree.sync()
            desc = f"\u2705 Synced **{len(synced)}** commands globally (may take up to 1 hour)."
        await ix.followup.send(embed=discord.Embed(description=desc, color=C_GREEN), ephemeral=True)
    except Exception as ex:
        await ix.followup.send(
            embed=discord.Embed(description=f"\u274c Sync failed: {ex}", color=C_RED), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    # Connect DB — wrapped so a DB failure never blocks command sync
    try:
        await db.connect()
    except Exception as ex:
        log.error(f"DB connect failed: {ex}  — bot will run without database until fixed!")

    # Sync slash commands.
    # Set GUILD_ID env var for instant guild-specific sync (great for testing).
    # Leave it unset for global sync (takes up to 1 hour to propagate on Discord's side).
    try:
        guild_id = int(os.getenv("GUILD_ID", "0"))
        if guild_id:
            guild_obj = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info(f"Synced {len(synced)} slash commands to guild {guild_id}")
        else:
            synced = await bot.tree.sync()
            log.info(f"Synced {len(synced)} slash commands globally")
    except Exception as ex:
        log.error(f"Slash sync failed: {ex}")

    log.info(f"Online as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_app_command_error(ix: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure): return
    # Unwrap CommandInvokeError to get the real cause
    cause = getattr(error, "original", error)
    if isinstance(cause, RuntimeError) and "DB_NOT_READY" in str(cause):
        msg = "\u26a0\ufe0f Bot is still starting up — try again in a moment."
    elif isinstance(cause, AttributeError) and "NoneType" in str(cause):
        msg = "\u26a0\ufe0f Bot is still starting up — try again in a moment."
    else:
        msg = "\u274c Something went wrong. Please try again."
    log.error(f"App command error ({type(cause).__name__}): {cause}")
    try:
        if ix.response.is_done(): await ix.followup.send(embed=discord.Embed(description=msg, color=C_GOLD), ephemeral=True)
        else: await ix.response.send_message(embed=discord.Embed(description=msg, color=C_GOLD), ephemeral=True)
    except: pass

# ══════════════════════════════════════════════════════════════════════════════
# WEB SERVER — Render keep-alive + UptimeRobot
# ══════════════════════════════════════════════════════════════════════════════

async def start_web():
    import aiohttp.web as web

    async def health(req):
        return web.Response(text="OK", content_type="text/plain")

    async def index(req):
        return web.Response(
            text="<html><body><h2>\U0001fa99 Shillings Bot is online!</h2></body></html>",
            content_type="text/html")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server on port {port}")

async def main():
    async with bot:
        await start_web()
        token = os.getenv("BOT_TOKEN", "")
        if not token: raise RuntimeError("BOT_TOKEN not set!")
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
