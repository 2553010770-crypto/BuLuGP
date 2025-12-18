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
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- CẤU HÌNH ---
# Load biến môi trường
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# Cấu hình thời gian trả lời câu hỏi (giây)
WAIT_TIME = 20 

# --- PHẦN FIX LỖI RENDER (QUAN TRỌNG) ---
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"🌍 Web server started on port {port}")
    server.serve_forever()

def keep_alive():
    t = threading.Thread(target=start_web_server)
    t.daemon = True
    t.start()
# ----------------------------------------

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
    # Không exit để test local nếu không có DB, nhưng nên có DB
    pass

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

# --- HELPER FUNCTIONS ---

async def fetch_url(session, url):
    async with session.get(url, timeout=5) as response:
        if response.status == 200:
            return await response.json()
    return None

async def get_btc_price():
    current_time = time.time()
    if current_time - btc_cache["last_updated"] < btc_cache["ttl"]:
        return btc_cache["price"]

    price = None
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_url(session, "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            if data: price = float(data["price"])
        except: pass

        if price is None:
            try:
                data = await fetch_url(session, "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
                if data: price = float(data["bitcoin"]["usd"])
            except: pass

    if price:
        btc_cache["price"] = price
        btc_cache["last_updated"] = current_time
        return price
    
    return btc_cache["price"] 

def load_questions():
    if not os.path.exists("questions.json"):
        # Mẫu json có ảnh
        sample = [
            {"question": "1 + 1 = ?", "answer": "2", "image_url": None},
            {"question": "Đây là con gì?", "answer": "Mèo", "image_url": "https://i.imgur.com/example_cat.jpg"}
        ]
        with open("questions.json", "w", encoding="utf-8") as f: json.dump(sample, f)
        return sample
    try:
        with open("questions.json", "r", encoding="utf-8") as f: return json.load(f)
    except: return []

questions_bank = load_questions()
active_games = {} 

# --- VIEW: IMAGE GALLERY (MỚI) ---
class GalleryView(discord.ui.View):
    def __init__(self, questions_with_images):
        super().__init__(timeout=120)
        self.data = questions_with_images
        self.index = 0
        self.update_buttons()

    def update_buttons(self):
        # Vô hiệu hóa nút lùi nếu ở trang đầu
        self.prev_btn.disabled = (self.index == 0)
        # Vô hiệu hóa nút tiến nếu ở trang cuối
        self.next_btn.disabled = (self.index == len(self.data) - 1)

    def get_embed(self):
        q = self.data[self.index]
        embed = discord.Embed(title=f"🖼️ Thư viện ảnh ({self.index + 1}/{len(self.data)})", color=discord.Color.blue())
        embed.description = f"**Câu hỏi:** {q['question']}\n**Đáp án:** ||{q['answer']}||"
        embed.set_image(url=q['image_url'])
        return embed

    @discord.ui.button(label="⬅️ Trước", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Tiếp ➡️", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

# --- VIEW: BITCOIN TRANSACTION ---
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

# --- GAME LOGIC (ĐÃ SỬA THỜI GIAN) ---
async def game_loop(channel):
    channel_id = channel.id
    active_games[channel_id] = {"active": True, "fails": 0, "history": []}
    
    while active_games.get(channel_id, {}).get("active"):
        if not questions_bank:
            await channel.send("⚠️ Hết câu hỏi.")
            break

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
        
        # SỬ DỤNG BIẾN WAIT_TIME ĐỂ DỄ QUẢN LÝ
        end_time = time.time() + WAIT_TIME
        
        embed = discord.Embed(title="🎯 TRIVIA!", description=f"**{q_data['question']}**", color=0xD4AF37)
        if q_data.get("image_url"): embed.set_image(url=q_data["image_url"])
        
        # Hiển thị thời gian đếm ngược đẹp hơn
        embed.add_field(name="Thời gian", value=f"⏳ <t:{int(end_time)}:R> ({WAIT_TIME}s)")
        
        await channel.send(embed=embed)

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
                    break 
                else:
                    try: await msg.add_reaction("❌")
                    except: pass
            
            except asyncio.TimeoutError:
                break
        
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

@bot.tree.command(name="reload_qs", description="Tải lại bộ câu hỏi từ file")
async def reload_qs(interaction: discord.Interaction):
    global questions_bank
    questions_bank = load_questions()
    await interaction.response.send_message(f"✅ Đã tải lại! Hiện có **{len(questions_bank)}** câu hỏi.", ephemeral=True)

# LỆNH MỚI: GALLERY
@bot.tree.command(name="gallery", description="Xem tất cả ảnh trong bộ câu hỏi")
async def gallery(interaction: discord.Interaction):
    # Lọc ra các câu hỏi có chứa ảnh
    questions_with_images = [q for q in questions_bank if q.get("image_url") and q["image_url"].strip()]
    
    if not questions_with_images:
        await interaction.response.send_message("❌ Không có câu hỏi nào chứa ảnh trong dữ liệu.", ephemeral=True)
        return
    
    view = GalleryView(questions_with_images)
    await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)

@bot.tree.command(name="bitcoin", description="Xem giá BTC")
async def bitcoin_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    price = await get_btc_price()
    user = await run_db_task(_get_user_data_sync, interaction.user.id)
    view = CryptoView(current_price=price)
    embed = discord.Embed(title="📊 SÀN BTC", description=f"Giá: **${price:,.2f}**", color=0xF7931A)
    embed.add_field(name="Ví bạn", value=f"💵 ${user['balance']:,.2f}\n🪙 {user['btc']:.6f} BTC")
    embed.set_footer(text="Nguồn: Binance / CoinGecko")
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
    if not BOT_TOKEN: 
        print("Missing Token")
    else: 
        keep_alive() 
        bot.run(BOT_TOKEN)
