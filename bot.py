import os
import time
import logging
import json
import hmac
import hashlib
import re
import shutil
import subprocess
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import RPi.GPIO as GPIO
import tinytuya
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, WebAppInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

def env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Missing {name} in .env (see .env.example)")
    return value

TELEGRAM_TOKEN = env("TELEGRAM_TOKEN", required=True)
ACCESS_CODES = {'admin': env("ACCESS_CODE_ADMIN", required=True), 'tech': env("ACCESS_CODE_TECH", required=True)}
if env("ACCESS_CODE_PROSFORA"):
    ACCESS_CODES['prosfora'] = env("ACCESS_CODE_PROSFORA")
ROLE_NAMES = {'admin': "Администратор", 'tech': "Техник", 'prosfora': "Просфоры"}
PERMS = {
    'admin': {'boiler', 'relay', 'light', 'admin', 'schedule', 'history'},
    'tech': {'boiler', 'relay', 'schedule', 'history'},
    'prosfora': {'temple_light'},
}
MAX_SCHEDULES = 20
DATA_DIR = env("DATA_DIR", BASE_DIR)
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
SCHEDULE_WINDOW_MIN = 10
MAX_ATTEMPTS = 5
BLOCK_SEC = 600
LIGHT_POLL_SEC = 60
PUMP_POLL_SEC = 5
WEB_PORT = int(env("WEB_PORT", "8080"))
WEBAPP_URL = env("WEBAPP_URL", "")
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
TZ = ZoneInfo(env("TIMEZONE", "Europe/Kyiv"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def now_str():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")

# ==================== TUYA ====================
def load_tuya_devices():
    try:
        with open(DEVICES_FILE) as f:
            return {d['name']: d for d in json.load(f)}
    except Exception as e:
        log.error(f"Failed to load devices.json: {e}")
        return {}

tuya_devices = load_tuya_devices()

SOCKET_NAME = env("PUMP_SOCKET_NAME", "WiFi Socket")
RELAY_NAME = env("LIGHT_RELAY_NAME", "DIN-рейка Wi-Fi выключатель с измерением")

last_seen = {}
fails = {}
cache = {'pump': None, 'relay': None, 'light': None}
tuya_lock = Lock()
RELAY_POLL_SEC = 5

def get_tuya_device(name):
    dev = tuya_devices.get(name)
    if not dev:
        return None
    d = tinytuya.OutletDevice(dev['id'], dev['ip'], dev['key'])
    d.set_version(float(dev['version']))
    d.set_socketTimeout(3)
    d.set_socketRetryLimit(2)
    return d

def tuya_status(name):
    try:
        d = get_tuya_device(name)
        if not d:
            return None
        with tuya_lock:
            result = d.status()
    except Exception as e:
        log.error(f"Status error {name}: {e}")
        fails[name] = fails.get(name, 0) + 1
        return None
    dps = result.get('dps') if isinstance(result, dict) else None
    if not dps:
        log.warning(f"No dps from {name}: {result}")
        fails[name] = fails.get(name, 0) + 1
        return None
    fails[name] = 0
    last_seen[name] = now_str()
    return dps

def tuya_set(name, state: bool):
    try:
        d = get_tuya_device(name)
        if not d:
            return False
        with tuya_lock:
            result = d.set_status(state, switch=1)
        return not (isinstance(result, dict) and result.get('Error'))
    except Exception as e:
        log.error(f"Set error {name}: {e}")
        return False

CLOUD_FILE = os.path.join(DATA_DIR, "tinytuya.json")
FLOWERS_ID = env("HOME_LIGHT_DEVICE_ID", "")
_cloud = None

def get_cloud():
    global _cloud
    if _cloud is None:
        with open(CLOUD_FILE) as f:
            c = json.load(f)
        _cloud = tinytuya.Cloud(apiRegion=c['apiRegion'], apiKey=c['apiKey'], apiSecret=c['apiSecret'])
    return _cloud

def cloud_info(dev_id):
    try:
        r = get_cloud().cloudrequest(f'/v1.0/devices/{dev_id}')
    except Exception as e:
        log.error(f"Cloud info error {dev_id}: {e}")
        return None
    if not isinstance(r, dict) or not r.get('success'):
        log.warning(f"Cloud info failed {dev_id}: {r}")
        return None
    return r.get('result')

def cloud_set(dev_id, state: bool):
    try:
        r = get_cloud().cloudrequest(f'/v1.0/devices/{dev_id}/commands', action='POST',
                                     post={'commands': [{'code': 'switch_1', 'value': state}]})
        return bool(isinstance(r, dict) and r.get('success'))
    except Exception as e:
        log.error(f"Cloud set error {dev_id}: {e}")
        return False

LINE = "━━━━━━━━━━━━━━"

def card(title, body):
    return f"{title}\n{LINE}\n\n{body}\n\n{LINE}\n🕐 {now_str()}"

def unavailable_text(title, name):
    body = "Состояние: ⚠️ *Недоступно*\nНет связи с устройством"
    if name in last_seen:
        body += f"\nПоследний ответ: {last_seen[name]}"
    return card(title, body)

# ==================== MOTOR ====================
MOTOR_PINS = [int(p) for p in env("MOTOR_PINS", "17,18,27,22").split(",")]
MOTOR_DELAY = 0.003
STATE_FILE = os.path.join(DATA_DIR, "motor_state.json")
BACKLASH = 0

TEMP_STEPS = {
    'off': 0,
    35:  326,
    40:  354,
    45:  382,
    50:  410,
    55:  438,
    60:  466,
    65:  494,
    70:  522,
}

SEQ_FWD = [[1,0,0,0],[1,1,0,0],[0,1,0,0],[0,1,1,0],[0,0,1,0],[0,0,1,1],[0,0,0,1],[1,0,0,1]]
SEQ_BWD = list(reversed(SEQ_FWD))

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
for p in MOTOR_PINS:
    GPIO.setup(p, GPIO.OUT)
    GPIO.output(p, GPIO.LOW)

motor_lock = Lock()

startup_notes = []

def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def read_json_dict(path):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data

def load_state():
    try:
        return read_json_dict(STATE_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error(f"State file is damaged: {e}")
        try:
            shutil.copy2(STATE_FILE, f"{STATE_FILE}.corrupt-{int(time.time())}")
        except OSError:
            pass
    try:
        data = read_json_dict(STATE_FILE + ".bak")
        log.warning("State restored from backup copy")
        startup_notes.append("⚠️ Файл настроек был повреждён (возможно, пропадало питание). "
                             "Данные восстановлены из резервной копии, последние изменения могли не сохраниться.")
        return data
    except Exception:
        return {}

def ensure_files():
    os.makedirs(WEBAPP_DIR, exist_ok=True)
    if not os.path.exists(HISTORY_FILE):
        open(HISTORY_FILE, 'a').close()
        log.info("History file created")
    for path, label in ((DEVICES_FILE, "devices.json"),) + (((CLOUD_FILE, "tinytuya.json"),) if FLOWERS_ID else ()):
        if not os.path.exists(path):
            log.error(f"{label} not found: {path}")
            startup_notes.append(f"⚠️ Не найден файл {label}: связь с устройствами не заработает.")
    for name in (SOCKET_NAME, RELAY_NAME):
        if name not in tuya_devices:
            log.error(f"Device not found in devices.json: {name}")
            startup_notes.append(f"⚠️ В devices.json нет устройства «{name}».")
    if not os.path.exists(os.path.join(WEBAPP_DIR, "index.html")):
        log.warning("webapp/index.html is missing")
        startup_notes.append("⚠️ Не найден webapp/index.html: панель не откроется.")

state_lock = Lock()

def save_state():
    data = {
        'motor_position': motor_position,
        'mode': boiler_mode,
        'temple_temp': temple_temp,
        'moving': motor_moving,
        'users': users,
        'group_id': group_id,
        'light': light_state,
        'relay': relay_state,
        'schedules': schedules,
    }
    try:
        with state_lock:
            if os.path.exists(STATE_FILE):
                shutil.copy2(STATE_FILE, STATE_FILE + ".bak")
            atomic_write(STATE_FILE, json.dumps(data, ensure_ascii=False))
    except Exception as e:
        log.error(f"Save error: {e}")

ensure_files()
_state = load_state()
motor_position = _state.get('motor_position', 0)
temple_temp = _state.get('temple_temp', 45)
boiler_mode = _state.get('mode')
if boiler_mode not in ('off', 'self', 'temple'):
    if _state.get('heat_mode'):
        boiler_mode = 'self'
    else:
        boiler_mode = 'off' if motor_position == 0 else 'temple'
users = _state.get('users', {})
group_id = _state.get('group_id')
light_state = _state.get('light')
motor_moving = _state.get('moving')
relay_state = _state.get('relay')
schedules = _state.get('schedules', [])

# ==================== HISTORY ====================
history_lock = Lock()

def _load_history():
    items = []
    try:
        with open(HISTORY_FILE) as f:
            lines = f.readlines()
        if len(lines) > 1500:
            lines = lines[-800:]
            atomic_write(HISTORY_FILE, "".join(lines))
        for line in lines[-500:]:
            try:
                items.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return items

history = _load_history()

def history_add(kind, text, scope='all', who=None):
    entry = {'ts': time.time(), 'kind': kind, 'text': text, 'scope': scope, 'who': who}
    with history_lock:
        history.append(entry)
        del history[:-500]
        try:
            with open(HISTORY_FILE, 'a') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error(f"History write error: {e}")

def history_for(role, offset=0, limit=10, kind='all'):
    with history_lock:
        items = [e for e in reversed(history)
                 if (role == 'admin' or e.get('scope') != 'admin')
                 and (kind == 'all' or e.get('kind') == kind)]
    return items[offset:offset + limit + 1]

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

def day_label(ts):
    d = datetime.fromtimestamp(ts, TZ).date()
    today = datetime.now(TZ).date()
    if d == today:
        return "Сегодня"
    if (today - d).days == 1:
        return "Вчера"
    return f"{d.day} {MONTHS[d.month - 1]}"

def clock(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")

def md_escape(text):
    return re.sub(r'[_*`\[\]]', ' ', str(text or ""))

if motor_moving:
    note = (f"⚠️ Питание или бот пропадали, пока двигался мотор "
            f"(шаги {motor_moving.get('from')} → {motor_moving.get('to')}). "
            "Положение клапана могло сбиться, проверьте его.")
    startup_notes.append(note)
    history_add('boiler', note)
    motor_moving = None
    save_state()
elif not os.path.exists(STATE_FILE):
    save_state()
    log.info("State file created")

def motor_release():
    for p in MOTOR_PINS:
        GPIO.output(p, GPIO.LOW)

motor_progress = {'busy': False, 'stage': 'idle', 'title': '', 'done': 0, 'total': 0,
                  'from': 0, 'to': 0, 'result': None, 'finished_at': 0}

def _run_steps(seq, steps):
    for n in range(steps):
        for s in seq:
            for i, p in enumerate(MOTOR_PINS):
                GPIO.output(p, s[i])
            time.sleep(MOTOR_DELAY)
        motor_progress['done'] = n + 1
    motor_release()

def motor_move_to(target):
    global motor_position, motor_moving
    with motor_lock:
        diff = target - motor_position
        if diff == 0:
            return
        motor_moving = {'from': motor_position, 'to': target}
        save_state()
        motor_progress.update(done=0, total=abs(diff) + (BACKLASH if diff < 0 else 0),
                              **{'from': motor_position, 'to': target})
        if diff > 0:
            log.info(f"FWD: {motor_position} -> {target}")
            _run_steps(SEQ_FWD, diff)
        else:
            total_back = BACKLASH + (motor_position - target)
            log.info(f"BWD: {motor_position} -> {target} ({total_back} steps)")
            _run_steps(SEQ_BWD, total_back)
        motor_position = target
        motor_moving = None
        save_state()

MODE_NAMES = {'off': "Выключено", 'self': "Самообогрев котельной", 'temple': "Обогрев храма"}

def current_label():
    if motor_position == 0:
        return "выключен"
    for label, steps in TEMP_STEPS.items():
        if label != 'off' and motor_position == steps:
            return f"{label}°C"
    return f"{motor_position} шагов"

# ==================== TELEGRAM ====================
bot = TeleBot(TELEGRAM_TOKEN, threaded=False)

def make_main_keyboard(role):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    names = [('boiler', "🏭 Котельная"), ('relay', "⚡️ Реле"), ('schedule', "⏰ Расписание"),
             ('history', "📜 История"), ('light', "🏠 Свет дома"), ('temple_light', "⛪️ Свет в храме")]
    kb.add(*[KeyboardButton(label) for feature, label in names if feature in PERMS[role]])
    return kb

def make_boiler_inline():
    kb = InlineKeyboardMarkup(row_width=4)
    kb.row(InlineKeyboardButton(("✅ " if boiler_mode == 'off' else "") + "⛔️ Выключить котёл и насос",
                                callback_data="mode_off"))
    kb.row(InlineKeyboardButton(("✅ " if boiler_mode == 'self' else "") + "🏠 Самообогрев котельной",
                                callback_data="mode_self"))
    temps = [35, 40, 45, 50, 55, 60, 65, 70]
    buttons = []
    for t in temps:
        mark = "✅ " if boiler_mode == 'temple' and t == temple_temp else ""
        buttons.append(InlineKeyboardButton(f"{mark}{t}°", callback_data=f"set_{t}"))
    kb.row(*buttons[:4])
    kb.row(*buttons[4:])
    kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="boiler_refresh"))
    return kb

def get_boiler_text():
    if boiler_mode == 'off':
        mode = "⛔️ *Выключено*\nКотёл и насос выключены"
        boiler = "выключен"
    elif boiler_mode == 'self':
        mode = "🏠 *Самообогрев котельной*\nКотёл на 35°C, насос выключен"
        boiler = "35°C"
    else:
        mode = f"🔥 *Обогрев храма*\nКотёл на {temple_temp}°C, насос включён"
        boiler = f"{temple_temp}°C"
    body = f"{mode}\n\n🌡 Котёл: *{boiler}*\n{pump_text()}"
    body += ("\n\n_⛔️ выключает котёл и насос_\n"
             "_🏠 котёл 35°, насос выключен_\n"
             "_35°…70° обогрев храма: насос включается_")
    return card("🏭 *КОТЕЛЬНАЯ*", body)

def pump_text():
    dps = tuya_status(SOCKET_NAME)
    if dps is None:
        text = "💧 Насос: ⚠️ *Недоступно*"
        if SOCKET_NAME in last_seen:
            text += f"\nПоследний ответ: {last_seen[SOCKET_NAME]}"
        return text
    text = f"💧 Насос: {'✅ включён' if dps.get('1') else '❌ выключен'}"
    return text + f"\n⚡️ Напряжение: *{dps.get('20', 0) / 10} В*"

mode_lock = Lock()

def act_text(action, temp=None):
    if action == 'off':
        return "⛔️ Выключить котёл и насос"
    if action == 'self':
        return "🏠 Самообогрев котельной"
    return f"🔥 Обогрев храма {temp}°"

def mode_done_text(mode, temp):
    if mode == 'off':
        return "⛔️ Котёл и насос выключены"
    if mode == 'self':
        return "🏠 Самообогрев котельной: котёл 35°, насос выключен"
    return f"🔥 Обогрев храма: котёл {temp}°, насос включён"

def apply_mode(mode, temp=None, who=None):
    global boiler_mode, temple_temp
    with mode_lock:
        if mode == 'off':
            target, pump_on, title = TEMP_STEPS['off'], False, "Выключаю котёл и насос"
        elif mode == 'self':
            target, pump_on, title = TEMP_STEPS[35], False, "Включаю самообогрев котельной"
        else:
            if temp:
                temple_temp = temp
            target, pump_on, title = TEMP_STEPS[temple_temp], True, f"Ставлю {temple_temp}°C"
        motor_progress.update(busy=True, stage='motor', title=title, done=0, total=0, result=None)
        ok = False
        try:
            motor_move_to(target)
            boiler_mode = mode
            save_state()
            motor_progress['stage'] = 'pump'
            ok = tuya_set(SOCKET_NAME, pump_on)
            if not ok:
                log.error("Pump switch failed")
            cache['pump'] = tuya_status(SOCKET_NAME)
        finally:
            motor_progress.update(busy=False, stage='idle', result='ok' if ok else 'error',
                                  finished_at=time.time())
        text = mode_done_text(mode, temple_temp)
        if not ok:
            text += " ⚠️ насос не переключился"
        history_add('boiler', text, who=who)
        return ok

def make_relay_inline():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("✅ Вкл", callback_data="relay_on"),
        InlineKeyboardButton("❌ Выкл", callback_data="relay_off"),
    )
    kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="relay_refresh"))
    return kb

