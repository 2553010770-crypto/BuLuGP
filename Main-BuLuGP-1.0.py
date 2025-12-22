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
from bson.objectid import ObjectId
import io

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
IMAGE_STORAGE_CHANNEL_ID = 1452547718248398931

WAIT_TIME = 12

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

def keep_alive():
    t = threading.Thread(target=start_web_server)
    t.daemon = True
    t.start()

DB_NAME = "DiscordBotDB"
COLLECTION_USERS = "users"
COLLECTION_QUESTIONS = "questions"

try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    users_col = db[COLLECTION_USERS]
    questions_col = db[COLLECTION_QUESTIONS]
    print("Connected to MongoDB")
except Exception as e:
    print(f"MongoDB Error: {e}")

questions_cache = []

def refresh_questions_cache():
    global questions_cache
    try:
        raw_data = list(questions_col.find())
        for item in raw_data:
            item['_id'] = str(item['_id'])
        questions_cache = raw_data
        print(f"Loaded {len(questions_cache)} questions")
    except Exception as e:
        print(f"Cache Error: {e}")
        questions_cache = []

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

def _add_question_sync(question, answer, image_url):
    doc = {"question": question, "answer": answer, "image_url": image_url}
    questions_col.insert_one(doc)

def _insert_many_sync(data_list):
    if data_list:
        questions_col.insert_many(data_list)

def _delete_question_sync(index):
    if 0 <= index < len(questions_cache):
        q_id = questions_cache[index]['_id']
        questions_col.delete_one({"_id": ObjectId(q_id)})
        return True
    return False

active_games = {}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def process_image_url(url):
    if not url:
        return None
    
    url = str(url).strip()
    if "discordapp.com" in url or "discordapp.net" in url:
        return url
        
    try:
        channel = bot.get_channel(IMAGE_STORAGE_CHANNEL_ID)
        if not channel:
            print("Storage Channel not found")
            return url

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return url
                
                data = await resp.read()
                filename = url.split("/")[-1].split("?")[0]
                if not filename: filename = "image.png"
                
                file_obj = discord.File(io.BytesIO(data), filename=filename)
                msg = await channel.send(file=file_obj)
                
                if msg.attachments:
                    return msg.attachments[0].url
    except Exception as e:
        print(f"Image process error: {e}")
        return url
    
    return url

@bot.event
async def on_ready():
    print(f'Bot Online: {bot.user}')
    refresh_questions_cache()
    await bot.tree.sync()

@bot.tree.command(name="add_q", description="Thêm câu hỏi thủ công")
@app_commands.describe(question="Câu hỏi", answer="Đáp án", image_url="Link ảnh (tùy chọn)")
async def add_q(interaction: discord.Interaction, question: str, answer: str, image_url: str = None):
    await interaction.response.defer(ephemeral=True)
    
    final_image_url = await process_image_url(image_url)
    
    await run_db_task(_add_question_sync, question, answer, final_image_url)
    refresh_questions_cache()
    
    embed = discord.Embed(title="Đã thêm câu hỏi", color=discord.Color.green())
    embed.add_field(name="Hỏi", value=question, inline=False)
    embed.add_field(name="Đáp án", value=answer, inline=False)
    if final_image_url: embed.set_image(url=final_image_url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="upload_json", description="Nạp câu hỏi từ file JSON")
