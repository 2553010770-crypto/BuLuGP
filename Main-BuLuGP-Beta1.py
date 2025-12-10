import discord
from discord.ext import commands
from discord import app_commands
import json
import random
import asyncio
import aiohttp
import os
import math
import pymongo
from pymongo.errors import ConnectionFailure, OperationFailure
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

if not BOT_TOKEN or not MONGO_URI:
    print("Missing environment variables. Check .env file.")
    exit()

DB_NAME = "DiscordBotDB"
COLLECTION_NAME = "users"

print("Connecting to Database...")
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    mongo_client.admin.command('ping')
    db = mongo_client[DB_NAME]
    users_col = db[COLLECTION_NAME]
    print("Connected to MongoDB!")
except Exception as e:
    print(f"MongoDB Error: {e}")
    exit()

def get_user_data(user_id):
    user_id = str(user_id)
    user = users_col.find_one({"_id": user_id})
    if not user:
        new_user = {"_id": user_id, "balance": 0.0, "btc": 0.0}
        users_col.insert_one(new_user)
        return new_user
    return user

def update_user_balance(user_id, balance_change=0, btc_change=0):
    users_col.update_one(
        {"_id": str(user_id)},
        {"$inc": {"balance": balance_change, "btc": btc_change}},
        upsert=True
    )

async def get_btc_price():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return float(data["bitcoin"]["usd"])
    except:
        pass
    return 95000.0

def create_time_bar(seconds_left, total_seconds=20):
    percent = seconds_left / total_seconds
    num_blocks = 10
    filled_blocks = math.ceil(percent * num_blocks)
    emoji = "🟩" if percent > 0.5 else "🟨" if percent > 0.2 else "🟥"
    bar = emoji * filled_blocks + "⬜" * (num_blocks - filled_blocks)
    return f"{bar} ({int(seconds_left)}s)"

def load_questions():
    if not os.path.exists("questions.json"):
        with open("questions.json", "w", encoding="utf-8") as f: json.dump([], f)
        return []
    try:
        with open("questions.json", "r", encoding="utf-8") as f: return json.load(f)
    except: return []

questions_bank = load_questions()
game_data = {
    "is_active": False,
    "channel_id": None,
    "current_q": None,
    "recent_indices": [],
    "consecutive_fails": 0
}

