from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler
import os
import traceback
import logging
from datetime import datetime, timedelta
from datetime import time as dt_time
import time
import pytz
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import math
import random
from telegram import BotCommand
from math import radians, sin, cos, sqrt, atan2
from selenium.common.exceptions import TimeoutException
from threading import Lock
from asyncio import create_task
import asyncio

def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calculate distance in meters between two coordinates using Haversine formula
    """
    R = 6373.0  # Earth radius in kilometers

    lat1 = radians(lat1)
    lon1 = radians(lon1)
    lat2 = radians(lat2)
    lon2 = radians(lon2)

    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))

    return round(R * c * 1000, 1)  # Convert to meters and round
  
# Configuration
USERNAME = os.getenv('PMD_USERNAME')
PASSWORD = os.getenv('PMD_PASSWORD')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
AUTHORIZED_USERS = [uid.strip() for uid in os.getenv('AUTHORIZED_USERS', '').split(',') if uid.strip()]
TIMEZONE = pytz.timezone(os.getenv('TIMEZONE', 'Asia/Bangkok'))
BASE_LATITUDE = float(os.getenv('BASE_LATITUDE', '11.545380'))
BASE_LONGITUDE = float(os.getenv('BASE_LONGITUDE', '104.911449'))
MAX_DEVIATION_METERS = 150
CHAT_ID = os.getenv('CHAT_ID')

active_drivers = {}
driver_lock = Lock()
scan_tasks = {}
is_auto_scan_running = False

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def generate_random_coordinates():
    """Generate random coordinates within MAX_DEVIATION_METERS of base location"""
    # Convert meters to degrees (approximate)
    radius_deg = MAX_DEVIATION_METERS / 111320  # 1 degree ≈ 111,320 meters
    
    # Random direction (0-360 degrees)
    angle = math.radians(random.uniform(0, 360))
    
    # Random distance (0 to max deviation)
    distance = random.uniform(0, radius_deg)
    
    # Calculate new coordinates
    new_lat = BASE_LATITUDE + (distance * math.cos(angle))
    new_lon = BASE_LONGITUDE + (distance * math.sin(angle))
    
    return new_lat, new_lon

def create_driver():
    options = Options()
    options.binary_location = '/usr/bin/chromium'
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--allow-geolocation')
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.geolocation": 1,
    })

    service = Service(executable_path='/usr/bin/chromedriver')
    driver = webdriver.Chrome(service=service, options=options)
    
    # Generate random coordinates
    lat, lon = generate_random_coordinates()
    logger.info(f"Using coordinates: {lat:.6f}, {lon:.6f}")
    
    # Set randomized geolocation
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": lat,
        "longitude": lon,
        "accuracy": 100
    })
    return driver, (lat, lon)
    
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Send reminder about upcoming mission"""
    try:
        chat_id = os.getenv('CHAT_ID')
        next_run = context.job.data  # ONLY THIS LINE NEEDED
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Reminder: Auto mission scheduled at {next_run.strftime('%H:%M')} ICT (in 1 hour)"
        )
        logger.info(f"Sent reminder for {next_run}")
    except Exception as e:
        logger.error(f"Reminder error: {str(e)}")
        
async def auto_scanin_job(context: ContextTypes.DEFAULT_TYPE):
    global is_auto_scan_running
    if is_auto_scan_running:
        logger.warning("⚠️ Skipping auto mission: already running.")
        return
    is_auto_scan_running = True
    try:
        chat_id = os.getenv('CHAT_ID')
        logger.info("🔄 Starting automated mission...")
        await context.bot.send_message(chat_id, "⏰ Starting automated mission...")
        success = await perform_scan_in(context.bot, chat_id)
    except Exception as e:
        logger.error(f"Automated mission failed: {str(e)}")
        await context.bot.send_message(chat_id, f"❌ Automated mission failed: {str(e)}")
    finally:
        is_auto_scan_running = False
        logger.info("✅ Mission automatically job completed. Cleaning up and scheduling next.")
        # Always remove old scheduled jobs (including self)
        for job in context.job_queue.get_jobs_by_name("auto_scanin"):
            job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name("reminder"):
            job.schedule_removal()
    
        # Schedule next job regardless of what just happened
        schedule_next_scan(context.job_queue)
        