def get_relay_text():
    dps = tuya_status(RELAY_NAME)
    if dps is None:
        return unavailable_text("⚡️ *РЕЛЕ (храм)*", RELAY_NAME)
    state = dps.get('1', False)
    body = f"Состояние: {'✅ *включено*' if state else '❌ *выключено*'}\n\n"
    body += f"🔌 Напряжение: *{dps.get('20', 0) / 10} В*\n"
    body += f"〰️ Ток: *{dps.get('18', 0) / 1000:.3f} А*\n"
    body += f"💡 Мощность: *{dps.get('19', 0) / 10} Вт*"
    return card("⚡️ *РЕЛЕ (храм)*", body)

def make_light_inline(uid):
    kb = InlineKeyboardMarkup(row_width=2)
    if can(uid, 'admin'):
        kb.row(
            InlineKeyboardButton("✅ Вкл", callback_data="light_on"),
            InlineKeyboardButton("❌ Выкл", callback_data="light_off"),
        )
    kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="light_refresh"))
    return kb

def fmt_dur(sec):
    m = int(sec) // 60
    return f"{m // 60}:{m % 60:02d}"

def get_light_text():
    info = cloud_info(FLOWERS_ID)
    title = "💡 *СВЕТ ДОМА*"
    if info is None:
        body = "⚠️ *Нет связи с облаком*"
        if light_state:
            word = "есть" if light_state['online'] else "нет"
            since = datetime.fromtimestamp(light_state['since'], TZ).strftime("%d.%m %H:%M")
            body += f"\nПоследнее известное: свет {word} (с {since})"
        return card(title, body)
    online = bool(info.get('online'))
    st = {i['code']: i['value'] for i in info.get('status', [])}
    body = "✅ *Свет есть*" if online else "❌ *Света нет*"
    if light_state and light_state['online'] == online:
        body += f"\n{'Присутствует' if online else 'Отсутствует'}: *{fmt_dur(time.time() - light_state['since'])}*"
    if online:
        last_seen[FLOWERS_ID] = now_str()
        body += f"\n\n🔌 Напряжение: *{st.get('cur_voltage', 0) / 10} В*"
        body += f"\n💡 Мощность: *{st.get('cur_power', 0) / 10} Вт*"
        body += f"\n🪴 Лампа: {'включена' if st.get('switch_1') else 'выключена'}"
    elif FLOWERS_ID in last_seen:
        body += f"\n\nПоследний ответ: {last_seen[FLOWERS_ID]}"
    return card(title, body)

