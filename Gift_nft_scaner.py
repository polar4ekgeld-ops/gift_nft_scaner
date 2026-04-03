import asyncio
import html
import json
import logging
import os
import random
import re
import shlex
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from colorama import Fore, init

# ========= INIT =========
init(autoreset=True)
logging.basicConfig(level=logging.INFO)


def load_env_file(path: str = ".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "@Gift_nft_scaner")
RARE_CHAT_ID = os.getenv("RARE_CHAT_ID", "@onlyfanfarm")
ADMIN_IDS = [1977608232]

DATA_FILE = "collections.json"
STATS_FILE = "stats.json"
SETTINGS_FILE = "settings.json"
RECENT_FILE = "recent_stats.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
DEDUP_FILE = "dedup.json"
OWNER_STATS_FILE = "owner_stats.json"
GAMIFICATION_FILE = "gamification.json"
MAX_HISTORY = 50
FILTER_OPTIONS = [0.3, 0.5, 0.8, 1, 1.5, 2, 2.5, 3, 3.5, 4]
DELAY_OPTIONS = [0.2, 0.3, 0.5, 0.7, 1.0, 1.5]
PRO_STARS_PRICE = 199  # Telegram Stars (XTR)
PRO_STARS_DAYS = 30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
START_TS = time.time()
WIZARD_STATE = {}

# ========= GLOBAL ATTR_EMOJI =========
def attr_emoji(name, percent, is_black=False):
    if is_black and percent is not None and percent <= 1:
        return "🔥"
    if is_black:
        return "🖤"
    if percent is not None and percent <= 1:
        return "💎"
    return "✨"


def fmt_percent(value):
    return "?" if value is None else f"{value}%"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def ensure_admin_callback(call: types.CallbackQuery) -> bool:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав", show_alert=True)
        return False
    return True


def mode_threshold(collection: str):
    max_percent = COLLECTIONS[collection].get("max_percent", 2.0)
    mode = SETTINGS.get("signal_mode", "balanced")
    if mode == "conservative":
        return min(max_percent, 1.0)
    if mode == "aggressive":
        return max(max_percent, 3.0)
    return max_percent


def get_user_profile(uid: str):
    users = GAMIFICATION.setdefault("users", {})
    users.setdefault(
        uid,
        {
            "alerts_received": 0,
            "rare_alerts_received": 0,
            "referrals": 0,
            "referrals_rewarded": 0,
            "weekly_referrals": 0,
            "referred_by": None,
            "streak": 0,
            "last_active": "",
            "pro_until": "",
            "hits": [],
        },
    )
    return users[uid]


def current_week_key() -> str:
    now = datetime.utcnow()
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def ensure_weekly_tracking():
    wk = current_week_key()
    if GAMIFICATION.get("week_key") != wk:
        GAMIFICATION["week_key"] = wk
        for uid in GAMIFICATION.get("users", {}):
            GAMIFICATION["users"][uid]["weekly_referrals"] = 0
        save_gamification()


def grant_pro_days(uid: str, days: int):
    profile = get_user_profile(uid)
    now = datetime.utcnow()
    pro_until_raw = profile.get("pro_until")
    if pro_until_raw:
        try:
            pro_until = datetime.strptime(pro_until_raw, "%Y-%m-%d")
            start = pro_until if pro_until > now else now
        except ValueError:
            start = now
    else:
        start = now
    new_until = (start + timedelta(days=days)).strftime("%Y-%m-%d")
    profile["pro_until"] = new_until
    return new_until


def is_pro_active(uid: str) -> bool:
    profile = get_user_profile(uid)
    pro_until = profile.get("pro_until")
    if not pro_until:
        return False
    try:
        return datetime.strptime(pro_until, "%Y-%m-%d").date() >= datetime.utcnow().date()
    except ValueError:
        return False


def touch_user(uid: str):
    profile = get_user_profile(uid)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if profile["last_active"] == today:
        return profile
    y, m, d = map(int, today.split("-"))
    # простая streak-модель: +1 если был вчера
    if profile["last_active"]:
        py, pm, pd = map(int, profile["last_active"].split("-"))
        prev = datetime(py, pm, pd)
        now = datetime(y, m, d)
        delta = (now - prev).days
        profile["streak"] = profile["streak"] + 1 if delta == 1 else 1
    else:
        profile["streak"] = 1
    profile["last_active"] = today
    return profile


def current_daily_challenge():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if GAMIFICATION.get("last_challenge_date") == today and GAMIFICATION.get("challenge"):
        challenge = GAMIFICATION["challenge"]
        # Поддержка старых сохранённых челленджей без новых полей.
        challenge.setdefault("reward_days", 1)
        challenge.setdefault("tagline", "Найди NFT, который подходит под условия дня")
        challenge.setdefault("title", "Daily Hunter")
        GAMIFICATION["challenge"] = challenge
        return challenge
    templates = [
        {"title": "Black Hunter", "max_percent": 1.0, "black_only": True, "reward_days": 1, "tagline": "Охота на редкий NFT с черным фоном"},
        {"title": "Ultra Rare", "max_percent": 0.5, "black_only": False, "reward_days": 2, "tagline": "Поймай ультра-редкий NFT с минимальным процентом"},
        {"title": "Balanced Scout", "max_percent": 1.5, "black_only": False, "reward_days": 1, "tagline": "Найди качественный NFT без жестких ограничений по фону"},
        {"title": "Night Collector", "max_percent": 1.2, "black_only": True, "reward_days": 2, "tagline": "Черный фон + хороший процент — охота для внимательных"},
        {"title": "Lucky Symbol", "max_percent": 2.0, "black_only": False, "reward_days": 1, "tagline": "Поймай редкий символ среди свежих минтов"},
        {"title": "Precision Run", "max_percent": 0.8, "black_only": False, "reward_days": 2, "tagline": "Точный забег за NFT с очень низким model%"},
        {"title": "Steady Grinder", "max_percent": 2.5, "black_only": False, "reward_days": 1, "tagline": "Стабильная ежедневная охота на сильные сигналы"},
    ]
    prev_title = (GAMIFICATION.get("challenge") or {}).get("title")
    pool = [t for t in templates if t["title"] != prev_title] or templates
    challenge = random.choice(pool)
    GAMIFICATION["last_challenge_date"] = today
    GAMIFICATION["challenge"] = challenge
    save_gamification()
    return challenge


def active_ref_event():
    event = GAMIFICATION.get("ref_event") or {}
    if not event:
        return None
    end_date = event.get("end_date")
    if not end_date:
        return None
    try:
        if datetime.utcnow().date() <= datetime.strptime(end_date, "%Y-%m-%d").date():
            return event
    except ValueError:
        return None
    return None


def normalize_hunt_value(value: str) -> str:
    low = (value or "").strip().lower()
    if low in ("*", "any", "all", ""):
        return "any"
    return (value or "").strip()


def parse_hunt_input(raw: str):
    raw = raw.strip()
    if not raw:
        return None

    # Формат 1 (рекомендуемый): model=...; bg=...; symbol=...
    if ";" in raw and "=" in raw:
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        data = {}
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            data[k.strip().lower()] = v.strip()
        model = normalize_hunt_value(data.get("model", "any"))
        bg = normalize_hunt_value(data.get("bg", "any"))
        symbol = normalize_hunt_value(data.get("symbol", "any"))
        return model, bg, symbol

    # Формат 2: /hunt "model with space" "bg with space" "symbol with space"
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return None
    if len(tokens) == 3:
        return normalize_hunt_value(tokens[0]), normalize_hunt_value(tokens[1]), normalize_hunt_value(tokens[2])
    return None


def dedup_key(kind: str, collection: str, nft_id: int, target: str):
    return f"{kind}:{collection}:{nft_id}:{target}"


def already_sent(key: str):
    return key in SENT_CACHE


def mark_sent(key: str):
    SENT_CACHE.append(key)
    del SENT_CACHE[:-5000]
    save_dedup()


# ========= HTML CLEANING =========
ALLOWED_TAGS = ["b", "strong", "i", "em", "u", "a", "code", "pre", "tg-spoiler"]


def clean_html(text: str) -> str:
    text = html.escape(text, quote=False)
    for tag in ALLOWED_TAGS:
        text = re.sub(rf"&lt;({tag})( [^&]*)?&gt;", r"<\1>", text)
        text = re.sub(rf"&lt;/({tag})&gt;", r"</\1>", text)
    return text


# ========= LOAD / SAVE =========
def load_collections():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for c in data:
                data[c].setdefault("rare_count", 0)
                data[c].setdefault("max_percent", 2.0)
            return data

    return {
        "StellarRocket": {"start": 136660, "enabled": True, "rare_count": 0, "max_percent": 2.0},
        "TimelessBook": {"start": 749990, "enabled": True, "rare_count": 0, "max_percent": 2.0},
        "PlushPepe": {"start": 2824, "enabled": True, "rare_count": 0, "max_percent": 2.0},
        "DurovsCap": {"start": 4708, "enabled": True, "rare_count": 0, "max_percent": 2.0},
        "PoolFloat": {"start": 196693, "enabled": True, "rare_count": 0, "max_percent": 2.0},
        "MoodPack": {"start": 166928, "enabled": True, "rare_count": 0, "max_percent": 2.0},
        "ChillFlame": {"start": 374000, "enabled": True, "rare_count": 0, "max_percent": 2.0},
    }


def save_collections():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(COLLECTIONS, f, ensure_ascii=False, indent=4)


def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_stats():
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(STATS, f, ensure_ascii=False, indent=4)


def load_recent_stats():
    if os.path.exists(RECENT_FILE):
        with open(RECENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_recent_stats():
    with open(RECENT_FILE, "w", encoding="utf-8") as f:
        json.dump(RECENT_STATS, f, ensure_ascii=False, indent=4)


def load_subscriptions():
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_subscriptions():
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(SUBSCRIPTIONS, f, ensure_ascii=False, indent=4)


def load_dedup():
    if os.path.exists(DEDUP_FILE):
        with open(DEDUP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_dedup():
    with open(DEDUP_FILE, "w", encoding="utf-8") as f:
        json.dump(SENT_CACHE, f, ensure_ascii=False, indent=2)


def load_owner_stats():
    if os.path.exists(OWNER_STATS_FILE):
        with open(OWNER_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_owner_stats():
    with open(OWNER_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(OWNER_STATS, f, ensure_ascii=False, indent=2)


def load_gamification():
    if os.path.exists(GAMIFICATION_FILE):
        with open(GAMIFICATION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "last_challenge_date": "", "challenge": {}}


def save_gamification():
    with open(GAMIFICATION_FILE, "w", encoding="utf-8") as f:
        json.dump(GAMIFICATION, f, ensure_ascii=False, indent=2)


def load_settings():
    defaults = {
        "chat_id": CHAT_ID,
        "rare_chat_id": RARE_CHAT_ID,
        "send_delay_main": 0.3,
        "send_delay_rare": 0.5,
        "signal_mode": "balanced",
        "live_stats_enabled": False,
        "live_stats_interval": 120,
        "dashboard_enabled": False,
        "dashboard_chat_id": CHAT_ID,
        "dashboard_message_id": None,
        "dashboard_style": "v2",
    }
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for key, value in defaults.items():
                data.setdefault(key, value)
            return data
    return defaults


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=4)


COLLECTIONS = load_collections()
STATS = load_stats()
SETTINGS = load_settings()
RECENT_STATS = load_recent_stats()
SUBSCRIPTIONS = load_subscriptions()
SENT_CACHE = load_dedup()
OWNER_STATS = load_owner_stats()
GAMIFICATION = load_gamification()
LIVE_STATS_LAST_HASH = ""
DASHBOARD_LAST_HASH = ""
for c in COLLECTIONS:
    STATS.setdefault(c, [])
    RECENT_STATS.setdefault(c, [])


# ========= LOG =========
def log(msg, color=Fore.WHITE):
    print(color + msg)


# ========= HELPER =========
def extract(value: str):
    value = re.sub(r"<.*?>", "", value).strip()
    match = re.match(r"(.*?)(?:\s*\(?(\d+(?:\.\d+)?)%\)?)?$", value)
    if match:
        name = match.group(1).strip()
        percent = float(match.group(2)) if match.group(2) else None
        return name, percent
    return value, None


# ========= ADMIN =========
def main_admin_menu():
    buttons = []
    autostats_state = "🟢 ON" if SETTINGS.get("autostats") else "🔴 OFF"
    dashboard_state = "🟢 ON" if SETTINGS.get("dashboard_enabled") else "🔴 OFF"
    for c, info in COLLECTIONS.items():
        status = "✅" if info["enabled"] else "❌"
        rare_count = info.get("rare_count", 0)
        max_percent = info.get("max_percent", 2.0)
        buttons.append([
            InlineKeyboardButton(
                text=f"{c} {status} | ≤ {max_percent}% | Редких: {rare_count}",
                callback_data=f"collection_{c}",
            )
        ])
    buttons.extend(
        [
            [InlineKeyboardButton(text="ℹ️ Как пользоваться админ-меню", callback_data="admin_overview")],
            [InlineKeyboardButton(text="📊 Быстрая статистика коллекций", callback_data="quick_stats_menu")],
            [InlineKeyboardButton(text="📈 LIVE /stats (сводка)", callback_data="admin_live_stats")],
            [InlineKeyboardButton(text="👑 Owners / владельцы", callback_data="admin_owners")],
            [InlineKeyboardButton(text="🩺 Health / здоровье", callback_data="admin_health")],
            [InlineKeyboardButton(text=f"📡 AutoStats: {autostats_state}", callback_data="admin_toggle_autostats")],
            [InlineKeyboardButton(text=f"📌 Dashboard: {dashboard_state}", callback_data="admin_toggle_dashboard")],
            [InlineKeyboardButton(text="📚 Админ команды", callback_data="admin_commands_help")],
            [InlineKeyboardButton(text="🧹 Сбросить счётчик редких", callback_data="reset_rare_confirm")],
            [InlineKeyboardButton(text=f"🕒 Delay main: {SETTINGS['send_delay_main']}s", callback_data="delay_main_menu")],
            [InlineKeyboardButton(text=f"🕒 Delay rare: {SETTINGS['send_delay_rare']}s", callback_data="delay_rare_menu")],
            [InlineKeyboardButton(text=f"🎛 Режим: {SETTINGS.get('signal_mode', 'balanced')}", callback_data="mode_menu")],
            [InlineKeyboardButton(text="📨 Настройка чатов", callback_data="chat_help")],
            [InlineKeyboardButton(text="⬅️ Пользовательское меню", callback_data="back_user_menu")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def collection_menu(name: str):
    collection = COLLECTIONS[name]
    status_text = "✅ Включена" if collection.get("enabled", True) else "❌ Выключена"
    controls = [
        [InlineKeyboardButton(text=f"Статус: {status_text}", callback_data=f"toggle_{name}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ]
    for p in FILTER_OPTIONS:
        controls.insert(-1, [InlineKeyboardButton(text=f"Фильтр ≤ {p}%", callback_data=f"setpercent_{name}_{p}")])

    return InlineKeyboardMarkup(inline_keyboard=controls)


def delay_menu(kind: str):
    callback_prefix = "delaymain" if kind == "main" else "delayrare"
    controls = [[InlineKeyboardButton(text=f"{d}s", callback_data=f"{callback_prefix}_{d}")] for d in DELAY_OPTIONS]
    controls.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=controls)


def quick_stats_menu():
    controls = [[InlineKeyboardButton(text=f"📈 {c}", callback_data=f"qstats_{c}")] for c in COLLECTIONS]
    controls.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=controls)


def signal_mode_menu():
    current = SETTINGS.get("signal_mode", "balanced")
    controls = [
        [InlineKeyboardButton(text=f"{'✅ ' if current == 'conservative' else ''}Conservative", callback_data="setmode_conservative")],
        [InlineKeyboardButton(text=f"{'✅ ' if current == 'balanced' else ''}Balanced", callback_data="setmode_balanced")],
        [InlineKeyboardButton(text=f"{'✅ ' if current == 'aggressive' else ''}Aggressive", callback_data="setmode_aggressive")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=controls)


def user_menu_keyboard(is_admin_user: bool):
    buttons = [
        [InlineKeyboardButton(text="⚡ Быстрый старт", callback_data="um_quickstart")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="um_profile"),
            InlineKeyboardButton(text="🏅 Лидерборд", callback_data="um_leaderboard"),
        ],
        [
            InlineKeyboardButton(text="🎯 Челлендж", callback_data="um_challenge"),
            InlineKeyboardButton(text="🔔 Подписки", callback_data="um_submenu"),
        ],
        [InlineKeyboardButton(text="🎯 PRO hunts", callback_data="um_huntmenu")],
        [InlineKeyboardButton(text="⭐ Купить PRO (Stars)", callback_data="um_buypro")],
        [InlineKeyboardButton(text="📣 TOP рефов", callback_data="um_toprefs")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="um_home")],
    ]
    if is_admin_user:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ меню", callback_data="um_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_mysubs_text(user_id: str) -> str:
    subs = SUBSCRIPTIONS.get(user_id, [])
    if not subs:
        return "🔔 У вас нет подписок. Пример: /subscribe all 1.0 0"
    text = "🔔 <b>Ваши подписки:</b>\n"
    for i, sub in enumerate(subs, 1):
        text += f"{i}) {sub['collection']} ≤ {sub['max_percent']}% | black_only={sub['black_only']}\n"
    text += "\nНажмите кнопку удаления под списком."
    return text


def mysubs_manage_keyboard(user_id: str):
    subs = SUBSCRIPTIONS.get(user_id, [])
    controls = [[InlineKeyboardButton(text=f"🗑 Удалить #{i}", callback_data=f"um_delsub_{i}")] for i in range(1, len(subs) + 1)]
    controls.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")])
    return InlineKeyboardMarkup(inline_keyboard=controls)


def subs_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои подписки", callback_data="um_mysubs")],
            [InlineKeyboardButton(text="➕ Как добавить подписку", callback_data="um_sub_add_help")],
            [InlineKeyboardButton(text="✨ Конструктор подписки", callback_data="um_sub_wizard_start")],
            [InlineKeyboardButton(text="🧹 Удалить все подписки", callback_data="um_subs_clear_confirm")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")],
        ]
    )


def hunts_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои PRO hunts", callback_data="um_myhunts")],
            [InlineKeyboardButton(text="➕ Как добавить hunt", callback_data="um_hunt_add_help")],
            [InlineKeyboardButton(text="✨ Конструктор PRO hunt", callback_data="um_hunt_wizard_start")],
            [InlineKeyboardButton(text="⭐ Купить/Продлить PRO", callback_data="um_buypro")],
            [InlineKeyboardButton(text="🧹 Очистить все PRO hunts", callback_data="um_hunts_clear_confirm")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")],
        ]
    )


def build_myhunts_text(user_id: str) -> str:
    if not is_pro_active(user_id):
        return "🔒 PRO не активен. Для доступа к PRO hunts активируйте PRO статус (через рефералов, /buypro или /givepro)."
    profile = get_user_profile(user_id)
    hunts = profile.get("hunts", [])
    if not hunts:
        return (
            "🎯 <b>PRO hunts пока пустой</b>\n"
            "Добавьте первый hunt через <b>✨ Конструктор PRO hunt</b> или командой:\n"
            "<code>/hunt \"Model Name\" \"BG Name\" \"Symbol Name\"</code>"
        )
    text = "🎯 <b>Ваши PRO hunts</b>\n"
    for i, h in enumerate(hunts, 1):
        gift = html.escape(str(h.get("collection", "all")))
        model = html.escape(str(h.get("model", "any")))
        bg = html.escape(str(h.get("bg", "any")))
        symbol = html.escape(str(h.get("symbol", "any")))
        text += f"{i}) 🎁 <code>{gift}</code> | 🧬 <code>{model}</code> | 🎨 <code>{bg}</code> | 🔣 <code>{symbol}</code>\n"
    text += "\nНажмите кнопку удаления под списком."
    return text


def myhunts_manage_keyboard(user_id: str):
    profile = get_user_profile(user_id)
    hunts = profile.get("hunts", [])
    controls = [[InlineKeyboardButton(text=f"🗑 Удалить hunt #{i}", callback_data=f"um_delhunt_{i}")] for i in range(1, len(hunts) + 1)]
    controls.append([InlineKeyboardButton(text="⬅️ Назад в PRO hunts", callback_data="um_huntmenu")])
    return InlineKeyboardMarkup(inline_keyboard=controls)


def hunt_collection_keyboard():
    rows = [[InlineKeyboardButton(text="🌐 Все подарки", callback_data="um_hwc_all")]]
    rows.extend([[InlineKeyboardButton(text=name, callback_data=f"um_hwc_{name}")] for name in list(COLLECTIONS.keys())[:12]])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="um_huntmenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hunt_options_keyboard(prefix: str, options: list[str]):
    rows = [[InlineKeyboardButton(text=f"{opt}", callback_data=f"um_{prefix}_{idx}")] for idx, opt in enumerate(options)]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="um_huntmenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_wizard_collection_keyboard():
    popular = list(COLLECTIONS.keys())[:8]
    rows = [[InlineKeyboardButton(text="🌐 all", callback_data="um_sw_col_all")]]
    rows.extend([[InlineKeyboardButton(text=name, callback_data=f"um_sw_col_{name}")] for name in popular])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="um_submenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_wizard_percent_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="0.5%", callback_data="um_sw_pct_0.5"),
                InlineKeyboardButton(text="1.0%", callback_data="um_sw_pct_1.0"),
                InlineKeyboardButton(text="2.0%", callback_data="um_sw_pct_2.0"),
            ],
            [
                InlineKeyboardButton(text="3.0%", callback_data="um_sw_pct_3.0"),
                InlineKeyboardButton(text="4.0%", callback_data="um_sw_pct_4.0"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="um_submenu")],
        ]
    )


def sub_wizard_black_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖤 Только black bg", callback_data="um_sw_black_1")],
            [InlineKeyboardButton(text="✨ Любой фон", callback_data="um_sw_black_0")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="um_submenu")],
        ]
    )