def schedule_next_scan(job_queue, force_next_morning=False):
    for job in job_queue.get_jobs_by_name("auto_scanin"):
        job.schedule_removal()
    for job in job_queue.get_jobs_by_name("reminder"):
        job.schedule_removal()
    now = TIMEZONE.localize(datetime.now())

    def get_next_morning(now):
        for i in range(8):
            future = now + timedelta(days=i)
            if future.weekday() in range(6):
                hour = 7
                minute = random.randint(45, 59)
                next_run = TIMEZONE.localize(datetime.combine(future.date(), dt_time(hour, minute)))
                if next_run > now:
                    return next_run
        return None

    next_run = get_next_morning(now)
    if not next_run or next_run <= now:
        logger.warning(f"⚠️ Calculated next_run ({next_run}) was in the past. Trying again.")
        return schedule_next_scan(job_queue, force_next_morning=True)

    delay_seconds = (next_run - now).total_seconds()
    reminder_time = next_run - timedelta(hours=1)
    delay_reminder = (reminder_time - now).total_seconds()

    job_queue.run_once(auto_scanin_job, when=delay_seconds, name="auto_scanin")
    logger.info(f"✅ Scheduled next mission at {next_run.strftime('%Y-%m-%d %H:%M:%S')} ICT")

    if delay_reminder > 0:
        job_queue.run_once(send_reminder, when=delay_reminder, data=next_run, name="reminder")
        logger.info(f"⏰ Scheduled reminder at {reminder_time.strftime('%Y-%m-%d %H:%M:%S')} ICT")

    return next_run
    
async def cancelauto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this command")
        return

    # Remove both mission and reminder jobs
    jobs = context.job_queue.get_jobs_by_name("auto_scanin") + context.job_queue.get_jobs_by_name("reminder")
    if not jobs:
        await update.message.reply_text("ℹ️ No scheduled auto missions to cancel")
        return

    for job in jobs:
        job.schedule_removal()

    # Schedule for next weekday morning
    next_run = schedule_next_scan(context.job_queue, force_next_morning=True)
    
    await update.message.reply_text(
        f"🚫 Auto missions canceled until {next_run.strftime('%A')} morning\n"
        f"⏰ Next mission: {next_run.strftime('%a %H:%M')} ICT\n"
        "⚠️ Reminder: Manual mission still available via /letgo",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ Unauthorized")
        return

    task = scan_tasks.get(chat_id)
    with driver_lock:
        driver = active_drivers.get(chat_id)

    if not task and not driver:
        await update.message.reply_text("ℹ️ No active operation to cancel")
        return

    if task:
        task.cancel()
        scan_tasks.pop(chat_id, None)

    if driver:
        try:
            filename = f"cancelled_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            driver.save_screenshot(filename)
            with open(filename, 'rb') as photo:
                await update.message.reply_photo(photo=photo)
            driver.quit()
            await update.message.reply_text(f"🛑 Operation cancelled with screenshot: {filename}")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error during cancellation: {str(e)}")
        finally:
            del active_drivers[chat_id]
            if os.path.exists(filename):
                os.remove(filename)
    else:
        await update.message.reply_text("✅ Operation cancelled")
            
# Update post_init
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Show welcome message"),
        BotCommand("letgo", "Initiate mission"),
        BotCommand("cancelauto", "Cancel next auto mission and reschedule"),
        BotCommand("cancel", "Cancel ongoing operation")
    ])
    schedule_next_scan(application.job_queue)  # Start scheduling
    
