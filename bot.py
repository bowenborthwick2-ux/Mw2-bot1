import os
import re
import sqlite3
import discord
from discord.ext import commands
import aiohttp

# ---------------------------------------------------------------------------
# 1. SETUP & CONFIGURATION
# ---------------------------------------------------------------------------

# Fetch secret keys and settings from Railway environment variables
TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_KEY = os.getenv("GOOGLE_API_KEY")
INPUT_CHAN_ID = int(os.getenv("INPUT_CHANNEL", 0))
OUTPUT_CHAN_ID = int(os.getenv("OUTPUT_CHANNEL", 0))

# Configure Discord bot permissions (intents) to read messages and content
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Google Cloud Vision API endpoint for text detection
VISION_API_URL = f"https://googleapis.com{GOOGLE_KEY}"

# ---------------------------------------------------------------------------
# 2. DATABASE SETUP
# ---------------------------------------------------------------------------

def init_db():
    """Creates the local database file and table if they do not exist."""
    conn = sqlite3.connect("leaderboard.db")
    cursor = conn.cursor()
    # Create a table to store unique player names and their Personal Bests (PB)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard (
            player_name TEXT PRIMARY KEY,
            max_kills INTEGER DEFAULT 0,
            max_score INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def update_player_pb(name, kills, score):
    """Updates a player's record only if they beat their personal best."""
    conn = sqlite3.connect("leaderboard.db")
    cursor = conn.cursor()
    
    # Check if the player already exists in our database
    cursor.execute("SELECT max_kills, max_score FROM leaderboard WHERE player_name = ?", (name,))
    row = cursor.fetchone()
    
    if row:
        current_max_kills, current_max_score = row
        # Determine if the new stats are higher than the old ones
        new_kills = max(current_max_kills, kills)
        new_score = max(current_max_score, score)
        
        cursor.execute("""
            UPDATE leaderboard 
            SET max_kills = ?, max_score = ? 
            WHERE player_name = ?
        """, (new_kills, new_score, name))
    else:
        # If player is new, insert their current stats as their personal best
        cursor.execute("""
            INSERT INTO leaderboard (player_name, max_kills, max_score) 
            VALUES (?, ?, ?)
        """, (name, kills, score))
        
    conn.commit()
    conn.close()

def get_top_20(stat_type):
    """Retrieves top 20 players sorted by either 'max_kills' or 'max_score'."""
    conn = sqlite3.connect("leaderboard.db")
    cursor = conn.cursor()
    if stat_type == "kills":
        cursor.execute("SELECT player_name, max_kills FROM leaderboard ORDER BY max_kills DESC LIMIT 20")
    else:
        cursor.execute("SELECT player_name, max_score FROM leaderboard ORDER BY max_score DESC LIMIT 20")
    results = cursor.fetchall()
    conn.close()
    return results

# ---------------------------------------------------------------------------
# 3. TEXT PARSING & OCR LOGIC
# ---------------------------------------------------------------------------

def clean_ocr_text(text_lines):
    """
    Parses raw text rows to find player stats.
    Handles MW2 (2009) overlays and ignores [TAG] clan tags.
    """
    parsed_data = []
    
    # Words to ignore caused by game overlays like "VICTORY" or "SCORE LIMIT REACHED"
    ignore_words = {"victory", "defeat", "score", "limit", "reached", "tie", "match", "bonus"}
    
    for line in text_lines:
        clean_line = line.strip()
        if not clean_line or any(w in clean_line.lower() for w in ignore_words):
            continue
            
        # Remove clan tags in brackets, e.g., [XYZ] Player -> Player
        clean_line = re.sub(r'\[.*?\]\s*', '', clean_line)
        
        # Regex to match: PlayerName followed by numbers (Kills and Score)
        # Handles typical messy OCR spacing or dashes
        match = re.search(r'^(.+?)\s+([\d\s\-]+)$', clean_line)
        if match:
            player_name = match.group(1).strip()
            numbers_part = match.group(2)
            
            # Extract all distinct numbers from the end of the line
            numbers = re.findall(r'\d+', numbers_part)
            
            # MW2 scoreboards require at least a Name, Kills, and Score entry
            if len(numbers) >= 2 and len(player_name) > 1:
                try:
                    kills = int(numbers[0])
                    score = int(numbers[-1]) # Score is usually the last number on the row
                    parsed_data.append((player_name, kills, score))
                except ValueError:
                    continue
                    
    return parsed_data

