# tele_bot_interaktif_final_fix_menu.py
import os, io, time, asyncio, threading, logging, json, base64
from datetime import datetime, timedelta
from collections import defaultdict

import requests.exceptions
import matplotlib
matplotlib.use("Agg")  # backend headless untuk server
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

import firebase_admin
from firebase_admin import credentials, db
from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter
from google.auth.exceptions import TransportError
from google.oauth2 import service_account  # Firestore creds

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("fish-bot")

# =========================
# Konfigurasi via ENV VARS
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # wajib
CHAT_ID = int(os.getenv("BOT_CHAT_ID", "0"))  # wajib → ganti di env
RTDB_URL = os.getenv("RTDB_URL")  # wajib: ex https://xxxxx-default-rtdb.asia-southeast1.firebasedatabase.app/
GA_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()  # opsional (fallback)
GA_JSON_RAW = os.getenv("FIREBASE_CREDENTIALS_JSON", "").strip()   # prefer

if not BOT_TOKEN:
    raise RuntimeError("Env BOT_TOKEN belum diset.")
if CHAT_ID == 0:
    raise RuntimeError("Env BOT_CHAT_ID belum diset.")
if not RTDB_URL:
    raise RuntimeError("Env RTDB_URL belum diset.")

def _extract_json_blob(s: str) -> str:
    """
    Ambil substring JSON dari string yang mungkin mengandung prefix/suffix seperti
    '${{shared.FIREBASE_CREDENTIALS_JSON}}{ ... }' dari Railway.
    """
    if not s:
        return s
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return s
    return s[start:end+1]

def _load_credentials():
    """
    Prioritas:
      1) FIREBASE_CREDENTIALS_JSON (string JSON / base64)
      2) GOOGLE_APPLICATION_CREDENTIALS (path file)
    Return: (cred_dict, sa_credentials)
      - cred_dict untuk firebase_admin.credentials.Certificate
      - sa_credentials untuk Firestore (google.oauth2.service_account.Credentials)
    """
    # 1) Dari ENV JSON string / base64
    if GA_JSON_RAW:
        blob = _extract_json_blob(GA_JSON_RAW)
        cred_dict = None

        # Coba parse langsung sebagai JSON
        try:
            cred_dict = json.loads(blob)
        except json.JSONDecodeError:
            # Coba decode base64 -> JSON
            try:
                decoded = base64.b64decode(blob).decode("utf-8")
                cred_dict = json.loads(decoded)
            except Exception as e:
                raise RuntimeError(f"FIREBASE_CREDENTIALS_JSON tidak valid: {e}")

        # Normalisasi private_key
        pk = cred_dict.get("private_key")
        if isinstance(pk, str):
            # tangani literal '\\n' dan CRLF
            cred_dict["private_key"] = pk.replace("\\n", "\n").replace("\r\n", "\n")

        # Service account credentials untuk Firestore
        sa_credentials = service_account.Credentials.from_service_account_info(cred_dict)
        return cred_dict, sa_credentials

    # 2) Fallback: dari file path
    if GA_PATH and os.path.exists(GA_PATH):
        # Baca dict untuk firebase_admin
        try:
            with open(GA_PATH, "r", encoding="utf-8") as f:
                cred_dict = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Gagal membaca file kredensial {GA_PATH}: {e}")

        pk = cred_dict.get("private_key")
        if isinstance(pk, str):
            cred_dict["private_key"] = pk.replace("\\n", "\n").replace("\r\n", "\n")

        # Firestore credential dari file
        sa_credentials = service_account.Credentials.from_service_account_file(GA_PATH)
        return cred_dict, sa_credentials

    raise RuntimeError("Credential Firebase tidak ditemukan. Set FIREBASE_CREDENTIALS_JSON atau GOOGLE_APPLICATION_CREDENTIALS.")

# =========================
# Inisialisasi Firebase & Firestore
# =========================
cred_dict, sa_credentials = _load_credentials()

if not firebase_admin._apps:
    app_cred = credentials.Certificate(cred_dict)  # langsung dari dict
    firebase_admin.initialize_app(app_cred, {'databaseURL': RTDB_URL})