def safe_edit(chat_id, message_id, text, markup):
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                              reply_markup=markup, parse_mode='Markdown')
    except Exception as e:
        if "message is not modified" not in str(e):
            log.error(f"Edit error: {e}")

# ==================== ACCESS ====================
def role_of(uid):
    return users.get(str(uid), {}).get('role')

def can(uid, feature):
    role = role_of(uid)
    return bool(role) and feature in PERMS[role]

attempts = {}

def ask_code(chat_id):
    bot.send_message(chat_id, "🔐 Введите код доступа", reply_markup=ReplyKeyboardRemove())

def guard(message, feature):
    if message.chat.type != 'private':
        return False
    if can(message.from_user.id, feature):
        return True
    ask_code(message.chat.id)
    return False

def try_code(message):
    uid = message.from_user.id
    count, blocked_until = attempts.get(uid, (0, 0))
    if blocked_until > time.time():
        left = int((blocked_until - time.time()) // 60) + 1
        bot.send_message(message.chat.id, f"⛔️ Слишком много попыток. Повторите через {left} мин.")
        return
    code = (message.text or "").strip().lower()
    role = next((r for r, c in ACCESS_CODES.items() if c == code), None)
    if role:
        attempts.pop(uid, None)
        users[str(uid)] = {'role': role, 'name': message.from_user.first_name or "", 'seen': BOT_VERSION}
        save_state()
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        bot.send_message(message.chat.id, f"✅ Доступ открыт: *{ROLE_NAMES[role]}*",
                         reply_markup=make_main_keyboard(role), parse_mode='Markdown')
        if 'boiler' in PERMS[role]:
            send_panel(message.chat.id)
        return
    count += 1
    if count >= MAX_ATTEMPTS:
        attempts[uid] = (0, time.time() + BLOCK_SEC)
        bot.send_message(message.chat.id, f"⛔️ Доступ заблокирован на {BLOCK_SEC // 60} мин.")
    else:
        attempts[uid] = (count, 0)
        bot.send_message(message.chat.id, f"❌ Неверный код. Осталось попыток: {MAX_ATTEMPTS - count}")

# ==================== SCHEDULE / HISTORY UI ====================
wizard = {}

def user_name(uid, fallback=""):
    return users.get(str(uid), {}).get('name') or fallback

def sched_list_text():
    if not schedules:
        body = ("Расписаний пока нет.\n\nНажмите «➕ Добавить расписание». Например:\n"
                "«Каждый день в 06:00 — обогрев храма 50°».")
    else:
        lines = []
        for sc in schedules:
            icon = "✅" if sc['enabled'] else "⏸"
            lines.append(f"{icon} *{sc['time']}* · {days_text(sc['days'])}\n      {act_text(sc['action'], sc.get('temp'))}")
        body = "\n\n".join(lines)
        nxt = next_overall()
        body += f"\n\n🔜 Ближайшее: {next_label(nxt[0])}" if nxt else "\n\nВсе расписания приостановлены"
    return card("⏰ *РАСПИСАНИЕ*", body)

def sched_list_markup():
    kb = InlineKeyboardMarkup()
    for sc in schedules:
        icon = "✅" if sc['enabled'] else "⏸"
        kb.row(InlineKeyboardButton(
            f"{icon} {sc['time']} · {days_text(sc['days'])} · {short_act(sc['action'], sc.get('temp'))}",
            callback_data=f"sch_open_{sc['id']}"))
    if len(schedules) < MAX_SCHEDULES:
        kb.row(InlineKeyboardButton("➕ Добавить расписание", callback_data="sw_new"))
    return kb

def sched_detail(sc):
    state = "✅ включено" if sc['enabled'] else "⏸ приостановлено"
    body = f"*{sc['time']}* · {days_text(sc['days'])}\n{act_text(sc['action'], sc.get('temp'))}\n\nСтатус: {state}"
    if sc['enabled']:
        d = next_run(sc)
        if d:
            body += f"\n🔜 Ближайшее: {next_label(d)}"
    if sc.get('by'):
        body += f"\nСоздал: {md_escape(sc['by'])}"
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⏸ Приостановить" if sc['enabled'] else "▶️ Возобновить",
                                callback_data=f"sch_tog_{sc['id']}"))
    kb.row(InlineKeyboardButton("🗑 Удалить", callback_data=f"sch_del_{sc['id']}"))
    kb.row(InlineKeyboardButton("‹ К списку", callback_data="sch_list"))
    return card("⏰ *РАСПИСАНИЕ*", body), kb