async def extract_text_from_url(image_url):
    """Sends the image URL to Google Cloud Vision API via REST."""
    payload = {
        "requests": [
            {
                "image": {"source": {"imageUri": image_url}},
                "features": [{"type": "TEXT_DETECTION"}]
            }
        ]
    }
    
    async with aiohttp.ClientSession() as session:
        async def post_request():
            return await session.post(VISION_API_URL, json=payload)
        async with await post_request() as response:
            if response.status == 200:
                data = await response.json()
                try:
                    # Extract the full block of text found by Google
                    annotations = data["responses"]["textAnnotations"]
                    if annotations:
                        full_text = annotations[0]["description"]
                        return full_text.split("\n")
                except (KeyError, IndexError):
                    pass
    return []

# ---------------------------------------------------------------------------
# 4. DISCORD EMBED UPDATE SYSTEM
# ---------------------------------------------------------------------------

async def refresh_leaderboard_embeds():
    """Generates and updates the permanent embeds in the OUTPUT_CHANNEL."""
    channel = bot.get_channel(OUTPUT_CHAN_ID)
    if not channel:
        print("Output channel not found. Check your OUTPUT_CHANNEL ID.")
        return

    # Build Embed 1: Top 20 Kills
    embed_kills = discord.Embed(title="🏆 Top 20 Personal Best: Kills", color=0x3498db)
    top_kills = get_top_20("kills")
    if top_kills:
        description = "\n".join([f"**#{i+1}** {name} — `{kills} Kills`" for i, (name, kills) in enumerate(top_kills)])
        embed_kills.description = description
    else:
        embed_kills.description = "No stats recorded yet."

    # Build Embed 2: Top 20 Score
    embed_score = discord.Embed(title="⭐ Top 20 Personal Best: Score", color=0x2ecc71)
    top_score = get_top_20("score")
    if top_score:
        description = "\n".join([f"**#{i+1}** {name} — `{score} pts`" for i, (name, score) in enumerate(top_score)])
        embed_score.description = description
    else:
        embed_score.description = "No stats recorded yet."

    # Search the last 50 messages in the channel to find existing bot embeds to edit
    existing_message = None
    async for message in channel.history(limit=50):
        if message.author == bot.user and len(message.embeds) == 2:
            existing_message = message
            break

    # If the message exists, edit it. Otherwise, post it for the first time.
    if existing_message:
        await existing_message.edit(embeds=[embed_kills, embed_score])
    else:
        await channel.send(embeds=[embed_kills, embed_score])

# ---------------------------------------------------------------------------
# 5. BOT EVENTS
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    """Runs when the bot successfully signs into Discord."""
    print(f"Bot logged in as {bot.user.name}")
    init_db()
    print("Database verified successfully.")

@bot.event
async def on_message(message):
    """Triggers whenever a message is posted in a channel the bot can see."""
    # Ignore messages sent by the bot itself
    if message.author == bot.user:
        return

    # Process only if the message arrives in the designated INPUT_CHANNEL
    if message.channel.id == INPUT_CHAN_ID:
        # Check if the message contains any file attachments
        for attachment in message.attachments:
            # Verify if the file attachment is an image
            if attachment.content_type and attachment.content_type.startswith("image/"):
                # Notify users processing has started
                status_msg = await message.reply("🔄 Reading scoreboard image via Google Vision...")
                
                # Step 1: Run the image through Google OCR
                lines = await extract_text_from_url(attachment.url)
                
                # Step 2: Parse text lines into actual player stats
                match_results = clean_ocr_text(lines)
                
                if match_results:
                    # Step 3: Update database records for each player found
                    for name, kills, score in match_results:
                        update_player_pb(name, kills, score)
                    
                    await status_msg.edit(content="✅ Database updated! Refreshing live leaderboards...")
                    
                    # Step 4: Refresh the sticky leaderboard display
                    await refresh_leaderboard_embeds()
                else:
                    await status_msg.edit(content="❌ Failed to parse scoreboard data. Ensure names, kills, and scores are visible.")

    # Allows processing of prefix commands if any are added later
    await bot.process_commands(message)

# Launch the bot
if __name__ == "__main__":