@dp.message(Command("admin"))
async def admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    uid = str(message.from_user.id)
    touch_user(uid)
    ensure_weekly_tracking()
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_uid = args[1].replace("ref_", "").strip()
        profile = get_user_profile(uid)
        if ref_uid != uid and not profile.get("referred_by"):
            profile["referred_by"] = ref_uid
            ref_profile = get_user_profile(ref_uid)
            ref_profile["referrals"] += 1
            ref_profile["weekly_referrals"] = ref_profile.get("weekly_referrals", 0) + 1

            # Базовая награда: каждые 3 реферала дают 7 дней PRO
            referral_chunks = ref_profile["referrals"] // 3
            already_rewarded = ref_profile.get("referrals_rewarded", 0)
            if referral_chunks > already_rewarded:
                chunks_to_grant = referral_chunks - already_rewarded
                new_until = grant_pro_days(ref_uid, chunks_to_grant * 7)
                ref_profile["referrals_rewarded"] = referral_chunks
                try:
                    await bot.send_message(
                        int(ref_uid),
                        f"🎉 Ты пригласил 3 друзей! PRO активирован до {new_until} (+{chunks_to_grant * 7} дней).",
                    )
                except Exception as e:
                    logging.warning("Cannot notify referrer %s: %s", ref_uid, e)

            # Бонус от лимитированного реф-ивента
            event = active_ref_event()
            if event:
                bonus_days = int(event.get("bonus_days", 0))
                if bonus_days > 0:
                    new_until = grant_pro_days(ref_uid, bonus_days)
                    try:
                        await bot.send_message(
                            int(ref_uid),
                            f"⚡ Ивент «{event.get('name', 'Referral Event')}»: +{bonus_days} дней PRO!\nДо: {new_until}",
                        )
                    except Exception as e:
                        logging.warning("Cannot send event bonus to %s: %s", ref_uid, e)
            save_gamification()
    bot_link = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_link}?start=ref_{uid}"
    welcome = (
        "🚀 <b>Добро пожаловать в @Gift_NFT_Scaner!</b>\n\n"
        "<b>Команды:</b>\n"
        "<blockquote>/profile</blockquote>\n"
        "<blockquote>/leaderboard</blockquote>\n"
        "<blockquote>/challenge</blockquote>\n"
        "<blockquote>/mysubs</blockquote>\n"
        "<blockquote>/toprefs</blockquote>\n\n"
        f"<b>Твоя реферальная ссылка:</b> {ref_link}\n\n"
    )
    await message.answer(
        welcome,
        parse_mode="HTML",
        disable_web_page_preview=False,
        reply_markup=user_menu_keyboard(is_admin(message.from_user.id)),
    )