def wizard_view(uid):
    d = wizard.get(uid)
    if d is None:
        return card("➕ *НОВОЕ РАСПИСАНИЕ*", "Черновик устарел. Начните заново."), sched_list_markup()
    step = d['step']
    kb = InlineKeyboardMarkup()
    cancel = InlineKeyboardButton("✖️ Отмена", callback_data="sw_x")
    if step == 'action':
        text = "Что нужно сделать?"
        kb.row(InlineKeyboardButton("⛔️ Выключить котёл и насос", callback_data="sw_a_off"))
        kb.row(InlineKeyboardButton("🏠 Самообогрев котельной", callback_data="sw_a_self"))
        kb.row(InlineKeyboardButton("🔥 Обогрев храма", callback_data="sw_a_temple"))
        kb.row(cancel)
    elif step == 'temp':
        text = "Какую температуру котла поставить?\nНасос при этом включится."
        temps = [35, 40, 45, 50, 55, 60, 65, 70]
        kb.row(*[InlineKeyboardButton(f"{t}°", callback_data=f"sw_t_{t}") for t in temps[:4]])
        kb.row(*[InlineKeyboardButton(f"{t}°", callback_data=f"sw_t_{t}") for t in temps[4:]])
        kb.row(cancel)
    elif step == 'hour':
        text = f"{act_text(d['action'], d.get('temp'))}\n\nВо сколько? Выберите час:"
        for row in range(4):
            kb.row(*[InlineKeyboardButton(f"{h:02d}", callback_data=f"sw_h_{h:02d}")
                     for h in range(row * 6, row * 6 + 6)])
        kb.row(cancel)
    elif step == 'minute':
        text = f"{act_text(d['action'], d.get('temp'))}\n\nВремя: *{d['hour']}:__*\nВыберите минуты:"
        for row in range(3):
            kb.row(*[InlineKeyboardButton(f"{m:02d}", callback_data=f"sw_m_{m:02d}")
                     for m in range(row * 20, row * 20 + 20, 5)])
        kb.row(cancel)
    else:
        days = d['days']
        text = (f"{act_text(d['action'], d.get('temp'))}\nВремя: *{d['time']}*\n\n"
                f"По каким дням?\nСейчас выбрано: *{days_text(days) if days else 'ничего'}*")
        kb.row(*[InlineKeyboardButton(("✅ " if i in days else "▫️ ") + DAY_NAMES[i], callback_data=f"sw_d_{i}")
                 for i in range(4)])
        kb.row(*[InlineKeyboardButton(("✅ " if i in days else "▫️ ") + DAY_NAMES[i], callback_data=f"sw_d_{i}")
                 for i in range(4, 7)])
        kb.row(InlineKeyboardButton("Каждый день", callback_data="sw_p_all"),
               InlineKeyboardButton("Будни", callback_data="sw_p_work"),
               InlineKeyboardButton("Выходные", callback_data="sw_p_end"))
        if days:
            kb.row(InlineKeyboardButton("💾 Сохранить расписание", callback_data="sw_save"))
        kb.row(cancel)
    return card("➕ *НОВОЕ РАСПИСАНИЕ*", text), kb

def handle_sched_cb(call):
    data, uid = call.data, call.from_user.id
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    who = user_name(uid, call.from_user.first_name)

    def show(text, kb):
        safe_edit(chat_id, msg_id, text, kb)

    if data == 'sch_list' or data == 'sw_x':
        wizard.pop(uid, None)
        show(sched_list_text(), sched_list_markup())
    elif data.startswith('sch_open_'):
        sc = find_schedule(int(data[9:]))
        show(*(sched_detail(sc) if sc else (sched_list_text(), sched_list_markup())))
    elif data.startswith('sch_tog_'):
        sc = toggle_schedule(int(data[8:]), who)
        show(*(sched_detail(sc) if sc else (sched_list_text(), sched_list_markup())))
    elif data.startswith('sch_del_'):
        sc = find_schedule(int(data[8:]))
        if sc:
            kb = InlineKeyboardMarkup()
            kb.row(InlineKeyboardButton("🗑 Да, удалить", callback_data=f"sch_yes_{sc['id']}"),
                   InlineKeyboardButton("Отмена", callback_data=f"sch_open_{sc['id']}"))
            show(card("⏰ *РАСПИСАНИЕ*", f"Удалить расписание?\n\n{sched_summary(sc)}"), kb)
        else:
            show(sched_list_text(), sched_list_markup())
    elif data.startswith('sch_yes_'):
        delete_schedule(int(data[8:]), who)
        show(sched_list_text(), sched_list_markup())
    elif data == 'sw_new':
        wizard[uid] = {'step': 'action', 'days': list(range(7))}
        show(*wizard_view(uid))
    else:
        d = wizard.get(uid)
        if d is None:
            show(*wizard_view(uid))
        elif data.startswith('sw_a_'):
            d['action'] = data[5:]
            d['temp'] = None
            d['step'] = 'temp' if d['action'] == 'temple' else 'hour'
            show(*wizard_view(uid))
        elif data.startswith('sw_t_'):
            d['temp'] = int(data[5:])
            d['step'] = 'hour'
            show(*wizard_view(uid))
        elif data.startswith('sw_h_'):
            d['hour'] = data[5:]
            d['step'] = 'minute'
            show(*wizard_view(uid))
        elif data.startswith('sw_m_'):
            d['time'] = f"{d['hour']}:{data[5:]}"
            d['step'] = 'days'
            show(*wizard_view(uid))
        elif data.startswith('sw_d_'):
            i = int(data[5:])
            d['days'] = sorted(set(d['days']) ^ {i})
            show(*wizard_view(uid))
        elif data.startswith('sw_p_'):
            d['days'] = {'all': list(range(7)), 'work': [0, 1, 2, 3, 4], 'end': [5, 6]}[data[5:]]
            show(*wizard_view(uid))
        elif data == 'sw_save':
            sc, err = add_schedule(d, who)
            wizard.pop(uid, None)
            if sc:
                show(sched_list_text(), sched_list_markup())
            else:
                show(card("⏰ *РАСПИСАНИЕ*", "Не удалось сохранить расписание."), sched_list_markup())

HIST_PAGE = 10
HIST_KINDS = [('all', "Все"), ('boiler', "Котёл"), ('light', "Свет")]

