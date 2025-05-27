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
from aiohttp import web
from asyncio import Lock

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
scan_lock = Lock()  # Async-safe lock
bot_context = {}
# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
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
    try:
        next_run = context.job.data
        message = f"🔔 Reminder: Auto scan-in scheduled at {next_run.strftime('%H:%M')} ICT"
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info(f"✅ Reminder sent: {message}")
    except Exception as e:
        logger.error(f"❌ Reminder job failed: {e}")
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="❌ Reminder job failed to execute."
        )

async def auto_scanin_job(context: ContextTypes.DEFAULT_TYPE):
    global is_auto_scan_running

    async with scan_lock:
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
            logger.error(f"❌ Automated mission failed: {str(e)}")
            await context.bot.send_message(chat_id, f"❌ Automated mission failed: {str(e)}")
        finally:
            schedule_next_scan(context.job_queue)
            is_auto_scan_running = False

def schedule_next_scan(job_queue, force_next_morning=False):
    now = datetime.now(TIMEZONE)
    logger.info(f"🕒 Current time: {now}")

    def get_next_slot(now):
        scan_slots = {
            0: [("morning", 7, 45, 59), ("evening", 18, 7, 37)],  # Monday
            1: [("morning", 7, 45, 59), ("evening", 18, 7, 37)],
            2: [("morning", 7, 45, 59), ("evening", 18, 7, 37)],
            3: [("morning", 7, 45, 59), ("evening", 18, 7, 37)],
            4: [("morning", 7, 45, 59), ("evening", 18, 7, 37)],
            5: [("morning", 7, 45, 59), ("afternoon", 12, 7, 17)],  # Saturday
        }

        for day_offset in range(8):
            future_day = now + timedelta(days=day_offset)
            weekday = future_day.weekday()
            if weekday in scan_slots:
                for scan_type, hour, min_start, min_end in scan_slots[weekday]:
                    minute = random.randint(min_start, min_end)
                    naive_time = datetime.combine(future_day.date(), dt_time(hour, minute))
                    candidate_time = TIMEZONE.localize(naive_time)
                    if candidate_time > now:
                        return scan_type, candidate_time
        return None, None

    if force_next_morning:
        next_run = None
        for i in range(1, 8):  # Check next 7 days
            future = now + timedelta(days=i)
            if future.weekday() in [0, 1, 2, 3, 4, 5]:  # Mon–Sat
                hour = 7
                minute = random.randint(45, 59)
                next_run = TIMEZONE.localize(datetime.combine(future.date(), dt_time(hour, minute)))
                scan_type = "morning"
                break
    else:
        scan_type, next_run = get_next_slot(now)
        logger.info(f"✅ Scheduled next mission at {next_run.strftime('%A %H:%M')} (Type: {scan_type})")

    if not next_run or next_run <= now:
        logger.warning(f"⚠️ Calculated next_run ({next_run}) was invalid or in the past. Forcing next weekday morning.")
        return schedule_next_scan(job_queue, force_next_morning=True)

    # 🧠 Fix is here: define delays before referencing them
    delay_seconds = (next_run - now).total_seconds()
    reminder_time = next_run - timedelta(hours=1)
    delay_reminder = (reminder_time - now).total_seconds()

    # Now conditionally schedule
    existing_auto = job_queue.get_jobs_by_name("auto_scanin")
    existing_reminder = job_queue.get_jobs_by_name("reminder")

    if existing_auto:
        logger.info("📌 Auto mission already scheduled. Skipping reschedule.")
    else:
        job_queue.run_once(auto_scanin_job, when=delay_seconds, name="auto_scanin", data=next_run)
        logger.info(f"✅ Scheduled next mission at {next_run.strftime('%Y-%m-%d %H:%M:%S')} ICT")

    if existing_reminder:
        logger.info("📌 Reminder already scheduled. Skipping reschedule.")
    elif delay_reminder > 0:
        job_queue.run_once(send_reminder, when=delay_reminder, data=reminder_time, name="reminder")
        logger.info(f"⏰ Scheduled reminder at {reminder_time.strftime('%Y-%m-%d %H:%M:%S')} ICT")

    return next_run

async def next_mission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.effective_user.id)
        if user_id not in AUTHORIZED_USERS:
            await update.message.reply_text("❌ You are not authorized to use this command")
            return

        auto_jobs = context.job_queue.get_jobs_by_name("auto_scanin")
        reminder_jobs = context.job_queue.get_jobs_by_name("reminder")

        logger.info(f"🧪 Auto jobs: {auto_jobs}")
        logger.info(f"🧪 Reminder jobs: {reminder_jobs}")

        if not auto_jobs:
            await update.message.reply_text("⚠️ No auto mission is currently scheduled.")
            return

        next_run = auto_jobs[0].data.astimezone(TIMEZONE)
        reply = f"📅 Next auto mission:\n🕒 {next_run.strftime('%A at %H:%M')} ICT"

        if reminder_jobs and reminder_jobs[0].data:
            reminder_time = reminder_jobs[0].data.astimezone(TIMEZONE)
            reply += f"\n🔔 Reminder scheduled for {reminder_time.strftime('%H:%M')} (1 hour before)"

        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"🔥 /next crashed: {e}")
        await update.message.reply_text("❌ Failed to get next mission. Please check logs.")

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
    async with driver_lock:
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