# Firestore client pakai credentials dari dict
fs_client = firestore.Client(project=cred_dict.get("project_id"), credentials=sa_credentials)

# =========================
# Event loop (untuk thread listener)
# =========================
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# =========================
# Helper Firebase dengan retry
# =========================
def safe_get(ref, retries=3, delay=5):
    for i in range(retries):
        try:
            return ref.get()
        except (requests.exceptions.ConnectionError, TransportError) as e:
            logger.warning(f"[Firebase] Gagal get (retry {i+1}/{retries}): {e}")
            time.sleep(delay)
    return None

def safe_set(ref, value, retries=3, delay=5):
    for i in range(retries):
        try:
            ref.set(value)
            return True
        except (requests.exceptions.ConnectionError, TransportError) as e:
            logger.warning(f"[Firebase] Gagal set (retry {i+1}/{retries}): {e}")
            time.sleep(delay)
    return False

# =========================
# Variabel global
# =========================
feed_schedules = []       # list "HHMM"
last_feed_time = None
FEED_COOLDOWN = timedelta(minutes=1)

LOW_TEMP_THRESHOLD = float(os.getenv("LOW_TEMP_THRESHOLD", "24.0"))
HIGH_TEMP_THRESHOLD = float(os.getenv("HIGH_TEMP_THRESHOLD", "30.0"))
NTU_THRESHOLD_HIGH  = int(os.getenv("NTU_THRESHOLD_HIGH", "1500"))
NTU_THRESHOLD_MAX   = int(os.getenv("NTU_THRESHOLD_MAX", "3000"))

last_notified_low_temp_date = None
last_notified_high_temp_date = None
last_notified_high_ntu_date = None
last_notified_feed = None

# Conversation states
SETJAM, PERIODE, TANGGAL, TIPE_GRAFIK, SETJAM_HARIAN = range(5)