def hist_view(role, kind, offset):
    items = history_for(role, offset, HIST_PAGE, kind)
    more = len(items) > HIST_PAGE
    items = items[:HIST_PAGE]
    if not items:
        body = "Пока записей нет."
    else:
        lines, last = [], None
        for e in items:
            label = day_label(e['ts'])
            if label != last:
                lines.append(f"{'' if last is None else chr(10)}📅 *{label}*")
                last = label
            who = f" · {md_escape(e['who'])}" if e.get('who') else ""
            lines.append(f"{clock(e['ts'])}  {e['text']}{who}")
        body = "\n".join(lines)
    kb = InlineKeyboardMarkup()
    kb.row(*[InlineKeyboardButton(("• " if k == kind else "") + name, callback_data=f"hist_{k}_0")
             for k, name in HIST_KINDS])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◂ Новее", callback_data=f"hist_{kind}_{max(0, offset - HIST_PAGE)}"))
    if more:
        nav.append(InlineKeyboardButton("Ещё ▸", callback_data=f"hist_{kind}_{offset + HIST_PAGE}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton("🔄 Обновить", callback_data=f"hist_{kind}_{offset}"))
    return card("📜 *ИСТОРИЯ*", body), kb

# ==================== NEWS ====================
BOT_VERSION = "1.1.0"
PREV_VERSIONS = ("2026-11-26", "1.0.0", "1.0.1")

def update_text(role):
    text = ("🔧 *Обновление " + BOT_VERSION + "*\n\n"
            "⛪️ Свет в храме бот определяет по двум устройствам: розетке (она работает от аккумулятора) и реле. "
            "Если отвечает только розетка, света нет.\n\n"
            "🔕 Бот больше не пишет, когда пропадает связь: в чат приходят только сообщения о свете.\n\n"
            "🥖 Новая роль «Просфоры»: получает только уведомления о свете в храме и может нажать «⛪️ Свет в храме», чтобы узнать, как сейчас.\n")
    return text + "\nПодробности всегда можно открыть командой /news"

def news_text(role, seen=None):
    if role == 'prosfora':
        return ("⛪️ *Свет в храме*\n\nБот пришлёт сообщение, когда свет в храме отключат или включат, "
                "и скажет, сколько это длилось. Узнать, как сейчас, можно кнопкой «⛪️ Свет в храме».")
    if seen in PREV_VERSIONS:
        return update_text(role)
    text = ("🆕 *Бот теперь умеет больше!*\n\n"
            "⏰ *Расписание*: котёл сам включится и выключится в нужное время. Кнопка «⏰ Расписание».\n\n"
            "📜 *История*: кто и когда менял режимы, когда пропадал и появлялся свет.\n\n"
            "🖥 *Панель*: кнопка «Панель» рядом с полем ввода. Живой прогресс мотора и всё управление в одном окне.\n\n"
            "⛪️ *Свет в храме*: бот пришлёт «Свет отключили» или «Свет включили» и скажет, сколько это длилось. "
            "Он проверяет два устройства: розетку (на аккумуляторе) и реле. Если отвечает только розетка, света нет.\n\n"
            "🥖 *Роль «Просфоры»*: получает только уведомления о свете в храме.\n\n"
            "🔎 Бот сам находит устройства в сети, если у них поменялся адрес.\n")
    if role == 'admin':
        text += ("\n🏠 *Свет дома*: отдельная кнопка и уведомления в группу. "
                 "Сообщения про храм подписаны «⛪️ Храм», про дом «🏠 Дом».\n")
    return text + "\nВерсия " + BOT_VERSION + ". Эту справку всегда можно открыть командой /news"

PERMANENT_ERRORS = ("403", "blocked", "chat not found", "deactivated", "user not found")

def post_start_loop():
    note_text = "\n".join(startup_notes)
    note_pending = {int(u) for u, d in users.items() if d['role'] == 'admin'} if note_text else set()
    while True:
        waiting = False
        for uid, u in list(users.items()):
            if u.get('seen') == BOT_VERSION:
                continue
            try:
                bot.send_message(int(uid), news_text(u['role'], u.get('seen')), reply_markup=make_main_keyboard(u['role']),
                                 parse_mode='Markdown')
                u['seen'] = BOT_VERSION
                save_state()
            except Exception as e:
                if any(k in str(e).lower() for k in PERMANENT_ERRORS):
                    u['seen'] = BOT_VERSION
                    save_state()
                else:
                    waiting = True
        for uid in list(note_pending):
            try:
                bot.send_message(uid, "Проверка при запуске:\n" + note_text)
                note_pending.discard(uid)
            except Exception as e:
                if any(k in str(e).lower() for k in PERMANENT_ERRORS):
                    note_pending.discard(uid)
                else:
                    waiting = True
        if not waiting:
            break
        time.sleep(30)

# ==================== HANDLERS ====================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if message.chat.type != 'private':
        return
    role = role_of(message.from_user.id)
    if not role:
        ask_code(message.chat.id)
        return
    bot.send_message(message.chat.id, f"⚡️ Бот запущен!\nДоступ: *{ROLE_NAMES[role]}*",
                     reply_markup=make_main_keyboard(role), parse_mode='Markdown')
    if 'boiler' in PERMS[role]:
        send_panel(message.chat.id)

@bot.message_handler(commands=["news"])
def cmd_news(message):
    if message.chat.type != 'private':
        return
    role = role_of(message.from_user.id)
    if not role:
        ask_code(message.chat.id)
        return
    bot.send_message(message.chat.id, news_text(role), parse_mode='Markdown')

@bot.message_handler(commands=["logout"])
def cmd_logout(message):
    if message.chat.type != 'private':
        return
    users.pop(str(message.from_user.id), None)
    save_state()
    bot.send_message(message.chat.id, "👋 Вы вышли. Для входа введите код доступа.",
                     reply_markup=ReplyKeyboardRemove())

@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not guard(message, 'admin'):
        return
    lines = ["👥 Пользователи\n"]
    for uid, u in users.items():
        lines.append(f"{u.get('name') or '—'} · {ROLE_NAMES[u['role']]} · `{uid}`")
    lines.append(f"\nГруппа уведомлений: `{group_id}`" if group_id else "\nГруппа уведомлений не задана")
    lines.append("Отозвать доступ: /revoke ID")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode='Markdown')

@bot.message_handler(commands=["revoke"])
def cmd_revoke(message):
    if not guard(message, 'admin'):
        return
    parts = message.text.split()
    if len(parts) != 2 or parts[1] not in users:
        bot.send_message(message.chat.id, "Использование: /revoke ID (список: /users)")
        return
    users.pop(parts[1])
    save_state()
    bot.send_message(message.chat.id, "✅ Доступ отозван")

@bot.message_handler(commands=["setgroup"])
def cmd_setgroup(message):
    global group_id
    if message.chat.type not in ('group', 'supergroup'):
        return
    if not can(message.from_user.id, 'admin'):
        return
    group_id = message.chat.id
    save_state()
    bot.send_message(message.chat.id, "✅ Группа сохранена. Сюда будут приходить уведомления о свете, статус: /light")

@bot.message_handler(commands=["light", "svet"])
def cmd_light(message):
    if message.chat.id == group_id:
        bot.send_message(message.chat.id, get_light_text(), parse_mode='Markdown')
    elif guard(message, 'light'):
        bot.send_message(message.chat.id, get_light_text(),
                         reply_markup=make_light_inline(message.from_user.id), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "🏭 Котельная")
def btn_boiler(message):
    if guard(message, 'boiler'):
        bot.send_message(message.chat.id, get_boiler_text(),
                         reply_markup=make_boiler_inline(), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "⚡️ Реле")
def btn_relay(message):
    if guard(message, 'relay'):
        bot.send_message(message.chat.id, get_relay_text(),
                         reply_markup=make_relay_inline(), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "⏰ Расписание")
def btn_schedule(message):
    if guard(message, 'schedule'):
        bot.send_message(message.chat.id, sched_list_text(),
                         reply_markup=sched_list_markup(), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "📜 История")
def btn_history(message):
    if guard(message, 'history'):
        text, kb = hist_view(role_of(message.from_user.id), 'all', 0)
        bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text in ("🏠 Свет дома", "💡 Свет"))
def btn_light(message):
    if guard(message, 'light'):
        bot.send_message(message.chat.id, get_light_text(),
                         reply_markup=make_light_inline(message.from_user.id), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "⛪️ Свет в храме")
def btn_temple_light(message):
    if guard(message, 'temple_light'):
        bot.send_message(message.chat.id, temple_light_text())

@bot.message_handler(func=lambda m: m.chat.type == 'private' and not (m.text or "").startswith('/'),
                     content_types=['text'])
def on_text(message):
    if role_of(message.from_user.id):
        return
    try_code(message)

def feature_of(data):
    if data.startswith(('mode_', 'set_', 'boiler_')):
        return 'boiler'
    if data.startswith(('sch_', 'sw_')):
        return 'schedule'
    if data.startswith('hist_'):
        return 'history'
    if data.startswith('relay_'):
        return 'relay'
    if data in ('light_on', 'light_off'):
        return 'admin'
    if data.startswith('light_'):
        return 'light'
    return None