async def watchdog_check(context: ContextTypes.DEFAULT_TYPE):
    auto_jobs = context.job_queue.get_jobs_by_name("auto_scanin")
    if not auto_jobs:
        logger.warning("🚨 No auto_scanin job found — rescheduling now.")
        schedule_next_scan(context.job_queue)
    else:
        logger.info("✅ Watchdog: auto_scanin job is scheduled.")

async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    auto_jobs = context.job_queue.get_jobs_by_name("auto_scanin")
    if auto_jobs and auto_jobs[0].data:
        next_run = auto_jobs[0].data.astimezone(TIMEZONE)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"📅 Daily Summary:\nNext auto mission at {next_run.strftime('%A %H:%M')} ICT"
        )
    else:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="📭 No auto mission currently scheduled."
        )

# Update post_init
async def post_init(application):
    logger.info("🔧 post_init() triggered ✅")
    try:
        logger.info("🔧 post_init() triggered ✅")
        await application.bot.set_my_commands([
            BotCommand("start", "Show welcome message"),
            BotCommand("letgo", "Initiate mission"),
            BotCommand("cancelauto", "Cancel next auto mission and reschedule"),
            BotCommand("cancel", "Cancel ongoing operation"),
            BotCommand("next", "Show next auto mission time"),
            BotCommand("status", "Show status"),
        ])

        # Schedule the next scan
        schedule_next_scan(application.job_queue)

        # 🛡 Watchdog: every hour
        application.job_queue.run_repeating(
            watchdog_check, interval=3600, first=60, name="watchdog"
        )
        logger.info("🔍 Watchdog scheduled to run every hour.")

        # 🗓 Daily Summary at 21:00 ICT
        summary_time = datetime.now(TIMEZONE).replace(hour=21, minute=0, second=0, microsecond=0)
        delay = (summary_time - datetime.now(TIMEZONE)).total_seconds()
        if delay < 0:
            delay += 86400  # schedule for next day
        application.job_queue.run_repeating(
            daily_summary, interval=86400, first=delay, name="daily_summary"
        )
        logger.info("🗓 Daily summary scheduled for 21:00 ICT.")

    except Exception as e:
        logger.error(f"❌ post_init() failed: {e}")
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text="🚨 post_init() failed — jobs not scheduled!"
        )
async def perform_scan_in(bot, chat_id, context=None):
    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
    driver, (lat, lon) = create_driver()
    screenshot_file = None
    async with driver_lock:
        active_drivers[chat_id] = driver
    try:
        start_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"🕒 Automation started at {start_time} (ICT)")

        # Step 1: Login
        async with driver_lock:
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
        async with driver_lock:
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
        async with driver_lock:
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
            async with driver_lock:  # ADD THIS CHECK
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
        # Cleanup active_drivers
        async with driver_lock:
            if chat_id in active_drivers:
                del active_drivers[chat_id]

        # File cleanup
        for f in [screenshot_file, f"error_{timestamp}.png", f"page_source_{timestamp}.html"]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception as e:
                    logger.error(f"Cleanup error: {str(e)}")

        # Safely quit driver
        if driver:
            driver.quit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    try:
        if not update.message:
            logger.error("⚠️ /start triggered without a message object!")
            return
        await update.message.reply_text(
            "🚀 *Mission Bot Ready!*\n\n"
            "Use /letgo to trigger automation\n"
            "Commands:\n"
            "- /cancelauto: Cancel scheduled missions\n"
            "- /next: Show next mission time\n"
            "- /cancel: Stop ongoing operations",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ /start failed: {str(e)}")

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
        except Exception as e:
            logger.error(f"🚨 Error in mission task: {str(e)}")
            await context.bot.send_message(chat_id, f"❌ Mission error: {str(e)}")
        finally:
            scan_tasks.pop(chat_id, None)

    task = create_task(task_wrapper())
    logger.info("🎯 Mission task scheduled for execution")
    scan_tasks[chat_id] = task

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this command")
        return

    jobs = context.job_queue.jobs()
    if not jobs:
        await update.message.reply_text("📭 No jobs currently scheduled.")
        return

    message = "📋 Scheduled jobs:\n"

    for job in jobs:
        job_time = (
    job.data.astimezone(TIMEZONE).strftime('%a %Y-%m-%d %I:%M %p') if job.data else "⏳ No time info"
)
        message += f"• `{job.name}` → {job_time}\n"

    await update.message.reply_text(message, parse_mode="Markdown")

# Put these above main()

async def main():
    application = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("letgo", letgo))
    application.add_handler(CommandHandler("cancelauto", cancelauto))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("next", next_mission))
    application.add_handler(CommandHandler("status", status))

    await application.initialize()
    await post_init(application)
    await application.start()  # Instead of run_polling()
    await application.updater.start_polling()
    # Start your own dummy aiohttp server for health checks
    app = web.Application()
    app.router.add_get("/healthz", lambda r: web.Response(text="OK"))
    app.router.add_get("/", lambda r: web.Response(text="✅ Bot is alive"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8000)))
    await site.start()

    logger.info("✅ Bot started and HTTP server is up.")

    # Wait forever — keeps everything running
    await asyncio.Event().wait()

# Run the bot
if __name__ == "__main__":
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Render environment: schedule as task
            loop.create_task(main())
        else:
            loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("👋 Shutting down...")