@dp.message(Command("profile"))
async def profile_cmd(message: types.Message):
    uid = str(message.from_user.id)
    await message.answer(build_profile_message(uid), parse_mode="HTML", reply_markup=profile_keyboard(uid))


@dp.message(Command("menu"))
async def menu_cmd(message: types.Message):
    await message.answer(
        "📱 <b>Пользовательское меню</b>\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=user_menu_keyboard(is_admin(message.from_user.id)),
    )


@dp.callback_query(F.data.startswith("um_"))
async def user_menu_callbacks(call: types.CallbackQuery):
    action = call.data.replace("um_", "")
    user_id = str(call.from_user.id)
    reply_markup = user_menu_keyboard(is_admin(call.from_user.id))
    if action.startswith("delsub_"):
        subs = SUBSCRIPTIONS.get(user_id, [])
        try:
            idx = int(action.replace("delsub_", "")) - 1
            if idx < 0:
                raise ValueError
        except ValueError:
            await call.answer("Некорректный номер подписки", show_alert=True)
            return
        if idx >= len(subs):
            await call.answer("Подписка уже удалена", show_alert=True)
            return
        removed = subs.pop(idx)
        SUBSCRIPTIONS[user_id] = subs
        save_subscriptions()
        text = build_mysubs_text(user_id)
        reply_markup = mysubs_manage_keyboard(user_id) if subs else user_menu_keyboard(is_admin(call.from_user.id))
        try:
            await call.message.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        await call.answer(f"Удалено: {removed['collection']} <= {removed['max_percent']}%")
        return
    if action.startswith("delhunt_"):
        profile = get_user_profile(user_id)
        hunts = profile.get("hunts", [])
        try:
            idx = int(action.replace("delhunt_", "")) - 1
            if idx < 0:
                raise ValueError
        except ValueError:
            await call.answer("Некорректный номер hunt", show_alert=True)
            return
        if idx >= len(hunts):
            await call.answer("Этот hunt уже удалён", show_alert=True)
            return
        removed = hunts.pop(idx)
        profile["hunts"] = hunts
        save_gamification()
        text = build_myhunts_text(user_id)
        reply_markup = myhunts_manage_keyboard(user_id) if hunts else hunts_menu_keyboard()
        try:
            await call.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        await call.answer(
            "Удален hunt: "
            f"collection={removed.get('collection', 'all')}, model={removed.get('model', 'any')}"
        )
        return
    if action.startswith("sw_col_"):
        collection = action.replace("sw_col_", "", 1)
        state = WIZARD_STATE.setdefault(user_id, {})
        state.update({"mode": "sub", "collection": collection})
        await call.message.edit_text(
            f"Шаг 2/3: выбрана коллекция <b>{html.escape(collection)}</b>\nВыберите max_percent:",
            parse_mode="HTML",
            reply_markup=sub_wizard_percent_keyboard(),
        )
        await call.answer()
        return
    if action.startswith("sw_pct_"):
        pct = action.replace("sw_pct_", "", 1)
        state = WIZARD_STATE.setdefault(user_id, {})
        if state.get("mode") != "sub" or "collection" not in state:
            await call.answer("Сначала выберите коллекцию", show_alert=True)
            return
        state["max_percent"] = pct
        await call.message.edit_text(
            f"Шаг 3/3: коллекция <b>{html.escape(state['collection'])}</b>, max_percent <b>{html.escape(pct)}%</b>\nВыберите режим фона:",
            parse_mode="HTML",
            reply_markup=sub_wizard_black_keyboard(),
        )
        await call.answer()
        return
    if action.startswith("sw_black_"):
        black_only = action.replace("sw_black_", "", 1) == "1"
        state = WIZARD_STATE.get(user_id, {})
        if state.get("mode") != "sub" or "collection" not in state or "max_percent" not in state:
            await call.answer("Конструктор подписки не завершён", show_alert=True)
            return
        SUBSCRIPTIONS.setdefault(user_id, [])
        SUBSCRIPTIONS[user_id].append(
            {"collection": state["collection"], "max_percent": float(state["max_percent"]), "black_only": black_only}
        )
        save_subscriptions()
        WIZARD_STATE.pop(user_id, None)
        text = (
            "✅ Подписка создана через конструктор!\n"
            f"collection={state['collection']}, max_percent={state['max_percent']}%, black_only={black_only}"
        )
        await call.message.edit_text(text, reply_markup=subs_menu_keyboard())
        await call.answer("Готово")
        return
    if action.startswith("hwc_"):
        collection = action.replace("hwc_", "", 1)
        state = WIZARD_STATE.setdefault(user_id, {})
        state.update({"mode": "hunt_btn", "collection": collection})
        model_options = ["any", "Hong Long", "Alpaca", "Iron Rose", "Red Molotov", "ChillFlame"]
        state["model_options"] = model_options
        await call.message.edit_text(
            f"Шаг 2/4: подарок <b>{html.escape(collection)}</b>\nВыберите модель:",
            parse_mode="HTML",
            reply_markup=hunt_options_keyboard("hwm", model_options),
        )
        await call.answer()
        return
    if action.startswith("hwm_"):
        state = WIZARD_STATE.get(user_id, {})
        options = state.get("model_options", [])
        try:
            idx = int(action.replace("hwm_", "", 1))
            state["model"] = normalize_hunt_value(options[idx])
        except Exception:
            await call.answer("Некорректный выбор модели", show_alert=True)
            return
        bg_options = ["any", "Black", "French Violet", "Dark Green", "Deep Black", "Silver"]
        state["bg_options"] = bg_options
        await call.message.edit_text(
            "Шаг 3/4: выберите фон:",
            reply_markup=hunt_options_keyboard("hwb", bg_options),
        )
        await call.answer()
        return
    if action.startswith("hwb_"):
        state = WIZARD_STATE.get(user_id, {})
        options = state.get("bg_options", [])
        try:
            idx = int(action.replace("hwb_", "", 1))
            state["bg"] = normalize_hunt_value(options[idx])
        except Exception:
            await call.answer("Некорректный выбор фона", show_alert=True)
            return
        symbol_options = ["any", "Straw Hat", "Venetian Mask", "Narcissus", "Golden Star", "Ruby Ring"]
        state["symbol_options"] = symbol_options
        await call.message.edit_text(
            "Шаг 4/4: выберите символ:",
            reply_markup=hunt_options_keyboard("hws", symbol_options),
        )
        await call.answer()
        return
    if action.startswith("hws_"):
        state = WIZARD_STATE.get(user_id, {})
        options = state.get("symbol_options", [])
        try:
            idx = int(action.replace("hws_", "", 1))
            symbol = normalize_hunt_value(options[idx])
        except Exception:
            await call.answer("Некорректный выбор символа", show_alert=True)
            return
        profile = get_user_profile(user_id)
        profile.setdefault("hunts", [])
        profile["hunts"].append(
            {
                "collection": normalize_hunt_value(state.get("collection", "all")),
                "model": normalize_hunt_value(state.get("model", "any")),
                "bg": normalize_hunt_value(state.get("bg", "any")),
                "symbol": symbol,
            }
        )
        profile["hunts"] = profile["hunts"][-20:]
        save_gamification()
        WIZARD_STATE.pop(user_id, None)
        await call.message.edit_text("✅ PRO hunt добавлен через конструктор кнопок!", reply_markup=myhunts_manage_keyboard(user_id))
        await call.answer("Готово")
        return

    if action == "home":
        text = "📱 <b>Пользовательское меню</b>\nВыберите раздел:"
    elif action == "quickstart":
        text = build_quickstart_message(user_id)
        reply_markup = quickstart_keyboard()
    elif action == "quickstart_refresh":
        text = build_quickstart_message(user_id)
        reply_markup = quickstart_keyboard()
    elif action == "submenu":
        text = (
            "🔔 <b>Меню подписок</b>\n"
            "Здесь можно посмотреть текущие подписки, узнать формат добавления и очистить список."
        )
        reply_markup = subs_menu_keyboard()
    elif action == "profile":
        text = build_profile_message(user_id)
        reply_markup = profile_keyboard(user_id)
    elif action == "leaderboard":
        text = build_leaderboard_message()
        top = get_leaderboard_top(10)
        if top:
            reply_markup = build_leaderboard_keyboard(top)
    elif action == "challenge":
        text = build_challenge_message(user_id)
        reply_markup = challenge_keyboard()
    elif action == "challenge_accept":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        profile = get_user_profile(user_id)
        profile["challenge_accepted_date"] = today
        save_gamification()
        text = build_challenge_message(user_id)
        reply_markup = challenge_keyboard()
    elif action == "challenge_autosub":
        ch = current_daily_challenge()
        profile = get_user_profile(user_id)
        profile["challenge_accepted_date"] = datetime.utcnow().strftime("%Y-%m-%d")
        save_gamification()
        SUBSCRIPTIONS.setdefault(user_id, [])
        SUBSCRIPTIONS[user_id].append(
            {"collection": "all", "max_percent": float(ch["max_percent"]), "black_only": bool(ch["black_only"])}
        )
        save_subscriptions()
        text = (
            "⚡ Подписка под челлендж добавлена автоматически!\n\n"
            + build_challenge_message(user_id)
        )
        reply_markup = challenge_keyboard()
    elif action == "challenge_refresh":
        text = build_challenge_message(user_id)
        reply_markup = challenge_keyboard()
    elif action == "buypro":
        text = (
            "⭐ <b>Покупка PRO Hunts</b>\n"
            f"Тариф: <b>{PRO_STARS_DAYS} дней PRO</b>\n"
            f"Цена: <b>{PRO_STARS_PRICE} Stars</b>\n\n"
            "Нажмите кнопку ниже для оплаты Telegram Stars."
        )
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Купить PRO Hunters", callback_data="um_buypro_pay")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="um_huntmenu")],
            ]
        )
    elif action == "buypro_pay":
        await send_pro_invoice(call.message.chat.id, user_id)
        await call.answer("Счёт на оплату отправлен", show_alert=True)
        return
    elif action == "toprefs":
        text = build_toprefs_message()
    elif action == "mysubs":
        text = build_mysubs_text(user_id)
        if SUBSCRIPTIONS.get(user_id):
            reply_markup = mysubs_manage_keyboard(user_id)
    elif action == "sub_add_help":
        text = (
            "➕ <b>Как добавить подписку</b>\n"
            "<code>/subscribe &lt;collection|all&gt; &lt;max_percent&gt; [black_only 0|1]</code>\n\n"
            "Примеры:\n"
            "<code>/subscribe all 1.0 1</code>\n"
            "<code>/subscribe ViceCream 2.5 0</code>"
        )
        reply_markup = subs_menu_keyboard()
    elif action == "sub_wizard_start":
        WIZARD_STATE[user_id] = {"mode": "sub"}
        text = "Шаг 1/3: выберите коллекцию для подписки:"
        reply_markup = sub_wizard_collection_keyboard()
    elif action == "subs_clear_confirm":
        text = "⚠️ Удалить <b>все</b> ваши подписки?"
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить все", callback_data="um_subs_clear_do")],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data="um_submenu")],
            ]
        )
    elif action == "subs_clear_do":
        had_subs = bool(SUBSCRIPTIONS.get(user_id, []))
        SUBSCRIPTIONS[user_id] = []
        save_subscriptions()
        text = "🧹 Все подписки удалены." if had_subs else "🔔 Подписок уже не было."
        reply_markup = subs_menu_keyboard()
    elif action == "huntmenu":
        text = (
            "🎯 <b>Меню PRO hunts</b>\n"
            "Управляйте PRO-поисками по model/bg/symbol."
        )
        reply_markup = hunts_menu_keyboard()
    elif action == "hunt_add_help":
        text = (
            "➕ <b>Как добавить PRO hunt</b>\n"
            "<code>/hunt \"Model Name\" \"BG Name\" \"Symbol Name\"</code>\n"
            "или\n"
            "<code>/hunt model=Model Name; bg=BG Name; symbol=Symbol Name</code>"
        )
        reply_markup = hunts_menu_keyboard()
    elif action == "hunt_wizard_start":
        if not is_pro_active(user_id):
            text = "🔒 PRO не активен. Конструктор PRO hunt доступен только с активным PRO."
            reply_markup = hunts_menu_keyboard()
        else:
            WIZARD_STATE[user_id] = {"mode": "hunt_btn", "step": "collection"}
            text = "Шаг 1/4: выберите название подарка (коллекцию):"
            reply_markup = hunt_collection_keyboard()
    elif action == "myhunts":
        text = build_myhunts_text(user_id)
        profile = get_user_profile(user_id)
        hunts = profile.get("hunts", [])
        reply_markup = myhunts_manage_keyboard(user_id) if hunts and is_pro_active(user_id) else hunts_menu_keyboard()
    elif action == "hunts_clear_confirm":
        if not is_pro_active(user_id):
            text = "🔒 PRO не активен. Конструктор PRO hunt доступен только с активным PRO."
            reply_markup = hunts_menu_keyboard()
        else:
            text = "⚠️ Удалить <b>все</b> PRO hunts?"
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, удалить все", callback_data="um_hunts_clear_do")],
                    [InlineKeyboardButton(text="⬅️ Отмена", callback_data="um_huntmenu")],
                ]
            )
    elif action == "hunts_clear_do":
        profile = get_user_profile(user_id)
        had_hunts = bool(profile.get("hunts", []))
        profile["hunts"] = []
        save_gamification()
        text = "🧹 Все PRO hunts удалены." if had_hunts else "🎯 Список PRO hunts уже пуст."
        reply_markup = hunts_menu_keyboard()
    elif action == "admin":
        if not is_admin(call.from_user.id):
            await call.answer("Недостаточно прав", show_alert=True)
            return
        await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())
        await call.answer()
        return
    else:
        await call.answer("Неизвестная кнопка", show_alert=True)
        return

    try:
        await call.message.edit_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await call.answer()