@app_commands.describe(file="File .json chứa danh sách câu hỏi")
async def upload_json(interaction: discord.Interaction, file: discord.Attachment):
    if not file.filename.endswith('.json'):
        return await interaction.response.send_message("❌ Vui lòng tải lên file .json", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        file_content = await file.read()
        data = json.loads(file_content)
        
        if not isinstance(data, list):
            return await interaction.followup.send("❌ Cấu trúc JSON phải là một danh sách (Array).")
            
        valid_questions = []
        count_processed = 0
        
        for item in data:
            if "question" in item and "answer" in item:
                original_url = item.get("image_url")
                new_url = await process_image_url(original_url)
                
                q_obj = {
                    "question": item["question"],
                    "answer": item["answer"],
                    "image_url": new_url
                }
                valid_questions.append(q_obj)
                count_processed += 1
                
                if count_processed % 10 == 0:
                    await asyncio.sleep(1)
        
        if valid_questions:
            await run_db_task(_insert_many_sync, valid_questions)
            refresh_questions_cache()
            await interaction.followup.send(f"✅ Đã nhập thành công **{len(valid_questions)}** câu hỏi! (Ảnh đã được backup)", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Không tìm thấy câu hỏi hợp lệ trong file.", ephemeral=True)
            
    except json.JSONDecodeError:
        await interaction.followup.send("❌ Lỗi định dạng JSON.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi hệ thống: {e}", ephemeral=True)

@bot.tree.command(name="del_q", description="Xóa câu hỏi theo STT")
async def del_q(interaction: discord.Interaction, index: int):
    await interaction.response.defer(ephemeral=True)
    success = await run_db_task(_delete_question_sync, index - 1)
    if success:
        refresh_questions_cache()
        await interaction.followup.send(f"Đã xóa câu hỏi số {index}.", ephemeral=True)
    else:
        await interaction.followup.send("Số thứ tự không tồn tại.", ephemeral=True)

@bot.tree.command(name="view_qs", description="Xem danh sách câu hỏi")
async def view_qs(interaction: discord.Interaction):
    if not questions_cache:
        return await interaction.response.send_message("Danh sách trống.", ephemeral=True)
    
    desc = ""
    for i, q in enumerate(questions_cache):
        has_img = "🖼️" if q.get("image_url") else ""
        line = f"**#{i+1}** {has_img} {q['question']} (ĐA: ||{q['answer']}||)\n"
        if len(desc) + len(line) > 3900:
            desc += "..."
            break
        desc += line
    
    embed = discord.Embed(title=f"Ngân hàng câu hỏi ({len(questions_cache)})", description=desc, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def game_loop(channel):
    channel_id = channel.id
    active_games[channel_id] = {"active": True, "fails": 0, "history": []}
    
    while active_games.get(channel_id, {}).get("active"):
        if not questions_cache:
            await channel.send("Database trống.")
            break

        total_qs = len(questions_cache)
        history = active_games[channel_id]["history"]
        
        limit_n = int(total_qs * 0.75)
        
        while len(history) > limit_n:
            history.pop(0)

        available = [i for i in range(total_qs) if i not in history]
        
        if not available:
            history.clear()
            available = list(range(total_qs))

        idx = random.choice(available)
        active_games[channel_id]["history"].append(idx)

        q_data = questions_cache[idx]
        correct_answer = str(q_data["answer"]).lower().strip()
        end_time = time.time() + WAIT_TIME
        
        embed = discord.Embed(title="TRIVIA!", description=f"**{q_data['question']}**", color=0xD4AF37)
        if q_data.get("image_url") and str(q_data["image_url"]).startswith("http"):
             embed.set_image(url=q_data["image_url"])
        embed.add_field(name="Thời gian", value=f"⏳ <t:{int(end_time)}:R>")
        
        await channel.send(embed=embed)

        winner = None
        while time.time() < end_time:
            remaining = end_time - time.time()
            if remaining <= 0: break
            try:
                def check(m): return m.channel.id == channel_id and not m.author.bot
                msg = await bot.wait_for('message', check=check, timeout=remaining)
                if msg.content.lower().strip() == correct_answer:
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
            await channel.send(f"✅ Chính xác! <@{winner.id}> +${bonus}.")
            active_games[channel_id]["fails"] = 0
            await asyncio.sleep(2)
        else:
            await channel.send(f"⏰ Hết giờ! Đáp án: **{q_data['answer']}**")
            active_games[channel_id]["fails"] += 1

        if active_games[channel_id]["fails"] >= 5:
            await channel.send("Game Over.")
            active_games[channel_id]["active"] = False
        
        await asyncio.sleep(3)

    active_games.pop(channel_id, None)

@bot.tree.command(name="startgp", description="Bắt đầu game")
async def startgp(interaction: discord.Interaction):
    if interaction.channel_id in active_games:
        return await interaction.response.send_message("Game đang chạy!", ephemeral=True)
    if not questions_cache:
        return await interaction.response.send_message("Database trống.", ephemeral=True)
    await interaction.response.send_message("🎮 Bắt đầu!")
    bot.loop.create_task(game_loop(interaction.channel))

@bot.tree.command(name="stopgp", description="Dừng game")
async def stopgp(interaction: discord.Interaction):
    if interaction.channel_id in active_games:
        active_games[interaction.channel_id]["active"] = False
        await interaction.response.send_message("Đang dừng game...", ephemeral=True)
    else:
        await interaction.response.send_message("Không có game nào.", ephemeral=True)

if __name__ == "__main__":
    if not BOT_TOKEN: 
        print("Missing Token")
    else: 
        keep_alive() 
        bot.run(BOT_TOKEN)