class TransactionModal(discord.ui.Modal):
    def __init__(self, action, current_price):
        super().__init__(title=f"{action} Bitcoin")
        self.action = action
        self.price = current_price
        self.amount_input = discord.ui.TextInput(
            label=f"Nhập số lượng {'Điểm' if action == 'BUY' else 'BTC'}",
            required=True
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        user_data = get_user_data(user_id)
        try:
            amount = float(self.amount_input.value)
            if amount <= 0: raise ValueError
            
            msg = ""
            if self.action == "BUY":
                if user_data["balance"] < amount:
                    await interaction.response.send_message("❌ Không đủ tiền.", ephemeral=True)
                    return
                btc_received = amount / self.price
                update_user_balance(user_id, balance_change=-amount, btc_change=btc_received)
                msg = f"✅ Mua **{btc_received:.6f} BTC** (-{amount} $)."
            else:
                if user_data["btc"] < amount:
                    await interaction.response.send_message("❌ Không đủ BTC.", ephemeral=True)
                    return
                points = amount * self.price
                update_user_balance(user_id, balance_change=points, btc_change=-amount)
                msg = f"📉 Bán **{amount} BTC** (+{points:.2f} $)."
            
            await interaction.response.send_message(msg, ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Số nhập không hợp lệ.", ephemeral=True)

class CryptoView(discord.ui.View):
    def __init__(self, current_price):
        super().__init__(timeout=60)
        self.current_price = current_price
        self.message = None

    async def on_timeout(self):
        if self.message:
            try: await self.message.delete()
            except: pass

    @discord.ui.button(label="MUA", style=discord.ButtonStyle.green, emoji="📈")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_price = await get_btc_price()
        await interaction.response.send_modal(TransactionModal("BUY", self.current_price))

    @discord.ui.button(label="BÁN", style=discord.ButtonStyle.red, emoji="📉")
    async def sell_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_price = await get_btc_price()
        await interaction.response.send_modal(TransactionModal("SELL", self.current_price))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_price = await get_btc_price()
        user = get_user_data(interaction.user.id)
        embed = discord.Embed(title="📊 SÀN BTC", description=f"Giá: **${self.current_price:,.2f}**", color=0xF7931A)
        embed.add_field(name="Ví bạn", value=f"💵 {user['balance']:,.2f}\n🪙 {user['btc']:.6f}")
        await interaction.response.edit_message(embed=embed, view=self)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'🤖 Bot Online: {bot.user}')
    await bot.tree.sync()

async def game_loop(channel):
    game_data["consecutive_fails"] = 0
    
    while game_data["is_active"]:
        if not questions_bank:
            await channel.send("Hết câu hỏi.")
            game_data["is_active"] = False
            break

        all_indices = list(range(len(questions_bank)))
        available = [i for i in all_indices if i not in game_data["recent_indices"]]
        if not available:
            game_data["recent_indices"] = []
            available = all_indices
        
        idx = random.choice(available)
        game_data["recent_indices"].append(idx)
        if len(game_data["recent_indices"]) > 30: game_data["recent_indices"].pop(0)

        q_data = questions_bank[idx]
        total_time = 20
        
        embed = discord.Embed(title=f"Question #{idx+1}", description=f"**{q_data['question']}**", color=0xD4AF37)
        if q_data.get("image_url"): embed.set_image(url=q_data["image_url"])
        embed.set_footer(text=f"⏳ {create_time_bar(total_time, total_time)}")
        
        game_msg = await channel.send(embed=embed)
        correct_answer = q_data["answer"].lower().strip()
        
        round_event = asyncio.Event()
        winner_msg = None

        async def timer_task():
            nonlocal total_time
            time_left = total_time
            while time_left > 0 and not round_event.is_set():
                await asyncio.sleep(2)
                time_left -= 2
                if round_event.is_set(): break
                try:
                    embed.set_footer(text=f"⏳ {create_time_bar(time_left, total_time)}")
                    await game_msg.edit(embed=embed)
                except: pass
            if not round_event.is_set():
                round_event.set() 

        async def listener_task():
            nonlocal winner_msg
            def check(m): return m.channel.id == channel.id and not m.author.bot
            while not round_event.is_set():
                try:
                    msg = await bot.wait_for('message', check=check, timeout=1)
                    user_ans = msg.content.lower().strip()
                    if user_ans == correct_answer:
                        winner_msg = msg
                        round_event.set()
                        return
                    else:
                        try: await msg.add_reaction("🖕")
                        except: pass
                except asyncio.TimeoutError:
                    continue 

        await asyncio.gather(timer_task(), listener_task())

        if winner_msg:
            update_user_balance(winner_msg.author.id, balance_change=36)
            await channel.send(f"✅ Chính xác! <@{winner_msg.author.id}> thưởng 36 điểm.")
            game_data["consecutive_fails"] = 0
        else:
            try:
                embed.set_footer(text="⌛ Hết giờ!", icon_url=None)
                embed.color = 0xFF0000
                await game_msg.edit(embed=embed)
            except: pass
            await channel.send(f"Hết giờ! Đáp án: **{q_data['answer']}**")
            game_data["consecutive_fails"] += 1
        
        if game_data["consecutive_fails"] >= 5:
            await channel.send("🛑 Game Over (5 câu sai liên tiếp).")
            game_data["is_active"] = False
        
        if not game_data["is_active"]: break
        await asyncio.sleep(2)

@bot.tree.command(name="startgp")
async def startgp(interaction: discord.Interaction):
    if not questions_bank:
         return await interaction.response.send_message("File câu hỏi trống!", ephemeral=True)
    if game_data["is_active"]:
        return await interaction.response.send_message("Game đang chạy.", ephemeral=True)

    game_data["is_active"] = True
    game_data["channel_id"] = interaction.channel_id
    await interaction.response.send_message("**Bắt đầu Trivia!**")
    bot.loop.create_task(game_loop(interaction.channel))

@bot.tree.command(name="stopgp")
async def stopgp(interaction: discord.Interaction):
    game_data["is_active"] = False
    await interaction.response.send_message("Đã dừng game.", ephemeral=True)

@bot.tree.command(name="bitcoin")
async def bitcoin_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    price = await get_btc_price()
    user = get_user_data(interaction.user.id)
    view = CryptoView(current_price=price)
    embed = discord.Embed(title="📊 SÀN BTC", description=f"Giá: **${price:,.2f}**", color=0xF7931A)
    embed.add_field(name="Ví bạn", value=f"💵 {user['balance']:,.2f}\n🪙 {user['btc']:.6f}")
    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg

@bot.tree.command(name="rank")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        price = await get_btc_price()
        all_users = list(users_col.find())
        if not all_users:
            await interaction.followup.send("Data trống.")
            return

        ranked = []
        for user in all_users:
            uid = user["_id"]
            bal = float(user.get("balance", 0.0))
            btc = float(user.get("btc", 0.0))
            nw = bal + (btc * price)
            ranked.append((uid, nw, btc))

        ranked.sort(key=lambda x: x[1], reverse=True)
        desc = ""
        for idx, (uid, nw, btc) in enumerate(ranked[:10], 1):
            medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"#{idx}"
            desc += f"{medal} <@{uid}>\n   💰 ${nw:,.0f} (BTC: {btc:.4f})\n"
            
        embed = discord.Embed(title="🏆 TOP SERVER", description=desc, color=0xD4AF37)
        embed.set_footer(text=f"BTC: ${price:,.0f}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"Lỗi: {e}")

@bot.tree.command(name="balance")
async def balance(interaction: discord.Interaction):
    user = get_user_data(interaction.user.id)
    await interaction.response.send_message(f"💳 **{interaction.user.name}**\n💵 {user.get('balance',0):,.2f}\n🪙 {user.get('btc',0):.6f}")

if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)