# =========================
# UI Helpers
# =========================
def main_menu_keyboard():
    keyboard = [
        ["/pakan", "/jadwal"],
        ["/setjam", "/hapusjam"],
        ["/monitoring", "/grafik"],
        ["/riwayat"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def fill_missing_dates(data, start_dt, end_dt, field):
    daily_values = defaultdict(list)
    for d in data:
        try:
            ts = datetime.strptime(d['timestamp'], "%Y-%m-%d_%H-%M")
        except Exception:
            ts = d['timestamp']
        date_key = ts.strftime("%Y-%m-%d")
        if d.get(field) is not None:
            daily_values[date_key].append(d[field])

    date_list, avg_values = [], []
    current_date = start_dt
    while current_date <= end_dt:
        date_str = current_date.strftime("%Y-%m-%d")
        date_list.append(date_str)
        if daily_values[date_str]:
            avg = sum(daily_values[date_str]) / len(daily_values[date_str])
        else:
            avg = 0
        avg_values.append(avg)
        current_date += timedelta(days=1)

    return date_list, avg_values

# =========================
# Realtime DB Helpers
# =========================
def load_feed_schedules():
    global feed_schedules
    ref = db.reference('/aquarium/schedule')
    data = safe_get(ref)
    feed_schedules = []

    if data:
        values = list(data.values()) if isinstance(data, dict) else data
        for v in values:
            if isinstance(v, str) and ":" in v:
                v = v.replace(":", "")
            if len(v) == 3:
                v = "0" + v
            if len(v) == 4 and v.isdigit():
                feed_schedules.append(v)

def save_feed_schedules():
    global feed_schedules
    ref = db.reference('/aquarium/schedule')
    safe_set(ref, {str(i): f"{v[:2]}:{v[2:]}" for i, v in enumerate(sorted(set(feed_schedules)))})

def get_realtime_sensors():
    ref = db.reference('/aquarium/sensors')
    data = safe_get(ref)
    if not data:
        return None
    return {
        "temperature": data.get("temperature", "--.-"),
        "ntu": data.get("ntu", "---"),
        "temp_status": data.get("temp_status", "--"),
        "ntu_status": data.get("ntu_status", "--")
    }

# =========================
# Firestore Helpers
# =========================
def fetch_sensor_history_firestore(start_date, end_date):
    results = []
    sensor_history_ref = fs_client.collection("sensorHistory")

    query = sensor_history_ref.where(
        filter=FieldFilter("timestamp", ">=", start_date)
    ).where(
        filter=FieldFilter("timestamp", "<=", end_date)
    )

    for doc in query.stream():
        data = doc.to_dict()
        results.append({
            "timestamp": data.get("timestamp", doc.id),
            "temperature": data.get("temperature", None),
            "ntu": data.get("ntu", None)
        })

    results.sort(key=lambda x: x["timestamp"])
    return results

def _parse_ts(ts_raw):
    if isinstance(ts_raw, datetime):
        return ts_raw
    try:
        return datetime.strptime(ts_raw, "%Y-%m-%d_%H-%M")
    except Exception:
        return None

def fetch_feed_history_firestore(limit=10, date_filter: str=None, id_exact: str=None):
    col = fs_client.collection("historyFeed")

    if id_exact:
        snap = col.document(id_exact).get()
        if not snap.exists:
            return []
        data = snap.to_dict() or {}
        return [{
            "timestamp": data.get("timestamp", snap.id),
            "mode": data.get("mode", "Unknown"),
            "raw": data
        }]

    if date_filter:
        start = f"{date_filter}_00-00"
        end   = f"{date_filter}_23-59"
        q = col.where(
            filter=FieldFilter("timestamp", ">=", start)
        ).where(
            filter=FieldFilter("timestamp", "<=", end)
        ).order_by("timestamp", direction=firestore.Query.ASCENDING)

        results = []
        for doc in q.stream():
            data = doc.to_dict() or {}
            results.append({
                "timestamp": data.get("timestamp", doc.id),
                "mode": data.get("mode", "Unknown"),
                "raw": data
            })
        return results

    q = col.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(max(1, limit))
    results = []
    for doc in q.stream():
        data = doc.to_dict() or {}
        results.append({
            "timestamp": data.get("timestamp", doc.id),
            "mode": data.get("mode", "Unknown"),
            "raw": data
        })
    return results

# =========================
# Grafik Helpers
# =========================
def create_plot(data, field, title, ylabel):
    timestamps = []
    for d in data:
        try:
            ts = datetime.strptime(d['timestamp'], "%Y-%m-%d_%H-%M")
        except Exception:
            ts = d['timestamp']
        timestamps.append(ts)

    values = [d[field] if d[field] is not None else 0 for d in data]

    plt.figure(figsize=(10,4))
    plt.plot(timestamps, values, marker='o')
    plt.xticks(rotation=45, ha='right')
    plt.title(title)
    plt.ylabel(ylabel)
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

def create_plot_mingguan(data, field, title, ylabel, start_dt=None, end_dt=None):
    if start_dt is None or end_dt is None:
        timestamps = []
        for d in data:
            try:
                ts = datetime.strptime(d['timestamp'], "%Y-%m-%d_%H-%M")
            except Exception:
                ts = d['timestamp']
            timestamps.append(ts)
        if timestamps:
            start_dt = min(timestamps)
            end_dt = max(timestamps)
        else:
            today = datetime.now()
            start_dt = today
            end_dt = today

    dates, avg_values = fill_missing_dates(data, start_dt, end_dt, field)
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]

    plt.figure(figsize=(10,4))
    plt.plot(date_objs, avg_values, marker='o')
    plt.title(title)
    plt.ylabel(ylabel)
    plt.xlabel("Tanggal")
    plt.grid(True, linestyle="--", alpha=0.7)

    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%d-%m"))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator(interval=1))
    plt.xticks(rotation=45, ha='right')

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

def create_dual_plot(data, start_date, end_date):
    timestamps = []
    for d in data:
        try:
            ts = datetime.strptime(d['timestamp'], "%Y-%m-%d_%H-%M")
        except Exception:
            ts = d['timestamp']
        timestamps.append(ts)

    suhu_values = [d['temperature'] if d['temperature'] is not None else 0 for d in data]
    ntu_values = [d['ntu'] if d['ntu'] is not None else 0 for d in data]

    fig, ax1 = plt.subplots(figsize=(10,4))
    ax1.set_xlabel("Waktu")
    ax1.set_ylabel("Suhu (°C)", color="tab:red")
    ax1.plot(timestamps, suhu_values, color="tab:red", marker='o', label="Suhu")
    ax1.tick_params(axis='y', labelcolor="tab:red")
    ax1.grid(True, linestyle="--", alpha=0.7)

    ax2 = ax1.twinx()
    ax2.set_ylabel("NTU", color="tab:blue")
    ax2.plot(timestamps, ntu_values, color="tab:blue", marker='x', label="NTU")
    ax2.tick_params(axis='y', labelcolor="tab:blue")

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    plt.title(f"Suhu & NTU ({start_date} s/d {end_date})")
    fig.autofmt_xdate()
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

def create_dual_plot_mingguan(data, start_date_str, end_date_str):
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")

    dates_suhu, avg_suhu = fill_missing_dates(data, start_dt, end_dt, 'temperature')
    dates_ntu, avg_ntu = fill_missing_dates(data, start_dt, end_dt, 'ntu')

    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates_suhu]

    fig, ax1 = plt.subplots(figsize=(10,4))
    ax1.plot(date_objs, avg_suhu, color="tab:red", marker='o', label="Rata-rata Suhu")
    ax1.set_xlabel("Tanggal")
    ax1.set_ylabel("Rata-rata Suhu (°C)", color="tab:red")
    ax1.tick_params(axis='y', labelcolor="tab:red")
    ax1.grid(True, linestyle="--", alpha=0.7)

    ax2 = ax1.twinx()
    ax2.plot(date_objs, avg_ntu, color="tab:blue", marker='x', label="Rata-rata NTU")
    ax2.set_ylabel("Rata-rata NTU", color="tab:blue")
    ax2.tick_params(axis='y', labelcolor="tab:blue")

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m"))
    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=1))
    fig.autofmt_xdate(rotation=45)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    plt.title(f"Rata-rata Suhu & NTU ({start_date_str} – {end_date_str})")
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

