import discord
from discord.ext import commands, tasks
import asyncpg
import requests
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# === UTILS ===
def get_price(pair):
    symbol = pair.upper()
    if len(symbol) == 6:
        symbol = symbol[:3] + "/" + symbol[3:]  # EURUSD -> EUR/USD

    url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={TWELVE_DATA_API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if 'price' in data:
            return float(data['price'])
    except Exception as e:
        print(f"Price fetch error for {pair}: {e}")
    return None

def calculate_pips(price1, price2, pair):
    pip_size = 0.01 if "JPY" in pair.upper() else 0.0001
    return round(abs(price1 - price2) / pip_size)

# === DATABASE ===
async def init_db():
    async with asyncpg.create_pool(DATABASE_URL) as pool:
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    pair TEXT,
                    direction TEXT,
                    entry DOUBLE PRECISION,
                    tp DOUBLE PRECISION,
                    sl DOUBLE PRECISION,
                    lot_size DOUBLE PRECISION
                );
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    pair TEXT,
                    direction TEXT,
                    entry DOUBLE PRECISION,
                    exit_price DOUBLE PRECISION,
                    tp DOUBLE PRECISION,
                    sl DOUBLE PRECISION,
                    lot_size DOUBLE PRECISION,
                    result TEXT,
                    profit DOUBLE PRECISION
                );
            ''')

async def add_trade(user_id, pair, direction, entry, tp, sl, lot_size):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('''
        INSERT INTO trades (user_id, pair, direction, entry, tp, sl, lot_size)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    ''', user_id, pair.upper(), direction.lower(), entry, tp, sl, lot_size)
    await conn.close()

async def delete_trade(rowid):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('DELETE FROM trades WHERE id = $1', rowid)
    await conn.close()

async def log_closed_trade(user_id, pair, direction, entry, exit_price, tp, sl, lot_size, result, profit):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('''
        INSERT INTO closed_trades (user_id, pair, direction, entry, exit_price, tp, sl, lot_size, result, profit)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ''', user_id, pair, direction, entry, exit_price, tp, sl, lot_size, result, profit)
    await conn.close()

# === SLASH COMMANDS ===
@bot.tree.command(name="addtrade", description="Add a forex trade")
@discord.app_commands.describe(
    pair="Currency pair (e.g., EURUSD)",
    direction="Trade direction: buy or sell",
    entry="Entry price",
    tp="Take profit",
    sl="Stop loss",
    lot_size="Optional lot size"
)
async def addtrade(interaction: discord.Interaction, pair: str, direction: str, entry: float, tp: float, sl: float, lot_size: float = None):
    await add_trade(interaction.user.id, pair, direction, entry, tp, sl, lot_size)
    price = get_price(pair)

    if price is None:
        await interaction.response.send_message(f"✅ Trade for `{pair.upper()}` added (but current price not available).")
        return

    pips_to_tp = calculate_pips(entry, tp, pair)
    pips_to_sl = calculate_pips(entry, sl, pair)
    rr_ratio = round(pips_to_tp / pips_to_sl, 2) if pips_to_sl else "∞"

    msg = (
        f"✅ Trade for `{pair.upper()}` added!\n"
        f"📈 Current Price: `{price}`\n"
        f"🎯 TP is `{calculate_pips(price, tp, pair)}` pips away | 🛑 SL is `{calculate_pips(price, sl, pair)}` pips away\n"
        f"📊 Risk:Reward = **{rr_ratio}**"
    )

    if lot_size:
        reward = pips_to_tp * lot_size * 10
        risk = pips_to_sl * lot_size * 10
        msg += f"\n💰 Risk: `${risk:.2f}` | Reward: `${reward:.2f}`"

    await interaction.response.send_message(msg)

@bot.tree.command(name="listtrades", description="View all your active trades")
async def listtrades(interaction: discord.Interaction):
    conn = await asyncpg.connect(DATABASE_URL)
    trades = await conn.fetch('SELECT id, pair, direction, entry, tp, sl, lot_size FROM trades WHERE user_id = $1', interaction.user.id)
    await conn.close()

    if not trades:
        await interaction.response.send_message("ℹ️ You have no active trades.")
        return

    msg = "**📋 Your Active Trades:**\n"
    for row in trades:
        rowid, pair, direction, entry, tp, sl, lot_size = row.values()
        price = get_price(pair)
        if not price:
            msg += f"❌ Could not fetch price for `{pair}`\n"
            continue

        pips_to_tp = calculate_pips(price, tp, pair)
        pips_to_sl = calculate_pips(price, sl, pair)

        if direction == "buy":
            pips = calculate_pips(price, entry, pair)
            profit = price >= entry
        else:
            pips = calculate_pips(entry, price, pair)
            profit = price <= entry

        msg += f"**ID:** `{rowid}` | `{pair}` {direction.upper()} from `{entry}` ➡️ TP: `{tp}`, SL: `{sl}`\n"
        msg += f"📈 Price: `{price}` | 🎯 TP in `{pips_to_tp}` | 🛑 SL in `{pips_to_sl}`\n"
        msg += f"💹 {'+' if profit else '-'}{pips} pips"

        if lot_size:
            dollars = pips * lot_size * 10
            msg += f" | 💵 {'+' if profit else '-'}${abs(dollars):.2f}"

        msg += "\n\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="profitcheck", description="Check real-time profit/loss of all trades")
async def profitcheck(interaction: discord.Interaction):
    conn = await asyncpg.connect(DATABASE_URL)
    trades = await conn.fetch('SELECT pair, direction, entry, tp, sl, lot_size FROM trades WHERE user_id = $1', interaction.user.id)
    await conn.close()

    if not trades:
        await interaction.response.send_message("You have no active trades.")
        return

    msg = "**📈 Live Profit Check:**\n"
    for row in trades:
        pair, direction, entry, tp, sl, lot_size = row.values()
        price = get_price(pair)
        if not price:
            msg += f"❌ Could not fetch price for `{pair}`\n"
            continue

        if direction == "buy":
            pips = calculate_pips(price, entry, pair)
            profit = price >= entry
        else:
            pips = calculate_pips(entry, price, pair)
            profit = price <= entry

        msg += f"`{pair}` {direction.upper()} | 📈 `{price}` | {'+' if profit else '-'}{pips} pips"
        if lot_size:
            dollars = pips * lot_size * 10
            msg += f" | 💵 {'+' if profit else '-'}${abs(dollars):.2f}"
        msg += "\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="tradehistory", description="View closed trade history")
async def tradehistory(interaction: discord.Interaction):
    conn = await asyncpg.connect(DATABASE_URL)
    trades = await conn.fetch(
        'SELECT pair, direction, entry, exit_price, result, profit FROM closed_trades WHERE user_id = $1 ORDER BY id DESC LIMIT 5',
        interaction.user.id
    )
    await conn.close()

    if not trades:
        await interaction.response.send_message("You have no closed trades.")
        return

    msg = "**📘 Recent Closed Trades:**\n"
    for row in trades:
        pair, direction, entry, exit_price, result, profit = row.values()
        msg += (
            f"`{pair}` {direction.upper()} | Entry: `{entry}` → Exit: `{exit_price}`\n"
            f"Result: **{result}** | 💵 {'+' if profit > 0 else ''}${profit:.2f}\n\n"
        )

    await interaction.response.send_message(msg)

# === BACKGROUND TASK ===
@tasks.loop(seconds=60)
async def check_trades():
    conn = await asyncpg.connect(DATABASE_URL)
    trades = await conn.fetch('SELECT id, user_id, pair, direction, entry, tp, sl, lot_size FROM trades')
    for row in trades:
        rowid, user_id, pair, direction, entry, tp, sl, lot_size = row.values()
        price = get_price(pair)
        if price is None:
            continue

        hit_tp = hit_sl = False
        if direction == "buy":
            hit_tp = price >= tp
            hit_sl = price <= sl
        elif direction == "sell":
            hit_tp = price <= tp
            hit_sl = price >= sl

        if hit_tp or hit_sl:
            result = "TP" if hit_tp else "SL"
            pips = calculate_pips(entry, price, pair)
            profit = pips * lot_size * 10 if lot_size else 0

            await log_closed_trade(user_id, pair, direction, entry, price, tp, sl, lot_size, result, profit)
            user = await bot.fetch_user(user_id)
            await user.send(f"🎯 Your `{pair}` trade hit **{result}** at `{price}`\n💰 P/L: `${profit:.2f}`")
            await delete_trade(rowid)

    await conn.close()

# === BOT READY ===
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    check_trades.start()
    print(f"✅ Bot is online as {bot.user}")

bot.run(DISCORD_TOKEN)
