import os
import logging
import asyncio
import math
import time
from concurrent.futures import ThreadPoolExecutor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from dotenv import load_dotenv
import yt_dlp

# Load environment variables
load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Executor for blocking tasks
executor = ThreadPoolExecutor(max_workers=4)

class DownloadProgress:
    def __init__(self, message, loop):
        self.message = message
        self.loop = loop
        self.last_update = 0
        self.start_time = time.time()

    def progress_hook(self, d):
        if d['status'] == 'downloading':
            current_time = time.time()
            # Update every 3 seconds
            if current_time - self.last_update > 3:
                self.last_update = current_time
                self._update_message(d)
        elif d['status'] == 'finished':
            self._update_message(d, finished=True)

    def _update_message(self, d, finished=False):
        if finished:
            text = "✅ Download complete! Processing..."
        else:
            percent = d.get('_percent_str', '').strip()
            speed = d.get('_speed_str', '').strip()
            eta = d.get('_eta_str', '').strip()
            
            # Create progress bar
            try:
                pct = float(d.get('downloaded_bytes', 0)) / float(d.get('total_bytes', 1) or d.get('total_bytes_estimate', 1))
                bars = 15
                filled = int(bars * pct)
                empty = bars - filled
                bar = '▓' * filled + '░' * empty
                pct_str = f"{int(pct * 100)}%"
            except Exception:
                bar = "Traitement..."
                pct_str = percent

            text = (
                f"� **Downloading Video**\n\n"
                f"{bar} {pct_str}\n\n"
                f"🚀 Speed: {speed}\n"
                f"⏳ ETA: {eta}"
            )

        # Schedule the coroutine
        asyncio.run_coroutine_threadsafe(
            self.message.edit_text(text, parse_mode='Markdown'),
            self.loop
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "👋 **Hi! I'm a Video Downloader Bot.**\n\n"
            "Send me a link from YouTube, Instagram, TikTok, etc.\n"
            "I'll verify the available qualities for you! 🎬"
        ),
        parse_mode='Markdown'
    )

def get_formats_sync(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info

async def ask_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    chat_id = update.effective_chat.id

    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="⚠ That doesn't look like a valid URL.")
        return

    msg = await context.bot.send_message(chat_id=chat_id, text="🔍 **Analyzing link...** Please wait.", parse_mode='Markdown')

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(executor, get_formats_sync, url)
        
        # Store essential info in user context to avoid re-fetching
        # We need a unique ID for the session, user_data is fine for simple bots
        context.user_data['url'] = url
        context.user_data['title'] = info.get('title', 'Unknown Title')
        
        # Determine available formats
        formats = info.get('formats', [])
        
        # Filter for video formats
        resolutions = set()
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('height'):
                resolutions.add(f['height'])
        
        sorted_res = sorted(list(resolutions), reverse=True)
        
        # Build Keyboard
        keyboard = []
        # Add video options (limit to top 4 to avoid huge list)
        for res in sorted_res[:4]:
            keyboard.append([InlineKeyboardButton(f"🎬 Video {res}p", callback_data=f"video_{res}")])
            
        # Add generic options
        if not sorted_res:
             keyboard.append([InlineKeyboardButton(f"🎬 Best Video", callback_data=f"video_best")])

        keyboard.append([InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data="audio_best")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        title = info.get('title', 'Video').replace('*', '').replace('_', '').replace('`', '')
        duration = info.get('duration_string') or info.get('duration')
        uploader = info.get('uploader', 'Unknown').replace('*', '').replace('_', '').replace('`', '')
        
        caption = (
            f"🎥 **{title}**\n\n"
            f"👤 Channel: {uploader}\n"
            f"⏱ Duration: {duration}\n\n"
            f"⬇ Choose your download quality:"
        )

        await msg.edit_text(text=caption, reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error fetching info: {e}")
        await msg.edit_text(text=f"❌ Failed to fetch video info.\nError: {str(e)}")


def download_sync(options, url):
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = update.effective_chat.id
    
    if data == "cancel":
        await query.message.delete()
        return

    url = context.user_data.get('url')
    if not url:
        await query.edit_message_text(text="❌ Session expired. Please send the link again.")
        return

    # Prepare download
    msg = await query.edit_message_text(text="🚀 **Initializing download...**", parse_mode='Markdown')
    loop = asyncio.get_running_loop()
    progress_tracker = DownloadProgress(msg, loop)
    
    # Base options
    ydl_opts = {
        'outtmpl': f'downloads/%(id)s.%(ext)s',
        'progress_hooks': [progress_tracker.progress_hook],
        'noplaylist': True,
        # max filesize 50MB is tricky with formats; simple retry logic or limit
        'max_filesize': 50 * 1024 * 1024,
    }

    if data.startswith("video_"):
        res = data.split("_")[1]
        if res == "best":
            ydl_opts['format'] = 'best[ext=mp4]/best'
        else:
            # Try to get specific height, fallback to best compatible
            ydl_opts['format'] = f'best[height<={res}][ext=mp4]/best[height<={res}]'
    
    elif data == "audio_best":
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        ydl_opts['outtmpl'] = f'downloads/%(id)s.mp3'

    file_path = None
    try:
        # Run download in thread
        file_path, info = await loop.run_in_executor(executor, download_sync, ydl_opts, url)
        
        # Handle case where file extension might differ (e.g. mkv vs mp4)
        if not os.path.exists(file_path):
            # Try to find the file if extension changed
            base, _ = os.path.splitext(file_path)
            for ext in ['.mp4', '.mkv', '.webm', '.mp3']:
                if os.path.exists(base + ext):
                    file_path = base + ext
                    break
        
        if not os.path.exists(file_path):
             raise Exception("File not found after download.")

        # Check size
        file_size = os.path.getsize(file_path)
        if file_size > 50 * 1024 * 1024:
             await msg.edit_text(text="❌ File is too large (>50MB) to upload via Telegram Bot API.")
             os.remove(file_path)
             return

        await msg.edit_text(text="📤 **Uploading to Telegram...**", parse_mode='Markdown')
        
        # Upload
        with open(file_path, 'rb') as f:
            # Escaping for caption
            title = info.get('title', 'Video').replace('*', '').replace('_', '').replace('`', '')
            caption = f"🎬 {title}\nDownload by @MyDownloaderBot"
            if data == "audio_best":
                 await context.bot.send_audio(chat_id=chat_id, audio=f, title=info.get('title'), caption=caption)
            else:
                 await context.bot.send_video(chat_id=chat_id, video=f, caption=caption, supports_streaming=True)
        
        await msg.delete()

    except Exception as e:
        logger.error(f"Download/Upload failed: {e}")
        # Clean error message
        safe_error = str(e).replace('*', '').replace('_', '').replace('`', '')
        await msg.edit_text(text=f"❌ Error occurred: {safe_error}")
    
    finally:
        # Clean up
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Deleted file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to delete file: {e}")

if __name__ == '__main__':
    if not TOKEN:
        print("Error: BOT_TOKEN is not set in .env file.")
        exit(1)

    # checks for downloads folder
    if not os.path.exists('downloads'):
        os.makedirs('downloads')

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), ask_quality))
    application.add_handler(CallbackQueryHandler(button_click))
    
    print("Bot is started...")
    application.run_polling()