# =========================
# Command Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = ("🐟 *Bot Pakan Ikan Otomatis*\n\n"
           "🔵 /pakan → Pakan sekarang\n"
           "📅 /jadwal → Lihat jadwal\n"
           "🕒 /setjam → Ubah jadwal (interaktif)\n"
           "❌ /hapusjam X → Hapus jadwal ke-X\n"
           "🔎 /monitoring → Cek Suhu & NTU\n"
           "📊 /grafik → Buat grafik sensor\n"
           "📜 /riwayat → Riwayat pakan")
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())

# Pakan manual
async def pakan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_feed_time
    now = datetime.now()

    if last_feed_time and now - last_feed_time < FEED_COOLDOWN:
        sisa = FEED_COOLDOWN - (now - last_feed_time)
        await update.message.reply_text(
            f"⏳ Tunggu {sisa.seconds} detik lagi sebelum memberi pakan lagi.",
            reply_markup=main_menu_keyboard()
        )
        return

    last_feed_time = now
    safe_set(db.reference('/aquarium/control/feed'), True)

    timestamp_str = now.strftime("%d %B %Y %H:%M")
    await update.message.reply_text(
        f"🐟 Pakan ikan dikirim!\nPakan Manual diberikan pada {timestamp_str}",
        reply_markup=main_menu_keyboard()
    )

# Notifikasi pakan otomatis
async def check_auto_feed(context: ContextTypes.DEFAULT_TYPE):
    global last_notified_feed
    now = datetime.now()
    current_hm = now.strftime("%H%M")
    if not feed_schedules:
        return

    for sched in feed_schedules:
        if current_hm == sched and sched != last_notified_feed:
            last_notified_feed = sched
            timestamp_str = now.strftime("%d %B %Y %H:%M")
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🐟 *Pakan Otomatis*\n\nMode: Otomatis\nWaktu: {timestamp_str}",
                parse_mode="Markdown"
            )
            safe_set(
                db.reference('/aquarium/control/last_feed'),
                {"time": now.strftime("%Y-%m-%d_%H-%M"), "mode": "Otomatis"}
            )