@bot.callback_query_handler(func=lambda c: True)
def handle_callback(call):
    try:
        data = call.data
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        uid = call.from_user.id

        feature = feature_of(data)
        if not feature or not can(uid, feature):
            bot.answer_callback_query(call.id, "🔐 Нет доступа", show_alert=True)
            return

        who = user_name(uid, call.from_user.first_name)

        if data.startswith(('sch_', 'sw_')):
            bot.answer_callback_query(call.id)
            handle_sched_cb(call)

        elif data.startswith('hist_'):
            bot.answer_callback_query(call.id)
            _, kind, offset = data.split('_')
            text, kb = hist_view(role_of(uid), kind, int(offset))
            safe_edit(chat_id, msg_id, text, kb)

        elif data.startswith('mode_'):
            mode = data.replace('mode_', '')
            bot.answer_callback_query(call.id, f"⏳ {MODE_NAMES[mode]}...")
            apply_mode(mode, who=who)
            safe_edit(chat_id, msg_id, get_boiler_text(), make_boiler_inline())

        elif data.startswith('set_'):
            temp = int(data.replace('set_', ''))
            bot.answer_callback_query(call.id, f"⏳ Ставлю {temp}°C...")
            apply_mode('temple', temp, who=who)
            safe_edit(chat_id, msg_id, get_boiler_text(), make_boiler_inline())

        elif data == 'boiler_refresh':
            bot.answer_callback_query(call.id, "🔄")
            safe_edit(chat_id, msg_id, get_boiler_text(), make_boiler_inline())

        elif data in ('relay_on', 'relay_off'):
            bot.answer_callback_query(call.id, "⏳ Секунду...")
            tuya_set(RELAY_NAME, data == 'relay_on')
            history_add('relay', f"⚡️ Реле {'включено' if data == 'relay_on' else 'выключено'}", who=who)
            time.sleep(1)
            safe_edit(chat_id, msg_id, get_relay_text(), make_relay_inline())

        elif data == 'relay_refresh':
            bot.answer_callback_query(call.id, "🔄")
            safe_edit(chat_id, msg_id, get_relay_text(), make_relay_inline())

        elif data in ('light_on', 'light_off'):
            bot.answer_callback_query(call.id, "⏳ Секунду...")
            cloud_set(FLOWERS_ID, data == 'light_on')
            time.sleep(1)
            safe_edit(chat_id, msg_id, get_light_text(), make_light_inline(uid))

        elif data == 'light_refresh':
            bot.answer_callback_query(call.id, "🔄")
            safe_edit(chat_id, msg_id, get_light_text(), make_light_inline(uid))

    except Exception as e:
        log.error(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Ошибка", show_alert=True)
        except:
            pass

# ==================== SCHEDULE ====================
DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

def days_text(days):
    d = sorted(set(days))
    if d == list(range(7)):
        return "Каждый день"
    if d == [0, 1, 2, 3, 4]:
        return "Будни"
    if d == [5, 6]:
        return "Выходные"
    return ", ".join(DAY_NAMES[i] for i in d)

def short_act(action, temp=None):
    if action == 'off':
        return "⛔️ Выкл"
    if action == 'self':
        return "🏠 Самообогрев"
    return f"🔥 {temp}°"

def sched_summary(sc):
    return f"{days_text(sc['days'])} в {sc['time']} — {act_text(sc['action'], sc.get('temp'))}"

def clean_schedule(data):
    try:
        action = data.get('action')
        if action not in ('off', 'self', 'temple'):
            return None
        temp = None
        if action == 'temple':
            temp = data.get('temp')
            if not isinstance(temp, int) or isinstance(temp, bool) or temp not in TEMP_STEPS or temp == 'off':
                return None
        t = data.get('time')
        if not isinstance(t, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            return None
        days = data.get('days')
        if not isinstance(days, list) or not days:
            return None
        days = sorted({d for d in days if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6})
        if not days:
            return None
        return {'action': action, 'temp': temp, 'time': t, 'days': days}
    except Exception:
        return None

def _time_passed_today(t):
    now = datetime.now(TZ)
    h, m = map(int, t.split(':'))
    return now.hour * 60 + now.minute >= h * 60 + m

def add_schedule(data, who):
    sc = clean_schedule(data)
    if not sc:
        return None, "bad"
    if len(schedules) >= MAX_SCHEDULES:
        return None, "limit"
    sc.update(id=max([x['id'] for x in schedules], default=0) + 1, enabled=True, by=who or "",
              last_run=datetime.now(TZ).strftime("%Y-%m-%d") if _time_passed_today(sc['time']) else None)
    schedules.append(sc)
    save_state()
    history_add('schedule', f"➕ Добавлено расписание: {sched_summary(sc)}", who=who)
    return sc, None

def find_schedule(sid):
    return next((x for x in schedules if x['id'] == sid), None)

def toggle_schedule(sid, who):
    sc = find_schedule(sid)
    if not sc:
        return None
    sc['enabled'] = not sc['enabled']
    if sc['enabled'] and _time_passed_today(sc['time']):
        sc['last_run'] = datetime.now(TZ).strftime("%Y-%m-%d")
    save_state()
    word = "возобновлено" if sc['enabled'] else "приостановлено"
    history_add('schedule', f"{'▶️' if sc['enabled'] else '⏸'} Расписание {word}: {sched_summary(sc)}", who=who)
    return sc

def delete_schedule(sid, who):
    sc = find_schedule(sid)
    if not sc:
        return False
    schedules.remove(sc)
    save_state()
    history_add('schedule', f"🗑 Удалено расписание: {sched_summary(sc)}", who=who)
    return True

def next_run(sc):
    now = datetime.now(TZ)
    h, m = map(int, sc['time'].split(':'))
    for offset in range(8):
        day = now.date().toordinal() + offset
        d = datetime.fromordinal(day).replace(hour=h, minute=m, tzinfo=TZ)
        if d.weekday() in sc['days'] and d > now:
            return d
    return None

def next_label(d):
    today = datetime.now(TZ).date()
    delta = (d.date() - today).days
    t = d.strftime("%H:%M")
    if delta == 0:
        return f"сегодня в {t}"
    if delta == 1:
        return f"завтра в {t}"
    return f"{DAY_NAMES[d.weekday()]} {d.strftime('%d.%m')} в {t}"

def next_overall():
    best = None
    for sc in schedules:
        if sc['enabled']:
            d = next_run(sc)
            if d and (best is None or d < best[0]):
                best = (d, sc)
    return best

def temple_light_text():
    if not relay_state:
        return "⛪️ Состояние света в храме пока неизвестно"
    length = fmt_dur(time.time() - relay_state['since'])
    if relay_state['online']:
        return f"⛪️ В храме свет есть\nУже {length}"
    return f"⛪️ В храме света нет\nУже {length}"

def light_watchers():
    return staff_ids() + [int(uid) for uid, u in users.items() if u['role'] == 'prosfora']

def staff_ids():
    return [int(uid) for uid, u in users.items() if u['role'] in ('tech', 'admin')]

def notify_staff(text):
    for uid in staff_ids():
        try:
            bot.send_message(uid, text)
        except Exception as e:
            log.error(f"Notify error: {e}")

def notify_light(text):
    for uid in light_watchers():
        try:
            bot.send_message(uid, text)
        except Exception as e:
            log.error(f"Notify error: {e}")

def run_schedule(sc):
    ok = apply_mode(sc['action'], sc.get('temp'), who="расписание")
    text = f"⏰ По расписанию выполнено:\n{act_text(sc['action'], sc.get('temp'))}"
    if not ok:
        text += "\n⚠️ Насос не переключился, проверьте розетку насоса"
    notify_staff(text)

def scheduler_loop():
    while True:
        try:
            now = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            minutes = now.hour * 60 + now.minute
            for sc in list(schedules):
                if not sc['enabled'] or now.weekday() not in sc['days'] or sc.get('last_run') == today:
                    continue
                h, m = map(int, sc['time'].split(':'))
                if 0 <= minutes - (h * 60 + m) < SCHEDULE_WINDOW_MIN:
                    if motor_progress['busy']:
                        continue
                    sc['last_run'] = today
                    save_state()
                    run_schedule(sc)
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        time.sleep(15)

# ==================== NETWORK CHECKS ====================
_gw = {'ip': None, 'ts': 0}

def default_gateway():
    if time.time() - _gw['ts'] > 60:
        _gw['ts'] = time.time()
        try:
            out = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=3).stdout
            match = re.search(r"default via (\S+)", out)
            _gw['ip'] = match.group(1) if match else None
        except Exception:
            _gw['ip'] = None
    return _gw['ip']

def lan_ok():
    gw = default_gateway()
    if not gw:
        return False
    for _ in range(2):
        try:
            if subprocess.run(['ping', '-c', '1', '-W', '1', gw], capture_output=True, timeout=4).returncode == 0:
                return True
        except Exception:
            pass
    return False

def update_device_ip(name, ip, version=None):
    dev = tuya_devices.get(name)
    if not dev:
        return
    try:
        with open(DEVICES_FILE) as f:
            data = json.load(f)
        shutil.copy2(DEVICES_FILE, DEVICES_FILE + ".prev")
        for d in data:
            if d['name'] == name:
                d['ip'] = ip
                if version:
                    d['version'] = str(version)
        tmp = DEVICES_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp, DEVICES_FILE)
    except Exception as e:
        log.error(f"devices.json update error: {e}")
    dev['ip'] = ip
    if version:
        dev['version'] = str(version)

