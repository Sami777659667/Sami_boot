import os
import re
import sys
import zipfile
import asyncio
import subprocess
import signal
import shutil
import warnings
import uuid
import requests
import datetime
import gc
import psutil # 🆕 لإضافة: قياس استهلاك الموارد

# --- إضافات Webhook/Flask ---
from flask import Flask, request, jsonify 

# --- إسكات التحذيرات ---
from telegram.warnings import PTBUserWarning
warnings.filterwarnings("ignore", category=PTBUserWarning)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler

import db

# --- إعدادات التكوين ---
HOST_TOKEN = "8536314905:AAEwZ16dgS4xoU9L8FM0ALSWcRSrlG4ZIVU"   # 🔴 ضع توكن البوت المضيف
ARCHIVE_CHANNEL_ID = -1003210194604     # 🔴 معرف قناة الأرشيف
ADMIN_ID = 7834574830                    # 🔴 معرف المشرف
TRIAL_DURATION = 600 # 10 دقائق (600 ثانية)

# المسارات الأساسية
BASE_DIR = os.path.abspath(os.getcwd())
HOSTING_DIR = os.path.join(BASE_DIR, "hosted_bots")
if not os.path.exists(HOSTING_DIR): os.makedirs(HOSTING_DIR)

# حالات المحادثة
WAITING_UPLOAD = 1
WAITING_TOKEN = 2
WAITING_ADMIN_ACTION = 3

# تهيئة قاعدة البيانات (مهم أن تكون هنا لحل مشكلة OperationalError)
db.init_db()

# --- 1. نظام الطابور (Message Queue) ---
deployment_queue = asyncio.Queue()

async def worker_processor(app: Application):
    """عامل يعمل في الخلفية لمعالجة الطابور"""
    print("👷 Worker started, waiting for tasks...")
    while True:
        task_data = await deployment_queue.get()
        user_id, chat_id, file_info, token, context = task_data
        
        try:
            await process_deployment(user_id, chat_id, file_info, token, context)
        except Exception as e:
            print(f"Queue Error: {e}")
            try:
                await context.bot.send_message(chat_id, f"❌ خطأ داخلي: {e}")
            except: pass
        
        deployment_queue.task_done()

# --- 2. Sandbox & Security ---
# ... (الكود الأصلي لـ SecurityScanner كما هو) ...
class SecurityScanner:
    DANGEROUS_PATTERNS = [
        r'os\.system\(', r'subprocess\.call\(', r'shutil\.rmtree\(',
        r'import\s+os', r'open\(.*w.*\)'
    ]
    @staticmethod
    def scan_directory(folder_path):
        warnings_found = []
        for root, _, files in os.walk(folder_path):
            for file in files:
                if file.endswith(".py"):
                    path = os.path.join(root, file)
                    try:
                        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            for pattern in SecurityScanner.DANGEROUS_PATTERNS:
                                if re.search(pattern, content):
                                    warnings_found.append(f"⚠️ `{file}`: `{pattern}`")
                    except: pass
        return warnings_found

# --- دوال المساعدة ---
# ... (الكود الأصلي لـ smart_inject_token و find_main_file كما هو) ...

def smart_inject_token(folder_path, token):
    token_patterns = [
        r'(TOKEN\s*=\s*)["\'].*?["\']',
        r'(API_KEY\s*=\s*)["\'].*?["\']',
        r'(bot_token\s*=\s*)["\'].*?["\']'
    ]
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f: content = f.read()
                    new_c = content
                    for p in token_patterns:
                        if re.search(p, content, re.IGNORECASE):
                            new_c = re.sub(p, f'\\1"{token}"', new_c, flags=re.IGNORECASE)
                    if content != new_c:
                        with open(path, 'w', encoding='utf-8') as f: f.write(new_c)
                except: pass

def find_main_file(folder_path):
    candidates = ["main.py", "bot.py", "run.py"]
    for f in os.listdir(folder_path):
        if f in candidates: return os.path.join(folder_path, f)
    for root, _, files in os.walk(folder_path):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    with open(path, 'r', errors='ignore') as fr:
                        if "ApplicationBuilder" in fr.read() or "Updater" in fr.read(): return path
                except: continue
    return None