# Listener suhu & NTU gabungan dari /aquarium/sensors
def start_temperature_listener(bot, loop):
    ref = db.reference('/aquarium/sensors')

    def listener(event):
        try:
            data = event.data
            if not isinstance(data, dict):
                return
            temp = data.get("temperature")
            ntu  = data.get("ntu")
            timestamp = data.get("time")
            asyncio.run_coroutine_threadsafe(
                check_temperature_ntu_alert(bot, temp, ntu, timestamp),
                loop
            )
        except Exception as e:
            logger.error(f"Listener error: {e}")

    try:
        ref.listen(listener)
    except Exception as e:
        logger.error(f"Gagal start listener Firebase: {e}")

async def check_temperature_ntu_alert(bot, temp, ntu, timestamp=None):
    global last_notified_low_temp_date, last_notified_high_temp_date, last_notified_high_ntu_date

    if timestamp:
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M")
        except Exception:
            dt = datetime.now()
    else:
        dt = datetime.now()

    timestamp_str = dt.strftime("%d %B %Y %H:%M")
    today_str = dt.strftime("%Y-%m-%d")

    # Suhu rendah
    if temp is not None and temp < LOW_TEMP_THRESHOLD:
        if last_notified_low_temp_date != today_str:
            last_notified_low_temp_date = today_str
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ *Suhu Rendah Terdeteksi*\n\nSuhu: {temp}°C\nWaktu: {timestamp_str}",
                parse_mode="Markdown"
            )

    # Suhu tinggi
    if temp is not None and temp > HIGH_TEMP_THRESHOLD:
        if last_notified_high_temp_date != today_str:
            last_notified_high_temp_date = today_str
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ *Suhu Tinggi Terdeteksi*\n\nSuhu: {temp}°C\nWaktu: {timestamp_str}",
                parse_mode="Markdown"
            )

    # NTU tinggi (sekali per hari)
    if ntu is not None and NTU_THRESHOLD_HIGH < ntu <= NTU_THRESHOLD_MAX:
        if last_notified_high_ntu_date != today_str:
            last_notified_high_ntu_date = today_str
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ *Kekeruhan Tinggi Terdeteksi*\n\nNTU: {ntu}\nWaktu: {timestamp_str}",
                parse_mode="Markdown"
            )

# Jadwal
async def jadwal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not feed_schedules:
        await update.message.reply_text("📅 Jadwal belum tersedia.", reply_markup=main_menu_keyboard())
        return
    msg = "📅 *Jadwal Pemberian Pakan*\n"
    for i, sch in enumerate(sorted(set(feed_schedules)), start=1):
        msg += f"{i}. {sch[:2]}:{sch[2:]}\n"
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())

# Setjam interaktif
async def setjam_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌚ Masukkan jadwal baru (contoh: 07:00,12:30,18:45)")
    return SETJAM