def ip_watcher():
    interval = 60
    last_scan = 0
    while True:
        time.sleep(20)
        lost = [n for n in tuya_devices if fails.get(n, 0) >= 3]
        if not lost or time.time() - last_scan < interval:
            continue
        if not lan_ok():
            continue
        last_scan = time.time()
        log.info(f"Searching for devices on the network: {lost}")
        try:
            try:
                found = tinytuya.deviceScan(verbose=False, color=False, poll=False)
            except TypeError:
                found = tinytuya.deviceScan(False)
        except Exception as e:
            log.error(f"Scan error: {e}")
            interval = min(interval * 2, 900)
            continue
        by_id = {v.get('gwId') or v.get('id'): v for v in (found or {}).values() if isinstance(v, dict)}
        recovered = False
        for name in lost:
            info = by_id.get(tuya_devices[name]['id'])
            if info and info.get('ip'):
                if info['ip'] != tuya_devices[name]['ip']:
                    log.info(f"{name}: new address {info['ip']}")
                    update_device_ip(name, info['ip'], info.get('version'))
                fails[name] = 0
                recovered = True
        interval = 60 if recovered else min(interval * 2, 900)

def change_text(on, duration, place=None):
    if on:
        text = f"💡 Свет включили\nОтсутствие длилось: {fmt_dur(duration)}"
    else:
        text = f"❌ Свет отключили\nПрисутствие длилось: {fmt_dur(duration)}"
    return f"{place}\n{text}" if place else text

def relay_monitor():
    global relay_state
    if relay_state is not None and 'online' not in relay_state:
        relay_state = None
    last_ok = None
    pending = 0
    net_lost_at = None
    net_checks = 0
    while True:
        dps = tuya_status(RELAY_NAME)
        cache['relay'] = dps
        online = dps is not None
        now = time.time()
        if online:
            last_ok = now
            net_checks = 0
        elif tuya_status(SOCKET_NAME) is None:
            net_checks += 1
            if net_checks >= 2 and net_lost_at is None:
                net_lost_at = now
                log.warning("Temple network is down, light state unknown")
                history_add('net', "⚠️ Нет связи с сетью храма. Состояние света сейчас неизвестно")
            time.sleep(RELAY_POLL_SEC)
            continue
        else:
            net_checks = 0
        if net_lost_at is not None:
            lost_for = now - net_lost_at
            net_lost_at = None
            history_add('net', f"✅ Связь с сетью храма восстановлена (не было {fmt_dur(lost_for)})")
        if relay_state is None:
            relay_state = {'online': online, 'since': now}
            save_state()
        elif online != relay_state['online']:
            pending += 1
            if pending >= (2 if online else 3):
                end = now if online else (last_ok or now)
                length = end - relay_state['since']
                text = change_text(online, length, "⛪️ Храм")
                log.info(f"Relay changed: online={online}")
                notify_light(text)
                if online:
                    history_add('light', f"💡 Свет в храме появился (не было {fmt_dur(length)})")
                else:
                    history_add('light', f"❌ Свет в храме пропал (был {fmt_dur(length)})")
                relay_state = {'online': online, 'since': end if not online else now}
                save_state()
                pending = 0
        else:
            pending = 0
        time.sleep(RELAY_POLL_SEC)

def light_notify(online, duration):
    text = change_text(online, duration, None if group_id else "🏠 Дом")
    log.info(f"Light changed: online={online}, group={group_id}")
    targets = [group_id] if group_id else [int(u) for u, d in users.items() if d['role'] == 'admin']
    for chat in targets:
        try:
            bot.send_message(chat, text)
        except Exception as e:
            log.error(f"Light notify error: {e}")

def light_monitor():
    global light_state
    pending = 0
    while True:
        info = cloud_info(FLOWERS_ID)
        cache['light'] = info
        if info is not None:
            online = bool(info.get('online'))
            now = time.time()
            if light_state is None:
                light_state = {'online': online, 'since': now}
                save_state()
            elif online != light_state['online']:
                pending += 1
                if pending >= 2:
                    length = now - light_state['since']
                    light_notify(online, length)
                    if online:
                        history_add('light', f"💡 Свет дома появился (не было {fmt_dur(length)})", scope='admin')
                    else:
                        history_add('light', f"❌ Свет дома пропал (был {fmt_dur(length)})", scope='admin')
                    light_state = {'online': online, 'since': now}
                    save_state()
                    pending = 0
            else:
                pending = 0
        time.sleep(LIGHT_POLL_SEC)

# ==================== WEB APP ====================
def verify_init_data(init_data):
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop('hash')
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get('auth_date', 0)) > 86400:
            return None
        return json.loads(pairs['user'])
    except Exception:
        return None

def pump_view():
    dps = cache['pump']
    if dps is None:
        return {'ok': False, 'last_seen': last_seen.get(SOCKET_NAME)}
    return {'ok': True, 'on': bool(dps.get('1')), 'voltage': dps.get('20', 0) / 10}

def relay_view():
    dps = cache['relay']
    if dps is None:
        return {'ok': False, 'last_seen': last_seen.get(RELAY_NAME)}
    return {'ok': True, 'on': bool(dps.get('1')), 'voltage': dps.get('20', 0) / 10,
            'current': dps.get('18', 0) / 1000, 'power': dps.get('19', 0) / 10}

def light_view(now):
    info = cache['light']
    if info is None:
        return {'ok': False,
                'known': light_state['online'] if light_state else None,
                'since': now - light_state['since'] if light_state else None}
    online = bool(info.get('online'))
    st = {i['code']: i['value'] for i in info.get('status', [])}
    since = None
    if light_state and light_state['online'] == online:
        since = now - light_state['since']
    return {'ok': True, 'online': online, 'since': since,
            'voltage': st.get('cur_voltage', 0) / 10, 'power': st.get('cur_power', 0) / 10,
            'lamp': bool(st.get('switch_1'))}

