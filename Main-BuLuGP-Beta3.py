import discord
from discord.ext import commands
from discord import app_commands
import json
import random
import asyncio
import aiohttp
import os
import pymongo
from pymongo.errors import ConnectionFailure
from dotenv import load_dotenv
import time
from functools import partial

# Load biến môi trường
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# --- DATABASE SETUP ---
DB_NAME = "DiscordBotDB"
COLLECTION_NAME = "users"

try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    mongo_client.admin.command('ping')
    db = mongo_client[DB_NAME]
    users_col = db[COLLECTION_NAME]
    print("✅ Connected to MongoDB!")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    exit()

# --- CACHE & CONFIG ---
btc_cache = {
    "price": 95000.0,
    "last_updated": 0,
    "ttl": 60 
}

# --- ASYNC DB WRAPPER ---
async def run_db_task(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))

def _get_user_data_sync(user_id):
    user_id = str(user_id)
    user = users_col.find_one({"_id": user_id})
    if not user:
        new_user = {"_id": user_id, "balance": 0.0, "btc": 0.0}
        users_col.insert_one(new_user)
        return new_user
    return user

def _update_user_balance_sync(user_id, balance_change=0, btc_change=0):
    users_col.update_one(
        {"_id": str(user_id)},
        {"$inc": {"balance": balance_change, "btc": btc_change}},
        upsert=True
    )

def _get_all_users_sync():
    return list(users_col.find())

# --- OPTIMIZED FUNCTIONS (SỬA LỖI BTC) ---

async def fetch_url(session, url):
    async with session.get(url, timeout=5) as response:
        if response.status == 200:
            return await response.json()
    return None

