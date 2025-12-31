import os
import logging
import time
import asyncio
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Hi! I'm a Video Downloader Bot. Send me a link and I'll let you choose the quality."
    )

async def ask_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    chat_id = update.effective_chat.id
    
    if not url.startswith(('http://', 'https://')):
        await context.bot.send_message(chat_id=chat_id, text="That doesn't look like a valid URL.")
        return

    # Store URL in user_data
    context.user_data['url'] = url

    keyboard = [
        [
            InlineKeyboardButton("🎬 Best Quality", callback_data='best'),
            InlineKeyboardButton("📱 Low Quality (360p)", callback_data='low'),
        ],
        [
            InlineKeyboardButton("🎵 Audio Only", callback_data='audio')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(chat_id=chat_id, text="Choose download quality:", reply_markup=reply_markup)

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data
    chat_id = update.effective_chat.id
    url = context.user_data.get('url')
    
    if not url:
        await query.edit_message_text(text="Session expired. Please send the link again.")
        return

    message = await query.edit_message_text(text=f"Starting download ({choice})...")
    
    # --- Progress Hook Logic ---
    last_edit_time = 0
    
    def progress_hook(d):
        nonlocal last_edit_time, message
        
        if d['status'] == 'downloading':
            current_time = time.time()
            # Update only every 3 seconds to avoid flood limits, or if it's the end
            if current_time - last_edit_time > 3 or d.get('completion_date'):
                last_edit_time = current_time
                
                percent = d.get('_percent_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                speed = d.get('_speed_str', 'N/A')
                
                # Create a simple progress bar
                try:
                    p = float(percent.strip('%'))
                    bar_len = 10
                    filled_len = int(bar_len * p / 100)
                    bar = '🟦' * filled_len + '⬜' * (bar_len - filled_len)
                except ValueError:
                    bar = ''

                text = f"Downloading: {percent}\n{bar}\nSpeed: {speed} | ETA: {eta}"
                
                # Run async edit in the event loop (since hooks are sync)
                try:
                    asyncio.run_coroutine_threadsafe(
                        message.edit_text(text),
                        application.loop
                    )
                except Exception as e:
                    # Ignore errors (like "Message is not modified")
                    pass

    # --- End Progress Hook ---

    ydl_opts = {
        'outtmpl': f'downloads/%(id)s.%(ext)s',
        'max_filesize': 50 * 1024 * 1024, # 50MB limit
        'progress_hooks': [progress_hook],
        'noplaylist': True,
    }
    
    if choice == 'best':
        ydl_opts['format'] = 'best[ext=mp4]/best'
    elif choice == 'low':
        ydl_opts['format'] = 'best[height<=360][ext=mp4]/best[height<=360]'
    elif choice == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        ydl_opts['outtmpl'] = f'downloads/%(id)s.mp3'

    video_file_path = None
    try:
        # We need to run blocking yt-dlp in a separate thread effectively, 
        # but since we're simple, we'll run it here. The hook uses threadsafe call.
        # Ideally we'd use run_in_executor but the file operations make it complex.
        
        # NOTE: Message editing inside hook is tricky with sync/async mismatch.
        # The above threadsafe call assumes application.loop is accessible.
        # We need to ensure 'application' is global or accessible. It is global below.
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            if choice == 'audio':
                filename = ydl.prepare_filename(info)
                video_file_path = os.path.splitext(filename)[0] + ".mp3"
            else:
                video_file_path = ydl.prepare_filename(info)

        await message.edit_text(text="Upload starting...")
        
        if choice == 'audio':
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=open(video_file_path, 'rb'),
                caption=f"🎧 {info.get('title', 'Audio')}"
            )
        else:
            await context.bot.send_video(
                chat_id=chat_id,
                video=open(video_file_path, 'rb'),
                caption=f"🎬 {info.get('title', 'Video')}"
            )
        
        if os.path.exists(video_file_path):
            os.remove(video_file_path)
            
        await message.delete()

    except yt_dlp.utils.DownloadError as e:
        await message.edit_text(text="Error: Video too large (>50MB) or link invalid.")
        logging.error(f"Download error: {e}")
    except Exception as e:
        await message.edit_text(text=f"Error: {e}")
        logging.error(f"Error: {e}")
        if video_file_path and os.path.exists(video_file_path):
            os.remove(video_file_path)

if __name__ == '__main__':
    if not TOKEN or TOKEN == "YOUR_TOKEN_HERE":
        print("Error: BOT_TOKEN is not set.")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()
    
    # Make application.loop accessible globally for the threadsafe hook
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), ask_quality))
    application.add_handler(CallbackQueryHandler(button_click))
    
    print("Bot is running...")
    application.run_polling()