def build_state(role):
    feats = PERMS[role]
    now = time.time()
    p = dict(motor_progress)
    pos = motor_position
    if p['busy'] and p['stage'] == 'motor' and p['total']:
        direction = 1 if p['to'] > p['from'] else -1
        pos = p['from'] + direction * min(p['done'], abs(p['to'] - p['from']))
    st = {
        'role': ROLE_NAMES[role],
        'features': sorted(feats),
        'progress': {
            'busy': p['busy'], 'stage': p['stage'], 'title': p['title'],
            'done': p['done'], 'total': p['total'], 'result': p['result'],
            'ago': now - p['finished_at'] if p['finished_at'] else None,
        },
    }
    if 'boiler' in feats:
        st['boiler'] = {'mode': boiler_mode, 'temp': temple_temp, 'pos': pos,
                        'max': TEMP_STEPS[70], 'steps': {str(t): v for t, v in TEMP_STEPS.items() if t != 'off'},
                        'pump': pump_view()}
    if 'relay' in feats:
        st['relay'] = relay_view()
    if 'light' in feats:
        st['light'] = light_view(now)
    if 'schedule' in feats:
        st['schedules'] = [sched_view(sc) for sc in schedules]
        nxt = next_overall()
        st['next'] = next_label(nxt[0]) if nxt else None
        st['max_schedules'] = MAX_SCHEDULES
    return st

def sched_view(sc):
    d = next_run(sc) if sc['enabled'] else None
    return {'id': sc['id'], 'action': sc['action'], 'temp': sc.get('temp'), 'time': sc['time'],
            'days': sc['days'], 'enabled': sc['enabled'], 'days_text': days_text(sc['days']),
            'title': act_text(sc['action'], sc.get('temp')), 'by': sc.get('by'),
            'next': next_label(d) if d else None}

def history_view(role, offset, limit, kind):
    items = history_for(role, offset, limit, kind)
    more = len(items) > limit
    return {'more': more, 'items': [
        {'day': day_label(e['ts']), 'time': clock(e['ts']), 'kind': e.get('kind'),
         'text': e['text'], 'who': e.get('who')} for e in items[:limit]]}

def run_action(role, body, who=None):
    feats = PERMS[role]
    act = body.get('action')
    if act in ('mode_off', 'mode_self', 'set_temp'):
        if 'boiler' not in feats:
            return 403, {'error': 'forbidden'}
        if motor_progress['busy']:
            return 409, {'error': 'busy'}
        if act == 'set_temp':
            temp = body.get('temp')
            if not isinstance(temp, int) or temp not in TEMP_STEPS:
                return 400, {'error': 'bad_temp'}
            args = ('temple', temp)
        else:
            args = ('off' if act == 'mode_off' else 'self', None)
        motor_progress['busy'] = True
        Thread(target=apply_mode, args=args + (who,), daemon=True).start()
        return 200, {'ok': True}
    if act in ('sched_add', 'sched_toggle', 'sched_delete'):
        if 'schedule' not in feats:
            return 403, {'error': 'forbidden'}
        if act == 'sched_add':
            sc, err = add_schedule(body.get('sched') or {}, who)
            return (200, {'ok': True, 'id': sc['id']}) if sc else (400, {'error': err})
        sid = body.get('id')
        if not isinstance(sid, int):
            return 400, {'error': 'bad_id'}
        if act == 'sched_toggle':
            return (200, {'ok': True}) if toggle_schedule(sid, who) else (404, {'error': 'not_found'})
        return (200, {'ok': True}) if delete_schedule(sid, who) else (404, {'error': 'not_found'})
    if act in ('relay_on', 'relay_off'):
        if 'relay' not in feats:
            return 403, {'error': 'forbidden'}
        ok = tuya_set(RELAY_NAME, act == 'relay_on')
        history_add('relay', f"⚡️ Реле {'включено' if act == 'relay_on' else 'выключено'}", who=who)
        cache['relay'] = tuya_status(RELAY_NAME)
        return 200, {'ok': ok}
    if act in ('light_on', 'light_off'):
        if 'admin' not in feats:
            return 403, {'error': 'forbidden'}
        ok = cloud_set(FLOWERS_ID, act == 'light_on')
        time.sleep(1)
        cache['light'] = cloud_info(FLOWERS_ID)
        return 200, {'ok': ok}
    if act == 'light_refresh':
        if 'light' not in feats:
            return 403, {'error': 'forbidden'}
        cache['light'] = cloud_info(FLOWERS_ID)
        return 200, {'ok': True}
    return 400, {'error': 'bad_action'}

class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def _auth(self):
        user = verify_init_data(self.headers.get('X-Init-Data', ''))
        if not user:
            self._json(401, {'error': 'unauthorized'})
            return None, None
        role = role_of(user.get('id'))
        if not role or 'boiler' not in PERMS[role]:
            self._json(403, {'error': 'no_access'})
            return None, None
        name = user_name(user.get('id'), user.get('first_name', ''))
        return role, name

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            try:
                with open(os.path.join(WEBAPP_DIR, 'index.html'), 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            except OSError:
                self._json(404, {'error': 'no_page'})
        elif path == '/api/state':
            role, _ = self._auth()
            if role:
                self._json(200, build_state(role))
        elif path == '/api/history':
            role, _ = self._auth()
            if role:
                if 'history' not in PERMS[role]:
                    self._json(403, {'error': 'forbidden'})
                    return
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    offset = max(0, int(q.get('offset', ['0'])[0]))
                    limit = min(50, max(1, int(q.get('limit', ['20'])[0])))
                except ValueError:
                    offset, limit = 0, 20
                kind = q.get('kind', ['all'])[0]
                if kind not in ('all', 'boiler', 'light'):
                    kind = 'all'
                self._json(200, history_view(role, offset, limit, kind))
        else:
            self._json(404, {'error': 'not_found'})

    def do_POST(self):
        if self.path.split('?')[0] != '/api/action':
            self._json(404, {'error': 'not_found'})
            return
        role, who = self._auth()
        if not role:
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 2048:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            self._json(400, {'error': 'bad_request'})
            return
        code, obj = run_action(role, body, who)
        self._json(code, obj)

def web_thread():
    log.info(f"🌐 Web app on 127.0.0.1:{WEB_PORT}")
    ThreadingHTTPServer(('127.0.0.1', WEB_PORT), WebHandler).serve_forever()

def pump_monitor():
    while True:
        cache['pump'] = tuya_status(SOCKET_NAME)
        time.sleep(PUMP_POLL_SEC)

def panel_markup():
    if not WEBAPP_URL:
        return None
    try:
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("🖥 Открыть панель", web_app=WebAppInfo(WEBAPP_URL)))
        return kb
    except Exception as e:
        log.error(f"Panel button error: {e}")
        return None

def send_panel(chat_id):
    kb = panel_markup()
    if kb:
        bot.send_message(chat_id, "🖥 Панель с живым прогрессом — по кнопке ниже", reply_markup=kb)

def setup_menu_button():
    if not WEBAPP_URL:
        return
    from telebot.types import MenuButtonWebApp
    for _ in range(10):
        try:
            bot.set_chat_menu_button(menu_button=MenuButtonWebApp(type="web_app", text="🖥 Панель", web_app=WebAppInfo(WEBAPP_URL)))
            log.info("Menu button set")
            return
        except Exception as e:
            log.error(f"Menu button error: {e}")
            time.sleep(10)

def telegram_thread():
    log.info("📱 Telegram thread started")
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=20)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)

def main():
    log.info("⚡ BOT started")
    log.info(f"Motor position: {motor_position} ({current_label()})")

    Thread(target=telegram_thread, daemon=True, name="Telegram").start()
    Thread(target=relay_monitor, daemon=True, name="RelayMonitor").start()
    if FLOWERS_ID:
        Thread(target=light_monitor, daemon=True, name="LightMonitor").start()
    Thread(target=pump_monitor, daemon=True, name="PumpMonitor").start()
    Thread(target=scheduler_loop, daemon=True, name="Scheduler").start()
    Thread(target=post_start_loop, daemon=True, name="PostStart").start()
    Thread(target=ip_watcher, daemon=True, name="IpWatcher").start()
    Thread(target=web_thread, daemon=True, name="Web").start()
    Thread(target=setup_menu_button, daemon=True, name="MenuButton").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("👋 Shutting down...")
        save_state()
        motor_release()
        GPIO.cleanup()

if __name__ == "__main__":
    main()