async def get_btc_price():
    """
    Sửa lỗi: Thử nhiều nguồn (Binance -> CoinGecko -> CoinDesk)
    """
    current_time = time.time()
    if current_time - btc_cache["last_updated"] < btc_cache["ttl"]:
        return btc_cache["price"]

    price = None
    async with aiohttp.ClientSession() as session:
        # 1. Thử Binance
        try:
            data = await fetch_url(session, "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            if data: price = float(data["price"])
        except: pass

        # 2. Nếu lỗi, thử CoinGecko
        if price is None:
            try:
                data = await fetch_url(session, "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
                if data: price = float(data["bitcoin"]["usd"])
            except: pass

        # 3. Nếu vẫn lỗi, thử CoinDesk (Rất ổn định)
        if price is None:
            try:
                data = await fetch_url(session, "https://api.coindesk.com/v1/bpi/currentprice/USD.json")
                if data: price = float(data["bpi"]["USD"]["rate_float"])
            except: pass

    if price:
        btc_cache["price"] = price
        btc_cache["last_updated"] = current_time
        return price
    
    return btc_cache["price"] # Trả về giá cũ nếu tất cả đều lỗi

def load_questions():
    if not os.path.exists("questions.json"):
        sample = [{"question": "1 + 1 = ?", "answer": "2", "image_url": None}]
        with open("questions.json", "w", encoding="utf-8") as f: json.dump(sample, f)
        return sample
    try:
        with open("questions.json", "r", encoding="utf-8") as f: return json.load(f)
    except: return []

questions_bank = load_questions()
active_games = {} 

# --- DISCORD COMPONENTS ---
# (Giữ nguyên TransactionModal và CryptoView như cũ)
class TransactionModal(discord.ui.Modal):
    def __init__(self, action, current_price):
        super().__init__(title=f"{action} Bitcoin")
        self.action = action
        self.price = current_price
        self.amount_input = discord.ui.TextInput(
            label=f"Nhập số lượng {'USD' if action == 'BUY' else 'BTC'}",
            placeholder=f"Giá hiện tại: ${current_price:,.0f}",
            required=True
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        user_data = await run_db_task(_get_user_data_sync, user_id)
        try:
            amount = float(self.amount_input.value)
            if amount <= 0: raise ValueError
            msg = ""
            if self.action == "BUY":
                if user_data["balance"] < amount:
                    await interaction.followup.send("❌ Không đủ tiền USD.", ephemeral=True)
                    return
                btc_received = amount / self.price
                await run_db_task(_update_user_balance_sync, user_id, balance_change=-amount, btc_change=btc_received)
                msg = f"✅ Đã mua **{btc_received:.6f} BTC** với giá ${amount:,.2f}."
            else:
                if user_data["btc"] < amount:
                    await interaction.followup.send("❌ Không đủ BTC.", ephemeral=True)
                    return
                usd_received = amount * self.price
                await run_db_task(_update_user_balance_sync, user_id, balance_change=usd_received, btc_change=-amount)
                msg = f"📉 Đã bán **{amount:.6f} BTC** thu về ${usd_received:,.2f}."
            await interaction.followup.send(msg, ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ Số nhập vào không hợp lệ.", ephemeral=True)

class CryptoView(discord.ui.View):
    def __init__(self, current_price):
        super().__init__(timeout=60)
        self.current_price = current_price

    @discord.ui.button(label="MUA (USD)", style=discord.ButtonStyle.green, emoji="📈")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_price = await get_btc_price()
        await interaction.response.send_modal(TransactionModal("BUY", self.current_price))

    @discord.ui.button(label="BÁN (BTC)", style=discord.ButtonStyle.red, emoji="📉")
    async def sell_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_price = await get_btc_price()
        await interaction.response.send_modal(TransactionModal("SELL", self.current_price))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.current_price = await get_btc_price()
        user = await run_db_task(_get_user_data_sync, interaction.user.id)
        embed = discord.Embed(title="📊 SÀN BTC", description=f"Giá: **${self.current_price:,.2f}**", color=0xF7931A)
        embed.add_field(name="Ví bạn", value=f"💵 ${user['balance']:,.2f}\n🪙 {user['btc']:.6f} BTC")
        await interaction.edit_original_response(embed=embed, view=self)

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'🤖 Bot Online: {bot.user}')
    await bot.tree.sync()

# --- GAME LOGIC (ĐÃ CẬP NHẬT) ---
async def game_loop(channel):
    channel_id = channel.id
    active_games[channel_id] = {"active": True, "fails": 0, "history": []}
    
    while active_games.get(channel_id, {}).get("active"):
        if not questions_bank:
            await channel.send("⚠️ Hết câu hỏi.")
            break

        # Chọn câu hỏi (Logic cũ)
        recent = active_games[channel_id]["history"]
        available = [i for i in range(len(questions_bank)) if i not in recent]
        if not available:
            recent.clear()
            available = list(range(len(questions_bank)))
            active_games[channel_id]["history"] = []

        idx = random.choice(available)
        active_games[channel_id]["history"].append(idx)
        if len(active_games[channel_id]["history"]) > 20: active_games[channel_id]["history"].pop(0)

        q_data = questions_bank[idx]
        correct_answer = q_data["answer"].lower().strip()
        
        # --- THAY ĐỔI: Giảm thời gian còn 15s ---
        wait_time = 15 
        end_time = time.time() + wait_time
        
        embed = discord.Embed(title="🎯 TRIVIA!", description=f"**{q_data['question']}**", color=0xD4AF37)
        if q_data.get("image_url"): embed.set_image(url=q_data["image_url"])
        embed.add_field(name="Thời gian", value=f"⏳ <t:{int(end_time)}:R>")
        await channel.send(embed=embed)

        # --- THAY ĐỔI: Cho phép trả lời sai nhiều lần ---
        winner = None
        
        while time.time() < end_time:
            remaining = end_time - time.time()
            if remaining <= 0: break

            try:
                def check(m): return m.channel.id == channel_id and not m.author.bot
                
                msg = await bot.wait_for('message', check=check, timeout=remaining)
                user_ans = msg.content.lower().strip()

                if user_ans == correct_answer:
                    winner = msg.author
                    break # Thoát vòng lặp trả lời ngay
                else:
                    # Nếu sai, thả react X và tiếp tục vòng lặp
                    try: await msg.add_reaction("❌")
                    except: pass
            
            except asyncio.TimeoutError:
                break
        
        # Xử lý kết quả sau khi vòng lặp kết thúc
        if winner:
            bonus = 36
            await run_db_task(_update_user_balance_sync, winner.id, balance_change=bonus)
            await channel.send(f"✅ **Chính xác!** <@{winner.id}> +${bonus}.")
            active_games[channel_id]["fails"] = 0
            await asyncio.sleep(2)
        else:
            await channel.send(f"⏰ Hết giờ! Đáp án: **{q_data['answer']}**")
            active_games[channel_id]["fails"] += 1

        if active_games[channel_id]["fails"] >= 5:
            await channel.send("🛑 Game Over (5 câu sai liên tiếp).")
            active_games[channel_id]["active"] = False
        
        await asyncio.sleep(3)

    active_games.pop(channel_id, None)

# --- COMMANDS ---

@bot.tree.command(name="startgp", description="Bắt đầu game")
async def startgp(interaction: discord.Interaction):
    if interaction.channel_id in active_games:
        return await interaction.response.send_message("Game đang chạy!", ephemeral=True)
    if not questions_bank:
        return await interaction.response.send_message("File câu hỏi trống.", ephemeral=True)
    await interaction.response.send_message("🎮 **Bắt đầu!**")
    bot.loop.create_task(game_loop(interaction.channel))

@bot.tree.command(name="stopgp", description="Dừng game")
async def stopgp(interaction: discord.Interaction):
    if interaction.channel_id in active_games:
        active_games[interaction.channel_id]["active"] = False
        await interaction.response.send_message("🛑 Đang dừng game...", ephemeral=True)
    else:
        await interaction.response.send_message("Không có game nào.", ephemeral=True)

# --- THAY ĐỔI: Lệnh Reload ---
@bot.tree.command(name="reload_qs", description="Tải lại bộ câu hỏi từ file")
async def reload_qs(interaction: discord.Interaction):
    global questions_bank
    questions_bank = load_questions()
    await interaction.response.send_message(f"✅ Đã tải lại! Hiện có **{len(questions_bank)}** câu hỏi.", ephemeral=True)

@bot.tree.command(name="bitcoin", description="Xem giá BTC")
async def bitcoin_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    price = await get_btc_price()
    user = await run_db_task(_get_user_data_sync, interaction.user.id)
    view = CryptoView(current_price=price)
    embed = discord.Embed(title="📊 SÀN BTC", description=f"Giá: **${price:,.2f}**", color=0xF7931A)
    embed.add_field(name="Ví bạn", value=f"💵 ${user['balance']:,.2f}\n🪙 {user['btc']:.6f} BTC")
    embed.set_footer(text="Nguồn: Binance / CoinGecko / CoinDesk")
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="rank", description="Bảng xếp hạng")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        price = await get_btc_price()
        all_users = await run_db_task(_get_all_users_sync)
        if not all_users: return await interaction.followup.send("Data trống.")
        
        ranked = []
        for user in all_users:
            nw = user.get("balance", 0) + (user.get("btc", 0) * price)
            ranked.append((user["_id"], nw))
        ranked.sort(key=lambda x: x[1], reverse=True)
        
        desc = ""
        for idx, (uid, nw) in enumerate(ranked[:10], 1):
            medal = "🥇" if idx==1 else "🥈" if idx==2 else "🥉" if idx==3 else f"#{idx}"
            desc += f"{medal} <@{uid}>: ${nw:,.0f}\n"
            
        embed = discord.Embed(title="🏆 TOP SERVER", description=desc, color=0xD4AF37)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"Lỗi: {e}")

if __name__ == "__main__":
    if not BOT_TOKEN: print("Missing Token")
    else: bot.run(BOT_TOKEN)