async def setjam_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    times = text.replace(" ", "").split(",")
    new_schedules = []
    for t in times:
        t = t.replace(":", "")
        if len(t) == 3:
            t = "0" + t
        if len(t) == 4 and t.isdigit():
            new_schedules.append(t)

    if new_schedules:
        global feed_schedules
        feed_schedules = sorted(set(new_schedules))
        save_feed_schedules()
        teks = "\n".join(f"- {s[:2]}:{s[2:]}" for s in feed_schedules)
        await update.message.reply_text("✅ Jadwal tersimpan:\n" + teks, reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("❌ Format salah, gunakan HH:MM", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def setjam_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Perubahan jadwal dibatalkan.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# Hapus jam
async def hapusjam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        index = int(text.split()[1])
        if index < 1 or index > len(feed_schedules):
            await update.message.reply_text("❌ Index jadwal tidak valid.", reply_markup=main_menu_keyboard())
            return
        feed_schedules.pop(index-1)
        save_feed_schedules()
        await update.message.reply_text(f"✅ Jadwal ke-{index} berhasil dihapus.", reply_markup=main_menu_keyboard())
    except Exception:
        await update.message.reply_text("❌ Format salah. Contoh: /hapusjam 1", reply_markup=main_menu_keyboard())

# Monitoring
async def monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_realtime_sensors()
    if not data:
        await update.message.reply_text("❌ Gagal ambil data sensor", reply_markup=main_menu_keyboard())
        return
    msg = (f"🔎 *Monitoring Aquarium*\n\n"
           f"🌡 Suhu: {data['temperature']}°C ({data['temp_status']})\n"
           f"💧 NTU : {data['ntu']} ({data['ntu_status']})\n")
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())

# Grafik interaktif
async def grafik_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Harian (Suhu+NTU)"],
        ["Mingguan", "Bulanan"],
        ["Batal"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("📊 Pilih periode grafik:", reply_markup=markup)
    return PERIODE

async def grafik_periode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text == "batal":
        await update.message.reply_text("❌ Dibatalkan.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    if "harian" in text:
        context.user_data['periode'] = "harian"
        await update.message.reply_text("Masukkan tanggal (YYYY-MM-DD):", reply_markup=ReplyKeyboardRemove())
        return TANGGAL
    elif text == "mingguan":
        context.user_data['periode'] = "mingguan"
        await update.message.reply_text("Masukkan tanggal akhir (YYYY-MM-DD):", reply_markup=ReplyKeyboardRemove())
        return TANGGAL
    elif text == "bulanan":
        context.user_data['periode'] = "bulanan"
        await update.message.reply_text("Masukkan tanggal awal bulan (YYYY-MM-DD):", reply_markup=ReplyKeyboardRemove())
        return TANGGAL
    else:
        await update.message.reply_text("❌ Pilih Harian / Mingguan / Bulanan.", reply_markup=main_menu_keyboard())
        return PERIODE

async def grafik_tanggal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        selected_date = datetime.strptime(text, "%Y-%m-%d")
        periode = context.user_data['periode']

        if periode == "bulanan":
            start_dt = selected_date
            end_dt = selected_date + timedelta(days=29)

            start_date_str = start_dt.strftime("%Y-%m-%d")
            end_date_str = end_dt.strftime("%Y-%m-%d")

            data = fetch_sensor_history_firestore(start_dt.strftime("%Y-%m-%d_00-00"), end_dt.strftime("%Y-%m-%d_23-59"))
            buf1 = create_plot_mingguan(data, 'temperature', f"Suhu Bulanan", "°C", start_dt, end_dt)
            buf2 = create_plot_mingguan(data, 'ntu', f"NTU Bulanan", "NTU", start_dt, end_dt)
            buf3 = create_dual_plot_mingguan(data, start_date_str, end_date_str)

            await update.message.reply_photo(photo=buf1, caption="📊 Grafik Suhu (Bulanan)")
            await update.message.reply_photo(photo=buf2, caption="📊 Grafik NTU (Bulanan)")
            await update.message.reply_photo(photo=buf3, caption="📊 Grafik Suhu+NTU (Bulanan)", reply_markup=main_menu_keyboard())
            return ConversationHandler.END

        elif periode == "harian":
            start_dt = selected_date
            end_dt = selected_date

            context.user_data['start_date'] = start_dt.strftime("%Y-%m-%d")
            context.user_data['end_date'] = end_dt.strftime("%Y-%m-%d")

            await update.message.reply_text(
                "⌚ Masukkan jam awal dan jam akhir (format HH:MM-HH:MM), contoh: 08:00-18:00\n"
                "Jika ingin seluruh hari, ketik: 00:00-23:59",
                reply_markup=ReplyKeyboardRemove()
            )
            return SETJAM_HARIAN

        elif periode == "mingguan":
            start_dt = selected_date - timedelta(days=6)
            end_dt = selected_date

            start_date_str = start_dt.strftime("%Y-%m-%d")
            end_date_str = end_dt.strftime("%Y-%m-%d")

            data = fetch_sensor_history_firestore(start_dt.strftime("%Y-%m-%d_00-00"), end_dt.strftime("%Y-%m-%d_23-59"))
            buf1 = create_plot_mingguan(data, 'temperature', f"Suhu Mingguan", "°C", start_dt, end_dt)
            buf2 = create_plot_mingguan(data, 'ntu', f"NTU Mingguan", "NTU", start_dt, end_dt)
            buf3 = create_dual_plot_mingguan(data, start_date_str, end_date_str)

            await update.message.reply_photo(photo=buf1, caption="📊 Grafik Suhu (Mingguan)")
            await update.message.reply_photo(photo=buf2, caption="📊 Grafik NTU (Mingguan)")
            await update.message.reply_photo(photo=buf3, caption="📊 Grafik Suhu+NTU (Mingguan)", reply_markup=main_menu_keyboard())
            return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"❌ Format tanggal salah. Gunakan YYYY-MM-DD\n{str(e)}", reply_markup=main_menu_keyboard())
        return TANGGAL

async def grafik_setjam_harian(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        jam_awal, jam_akhir = text.split("-")
        datetime.strptime(jam_awal, "%H:%M")
        datetime.strptime(jam_akhir, "%H:%M")
        context.user_data['jam_awal'] = jam_awal
        context.user_data['jam_akhir'] = jam_akhir

        await update.message.reply_text(
            "Pilih Suhu, NTU, Semua grafik:",
            reply_markup=ReplyKeyboardMarkup([["Suhu","NTU","Semua"]], one_time_keyboard=True, resize_keyboard=True)
        )
        return TIPE_GRAFIK

    except Exception:
        await update.message.reply_text(
            "❌ Format salah. Gunakan HH:MM-HH:MM, contoh: 08:00-18:00",
            reply_markup=ReplyKeyboardRemove()
        )
        return SETJAM_HARIAN

async def grafik_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Batal melihat grafik.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def grafik_tipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    periode = context.user_data.get('periode')
    tipe = update.message.text.lower()

    if periode == "harian":
        jam_awal = context.user_data.get('jam_awal', "00:00")
        jam_akhir = context.user_data.get('jam_akhir', "23:59")
        start_date_full = context.user_data['start_date'] + f"_{jam_awal.replace(':','-')}"
        end_date_full   = context.user_data['end_date'] + f"_{jam_akhir.replace(':','-')}"

        data = fetch_sensor_history_firestore(start_date_full, end_date_full)
        if not data:
            await update.message.reply_text("❌ Data sensor tidak tersedia.", reply_markup=main_menu_keyboard())
            return ConversationHandler.END

        start_date = context.user_data['start_date']
        end_date = context.user_data['end_date']

        if tipe == "suhu":
            buf = create_plot(data, 'temperature', f"Suhu ({start_date} s/d {end_date})", "°C")
            await update.message.reply_photo(photo=buf, caption="📊 Grafik Suhu", reply_markup=main_menu_keyboard())
        elif tipe == "ntu":
            buf = create_plot(data, 'ntu', f"Kekeruhan NTU ({start_date} s/d {end_date})", "NTU")
            await update.message.reply_photo(photo=buf, caption="📊 Grafik NTU", reply_markup=main_menu_keyboard())
        elif "suhu+ntu" in tipe or "semua" in tipe:
            buf = create_dual_plot(data, start_date, end_date)
            await update.message.reply_photo(photo=buf, caption="📊 Grafik Suhu & NTU", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("❌ Pilih Suhu / NTU / Suhu+NTU", reply_markup=main_menu_keyboard())
            return TIPE_GRAFIK

    return ConversationHandler.END

# Riwayat pakan
async def riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /riwayat
    /riwayat YYYY-MM-DD
    /riwayat YYYY-MM-DD_HH-MM
    """
    args = context.args if hasattr(context, "args") else []
    id_exact = None
    date_filter = None
    title = "📜 *Riwayat Pakan Hari Ini*"

    if args:
        param = args[0].strip()
        if "_" in param:
            id_exact = param
            title = f"📜 *Riwayat Pakan (Dokumen {param})*"
        else:
            try:
                datetime.strptime(param, "%Y-%m-%d")
                date_filter = param
                title = f"📜 *Riwayat Pakan ({param})*"
            except Exception:
                await update.message.reply_text(
                    "❌ Format salah.\nGunakan salah satu:\n"
                    "/riwayat\n"
                    "/riwayat YYYY-MM-DD\n"
                    "/riwayat YYYY-MM-DD_HH-MM",
                    reply_markup=main_menu_keyboard()
                )
                return
    else:
        date_filter = datetime.now().strftime("%Y-%m-%d")

    rows = fetch_feed_history_firestore(
        limit=200 if (date_filter or id_exact) else 10,
        date_filter=date_filter,
        id_exact=id_exact
    )

    if not rows:
        notf = "Tidak ada riwayat pakan ditemukan."
        if id_exact:
            notf = f"Dokumen {id_exact} tidak ditemukan."
        elif date_filter:
            notf = f"Tidak ada event pakan pada tanggal {date_filter}."
        await update.message.reply_text(f"❌ {notf}", reply_markup=main_menu_keyboard())
        return

    msg = f"{title}\n\n"
    for i, d in enumerate(rows, start=1):
        ts_raw = d.get("timestamp", "")
        ts_obj = _parse_ts(ts_raw)
        waktu = ts_obj.strftime("%d %B %Y %H:%M") if ts_obj else str(ts_raw)
        mode = d.get("mode", "Unknown")
        msg += f"{i}. {waktu} → Mode: {mode}\n"

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())

# NTU alert direct (jaga-jaga listener per field)
async def check_ntu_alert_direct(bot, ntu, timestamp=None):
    try:
        if ntu is None:
            return

        if timestamp:
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M")
            except Exception:
                dt = datetime.now()
        else:
            dt = datetime.now()

        today_str = dt.strftime("%Y-%m-%d")
        timestamp_str = dt.strftime("%d %B %Y %H:%M")

        if not hasattr(check_ntu_alert_direct, "last_notified_date"):
            check_ntu_alert_direct.last_notified_date = None

        if 1501 <= ntu <= 3000:
            if check_ntu_alert_direct.last_notified_date != today_str:
                check_ntu_alert_direct.last_notified_date = today_str
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"⚠️ *NTU Keruh Terdeteksi*\n\nNTU: {ntu}\nWaktu: {timestamp_str}",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Gagal mengirim notif Telegram: {e}")
        else:
            check_ntu_alert_direct.last_notified_date = None
    except Exception as e:
        logger.error(f"Error di check_ntu_alert_direct: {e}")

# =========================
# Main App
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Load jadwal
    load_feed_schedules()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("pakan", pakan))
    app.add_handler(CommandHandler("jadwal", jadwal))
    app.add_handler(CommandHandler("hapusjam", hapusjam))
    app.add_handler(CommandHandler("monitoring", monitoring))
    app.add_handler(CommandHandler("riwayat", riwayat))

    # Jobs
    app.job_queue.run_repeating(check_auto_feed, interval=60, first=5)

    # Conversation: setjam
    setjam_handler = ConversationHandler(
        entry_points=[CommandHandler("setjam", setjam_start)],
        states={ SETJAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, setjam_input)] },
        fallbacks=[CommandHandler("batal", setjam_cancel)]
    )
    app.add_handler(setjam_handler)

    # Conversation: grafik (lengkap dengan SETJAM_HARIAN di dalam states)
    grafik_handler = ConversationHandler(
        entry_points=[CommandHandler('grafik', grafik_start)],
        states={
            PERIODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, grafik_periode)],
            TANGGAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, grafik_tanggal)],
            SETJAM_HARIAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, grafik_setjam_harian)],
            TIPE_GRAFIK: [MessageHandler(filters.TEXT & ~filters.COMMAND, grafik_tipe)]
        },
        fallbacks=[CommandHandler('batal', grafik_cancel)]
    )
    app.add_handler(grafik_handler)

    # Listener sensors (jalan di thread terpisah)
    threading.Thread(target=start_temperature_listener, args=(app.bot, loop), daemon=True).start()

    logger.info("Bot siap berjalan (long polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
