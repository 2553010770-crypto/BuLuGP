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

# Kết nối MongoDB (Giữ kết nối sync nhưng sẽ chạy trong executor)
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    mongo_client.admin.command('ping')
    db = mongo_client[DB_NAME]
    users_col = db[COLLECTION_NAME]
    print("✅ Connected to MongoDB!")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    exit()

# --- CONFIG & CACHE ---
# Cache giá BTC để tránh bị API ban
btc_cache = {
    "price": 95000.0,  # Giá mặc định ban đầu
    "last_updated": 0,
    "ttl": 60  # Thời gian sống của cache (giây)
}

# --- ASYNC DATABASE WRAPPERS (Tối ưu Non-blocking) ---
# Hàm này giúp chạy code đồng bộ (pymongo) trong luồng riêng để không chặn bot
async def run_db_task(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))

# Các hàm DB nguyên bản (Sync)
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

# --- OPTIMIZED FUNCTIONS ---

async def get_btc_price():
    """
    Thuật toán Caching:
    Kiểm tra xem dữ liệu cũ còn 'tươi' không. Nếu < 60s thì dùng lại.
    Nếu cũ, gọi Binance API (nhanh hơn CoinGecko).
    """
    current_time = time.time()
    
    # Nếu cache còn hạn, trả về ngay lập tức (Tối ưu tốc độ)
    if current_time - btc_cache["last_updated"] < btc_cache["ttl"]:
        return btc_cache["price"]

    # Binance API (Nhẹ và ít bị rate limit hơn)
    url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    new_price = float(data["price"])
                    
                    # Cập nhật Cache
                    btc_cache["price"] = new_price
                    btc_cache["last_updated"] = current_time
                    return new_price
                else:
                    print(f"API Error: {response.status}")
    except Exception as e:
        print(f"Fetch Error: {e}")
    
    # Nếu lỗi, trả về giá cũ trong cache
    return btc_cache["price"]

def load_questions():
    if not os.path.exists("questions.json"):
        # Tạo file mẫu nếu chưa có
        sample = [{"question": "1 + 1 = ?", "answer": "2", "image_url": None}]
        with open("questions.json", "w", encoding="utf-8") as f: json.dump(sample, f)
        return sample
    try:
        with open("questions.json", "r", encoding="utf-8") as f: return json.load(f)
    except: return []

questions_bank = load_questions()

# Hỗ trợ đa luồng game (Mỗi kênh một game riêng)
active_games = {} 

# --- DISCORD COMPONENTS ---

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
        await interaction.response.defer(ephemeral=True) # Defer để tránh timeout khi gọi DB
        
        user_id = str(interaction.user.id)
        # Gọi DB qua wrapper async
        user_data = await run_db_task(_get_user_data_sync, user_id)
        
        try:
            amount = float(self.amount_input.value)
            if amount <= 0: raise ValueError
            
            msg = ""
            if self.action == "BUY":
                # Logic Mua: Input là USD muốn tiêu
                if user_data["balance"] < amount:
                    await interaction.followup.send("❌ Không đủ tiền trong ví USD.", ephemeral=True)
                    return
                
                btc_received = amount / self.price
                await run_db_task(_update_user_balance_sync, user_id, balance_change=-amount, btc_change=btc_received)
                msg = f"✅ Đã mua **{btc_received:.6f} BTC** với giá ${amount:,.2f}."
                
            else: # SELL
                # Logic Bán: Input là số BTC muốn bán
                if user_data["btc"] < amount:
                    await interaction.followup.send("❌ Không đủ BTC để bán.", ephemeral=True)
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
        # Lấy lại giá mới nhất (từ cache hoặc api) để đảm bảo tính công bằng
        self.current_price = await get_btc_price()
        await interaction.response.send_modal(TransactionModal("BUY", self.current_price))

    @discord.ui.button(label="BÁN (BTC)", style=discord.ButtonStyle.red, emoji="📉")
    async def sell_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_price = await get_btc_price()
        await interaction.response.send_modal(TransactionModal("SELL", self.current_price))

    @discord.ui.button(label="Cập nhật giá", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.current_price = await get_btc_price()
        user = await run_db_task(_get_user_data_sync, interaction.user.id)
        
        embed = discord.Embed(title="📊 SÀN GIAO DỊCH BTC", description=f"Giá hiện tại: **${self.current_price:,.2f}**", color=0xF7931A)
        embed.add_field(name="Tài sản của bạn", value=f"💵 ${user['balance']:,.2f}\n🪙 {user['btc']:.6f} BTC")
        embed.set_footer(text="Dữ liệu từ Binance • Cập nhật mỗi 60s")
        
        await interaction.edit_original_response(embed=embed, view=self)

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'🤖 Bot Online: {bot.user}')
    await bot.tree.sync()