async def perform_scan_in(bot, chat_id, context=None):
    driver, (lat, lon) = create_driver()
    screenshot_file = None
    with driver_lock:
        active_drivers[chat_id] = driver
    try:
        start_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"🕒 Automation started at {start_time} (ICT)")
        
        # Step 1: Login
        with driver_lock:
            if chat_id not in active_drivers:
                await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                return False
        await bot.send_message(chat_id, "🚀 Starting browser automation...")
        wait = WebDriverWait(driver, 15)

        await bot.send_message(chat_id, "🌐 Navigating to login page")
        driver.get("https://tinyurl.com/ajrjyvx9")
                    
        username_field = wait.until(EC.visibility_of_element_located((By.ID, "txtUserName")))
        username_field.send_keys(USERNAME)
        await bot.send_message(chat_id, "👤 Username entered")

        password_field = driver.find_element(By.ID, "txtPassword")
        password_field.send_keys(PASSWORD)
        await bot.send_message(chat_id, "🔑 Password entered")

        driver.find_element(By.ID, "btnSignIn").click()
        await bot.send_message(chat_id, "🔄 Processing login...")

        wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "small-box")))
        await bot.send_message(chat_id, "✅ Login successful")

        # Step 2: Navigate to Attendance
        with driver_lock:
            if chat_id not in active_drivers:
                await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                return False
        await bot.send_message(chat_id, "🔍 Finding attendance card...")
        attendance_xpath = "//div[contains(@class,'small-box bg-aqua')]//h3[text()='Attendance']/ancestor::div[contains(@class,'small-box')]"
        attendance_card = wait.until(EC.presence_of_element_located((By.XPATH, attendance_xpath)))
        
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", attendance_card)
        time.sleep(1)
        more_info_link = attendance_card.find_element(By.XPATH, ".//a[contains(@href, 'ATT/frmclock.aspx')]")
        more_info_link.click()
        await bot.send_message(chat_id, "✅ Clicked 'More info'")

        # Step 3: Clock In
        with driver_lock:
            if chat_id not in active_drivers:
                await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                return False
        try:
            await bot.send_message(chat_id, "⏳ Waiting for Clock In link...")
            clock_in_xpath = "//a[contains(@href, 'frmclockin.aspx') and contains(., 'Clock In')]"
            clock_in_link = wait.until(EC.element_to_be_clickable((By.XPATH, clock_in_xpath)))
            
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", clock_in_link)
            time.sleep(0.5)
            clock_in_link.click()
            await bot.send_message(chat_id, "✅ Clicked Clock In link")
            
        except Exception as e:
            await bot.send_message(chat_id, f"⏰ Clock-in error: {str(e)}")
            raise
            
        # Step 4: Enable Scan In
        try:
            with driver_lock:  # ADD THIS CHECK
                if chat_id not in active_drivers:
                    await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                    return False
            await bot.send_message(chat_id, "🔍 Locating Scan In button...")
            scan_in_btn = wait.until(EC.presence_of_element_located((By.ID, "ctl00_maincontent_btnScanIn")))
            
            # Scroll to button for visibility
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", scan_in_btn)
            time.sleep(0.5)
            
            if scan_in_btn.get_attribute("disabled"):
                driver.execute_script("arguments[0].disabled = false;", scan_in_btn)
                time.sleep(0.5)
        
            scan_in_btn.click()
            await bot.send_message(chat_id, "🔄 Processing mission...")
            
            # Add timeout fallback
            WebDriverWait(driver, 25).until(
                EC.url_contains("frmclock.aspx")
            )
        except TimeoutException:
            await bot.send_message(chat_id, "❌ Timeout waiting for Mission button")
            driver.save_screenshot("timeout_mission.png")
            raise
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Mission Error: {str(e)}")
            raise
        # Step 5: Capture attendance table screenshot
        await bot.send_message(chat_id, "📸 Capturing attendance record...")
        
        # Wait for table to load with fresh data
        table_xpath = "//table[@id='ctl00_maincontent_GVList']//tr[contains(., 'Head Office')]"
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, table_xpath))
        )
        # Scroll to table and highlight
        table = driver.find_element(By.ID, "ctl00_maincontent_GVList")
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", table)
        driver.execute_script("arguments[0].style.border='3px solid #00ff00';", table)
        time.sleep(0.5)  # Allow border animation

        # Capture screenshot
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        screenshot_file = f"success_{timestamp}.png"
        table.screenshot(screenshot_file)  # Direct table capture

        # Send confirmation with screenshot
        base_lat = float(os.getenv('BASE_LATITUDE', '11.545380'))
        base_lon = float(os.getenv('BASE_LONGITUDE', '104.911449'))
        distance = calculate_distance(base_lat, base_lon, lat, lon)
        with open(screenshot_file, 'rb') as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=(
                  f"✅ Successful mission confirmed at {datetime.now(TIMEZONE).strftime('%H:%M:%S')} (ICT)\n"
                  f"📍 *Location:* `{lat:.6f}, {lon:.6f}`\n"
                  f"📏 *Distance from Office:* {distance}m\n"
                  f"🗺 [View on Map](https://maps.google.com/maps?q={lat},{lon})"
                ),
                parse_mode="Markdown"
            )

        return True

    except Exception as e:
        error_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"❌ Failed at {error_time} (ICT): {str(e)}")
        logger.error(traceback.format_exc())
        
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        driver.save_screenshot(f"error_{timestamp}.png")
        with open(f"page_source_{timestamp}.html", "w") as f:
            f.write(driver.page_source)
            
        with open(f"error_{timestamp}.png", 'rb') as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption="Error screenshot")
        return False
    finally:
        # Cleanup code
        with driver_lock:
            if chat_id in active_drivers:
                del active_drivers[chat_id]
        if screenshot_file and os.path.exists(screenshot_file):
            try:
                os.remove(screenshot_file)
            except Exception as e:
                logger.error(f"Error cleaning screenshot: {str(e)}")
        driver.quit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    await update.message.reply_text(
        "🚀 Mission Bot Ready!\n"
        "Use /letgo to trigger the automation process"
    )

async def letgo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this bot")
        return

    if chat_id in scan_tasks:
        await update.message.reply_text("⚠️ A mission is already in progress. Use /cancel to stop it.")
        return

    await update.message.reply_text("⏳ Mission task started in background...")
    
    async def task_wrapper():
        try:
            success = await perform_scan_in(context.bot, chat_id)
            if success:
                await context.bot.send_message(chat_id, "✅ Mission process completed successfully!")
            else:
                await context.bot.send_message(chat_id, "❌ Mission failed. Check previous messages for details.")
        finally:
            scan_tasks.pop(chat_id, None)

    task = create_task(task_wrapper())
    scan_tasks[chat_id] = task

async def main():
    application = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("letgo", letgo))
    application.add_handler(CommandHandler("cancelauto", cancelauto))
    application.add_handler(CommandHandler("cancel", cancel))

    await application.initialize()
    await post_init(application)
    await application.start()
    await application.start_polling()

    # Health check server
    app = web.Application()
    app.router.add_get("/healthz", lambda r: web.Response(text="OK"))
    app.router.add_get("/", lambda r: web.Response(text="✅ Bot is alive"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8000)))
    await site.start()

    print("✅ Bot and health server running...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Shutting down...")