def get_process_resource_usage(pid):
    """🆕 دالة لقياس استهلاك المعالج والذاكرة لعملية معينة."""
    if not pid: return 0, 0
    try:
        proc = psutil.Process(pid)
        cpu_percent = proc.cpu_percent(interval=None) # يتم قياس الاستهلاك منذ آخر استدعاء
        memory_info = proc.memory_info()
        ram_mb = memory_info.rss / (1024 * 1024) # تحويل من بايت إلى ميجابايت
        return cpu_percent, ram_mb
    except psutil.NoSuchProcess:
        return -1, -1 # العملية غير موجودة
    except Exception:
        return 0, 0

async def start_bot_process(bot_id, folder, script_name):
    log_file = os.path.join(folder, "log.txt")
    try:
        with open(log_file, "w") as logs:
            process = subprocess.Popen(
                [sys.executable, script_name], cwd=folder, stdout=logs, stderr=logs, text=True
            )
        await asyncio.sleep(2)
        if process.poll() is not None:
            with open(log_file, 'r') as f: return False, f.read()
        db.update_bot_status(bot_id, "running", process.pid)
        return True, "Started", process.pid
    except Exception as e: return False, str(e), None

def stop_bot_process(pid):
    try: os.kill(pid, signal.SIGTERM); return True
    except: return False
    