@dp.callback_query(F.data.startswith("lbp_"))
async def leaderboard_profile_callback(call: types.CallbackQuery):
    uid = call.data.replace("lbp_", "", 1)
    if not uid.isdigit():
        await call.answer("Некорректный ID профиля", show_alert=True)
        return
    text = build_profile_message(uid, update_activity=False)
    back_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К лидерборду", callback_data="lb_back")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="um_home")],
        ]
    )
    try:
        await call.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=back_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await call.answer()


@dp.callback_query(F.data == "lb_back")
async def leaderboard_back_callback(call: types.CallbackQuery):
    top = get_leaderboard_top(10)
    markup = build_leaderboard_keyboard(top) if top else user_menu_keyboard(is_admin(call.from_user.id))
    try:
        await call.message.edit_text(build_leaderboard_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await call.answer()


@dp.callback_query(F.data == "back_user_menu")
async def back_user_menu(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text(
        "📱 <b>Пользовательское меню</b>\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=user_menu_keyboard(True),
    )


def build_profile_message(uid: str, update_activity: bool = True):
    profile = touch_user(uid) if update_activity else get_user_profile(uid)
    if update_activity:
        save_gamification()
    rank_score = profile.get("rare_alerts_received", 0)
    if rank_score >= 50:
        rank = "🏆 Legend"
    elif rank_score >= 20:
        rank = "🥇 Gold"
    elif rank_score >= 10:
        rank = "🥈 Silver"
    else:
        rank = "🥉 Bronze"
    pro_status = format_pro_status(profile)
    msg = (
        "👤 <b>Профиль охотника</b>\n"
        "────────────────────\n"
        f"🏆 Ранг: <b>{rank}</b>\n"
        f"🔔 Получено алертов: <b>{profile.get('alerts_received', 0)}</b>\n"
        f"🔥 Редких алертов: <b>{profile.get('rare_alerts_received', 0)}</b>\n"
        f"⚡️ Активность (streak): <b>{profile.get('streak', 0)}</b> дн.\n"
        f"📣 Рефералы: <b>{profile.get('referrals', 0)}</b> (за неделю: {profile.get('weekly_referrals', 0)})\n"
        f"💎 PRO Hunters: <b>{pro_status}</b>\n"
        f"🎯 Активных PRO hunts: <b>{len(profile.get('hunts', []))}</b>"
    )
    hits = profile.get("hits", [])
    if hits:
        top5 = sorted(hits, key=lambda x: x.get("model_percent", 100))[:5]
        msg += "\n\n🏆 <b>ТОП 5 найденных NFT по подписке:</b>\n"
        for i, h in enumerate(top5, 1):
            marker = "⚫" if h.get("black_bg") else ""
            msg += (
                f"{i}) {marker}<a href='{h['link']}'>{h['collection']} #{h['nft_id']}</a> — "
                f"{h.get('model_percent', 0)}%\n"
            )
    return msg


@dp.message(Command("leaderboard"))
async def leaderboard_cmd(message: types.Message):
    top = get_leaderboard_top(10)
    await message.answer(
        build_leaderboard_message(),
        parse_mode="HTML",
        reply_markup=build_leaderboard_keyboard(top) if top else None,
    )


def get_leaderboard_top(limit: int = 10):
    users = GAMIFICATION.get("users", {})
    return sorted(users.items(), key=lambda kv: (kv[1].get("rare_alerts_received", 0), kv[1].get("alerts_received", 0)), reverse=True)[:limit]


def build_leaderboard_keyboard(top):
    rows = []
    for i, (uid, _) in enumerate(top, 1):
        rows.append([InlineKeyboardButton(text=f"👤 Профиль #{i}", callback_data=f"lbp_{uid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_leaderboard_message():
    top = get_leaderboard_top(10)
    if not top:
        return "Лидерборд пока пуст."
    msg = "🏅 <b>Лидерборд охотников</b>\n"
    for i, (uid, p) in enumerate(top, 1):
        msg += (
            f"{i}) <code>{uid}</code> — Rare: {p.get('rare_alerts_received', 0)}, "
            f"Alerts: {p.get('alerts_received', 0)}, <i>Профиль: кнопка ниже</i>\n"
        )
    return msg


@dp.message(Command("toprefs"))
async def toprefs_cmd(message: types.Message):
    await message.answer(build_toprefs_message(), parse_mode="HTML")


def build_toprefs_message():
    ensure_weekly_tracking()
    users = GAMIFICATION.get("users", {})
    if not users:
        return "Реферальная таблица пока пустая."
    week_key = GAMIFICATION.get("week_key", current_week_key())
    top = sorted(users.items(), key=lambda kv: kv[1].get("weekly_referrals", 0), reverse=True)[:10]
    msg = f"📣 <b>TOP рефереров недели ({week_key})</b>\n────────────────────\n"
    for i, (uid, p) in enumerate(top, 1):
        msg += f"{i}) <code>{uid}</code> — week: <b>{p.get('weekly_referrals', 0)}</b> | total: <b>{p.get('referrals', 0)}</b>\n"
    msg += "────────────────────"
    return msg


def challenge_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять челлендж", callback_data="um_challenge_accept")],
            [InlineKeyboardButton(text="⚡ Авто-настроить под челлендж", callback_data="um_challenge_autosub")],
            [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="um_challenge_refresh")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")],
        ]
    )


def profile_keyboard(uid: str):
    is_pro = is_pro_active(uid)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Купить PRO Hunters" if not is_pro else "⭐ Продлить PRO Hunters", callback_data="um_buypro_pay")],
            [InlineKeyboardButton(text="🎯 Открыть PRO hunts", callback_data="um_huntmenu")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")],
        ]
    )


def quickstart_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Создать подписку", callback_data="um_sub_wizard_start")],
            [InlineKeyboardButton(text="🎯 Создать PRO hunt", callback_data="um_hunt_wizard_start")],
            [InlineKeyboardButton(text="🎮 Открыть челлендж", callback_data="um_challenge")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="um_profile")],
            [InlineKeyboardButton(text="🔄 Обновить прогресс", callback_data="um_quickstart_refresh")],
        ]
    )