# --- GAME LOGIC (Optimized Loop) ---
async def game_loop(channel):
    channel_id = channel.id
    
    # Khởi tạo trạng thái game cho kênh này
    active_games[channel_id] = {
        "active": True,
        "fails": 0,
        "history": []
    }
    
    while active_games.get(channel_id, {}).get("active"):
        if not questions_bank:
            await channel.send("⚠️ Ngân hàng câu hỏi đang trống.")
            break

        # Thuật toán chọn câu hỏi: Tránh lặp lại 20 câu gần nhất
        # Tạo danh sách các index chưa được hỏi gần đây
        recent = active_games[channel_id]["history"]
        available_indices = [i for i in range(len(questions_bank)) if i not in recent]
        
        if not available_indices: # Nếu đã hỏi hết, reset lịch sử
            recent.clear()
            available_indices = list(range(len(questions_bank)))
            active_games[channel_id]["history"] = []

        idx = random.choice(available_indices)
        
        # Cập nhật lịch sử (Dùng hàng đợi FIFO)
        active_games[channel_id]["history"].append(idx)
        if len(active_games[channel_id]["history"]) > 20:
            active_games[channel_id]["history"].pop(0)

        q_data = questions_bank[idx]
        correct_answer = q_data["answer"].lower().strip()
        
        # Gửi câu hỏi
        wait_time = 20
        end_timestamp = int(time.time() + wait_time)
        embed = discord.Embed(title="🎯 TRIVIA TIME!", description=f"**{q_data['question']}**", color=0xD4AF37)
        if q_data.get("image_url"): embed.set_image(url=q_data["image_url"])
        embed.add_field(name="Thời gian", value=f"⏳ Hết giờ <t:{end_timestamp}:R>")
        
        await channel.send(embed=embed)

        def check(m):
            return m.channel.id == channel_id and not m.author.bot

        try:
            msg = await bot.wait_for('message', check=check, timeout=wait_time)
            user_ans = msg.content.lower().strip()

            if user_ans == correct_answer:
                # Cộng tiền (Async DB)
                bonus = 36
                await run_db_task(_update_user_balance_sync, msg.author.id, balance_change=bonus)
                
                await channel.send(f"✅ **Chính xác!** <@{msg.author.id}> nhận được ${bonus}.")
                active_games[channel_id]["fails"] = 0 # Reset fail counter
                await asyncio.sleep(2) # Nghỉ ngắn trước câu tiếp
            else:
                await channel.send(f"❌ Sai rồi! Đáp án đúng là: **{q_data['answer']}**")
                active_games[channel_id]["fails"] += 1

        except asyncio.TimeoutError:
            await channel.send(f"⏰ Hết giờ! Đáp án là: **{q_data['answer']}**")
            active_games[channel_id]["fails"] += 1

        # Điều kiện thua
        if active_games[channel_id]["fails"] >= 5:
            await channel.send("🛑 **Game Over!** (Sai liên tiếp 5 câu).")
            active_games[channel_id]["active"] = False
        
        await asyncio.sleep(3) # Delay giữa các câu hỏi

    # Cleanup khi game over
    active_games.pop(channel_id, None)

# --- COMMANDS ---

@bot.tree.command(name="startgp", description="Bắt đầu game đố vui")
async def startgp(interaction: discord.Interaction):
    if interaction.channel_id in active_games:
        return await interaction.response.send_message("Game đang chạy ở kênh này rồi!", ephemeral=True)
    
    if not questions_bank:
        return await interaction.response.send_message("File câu hỏi chưa có dữ liệu.", ephemeral=True)

    await interaction.response.send_message("🎮 **Bắt đầu Game Trivia!** Chuẩn bị nhé...")
    # Chạy game loop như một task nền
    bot.loop.create_task(game_loop(interaction.channel))

@bot.tree.command(name="stopgp", description="Dừng game đố vui")
async def stopgp(interaction: discord.Interaction):
    if interaction.channel_id in active_games:
        active_games[interaction.channel_id]["active"] = False
        await interaction.response.send_message("🛑 Đã gửi lệnh dừng game.", ephemeral=True)
    else:
        await interaction.response.send_message("Không có game nào đang chạy ở đây.", ephemeral=True)

@bot.tree.command(name="bitcoin", description="Xem giá và giao dịch Bitcoin")
async def bitcoin_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    price = await get_btc_price()
    user = await run_db_task(_get_user_data_sync, interaction.user.id)
    
    view = CryptoView(current_price=price)
    embed = discord.Embed(title="📊 SÀN GIAO DỊCH BTC", description=f"Giá: **${price:,.2f}**", color=0xF7931A)
    embed.add_field(name="Ví của bạn", value=f"💵 ${user['balance']:,.2f}\n🪙 {user['btc']:.6f} BTC")
    embed.set_footer(text="Dữ liệu từ Binance")
    
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="rank", description="Xem bảng xếp hạng tài sản")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        price = await get_btc_price()
        # Lấy data DB Async
        all_users = await run_db_task(_get_all_users_sync)
        
        if not all_users:
            await interaction.followup.send("Chưa có dữ liệu người dùng.")
            return

        # Tính tổng tài sản (Net Worth)
        ranked = []
        for user in all_users:
            uid = user["_id"]
            bal = float(user.get("balance", 0.0))
            btc = float(user.get("btc", 0.0))
            net_worth = bal + (btc * price)
            ranked.append((uid, net_worth, btc))

        # Sắp xếp (Sort Algorithm: Timsort của Python rất nhanh)
        ranked.sort(key=lambda x: x[1], reverse=True)
        
        desc = ""
        top_10 = ranked[:10]
        for idx, (uid, nw, btc) in enumerate(top_10, 1):
            medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"#{idx}"
            desc += f"{medal} <@{uid}>\n   💰 **${nw:,.0f}** (Hold: {btc:.4f} BTC)\n"
            
        embed = discord.Embed(title="🏆 BẢNG XẾP HẠNG ĐẠI GIA", description=desc, color=0xD4AF37)
        embed.set_footer(text=f"Quy đổi theo giá BTC: ${price:,.0f}")
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        await interaction.followup.send(f"Lỗi khi lấy bảng xếp hạng: {e}")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Thiếu BOT_TOKEN trong file .env")
    else:
        bot.run(BOT_TOKEN)