async def shutdown_timer_task(bot_id, token, chat_id, application):
    """🆕 إيقاف البوت تلقائيًا بعد انتهاء الفترة التجريبية."""
    await asyncio.sleep(TRIAL_DURATION)
    
    inf = db.get_bot_info(bot_id)
    if inf and inf['status'] == 'running' and inf['pid']:
        stop_bot_process(inf['pid'])
        db.update_bot_status(bot_id, "stopped", None)
        
        await application.bot.send_message(chat_id, 
                                           f"🛑 **انتهت الفترة التجريبية (10 دقائق)**\n"
                                           f"تم إيقاف بوتك **{inf['bot_name']}** (ID: `{bot_id}`).\n"
                                           f"للاستمرار في التشغيل 24/7، يرجى الاشتراك في الخدمة المدفوعة.", 
                                           parse_mode='Markdown')
        print(f"Bot {bot_id} (Trial) shut down automatically.")


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton("🚀 رفع بوت"), KeyboardButton("🤖 بوتاتي")],
          [KeyboardButton("📚 تعليمات"), KeyboardButton("👨‍💻 لوحة المالك") if update.effective_user.id == ADMIN_ID else None]]
    kb = [row for row in kb if row[0] is not None]
    await update.message.reply_text("🖥 **نظام الاستضافة المتقدم**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📦 ZIP/GitHub", callback_data='up_zip'), InlineKeyboardButton("📄 Py (فردي)", callback_data='up_single')],
          [InlineKeyboardButton("❌ إلغاء", callback_data='cancel')]]
    await update.message.reply_text("نوع الملف؟", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_UPLOAD

# ... (handle_choice كما هو) ...
async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == 'cancel': 
        await q.edit_message_text("تم الإلغاء.")
        return ConversationHandler.END
    context.user_data['up_type'] = q.data
    # 🆕 تعديل: لقبول الرابط
    await q.edit_message_text("📤 أرسل الملف المضغوط (.zip) أو رابط مستودع GitHub أو ملف .py الآن.")
    return WAITING_UPLOAD

# 🆕 تعديل receive_file_handler لدعم GitHub/URL (تم التعديل في جلسة سابقة)
async def receive_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    message_text = update.message.text
    file_path = None
    file_name = None
    
    temp_folder = os.path.join(HOSTING_DIR, "temp_downloads")
    os.makedirs(temp_folder, exist_ok=True)
    
    await update.message.reply_text("⏳ جاري معالجة الطلب...")

    if doc and doc.file_name.endswith(('.zip', '.py')):
        # الحالة 1: ملف من تلجرام
        file_id = doc.file_id
        file_name = doc.file_name
        remote_file = await context.bot.get_file(file_id)
        file_path = os.path.join(temp_folder, file_name)
        await remote_file.download_to_drive(file_path)
        
    elif message_text and re.match(r'https?://\S+', message_text):
        # الحالة 2: رابط (GitHub أو URL)
        url = message_text.strip()
        download_url = url
        
        # تحويل رابط GitHub إلى رابط تحميل مضغوط
        if 'github.com' in url and '/archive/refs/heads/' not in url:
            match = re.search(r'github\.com/([^/]+)/([^/]+)', url)
            if match:
                owner, repo = match.groups()
                download_url = f"https://github.com/{owner}/{repo}/archive/refs/main.zip"
                file_name = f"{repo}-main.zip"
            else:
                await update.message.reply_text("❌ لم يتم التعرف على رابط GitHub صالح.")
                return WAITING_UPLOAD
        
        else:
            file_name = "downloaded_bot.zip"
        
        await update.message.reply_text(f"⏳ جارٍ تحميل الملف المضغوط من الرابط...")

        try:
            r = requests.get(download_url, stream=True)
            if r.status_code != 200:
                await update.message.reply_text(f"❌ فشل التحميل من الرابط. رمز الخطأ: {r.status_code}")
                return WAITING_UPLOAD
            
            file_path = os.path.join(temp_folder, file_name)
            with open(file_path, 'wb') as f:
                f.write(r.content) 
            
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ في عملية تحميل الملف: {e}")
            return WAITING_UPLOAD
            
    else:
        await update.message.reply_text("❌ يرجى إرسال الملف المضغوط (`.zip`) أو رابط مستودع GitHub/ZIP.")
        return WAITING_UPLOAD
        
    context.user_data['file_path'] = file_path
    context.user_data['file_name_for_db'] = file_name
    
    await update.message.reply_text("🔑 **أرسل التوكن (Token) لإضافته للطابور.**")
    return WAITING_TOKEN

async def receive_token_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        await update.message.reply_text("❌ توكن غير صالح.")
        return WAITING_TOKEN
    
    # 🆕 التعديل: تمرير file_path إذا كان موجوداً
    task = (update.effective_user.id, update.effective_chat.id, 
            {'file_name': context.user_data.get('file_name_for_db'), 
             'up_type': context.user_data.get('up_type'),
             'file_path': context.user_data.get('file_path')},
            token, context)
    
    await deployment_queue.put(task)
    await update.message.reply_text(f"⏳ **تمت الجدولة!**\nالترتيب: {deployment_queue.qsize()}")
    return ConversationHandler.END


# --- Processing Logic ---
async def process_deployment(user_id, chat_id, file_info, token, context):
    bot_uuid = str(uuid.uuid4())[:8]
    user_folder = os.path.join(HOSTING_DIR, str(user_id), bot_uuid)
    os.makedirs(user_folder, exist_ok=True)
    temp_path = file_info.get('file_path')
    
    if not temp_path or not os.path.exists(temp_path):
         await context.bot.send_message(chat_id, "❌ خطأ في مسار الملف المؤقت.")
         return
    
    # Archive
    archive_fid = None
    if ARCHIVE_CHANNEL_ID:
        try:
            with open(temp_path, 'rb') as f_to_archive: 
                 msg = await context.bot.send_document(ARCHIVE_CHANNEL_ID, f_to_archive, caption=f"Backup: {bot_uuid} | User: {user_id}")
            archive_fid = msg.document.file_id
        except Exception as e: 
            print(f"Archive Error: {e}")
            pass
    
    # Extract & Locate
    target_folder = user_folder
    script_name = ""
    
    if file_info['up_type'] == 'up_zip' or temp_path.endswith('.zip'):
        try:
            with zipfile.ZipFile(temp_path, 'r') as z: z.extractall(user_folder)
            os.remove(temp_path)
            full_main = find_main_file(user_folder)
            if not full_main:
                await context.bot.send_message(chat_id, "❌ لم يتم العثور على ملف التشغيل.")
                return
            target_folder = os.path.dirname(full_main)
            script_name = os.path.basename(full_main)
        except: 
            await context.bot.send_message(chat_id, "❌ ملف تالف.")
            return
    
    else:
        script_name = file_info['file_name']
        final_path = os.path.join(user_folder, script_name)
        shutil.move(temp_path, final_path)
        target_folder = user_folder

    # Security & Inject
    sec_warn = SecurityScanner.scan_directory(target_folder)
    smart_inject_token(target_folder, token)
    
    bot_id = db.add_bot(user_id, file_info['file_name'], target_folder, script_name, archive_fid)
    db.update_bot_token(bot_id, token)
    
    success, msg, pid = await start_bot_process(bot_id, target_folder, script_name)
    warn_txt = f"\n⚠️ أمان: {sec_warn[0]}" if sec_warn else ""
    
    if success:
        await context.bot.send_message(chat_id, f"🎉 **تم التشغيل!**\n🆔 `{bot_id}`{warn_txt}\n\n**ملاحظة:** سيعمل البوت لمدة **10 دقائق** كفترة تجريبية ثم يتوقف تلقائياً.", parse_mode='Markdown')
        # 🆕 تشغيل مؤقت الإيقاف
        context.application.create_task(shutdown_timer_task(bot_id, token, chat_id, context.application))
    else:
        await context.bot.send_message(chat_id, f"❌ فشل التشغيل:\n`{msg[-200:]}`", parse_mode='Markdown')
        db.delete_bot_from_db(bot_id)

# --- Bot Control & Admin Logic ---
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bots = db.get_user_bots(user_id)
    # ... (الكود الأصلي لـ my_bots) ...
    if not bots: return await update.message.reply_text("❌ لم تقم باستضافة أي بوتات بعد.")
    text = "🤖 **لوحة التحكم بالبوتات المستضافة:**\n\n"
    keyboard = []
    for bot_id, bot_name, status, pid in bots:
        status_emoji = "🟢 يعمل" if status == 'running' else "🔴 متوقف"
        text += f"▪️ **{bot_name}** (`ID: {bot_id}`) - {status_emoji}\n"
        row = []
        if status == 'running':
            row.append(InlineKeyboardButton("⏸️ إيقاف", callback_data=f"stop_{bot_id}"))
        else:
            row.append(InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{bot_id}"))
        row.append(InlineKeyboardButton("🗑️ حذف", callback_data=f"del_{bot_id}"))
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')


async def btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act, bid = q.data.split('_')
    bid = int(bid)
    inf = db.get_bot_info(bid)
    
    if not inf: return await q.edit_message_text("❌ لم يتم العثور على البوت.", parse_mode='Markdown')
    bot_name = inf['bot_name']

    if act == "stop":
        if inf['pid']: stop_bot_process(inf['pid'])
        db.update_bot_status(bid, "stopped", None)
        await q.edit_message_text(f"🛑 تم إيقاف البوت: **{bot_name}**.", parse_mode='Markdown')
    
    elif act == "start":
        if inf['status'] == 'running': return await q.edit_message_text(f"البوت **{bot_name}** يعمل بالفعل.", parse_mode='Markdown')
        succ, msg, pid = await start_bot_process(bid, inf['folder_path'], inf['main_file'])
        if succ: 
            await q.edit_message_text(f"🟢 تم تشغيل البوت: **{bot_name}**.", parse_mode='Markdown')
            # 🆕 لا تشغيل فترة تجريبية عند إعادة التشغيل اليدوي
        else: await q.message.reply_text(f"❌ خطأ في التشغيل:\n`{msg[:200]}`", parse_mode='Markdown')
    
    elif act == "del":
        if inf['status'] == 'running':
             await q.edit_message_text(f"❌ يرجى إيقاف البوت **{bot_name}** أولاً قبل حذفه.", parse_mode='Markdown')
             return
             
        try: shutil.rmtree(inf['folder_path'])
        except Exception as e: print(f"Error deleting folder: {e}")
            
        db.delete_bot_from_db(bid)
        await q.edit_message_text(f"🗑️ تم حذف البوت **{bot_name}** نهائياً.", parse_mode='Markdown')


# ----------------------------------------------------------------------
# 👑 Admin Control Panel
# ----------------------------------------------------------------------
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    kb = [[InlineKeyboardButton("📊 إحصائيات البوتات", callback_data='admin_stats')],
          [InlineKeyboardButton("🧹 تنظيف الذاكرة يدوياً", callback_data='admin_cleanup')],
          [InlineKeyboardButton("🔄 تغيير توكن (إدخال)", callback_data='admin_change_token_start')]]
          
    await update.message.reply_text("👑 **لوحة تحكم المالك**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def admin_btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == 'admin_cleanup':
        await q.edit_message_text("⏳ جاري تنفيذ دورة التنظيف الذاتي...")
        # تنفيذ دالة التنظيف مباشرة
        context.application.create_task(cleanup_task(context.application, manual=True, chat_id=q.message.chat_id))
        
    elif data == 'admin_stats':
        await q.edit_message_text("⏳ جاري جلب إحصائيات الموارد...")
        
        running_bots = db.get_all_running_bots()
        output = "📊 **إحصائيات الموارد الحالية:**\n\n"
        
        for bot_id, pid in running_bots:
            bot_info = db.get_bot_info(bot_id)
            if not bot_info: continue
            
            cpu, ram = get_process_resource_usage(pid)
            
            # إذا ماتت العملية (pid=-1)، قم بتحديث الحالة
            if cpu == -1:
                 db.update_bot_status(bot_id, 'stopped', None)
                 output += f"🔴 البوت **{bot_info['bot_name']}** مات! تم تحديث الحالة.\n"
                 continue
                 
            output += f"🤖 **{bot_info['bot_name']}** (ID: {bot_id})\n"
            output += f"  - CPU: {cpu:.2f}%\n"
            output += f"  - RAM: {ram:.2f} MB\n\n"
            
        # إحصائيات البوت المضيف
        host_proc = psutil.Process(os.getpid())
        host_ram = host_proc.memory_info().rss / (1024 * 1024)
        output += f"🖥 **البوت المضيف (Host Bot):**\n  - RAM: {host_ram:.2f} MB\n"

        await q.edit_message_text(output, parse_mode='Markdown')

    elif data == 'admin_change_token_start':
        await q.edit_message_text("🔄 **أرسل ID البوت الجديد والتوكن مفصولين بمسافة (مثال: 12345 Token:AAAA...)**")
        return WAITING_ADMIN_ACTION

async def admin_receive_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    
    try:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) != 2: raise ValueError("تنسيق غير صحيح.")
        bot_id = int(parts[0].strip())
        new_token = parts[1].strip()
        
        inf = db.get_bot_info(bot_id)
        if not inf:
            await update.message.reply_text(f"❌ لم يتم العثور على البوت ID: {bot_id}")
            return WAITING_ADMIN_ACTION
            
        # 1. إيقاف البوت إذا كان يعمل
        if inf['status'] == 'running' and inf['pid']:
            stop_bot_process(inf['pid'])
            db.update_bot_status(bot_id, 'stopped', None)
            
        # 2. تغيير التوكن في قاعدة البيانات والملفات
        db.update_bot_token(bot_id, new_token)
        smart_inject_token(inf['folder_path'], new_token)
        
        # 3. إعادة التشغيل
        succ, msg, pid = await start_bot_process(bot_id, inf['folder_path'], inf['main_file'])
        
        if succ:
            await update.message.reply_text(f"✅ تم تغيير توكن البوت **{inf['bot_name']}** وإعادة تشغيله بنجاح! PID: `{pid}`", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ تم تغيير التوكن لكن فشل التشغيل:\n`{msg[:200]}`", parse_mode='Markdown')
            
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في المعالجة: {e}")

    return ConversationHandler.END


# ----------------------------------------------------------------------
# 🧹 دوال الصيانة الذاتية (Self-Cleanup)
# ----------------------------------------------------------------------

def check_and_cleanup_dead_processes():
    # ... (كما في الكود المقترح سابقاً)
    bots = db.get_all_running_bots()
    cleaned_count = 0
    for bot_id, pid in bots:
        if pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                db.update_bot_status(bot_id, 'stopped', None)
                cleaned_count += 1
            except Exception:
                db.update_bot_status(bot_id, 'stopped', None)
                cleaned_count += 1
    return cleaned_count

def remove_temp_files():
    # ... (كما في الكود المقترح سابقاً)
    temp_folder = os.path.join(HOSTING_DIR, "temp_downloads")
    count = 0
    if os.path.exists(temp_folder):
        try:
             shutil.rmtree(temp_folder)
             count = len(os.listdir(temp_folder)) # تقريبي قبل الحذف
             os.makedirs(temp_folder, exist_ok=True)
        except: pass
    return count

def cleanup_old_context_data(application):
    """🆕 تنظيف بيانات المحادثات غير الضرورية من الذاكرة."""
    # لتنظيف الذاكرة المستخدمة من قبل python-telegram-bot
    context_keys_to_clear = ['user_data', 'chat_data']
    cleaned_count = 0
    
    for context_key in context_keys_to_clear:
        # وصول آمن لبيانات السياق
        if hasattr(application, 'context') and hasattr(application.context, context_key):
             data_dict = getattr(application.context, context_key)
             # تنظيف البيانات القديمة/غير الضرورية هنا
             data_dict.clear() # طريقة مباشرة لكن فعالة لتحرير الذاكرة
             cleaned_count += 1

    return cleaned_count

async def cleanup_task(application: Application, manual=False, chat_id=None):
    """مهمة دورية لتنظيف وإدارة الذاكرة."""
    print("🧹 Auto-Cleanup Cycle Initiated.")
    CLEANUP_INTERVAL = 6 * 60 * 60 # كل 6 ساعات

    if not manual: await asyncio.sleep(CLEANUP_INTERVAL)

    start_time = datetime.datetime.now()
    
    dead_count = check_and_cleanup_dead_processes()
    temp_count = remove_temp_files()
    context_cleaned = cleanup_old_context_data(application)
    collected = gc.collect()
    
    end_time = datetime.datetime.now()
    duration = end_time - start_time
    
    message = (f"🧹 **دورة التنظيف الذاتي اكتملت!** ({duration.total_seconds():.2f}s)\n"
               f"   - تم إيقاف عمليات ميتة: `{dead_count}`\n"
               f"   - تم حذف ملفات مؤقتة: `{temp_count}`\n"
               f"   - تم تحرير كائنات RAM: `{collected}`")
    
    print(message)
    
    if manual and chat_id:
         await application.bot.send_message(chat_id, message, parse_mode='Markdown')
    elif chat_id != ADMIN_ID: # إرسال التقرير للمالك بشكل دوري
         await application.bot.send_message(ADMIN_ID, message, parse_mode='Markdown')


# ----------------------------------------------------------------------
# 👑 تعريف تطبيق Flask (لـ Webhook و Ping)
# ----------------------------------------------------------------------
flask_app = Flask(__name__)
WEBHOOK_PATH = f"/{HOST_TOKEN}"

# 🆕 مسار التأكد من أن الخادم حي (للرد على GET/المراقب الداخلي والخارجي)
@flask_app.route('/', methods=['GET'])
@flask_app.route('/ping', methods=['GET'])
def health_check():
    """مسار مشترك للرقابة الداخلية والخارجية (للاحتياط)."""
    return 'Server is awake and ready.', 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
# ... (بقية دالة telegram_webhook كما هي) ...
async def telegram_webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), app.bot)
        await app.update_queue.put(update)
    return jsonify({"status": "ok"})

# ... (دالة set_webhook كما هي) ...
async def set_webhook():
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("REPLIT_HOST")
    if WEBHOOK_URL:
        full_webhook_url = f"https://{WEBHOOK_URL.replace('http://', '').split('/')[0]}{WEBHOOK_PATH}"
        await app.bot.set_webhook(url=full_webhook_url)
        print(f"✅ Webhook Set To: {full_webhook_url}")
    else:
        print("❌ Webhook not set (URL not found).")


# ----------------------------------------------------------------------
# 🚀 نقطة التشغيل الرئيسية
# ----------------------------------------------------------------------

async def post_init(application: Application):
    
    # 1. تشغيل مهمة العامل في الخلفية (Worker)
    asyncio.create_task(worker_processor(application))
    
    # 2. تشغيل مهمة التنظيف الذاتي (دائماً تعمل)
    asyncio.create_task(cleanup_task(application))
    
    # 3. Webhook (إذا كانت البيئة تدعمه)
    if os.environ.get("RENDER") or os.environ.get("REPLIT_HOST"):
         await set_webhook()

if __name__ == '__main__':
    
    app = ApplicationBuilder().token(HOST_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🚀"), upload_start)],
        states={
            WAITING_UPLOAD: [CallbackQueryHandler(handle_choice), 
                             MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, receive_file_handler)],
            WAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token_handler)],
            WAITING_ADMIN_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_action)] # 🆕 حالة المالك
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_start, filters=filters.User(ADMIN_ID))) # 🆕 أمر المالك
    app.add_handler(CallbackQueryHandler(admin_btn_handler, pattern="^admin_")) # 🆕 أزرار المالك
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Regex("^🤖"), my_bots))
    app.add_handler(MessageHandler(filters.Regex("^👨‍💻"), admin_start))
    app.add_handler(CallbackQueryHandler(btn_handler, pattern="^(start|stop|del)_"))
    
    # 🌟 التشغيل بناءً على البيئة
    if os.environ.get("RENDER") or os.environ.get("REPLIT_HOST"):
        print("✅ Advanced Hosting Server Ready for Webhook.")
        # Webhook: يتم تشغيله بواسطة Gunicorn (خارجيًا)
    
    else:
        print("✅ Advanced Hosting Server Running (Polling Mode: 1.0s Heartbeat)...")
        # 🚨 التعديل الحاسم: ضبط poll_interval على 1.0 ثانية لمنع الخمول الدائم
        app.run_polling(poll_interval=1.0)