def format_pro_status(profile: dict) -> str:
    pro_until = profile.get("pro_until") or ""
    if not pro_until:
        return "🔒 Не активен"
    try:
        end_date = datetime.strptime(pro_until, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        days_left = (end_date - today).days
        if days_left < 0:
            return "🔒 Истёк"
        return f"✅ Активен до {pro_until} (осталось ~{days_left + 1} дн.)"
    except ValueError:
        return f"✅ Активен до {pro_until}"


def build_quickstart_message(uid: str):
    subs_count = len(SUBSCRIPTIONS.get(uid, []))
    profile = get_user_profile(uid)
    hunts_count = len(profile.get("hunts", []))
    pro_status = "✅ Активен" if is_pro_active(uid) else "🔒 Не активен"
    return (
        "⚡ <b>Быстрый старт (игровой)</b>\n"
        "Сделайте шаги и закрепитесь в топе охотников:\n\n"
        f"1) Подписки: <b>{subs_count}</b> {'✅' if subs_count else '⬜'}\n"
        f"2) PRO hunts: <b>{hunts_count}</b> {'✅' if hunts_count else '⬜'}\n"
        f"3) PRO статус: <b>{pro_status}</b>\n"
        f"4) Daily streak: <b>{profile.get('streak', 0)}</b> дн.\n\n"
        "Жмите кнопки ниже — никаких ручных команд."
    )


@dp.message(Command("challenge"))
async def challenge_cmd(message: types.Message):
    await message.answer(build_challenge_message(str(message.from_user.id)), parse_mode="HTML", reply_markup=challenge_keyboard())


def build_challenge_message(uid: str | None = None):
    ch = current_daily_challenge()
    condition = f"model ≤ {ch['max_percent']}%"
    if ch["black_only"]:
        condition += " + black bg"
    reward_days = ch.get("reward_days", 1)
    status = ""
    if uid:
        profile = get_user_profile(uid)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        accepted = profile.get("challenge_accepted_date") == today
        completed = profile.get("challenge_completed_date") == today
        status = f"\nСтатус: <b>{'🏆 Выполнен' if completed else ('✅ Принят' if accepted else '🕒 Не принят')}</b>"
    return (
        f"🎯 <b>Охота дня: {ch['title']}</b>\n"
        f"Описание: <i>{ch.get('tagline', 'Поймай лучший сигнал дня')}</i>\n"
        f"Условие: <b>{condition}</b>\n"
        f"Награда: <b>+{reward_days} дн. PRO</b> при выполнении.{status}\n"
        "Участвуй через подписки и отслеживание алертов."
    )


async def send_pro_invoice(chat_id: int, uid: str):
    payload = f"pro_{uid}_{int(time.time())}"
    prices = [LabeledPrice(label=f"PRO Hunters ({PRO_STARS_DAYS} days)", amount=PRO_STARS_PRICE)]
    await bot.send_invoice(
        chat_id=chat_id,
        title="PRO Hunters Subscription",
        description=f"Доступ к PRO Hunters на {PRO_STARS_DAYS} дней",
        payload=payload,
        currency="XTR",
        prices=prices,
        provider_token="",
        start_parameter="buypro",
    )


@dp.message(Command("refevent"))
async def referral_event_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /refevent start <name> <bonus_days> <YYYY-MM-DD> | /refevent stop")
        return
    if parts[1] == "stop":
        GAMIFICATION["ref_event"] = {}
        save_gamification()
        await message.answer("🛑 Реферальный ивент остановлен.")
        return
    if parts[1] == "start" and len(parts) >= 5:
        name = parts[2]
        try:
            bonus_days = int(parts[3])
            datetime.strptime(parts[4], "%Y-%m-%d")
        except ValueError:
            await message.answer("Формат: /refevent start <name> <bonus_days:int> <YYYY-MM-DD>")
            return
        GAMIFICATION["ref_event"] = {"name": name, "bonus_days": bonus_days, "end_date": parts[4]}
        save_gamification()
        await message.answer(f"✅ Ивент запущен: {name} | бонус {bonus_days} дней | до {parts[4]}")
        return
    await message.answer("Использование: /refevent start <name> <bonus_days> <YYYY-MM-DD> | /refevent stop")


@dp.message(Command("givepro"))
async def give_pro_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /givepro <user_id|me> <days>")
        return
    target = str(message.from_user.id) if parts[1].lower() == "me" else parts[1]
    try:
        int(target)
        days = int(parts[2])
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Проверьте параметры: user_id и days должны быть корректными.")
        return
    new_until = grant_pro_days(target, days)
    save_gamification()
    await message.answer(f"✅ PRO выдан: user={target}, days={days}, до {new_until}")
    try:
        await bot.send_message(int(target), f"🎁 Вам выдан PRO на {days} дней. Активен до: {new_until}")
    except Exception as e:
        logging.warning("Cannot notify user %s about PRO: %s", target, e)


@dp.message(Command("takepro"))
async def take_pro_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /takepro <user_id|me>")
        return
    target = str(message.from_user.id) if parts[1].lower() == "me" else parts[1]
    try:
        int(target)
    except ValueError:
        await message.answer("user_id должен быть числом или 'me'.")
        return
    profile = get_user_profile(target)
    profile["pro_until"] = ""
    save_gamification()
    await message.answer(f"✅ PRO снят у пользователя {target}")
    try:
        await bot.send_message(int(target), "ℹ️ Ваш PRO статус был отключен администратором.")
    except Exception as e:
        logging.warning("Cannot notify user %s about PRO removal: %s", target, e)


@dp.message(Command("buypro"))
async def buy_pro_cmd(message: types.Message):
    uid = str(message.from_user.id)
    await send_pro_invoice(message.chat.id, uid)


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payment = message.successful_payment
    if not payment or payment.currency != "XTR" or not payment.invoice_payload.startswith("pro_"):
        return
    uid = str(message.from_user.id)
    new_until = grant_pro_days(uid, PRO_STARS_DAYS)
    save_gamification()
    await message.answer(
        f"✅ Оплата получена! PRO активирован на {PRO_STARS_DAYS} дней.\n"
        f"Активен до: <b>{new_until}</b>",
        parse_mode="HTML",
        reply_markup=hunts_menu_keyboard(),
    )


@dp.callback_query(F.data == "back_main")
async def back_main(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


def build_health_message():
    uptime = int(time.time() - START_TS)
    h = uptime // 3600
    m = (uptime % 3600) // 60
    s = uptime % 60
    mode = SETTINGS.get("signal_mode", "balanced")
    enabled = sum(1 for c in COLLECTIONS.values() if c.get("enabled"))
    total_cache = len(SENT_CACHE)
    total_subs = sum(len(v) for v in SUBSCRIPTIONS.values())
    return (
        f"🟢 <b>HEALTH OK</b>\n"
        f"Uptime: <b>{h:02d}:{m:02d}:{s:02d}</b>\n"
        f"Mode: <b>{mode}</b>\n"
        f"Enabled collections: <b>{enabled}/{len(COLLECTIONS)}</b>\n"
        f"Main delay: <b>{SETTINGS.get('send_delay_main')}</b>s\n"
        f"Rare delay: <b>{SETTINGS.get('send_delay_rare')}</b>s\n"
        f"Subscriptions: <b>{total_subs}</b>\n"
        f"Dedup cache size: <b>{total_cache}</b>"
    )


@dp.callback_query(F.data == "admin_live_stats")
async def admin_live_stats(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text(build_live_stats_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=main_admin_menu())


@dp.callback_query(F.data == "admin_owners")
async def admin_owners(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text(build_owners_message("rare"), parse_mode="HTML", disable_web_page_preview=True, reply_markup=owners_sort_keyboard("rare"))


@dp.callback_query(F.data == "admin_health")
async def admin_health(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text(build_health_message(), parse_mode="HTML", reply_markup=main_admin_menu())


@dp.callback_query(F.data == "admin_toggle_autostats")
async def admin_toggle_autostats(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    SETTINGS["live_stats_enabled"] = not SETTINGS.get("live_stats_enabled", False)
    save_settings()
    await call.answer(f"AutoStats {'ON' if SETTINGS['live_stats_enabled'] else 'OFF'}")
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.callback_query(F.data == "admin_toggle_dashboard")
async def admin_toggle_dashboard(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    if SETTINGS.get("dashboard_enabled"):
        SETTINGS["dashboard_enabled"] = False
        save_settings()
        await call.answer("Dashboard OFF")
        await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())
        return

    chat_id = SETTINGS.get("dashboard_chat_id", str(call.message.chat.id))
    msg = await bot.send_message(chat_id, build_live_stats_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=dashboard_keyboard())
    SETTINGS["dashboard_enabled"] = True
    SETTINGS["dashboard_chat_id"] = chat_id
    SETTINGS["dashboard_message_id"] = msg.message_id
    save_settings()
    await call.answer("Dashboard ON")
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.callback_query(F.data == "admin_commands_help")
async def admin_commands_help(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    text = (
        "📚 <b>Полный список админ-команд</b>\n\n"
        "⚙️ <b>Панель и мониторинг</b>\n"
        "• <code>/admin</code> — открыть админ-панель\n"
        "• <code>/stats</code> — живая статистика коллекций\n"
        "• <code>/autostats on|off [interval_sec]</code> — авто-рассылка stats\n"
        "• <code>/dashboard start|stop [chat_id]</code> — включить/выключить dashboard\n"
        "• <code>/health</code> — проверка состояния бота\n\n"
        "🧩 <b>Управление данными</b>\n"
        "• <code>/setchat main|rare @channel_or_id</code> — смена чатов рассылки\n"
        "• <code>/owners</code> — рейтинг владельцев NFT\n\n"
        "🎁 <b>PRO / геймификация</b>\n"
        "• <code>/givepro &lt;user_id|me&gt; &lt;days&gt;</code> — выдать PRO вручную\n"
        "• <code>/takepro &lt;user_id|me&gt;</code> — снять PRO вручную\n"
        "• <code>/refevent start &lt;name&gt; &lt;bonus_days&gt; &lt;YYYY-MM-DD&gt;</code> — запустить ref-ивент\n"
        "• <code>/refevent stop</code> — остановить ref-ивент\n\n"
        "💡 Подсказка: большая часть настроек доступна через кнопки в админ-меню выше."
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=main_admin_menu())


@dp.callback_query(F.data == "admin_overview")
async def admin_overview(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    text = (
        "🧭 <b>Как пользоваться админ-меню</b>\n\n"
        "1) Выберите коллекцию в верхней части, чтобы управлять её фильтром и статусом.\n"
        "2) Используйте блоки ниже для live-статистики, owners и health.\n"
        "3) Переключайте AutoStats/Dashboard кнопками ON/OFF.\n"
        "4) Настройте задержки отправки и режим сигналов (conservative/balanced/aggressive).\n"
        "5) Для ручных операций используйте раздел <b>📚 Админ команды</b>.\n\n"
        "Нажмите «⬅️ Назад», чтобы вернуться в панель."
    )
    back = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=back)


@dp.callback_query(F.data.startswith("collection_"))
async def select_collection(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    name = call.data.replace("collection_", "")
    if name not in COLLECTIONS:
        await call.answer("Коллекция не найдена", show_alert=True)
        return

    await call.message.edit_text(
        f"🎯 Управление коллекцией <b>{name}</b>",
        parse_mode="HTML",
        reply_markup=collection_menu(name),
    )


@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_collection(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    name = call.data.replace("toggle_", "")
    if name in COLLECTIONS:
        COLLECTIONS[name]["enabled"] = not COLLECTIONS[name]["enabled"]
        save_collections()
        await call.answer("Статус обновлен")
        await call.message.edit_text(
            f"🎯 Управление коллекцией <b>{name}</b>",
            parse_mode="HTML",
            reply_markup=collection_menu(name),
        )


@dp.callback_query(F.data.startswith("setpercent_"))
async def set_percent(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    _, name, percent = call.data.split("_")
    if name not in COLLECTIONS:
        await call.answer("Коллекция не найдена", show_alert=True)
        return

    COLLECTIONS[name]["max_percent"] = float(percent)
    save_collections()

    await call.answer(f"Установлено: ≤ {percent}%")
    await call.message.edit_text(
        f"🎯 Управление коллекцией <b>{name}</b>",
        parse_mode="HTML",
        reply_markup=collection_menu(name),
    )


@dp.callback_query(F.data == "reset_rare_confirm")
async def reset_rare_confirm(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, сбросить", callback_data="reset_rare_do")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await call.message.edit_text("Подтвердите сброс счётчиков редких NFT для всех коллекций.", reply_markup=kb)


@dp.callback_query(F.data == "reset_rare_do")
async def reset_rare_do(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    for c in COLLECTIONS:
        COLLECTIONS[c]["rare_count"] = 0
    save_collections()
    await call.answer("Счётчики редких сброшены")
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.callback_query(F.data == "delay_main_menu")
async def delay_main_menu(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text("Выберите задержку отправки обычных NFT:", reply_markup=delay_menu("main"))


@dp.callback_query(F.data == "delay_rare_menu")
async def delay_rare_menu(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text("Выберите задержку отправки редких NFT:", reply_markup=delay_menu("rare"))


@dp.callback_query(F.data.startswith("delaymain_"))
async def delay_main_set(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    value = float(call.data.split("_")[1])
    SETTINGS["send_delay_main"] = value
    save_settings()
    await call.answer(f"Установлено: {value}s")
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.callback_query(F.data.startswith("delayrare_"))
async def delay_rare_set(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    value = float(call.data.split("_")[1])
    SETTINGS["send_delay_rare"] = value
    save_settings()
    await call.answer(f"Установлено: {value}s")
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.callback_query(F.data == "chat_help")
async def chat_help(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    text = (
        "📨 <b>Смена чатов рассылки</b>\n\n"
        "Текущий main: <code>{main}</code>\n"
        "Текущий rare: <code>{rare}</code>\n\n"
        "Команды:\n"
        "<code>/setchat main @channel_or_id</code>\n"
        "<code>/setchat rare @channel_or_id</code>"
    ).format(main=SETTINGS["chat_id"], rare=SETTINGS["rare_chat_id"])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]))


@dp.message(Command("setchat"))
async def set_chat(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or parts[1] not in ("main", "rare"):
        await message.answer("Использование: /setchat main|rare @channel_or_id")
        return
    key = "chat_id" if parts[1] == "main" else "rare_chat_id"
    SETTINGS[key] = parts[2].strip()
    save_settings()
    await message.answer(f"✅ Обновлено: {parts[1]} => {SETTINGS[key]}")


@dp.callback_query(F.data == "mode_menu")
async def mode_menu(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text("Выберите режим сигналов:", reply_markup=signal_mode_menu())


@dp.callback_query(F.data.startswith("setmode_"))
async def set_mode(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    mode = call.data.replace("setmode_", "")
    if mode not in ("conservative", "balanced", "aggressive"):
        await call.answer("Неизвестный режим", show_alert=True)
        return
    SETTINGS["signal_mode"] = mode
    save_settings()
    await call.answer(f"Режим установлен: {mode}")
    await call.message.edit_text("⚙️ Панель админа", reply_markup=main_admin_menu())


@dp.message(Command("subscribe"))
async def subscribe(message: types.Message):
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /subscribe <collection|all> <max_percent> [black_only 0|1]")
        return
    collection = parts[1]
    if collection != "all" and collection not in COLLECTIONS:
        await message.answer("Неизвестная коллекция.")
        return
    try:
        max_percent = float(parts[2])
    except ValueError:
        await message.answer("max_percent должен быть числом.")
        return
    black_only = len(parts) > 3 and parts[3] == "1"
    uid = str(message.from_user.id)
    SUBSCRIPTIONS.setdefault(uid, [])
    SUBSCRIPTIONS[uid].append({"collection": collection, "max_percent": max_percent, "black_only": black_only})
    save_subscriptions()
    await message.answer(f"✅ Подписка добавлена: {collection}, <= {max_percent}%, black_only={black_only}")


@dp.message(Command("hunt"))
async def hunt_add(message: types.Message):
    uid = str(message.from_user.id)
    if not is_pro_active(uid):
        await message.answer("🔒 Эта функция доступна только для PRO. Активируйте PRO через рефералов, /buypro или /givepro.")
        return
    raw = (message.text or "").replace("/hunt", "", 1).strip()
    parsed = parse_hunt_input(raw)
    if not parsed:
        await message.answer(
            "Использование:\n"
            "/hunt \"Model Name\" \"BG Name\" \"Symbol Name\"\n"
            "или\n"
            "/hunt model=Model Name; bg=BG Name; symbol=Symbol Name"
        )
        return
    model, bg, symbol = parsed
    profile = get_user_profile(uid)
    profile.setdefault("hunts", [])
    profile["hunts"].append({"collection": "all", "model": model, "bg": bg, "symbol": symbol})
    profile["hunts"] = profile["hunts"][-20:]
    save_gamification()
    await message.answer(f"🎯 PRO-поиск добавлен: collection=all, model={model}, bg={bg}, symbol={symbol}")


@dp.message(Command("myhunts"))
async def myhunts(message: types.Message):
    uid = str(message.from_user.id)
    text = build_myhunts_text(uid)
    profile = get_user_profile(uid)
    hunts = profile.get("hunts", [])
    markup = myhunts_manage_keyboard(uid) if hunts and is_pro_active(uid) else hunts_menu_keyboard()
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.message(Command("unhunt"))
async def unhunt(message: types.Message):
    uid = str(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unhunt <номер>")
        return
    profile = get_user_profile(uid)
    hunts = profile.get("hunts", [])
    if not hunts:
        await message.answer("Список PRO-поисков пуст.")
        return
    try:
        idx = int(parts[1]) - 1
        removed = hunts.pop(idx)
    except Exception:
        await message.answer("Некорректный номер.")
        return
    profile["hunts"] = hunts
    save_gamification()
    await message.answer(
        "🗑 Удален поиск: "
        f"collection={removed.get('collection', 'all')}, "
        f"model={removed.get('model', 'any')}, bg={removed.get('bg', 'any')}, symbol={removed.get('symbol', 'any')}"
    )


@dp.message()
async def wizard_text_input(message: types.Message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    uid = str(message.from_user.id)
    state = WIZARD_STATE.get(uid)
    if not state or state.get("mode") != "hunt":
        return

    if state.get("step") == "model":
        state["model"] = normalize_hunt_value(text)
        state["step"] = "bg"
        await message.answer(
            "Шаг 2/3: отправьте <b>фон</b> текстом.\n"
            "Можно написать <code>any</code> / <code>all</code> / <code>*</code>.",
            parse_mode="HTML",
        )
        return
    if state.get("step") == "bg":
        state["bg"] = normalize_hunt_value(text)
        state["step"] = "symbol"
        await message.answer(
            "Шаг 3/3: отправьте <b>символ</b> текстом.\n"
            "Можно написать <code>any</code> / <code>all</code> / <code>*</code>.",
            parse_mode="HTML",
        )
        return
    if state.get("step") == "symbol":
        state["symbol"] = normalize_hunt_value(text)
        profile = get_user_profile(uid)
        profile.setdefault("hunts", [])
        profile["hunts"].append({"collection": "all", "model": state["model"], "bg": state["bg"], "symbol": state["symbol"]})
        profile["hunts"] = profile["hunts"][-20:]
        save_gamification()
        WIZARD_STATE.pop(uid, None)
        await message.answer(
            f"✅ PRO hunt добавлен: model={state['model']}, bg={state['bg']}, symbol={state['symbol']}",
            reply_markup=hunts_menu_keyboard(),
        )


@dp.message(Command("mysubs"))
async def my_subs(message: types.Message):
    uid = str(message.from_user.id)
    subs = SUBSCRIPTIONS.get(uid, [])
    if not subs:
        await message.answer("У вас нет подписок. Пример: /subscribe all 1.0 0")
        return
    await message.answer(build_mysubs_text(uid), parse_mode="HTML", reply_markup=mysubs_manage_keyboard(uid))


@dp.message(Command("unsub"))
async def unsub(message: types.Message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unsub <номер>")
        return
    uid = str(message.from_user.id)
    subs = SUBSCRIPTIONS.get(uid, [])
    if not subs:
        await message.answer("Подписок нет.")
        return
    try:
        idx = int(parts[1]) - 1
        removed = subs.pop(idx)
    except Exception:
        await message.answer("Некорректный номер.")
        return
    SUBSCRIPTIONS[uid] = subs
    save_subscriptions()
    await message.answer(f"Удалено: {removed['collection']} <= {removed['max_percent']}%")


@dp.message(Command("owners"))
async def owners_top(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if not OWNER_STATS:
        await message.answer("Пока нет данных по владельцам.")
        return
    await message.answer(
        build_owners_message("rare"),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=owners_sort_keyboard("rare"),
    )


def owners_sort_keyboard(current: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{'✅ ' if current == 'rare' else ''}По rare", callback_data="owners_sort_rare"),
                InlineKeyboardButton(text=f"{'✅ ' if current == 'total' else ''}По total", callback_data="owners_sort_total"),
                InlineKeyboardButton(text=f"{'✅ ' if current == 'ratio' else ''}По ratio", callback_data="owners_sort_ratio"),
            ]
        ]
    )


def _global_counts():
    data = [item for items in RECENT_STATS.values() for item in items]
    total = len(data)
    black = sum(1 for n in data if n.get("black_bg"))
    rare = sum(1 for n in data if n.get("black_bg") and n.get("model_percent", 100) <= 1)
    return total, black, rare


def build_owners_message(sort_by: str):
    if sort_by == "total":
        ranking = sorted(OWNER_STATS.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)[:10]
    elif sort_by == "ratio":
        ranking = sorted(
            OWNER_STATS.items(),
            key=lambda kv: (kv[1].get("rare", 0) / max(1, kv[1].get("total", 1)), kv[1].get("total", 0)),
            reverse=True,
        )[:10]
    else:
        ranking = sorted(OWNER_STATS.items(), key=lambda kv: (kv[1].get("rare", 0), kv[1].get("total", 0)), reverse=True)[:10]

    total, black, rare = _global_counts()
    msg = (
        "👑 <b>OWNERS DASHBOARD v2</b>\n\n"
        f"• 📦 Всего NFT: <b>{total}</b>\n"
        f"• ⚫ Black: <b>{black}</b>\n"
        f"• 🔥 RAR: <b>{rare}</b>\n"
        f"• 🔀 Сортировка: <b>{sort_by}</b>\n"
        "────────────\n"
    )
    for i, (owner, info) in enumerate(ranking, 1):
        owner_link = info.get("link", "")
        owner_safe = clean_html(owner)
        owner_title = f"<a href='{owner_link}'>{owner_safe}</a>" if owner_link and owner_link.startswith("https://t.me") else owner_safe
        ratio = (info.get("rare", 0) / max(1, info.get("total", 1))) * 100
        msg += (
            f"<b>{i}) {owner_title}</b>\n"
            f"• 🔥 Rare: <b>{info.get('rare', 0)}</b>\n"
            f"• 📊 Total: <b>{info.get('total', 0)}</b>\n"
            f"• 📈 Ratio: <b>{ratio:.1f}%</b>\n\n"
        )
    msg += "────────────"
    return msg


@dp.callback_query(F.data.startswith("owners_sort_"))
async def owners_sort_callback(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    sort_by = call.data.replace("owners_sort_", "")
    if sort_by not in ("rare", "total", "ratio"):
        await call.answer("Неизвестная сортировка", show_alert=True)
        return
    await call.message.edit_text(
        build_owners_message(sort_by),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=owners_sort_keyboard(sort_by),
    )


@dp.message(Command("health"))
async def health(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(build_health_message(), parse_mode="HTML")


def _bars(values):
    if not values:
        return "нет данных"
    levels = [0, 0, 0, 0, 0]  # <=0.5, <=1, <=2, <=3, >3
    for v in values:
        if v <= 0.5:
            levels[0] += 1
        elif v <= 1:
            levels[1] += 1
        elif v <= 2:
            levels[2] += 1
        elif v <= 3:
            levels[3] += 1
        else:
            levels[4] += 1
    total = len(values)
    parts = [f"{n/total*100:.0f}%" for n in levels]
    return f"≤0.5:{parts[0]} | ≤1:{parts[1]} | ≤2:{parts[2]} | ≤3:{parts[3]} | >3:{parts[4]}"


def build_quick_stats(collection: str, limit: int = 10):
    data = RECENT_STATS.get(collection, [])
    if not data:
        return f"📊 <b>{collection}</b>\nНет данных."

    latest = data[-limit:]
    latest_lines = []
    for nft in reversed(latest):
        marker = "⚫" if nft.get("black_bg") else ""
        latest_lines.append(f"{marker}<a href='{nft['link']}'>#{nft['nft_id']}</a> — {nft.get('model_percent', 0)}%")

    top_rare = sorted(
        [n for n in data if n.get("black_bg") or n.get("model_percent", 100) <= 1],
        key=lambda x: x.get("model_percent", 100),
    )[:5]
    top_lines = []
    for idx, nft in enumerate(top_rare, 1):
        marker = "⚫" if nft.get("black_bg") else ""
        top_lines.append(f"{idx} Место: {marker}<a href='{nft['link']}'>#{nft['nft_id']}</a> — {nft.get('model_percent', 0)}%")
    if not top_lines:
        top_lines = ["нет данных"]

    model_values = [n.get("model_percent", 0) for n in data]
    black_ratio = sum(1 for n in data if n.get("black_bg")) / len(data)
    black_cnt = sum(1 for n in data if n.get("black_bg"))
    chart = (
        f"Model buckets: {_bars(model_values)}\n"
        f"Black BG ratio: {black_cnt}/{len(data)} ({black_ratio*100:.1f}%)"
    )

    return (
        f"📊 <b>{collection}</b> (быстрая статистика)\n"
        f"Последние {len(latest)} NFT:\n" + "\n".join(latest_lines) + "\n\n"
        f"🔥 ТОП редких NFT:\n" + "\n".join(top_lines) + "\n\n"
        f"📉 Графики:\n<code>{chart}</code>"
    )


@dp.callback_query(F.data == "quick_stats_menu")
async def quick_stats_menu_open(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    await call.message.edit_text("Выберите коллекцию для быстрой статистики:", reply_markup=quick_stats_menu())


@dp.callback_query(F.data.startswith("qstats_"))
async def quick_stats_show(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    collection = call.data.replace("qstats_", "")
    if collection not in COLLECTIONS:
        await call.answer("Коллекция не найдена", show_alert=True)
        return
    await call.message.edit_text(
        build_quick_stats(collection),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="quick_stats_menu")]]),
    )


# ========= FETCH =========
async def fetch(session, collection, nft_id):
    url = f"https://t.me/nft/{collection}-{nft_id}"
    try:
        async with session.get(url, timeout=6) as r:
            text = await r.text()
            if collection in text:
                logging.info("Fetched %s #%s", collection, nft_id)
                return text
            logging.warning("%s #%s not found in page", collection, nft_id)
            return None
    except Exception as e:
        logging.error("Error fetching %s #%s: %s", collection, nft_id, e)
        return None


# ========= PARSE =========
def parse(html_text):
    result = {
        "model": ("?", None),
        "bg": ("?", None),
        "symbol": ("?", None),
        "owner_name": "Unknown",
        "owner_link": "#",
    }

    rows = re.findall(r"<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*</tr>", html_text, re.S)
    for key, value in rows:
        key_low = key.lower()
        if "model" in key_low:
            result["model"] = extract(value)
        elif "backdrop" in key_low:
            result["bg"] = extract(value)
        elif "symbol" in key_low:
            result["symbol"] = extract(value)

    owner_value = None
    for key, value in rows:
        if "owner" in key.lower() or "владел" in key.lower():
            owner_value = value
            break

    if owner_value:
        owner = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', owner_value, re.S)
        if owner:
            link = owner.group(1).strip()
            name = re.sub(r"<.*?>", "", owner.group(2)).strip()

            if link.startswith("/"):
                link = "https://t.me" + link

            # Отбрасываем служебные/битые ссылки (иконки, статика telegram.org и т.п.)
            invalid_link = (
                "telegram.org/img" in link
                or link.endswith(".svg")
                or "website_icon" in link
            )

            if not invalid_link and link.startswith("https://t.me"):
                result["owner_link"] = link
                result["owner_name"] = name if name else "Unknown"

    return result


# ========= SEND MESSAGES =========
async def send(collection, nft_id, data):
    key = dedup_key("main", collection, nft_id, SETTINGS["chat_id"])
    if already_sent(key):
        return
    model_name, model_percent = data["model"]
    bg_name, bg_percent = data["bg"]
    symbol_name, symbol_percent = data["symbol"]
    is_black_bg = bg_name.lower() in ["black", "чёрный", "черный"]

    msg = f"""
🚀 <b>МИНТ НОВОГО NFT!</b>✨
────────────────────
🎁 <b>{collection} #{nft_id}</b>

🧬 <b>Модель:</b> {model_name} — <b>{fmt_percent(model_percent)}</b> {attr_emoji(model_name, model_percent)}
🎨 <b>Фон:</b> {bg_name} — <b>{fmt_percent(bg_percent)}</b> {attr_emoji(bg_name, bg_percent, is_black=is_black_bg)}
🔣 <b>Символ:</b> {symbol_name} — <b>{fmt_percent(symbol_percent)}</b> {attr_emoji(symbol_name, symbol_percent)}

🔗 <a href='https://t.me/nft/{collection}-{nft_id}'>Перейти к NFT</a>

👤 <b>Владелец:</b> <a href="{data.get('owner_link', '#')}">{data.get('owner_name', 'Unknown')}</a>

────────────────────
 💎 @Gift_NFT_Scaner | <a href='https://t.me/applepromax13'>Реклама?</a> | © <a href='https://t.me/+YVkj07r2_XlhOTcy'>Crypto Farm</a>
"""
    while True:
        try:
            await bot.send_message(SETTINGS["chat_id"], msg, parse_mode="HTML")
            await asyncio.sleep(float(SETTINGS.get("send_delay_main", 0.3)))
            mark_sent(key)
            break
        except Exception as e:
            logging.error("Error sending NFT %s #%s: %s", collection, nft_id, e)
            await asyncio.sleep(1)


async def send_rare(collection, nft_id, data):
    key = dedup_key("rare", collection, nft_id, SETTINGS["rare_chat_id"])
    if already_sent(key):
        return
    model_name, model_percent = data["model"]
    bg_name, bg_percent = data["bg"]
    symbol_name, symbol_percent = data["symbol"]
    owner_name = data.get("owner_name", "Unknown")
    owner_link = data.get("owner_link", "#")

    msg = f"""
💎 <b>НАЙДЕН РЕДКИЙ NFT!</b> 🔥
────────────────────
🎁 <b>{collection} #{nft_id}</b>

🧬 <b>Модель:</b> {model_name} — <b><i>{fmt_percent(model_percent)}</i></b> 🔥
🎨 <b>Фон:</b> {bg_name} 🖤 — <b><i>{fmt_percent(bg_percent)}</i></b>
🔣 <b>Символ:</b> {symbol_name} — <b><i>{fmt_percent(symbol_percent)}</i></b> 🔥

🔥 <b>BLACK + RARE</b>

🔗 <a href='https://t.me/nft/{collection}-{nft_id}'>Перейти к NFT</a>

👤 <b>Владелец:</b> <a href="{owner_link}">{owner_name}</a>


────────────────────
 💎 @Gift_NFT_Scaner | <a href='https://t.me/applepromax13'>Реклама?</a> | © <a href='https://t.me/+YVkj07r2_XlhOTcy'>Crypto Farm</a>
"""
    while True:
        try:
            await bot.send_message(SETTINGS["rare_chat_id"], msg, parse_mode="HTML")
            logging.info("Rare NFT sent: %s #%s", collection, nft_id)
            await asyncio.sleep(float(SETTINGS.get("send_delay_rare", 0.5)))
            mark_sent(key)
            break
        except Exception as e:
            logging.error("Error sending rare NFT %s #%s: %s", collection, nft_id, e)
            await asyncio.sleep(1)


# ========= RARE CHECK =========
def is_rare(data):
    return (
        data["model"][1] is not None
        and data["model"][1] <= 1
        and data["bg"][0].lower() in ["black", "черный", "чёрный"]
    )


def try_complete_challenge(uid: str, model_percent: float, black_bg: bool):
    profile = get_user_profile(uid)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if profile.get("challenge_accepted_date") != today:
        return None
    if profile.get("challenge_completed_date") == today:
        return None
    ch = current_daily_challenge()
    if model_percent > float(ch.get("max_percent", 100)):
        return None
    if ch.get("black_only") and not black_bg:
        return None
    reward_days = int(ch.get("reward_days", 1))
    new_until = grant_pro_days(uid, reward_days)
    profile["challenge_completed_date"] = today
    save_gamification()
    return {"title": ch.get("title", "Challenge"), "reward_days": reward_days, "new_until": new_until}


async def send_personal_alerts(collection, nft_id, data):
    model_percent = data["model"][1]
    if model_percent is None:
        return
    model_label, model_value = data["model"]
    bg_label, bg_value = data["bg"]
    symbol_label, symbol_value = data["symbol"]
    black_bg = data["bg"][0].lower() in ["black", "черный", "чёрный"]
    model_name = data["model"][0].lower()
    bg_name = data["bg"][0].lower()
    symbol_name = data["symbol"][0].lower()
    owner_link = data.get("owner_link", "#")
    owner_name = html.escape(str(data.get("owner_name", "Unknown")))
    owner_html = f"<a href='{owner_link}'>{owner_name}</a>" if owner_link.startswith("https://t.me") else owner_name
    for uid, subs in SUBSCRIPTIONS.items():
        for sub in subs:
            if sub["collection"] not in ("all", collection):
                continue
            if model_percent > float(sub.get("max_percent", 100)):
                continue
            if sub.get("black_only") and not black_bg:
                continue
            target = int(uid)
            key = dedup_key("sub", collection, nft_id, uid)
            if already_sent(key):
                continue
            sub_filter = (
                f"collection={sub.get('collection', 'all')}, "
                f"max_percent<={sub.get('max_percent', 100)}%, "
                f"black_only={bool(sub.get('black_only', False))}"
            )
            msg = (
                "🔔 <b>Подписка: найден NFT</b>\n"
                f"Фильтр: <code>{html.escape(sub_filter)}</code>\n"
                f"🎁 <b>{collection} #{nft_id}</b>\n\n"
                f"🧬 <b>Модель:</b> {html.escape(model_label)} — <b>{fmt_percent(model_value)}</b> {attr_emoji(model_label, model_value)}\n"
                f"🎨 <b>Фон:</b> {html.escape(bg_label)} — <b>{fmt_percent(bg_value)}</b> {attr_emoji(bg_label, bg_value, is_black=black_bg)}\n"
                f"🔣 <b>Символ:</b> {html.escape(symbol_label)} — <b>{fmt_percent(symbol_value)}</b> {attr_emoji(symbol_label, symbol_value)}\n\n"
                f"🔗 <a href='https://t.me/nft/{collection}-{nft_id}'>Перейти к NFT</a>\n\n"
                f"👤 <b>Владелец:</b> {owner_html}\n"
                "────────────────────\n"
                "💎 <a href='https://t.me/Gift_nft_scaner'>@Gift_NFT_Scaner</a> | "
                "<a href='https://t.me/onlyfanfarm'>@onlyfanfarm</a>"
            )
            try:
                await bot.send_message(target, msg, parse_mode="HTML", disable_web_page_preview=True)
                mark_sent(key)
                profile = touch_user(uid)
                profile["alerts_received"] += 1
                if model_percent <= 1 or black_bg:
                    profile["rare_alerts_received"] += 1
                profile.setdefault("hits", []).append(
                    {
                        "collection": collection,
                        "nft_id": nft_id,
                        "model_percent": model_percent,
                        "black_bg": black_bg,
                        "link": f"https://t.me/nft/{collection}-{nft_id}",
                    }
                )
                profile["hits"] = profile["hits"][-300:]
                save_gamification()
                challenge_reward = try_complete_challenge(uid, model_percent, black_bg)
                if challenge_reward:
                    try:
                        await bot.send_message(
                            target,
                            (
                                f"🏆 <b>Челлендж выполнен: {challenge_reward['title']}</b>\n"
                                f"Награда: +{challenge_reward['reward_days']} дн. PRO\n"
                                f"PRO активен до: <b>{challenge_reward['new_until']}</b>"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logging.warning("Cannot send challenge reward notice to %s: %s", uid, e)
            except Exception as e:
                logging.warning("Cannot send personal alert to %s: %s", uid, e)

    # PRO hunts (поиск по конкретным параметрам)
    for uid, user_data in GAMIFICATION.get("users", {}).items():
        if not is_pro_active(uid):
            continue
        hunts = user_data.get("hunts", [])
        for hunt in hunts:
            c = hunt.get("collection", "all").lower()
            m = hunt.get("model", "any").lower()
            b = hunt.get("bg", "any").lower()
            s = hunt.get("symbol", "any").lower()
            if c not in ("all", "any", "*") and c != collection.lower():
                continue
            if m not in ("any", "*") and m != model_name:
                continue
            if b not in ("any", "*") and b != bg_name:
                continue
            if s not in ("any", "*") and s != symbol_name:
                continue

            key = dedup_key("hunt", collection, nft_id, uid)
            if already_sent(key):
                continue
            hunt_filter = f"collection={c}, model={m}, bg={b}, symbol={s}"
            model_match = "✅ exact" if m not in ("any", "*") else "🎯 any"
            bg_match = "✅ exact" if b not in ("any", "*") else "🎯 any"
            symbol_match = "✅ exact" if s not in ("any", "*") else "🎯 any"
            msg = (
                "🎯 <b>PRO MATCH: найден NFT</b>\n"
                f"Фильтр: <code>{html.escape(hunt_filter)}</code>\n"
                f"Совпадения: model({model_match}) • bg({bg_match}) • symbol({symbol_match})\n"
                f"🎁 <b>{collection} #{nft_id}</b>\n\n"
                f"🧬 <b>Модель:</b> {html.escape(model_label)} — <b>{fmt_percent(model_value)}</b> {attr_emoji(model_label, model_value)}\n"
                f"🎨 <b>Фон:</b> {html.escape(bg_label)} — <b>{fmt_percent(bg_value)}</b> {attr_emoji(bg_label, bg_value, is_black=black_bg)}\n"
                f"🔣 <b>Символ:</b> {html.escape(symbol_label)} — <b>{fmt_percent(symbol_value)}</b> {attr_emoji(symbol_label, symbol_value)}\n\n"
                f"🔗 <a href='https://t.me/nft/{collection}-{nft_id}'>Перейти к NFT</a>\n\n"
                f"👤 <b>Владелец:</b> {owner_html}\n"
                "────────────────────\n"
                "💎 <a href='https://t.me/Gift_nft_scaner'>@Gift_NFT_Scaner</a> | "
                "<a href='https://t.me/onlyfanfarm'>@onlyfanfarm</a>"
            )
            try:
                await bot.send_message(int(uid), msg, parse_mode="HTML", disable_web_page_preview=True)
                mark_sent(key)
            except Exception as e:
                logging.warning("Cannot send PRO hunt alert to %s: %s", uid, e)


# ========= CHECK COLLECTION =========
async def check_collection(session, collection, current_ids):
    nft_id = current_ids[collection]
    html_text = await fetch(session, collection, nft_id)
    if not html_text:
        logging.warning("%s #%s skipped, no HTML", collection, nft_id)
        return

    data = parse(html_text)
    logging.info("Processing %s #%s", collection, nft_id)

    model_percent = data["model"][1]
    max_percent = mode_threshold(collection)

    if model_percent is not None and model_percent <= max_percent:
        await send(collection, nft_id, data)
        await send_personal_alerts(collection, nft_id, data)

        if is_rare(data):
            await send_rare(collection, nft_id, data)
            COLLECTIONS[collection]["rare_count"] += 1
            save_collections()

    owner_name = data.get("owner_name", "Unknown").strip() or "Unknown"
    owner_link = data.get("owner_link", "")
    OWNER_STATS.setdefault(owner_name, {"total": 0, "rare": 0, "link": ""})
    if "link" not in OWNER_STATS[owner_name]:
        OWNER_STATS[owner_name]["link"] = ""
    if owner_link.startswith("https://t.me"):
        OWNER_STATS[owner_name]["link"] = owner_link
    OWNER_STATS[owner_name]["total"] += 1
    if is_rare(data):
        OWNER_STATS[owner_name]["rare"] += 1
    save_owner_stats()

    black_bg = data["bg"][0].lower() in ["black", "черный", "чёрный"]

    item = {
        "nft_id": nft_id,
        "model_percent": model_percent if model_percent is not None else 0.0,
        "black_bg": black_bg,
        "link": f"https://t.me/nft/{collection}-{nft_id}",
    }
    STATS.setdefault(collection, []).append(item)
    save_stats()

    RECENT_STATS.setdefault(collection, []).append(item)
    RECENT_STATS[collection] = RECENT_STATS[collection][-200:]
    save_recent_stats()

    current_ids[collection] += 1
    COLLECTIONS[collection]["start"] = current_ids[collection]
    save_collections()


# ========= SEND STATS =========
async def send_stats_if_ready(collection, current_ids):
    del current_ids
    data = STATS.get(collection, [])
    if len(data) < MAX_HISTORY:
        return

    counts = {}
    total_black = 0
    total_rare = 0

    for nft in data:
        percent = round(nft.get("model_percent", 0), 2)
        counts.setdefault(percent, []).append(nft)
        if nft.get("black_bg"):
            total_black += 1
        if nft.get("black_bg") and percent <= 1:
            total_rare += 1

    msg = f"📊 <b>{collection}</b> — <b>{len(data)} NFT</b>\n"
    msg += "────────────────────\n"

    for model_percent, nft_list in sorted(counts.items()):
        is_black_block = any(n["black_bg"] for n in nft_list)
        block_emoji = "💎" if model_percent <= 1 else "🖤" if is_black_block else "✨"
        msg += f"— {model_percent}% модель: {block_emoji}\n"

        line = ""
        for i, nft in enumerate(nft_list, 1):
            line += f"{'⚫' if nft.get('black_bg') else ''}<a href='{nft['link']}'>#{nft['nft_id']}</a> "
            if i % 5 == 0:
                msg += line.strip() + "\n"
                line = ""
        if line:
            msg += line.strip() + "\n"

        msg += "────────────────────\n"

    rare_nfts = sorted(
        [n for n in data if n.get("black_bg") or (n.get("model_percent", 100) <= 1)],
        key=lambda x: x.get("model_percent", 100),
    )[:5]

    if rare_nfts:
        msg += "🔥 <b>ТОП редких NFT:</b>\n"
        for place, nft in enumerate(rare_nfts, 1):
            emoji = "💎" if nft.get("model_percent", 100) <= 1 else "🖤" if nft.get("black_bg") else "✨"
            msg += (
                f"{place} Место: {emoji} {'⚫' if nft.get('black_bg') else ''}"
                f"<a href='{nft['link']}'>#{nft['nft_id']}</a> — {nft.get('model_percent', 0)}%\n"
            )
        msg += "────────────────────\n"

    msg += f"🖤 Черных NFT: <b>{total_black}</b>\n"
    msg += f"🔥 Редких NFT: <b>{total_rare}</b>\n"
    msg += "────────────────────\n"
    msg += "💎 @Gift_NFT_Scaner | <a href='https://t.me/applepromax13'>Реклама?</a> | © <a href='https://t.me/+YVkj07r2_XlhOTcy'>Crypto Farm</a>"

    try:
        await bot.send_message(SETTINGS["chat_id"], msg, parse_mode="HTML", disable_web_page_preview=True)
        logging.info("Stats sent for %s", collection)
    except Exception as e:
        logging.error("Error sending stats for %s: %s", collection, e)

    STATS[collection] = []
    save_stats()


# ========= WATCH LOOP =========
async def watch(session):
    current_ids = {c: COLLECTIONS[c]["start"] for c in COLLECTIONS}

    while True:
        tasks = []
        for c in COLLECTIONS:
            if COLLECTIONS[c]["enabled"]:
                tasks.append(check_collection(session, c, current_ids))
                tasks.append(send_stats_if_ready(c, current_ids))
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0.5)


# ========= ADMIN STATS =========
@dp.message(Command("stats"))
async def stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        build_live_stats_message(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=dashboard_keyboard(),
    )


def dashboard_keyboard():
    style = SETTINGS.get("dashboard_style", "v2")
    next_style = "v1" if style == "v2" else "v2"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="dash_refresh"),
                InlineKeyboardButton(text=f"🧩 Стиль: {style} → {next_style}", callback_data="dash_toggle_style"),
            ]
        ]
    )


def build_live_stats_message(style: str | None = None):
    style = style or SETTINGS.get("dashboard_style", "v2")
    if style == "v1":
        lines = ["📊 <b>ЖИВАЯ статистика</b>", "────────────────────"]
    else:
        lines = ["📊 <b>LIVE DASHBOARD v2</b>", ""]
    for c in COLLECTIONS:
        data = STATS.get(c, [])
        rare_count = COLLECTIONS[c].get("rare_count", 0)
        max_percent = COLLECTIONS[c].get("max_percent", 2.0)
        enabled = "✅" if COLLECTIONS[c].get("enabled") else "❌"
        if not data:
            if style == "v1":
                lines.append(
                    f"{enabled} <b>{c}</b>\n"
                    f"└ данных: 0 | фильтр ≤ {max_percent}% | rare(total): {rare_count}"
                )
            else:
                lines.extend(
                    [
                        f"{enabled} <b>{c}</b>",
                        f"• 📦 Всего: <b>0</b>",
                        f"• ⚙️ Фильтр: <b>≤ {max_percent}%</b>",
                        f"• 🔥 Rare total: <b>{rare_count}</b>",
                        "",
                    ]
                )
            continue
        total_black = sum(1 for nft in data if nft["black_bg"])
        total_rare = sum(1 for nft in data if nft["black_bg"] and nft["model_percent"] <= 1)
        if style == "v1":
            lines.append(
                f"{enabled} <b>{c}</b>\n"
                f"└ всего: {len(data)} | black: {total_black} | rare(буфер): {total_rare}\n"
                f"└ фильтр: ≤ {max_percent}% | rare(total): {rare_count}"
            )
        else:
            lines.extend(
                [
                    f"{enabled} <b>{c}</b>",
                    f"• 📦 Всего: <b>{len(data)}</b>",
                    f"• ⚫ Black: <b>{total_black}</b>",
                    f"• 🔥 Rare (буфер): <b>{total_rare}</b>",
                    f"• ⚙️ Фильтр: <b>≤ {max_percent}%</b>",
                    f"• 💎 Rare total: <b>{rare_count}</b>",
                    "",
                ]
            )

    if style == "v1":
        lines.append("────────────────────")
    else:
        lines.append("────────────")
    lines.append(
        f"⚙️ Mode: <b>{SETTINGS.get('signal_mode', 'balanced')}</b>  |  "
        f"📡 AutoStats: <b>{'ON' if SETTINGS.get('live_stats_enabled') else 'OFF'}</b>"
    )
    return "\n".join(lines)


@dp.message(Command("autostats"))
async def autostats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1] not in ("on", "off"):
        await message.answer("Использование: /autostats on|off [interval_sec]")
        return

    SETTINGS["live_stats_enabled"] = parts[1] == "on"
    if len(parts) > 2:
        try:
            SETTINGS["live_stats_interval"] = max(30, int(parts[2]))
        except ValueError:
            await message.answer("interval_sec должен быть целым числом.")
            return
    save_settings()
    await message.answer(
        f"✅ AutoStats: {'ON' if SETTINGS['live_stats_enabled'] else 'OFF'} | "
        f"interval={SETTINGS['live_stats_interval']}s"
    )


@dp.message(Command("dashboard"))
async def dashboard_control(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1] not in ("start", "stop"):
        await message.answer("Использование: /dashboard start|stop [chat_id]")
        return
    action = parts[1]
    if action == "stop":
        SETTINGS["dashboard_enabled"] = False
        save_settings()
        await message.answer("🛑 Dashboard выключен.")
        return

    chat_id = parts[2] if len(parts) > 2 else SETTINGS.get("dashboard_chat_id", CHAT_ID)
    SETTINGS["dashboard_chat_id"] = chat_id
    text = build_live_stats_message()
    sent = await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=dashboard_keyboard(),
    )
    SETTINGS["dashboard_message_id"] = sent.message_id
    SETTINGS["dashboard_enabled"] = True
    save_settings()
    try:
        await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
    except Exception as e:
        logging.warning("Cannot pin dashboard message: %s", e)
    await message.answer(f"✅ Dashboard запущен в {chat_id}, message_id={sent.message_id}")


@dp.callback_query(F.data == "dash_refresh")
async def dash_refresh(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    try:
        await call.message.edit_text(
            build_live_stats_message(),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=dashboard_keyboard(),
        )
        await call.answer("Обновлено")
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "dash_toggle_style")
async def dash_toggle_style(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    SETTINGS["dashboard_style"] = "v1" if SETTINGS.get("dashboard_style", "v2") == "v2" else "v2"
    save_settings()
    await call.message.edit_text(
        build_live_stats_message(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=dashboard_keyboard(),
    )
    await call.answer(f"Стиль: {SETTINGS['dashboard_style']}")


async def live_stats_loop():
    global LIVE_STATS_LAST_HASH, DASHBOARD_LAST_HASH
    while True:
        try:
            msg = build_live_stats_message()
            digest = str(hash(msg))
            if SETTINGS.get("live_stats_enabled"):
                if digest != LIVE_STATS_LAST_HASH:
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, msg, parse_mode="HTML", disable_web_page_preview=True)
                        except Exception as e:
                            logging.warning("AutoStats send failed to %s: %s", admin_id, e)
                    LIVE_STATS_LAST_HASH = digest
            if SETTINGS.get("dashboard_enabled"):
                chat_id = SETTINGS.get("dashboard_chat_id", CHAT_ID)
                message_id = SETTINGS.get("dashboard_message_id")
                if message_id and digest != DASHBOARD_LAST_HASH:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=dashboard_keyboard(),
                        )
                        DASHBOARD_LAST_HASH = digest
                    except Exception as e:
                        # если сообщение удалили/не найдено — попробуем создать заново
                        logging.warning("Dashboard edit failed: %s", e)
                        sent = await bot.send_message(
                            chat_id,
                            msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=dashboard_keyboard(),
                        )
                        SETTINGS["dashboard_message_id"] = sent.message_id
                        save_settings()
                        DASHBOARD_LAST_HASH = digest
        except Exception as e:
            logging.error("live_stats_loop error: %s", e)
        await asyncio.sleep(max(30, int(SETTINGS.get("live_stats_interval", 120))))


# ========= MAIN =========
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Укажите его в .env или переменной окружения.")
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            watch(session),
            live_stats_loop(),
            dp.start_polling(bot),
        )


if __name__ == "__main__":
    asyncio.run(main())
