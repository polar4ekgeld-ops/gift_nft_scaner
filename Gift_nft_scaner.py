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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaPhoto
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
HUNT_PAUSED_USERS = set()  # Набор ID пользователей с остановленным поиском
TUTORIAL_STATE = {}  # Отслеживание прогресса туториала: {user_id: step_number}

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
        "ViceCream": {"start": 0, "enabled": True, "rare_count": 0, "max_percent": 3.0},
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


# ========= COLLECTION ATTRIBUTES CONFIG =========
COLLECTION_MODELS = {
    "ChillFlame": [
        "any",
        "Eye of Sauron — 0.1%",
        "Satis-fire — 0.1%",
        "Baba Yaga — 0.2%",
        "Dark Souls — 0.2%",
        "Skyrim — 0.2%",
        "Firebrand — 0.5%",
        "Flamethrower — 0.5%",
        "Ghost Rider — 0.5%",
        "Molotov — 0.5%",
        "Revolt — 0.5%",
        "Sochi — 0.5%",
        "Terraria — 0.5%",
        "The Match — 0.5%",
        "Camelot — 1%",
        "Desk Lamp — 1%",
        "Medieval — 1%",
        "Pixel Art — 1%",
        "Signal Flare — 1%",
        "Triumph — 1%",
        "Victory — 1%",
        "Dark Relic — 1.5%",
        "Elder Wand — 1.5%",
        "Sea Horse — 1.5%",
        "Bubble Prism — 2%",
        "Carousel — 2%",
        "Flashlight — 2%",
        "Ghost Light — 2.7%",
        "Balinese — 2.7%",
        "Bear Market — 3%",
        "Birthday Cake — 3%",
        "Bowl of Hygieia — 3%",
        "Bull Run — 3%",
        "Candelabra — 3%",
        "Druid Flame — 3%",
        "Dungeon — 3%",
        "Eternal Flame — 3%",
        "Ionic Column — 3%",
        "Iron Rose — 3%",
        "Lava Lamp — 3%",
        "Lego — 3%",
        "Los Angeles — 3%",
        "Oil Lamp — 3%",
        "Olympia — 3%",
        "Oracle — 3%",
        "Paper Lantern — 3%",
        "Prometheus — 3%",
        "Royal Goblet — 3%",
        "Spaceship — 3%",
        "Spark Plug — 3%",
        "Spring Grove — 3%",
        "Tiki Torch — 3%",
        "Tribal Totem — 3%"
    ],
    "MoodPack": [
        "any",
        "Battle Royale — 0.5%",
        "Jetpack — 0.5%",
        "Net Worth — 0.5%",
        "Pepe Unleashed — 0.5%",
        "Proton Pack — 0.5%",
        "Golden Dragon — 1%",
        "Jolly Roger — 1%",
        "Lady Arcana — 1%",
        "Laika Dog — 1%",
        "Moon Power — 1%",
        "Nezuko — 1%",
        "Travel Duck — 1%",
        "Void Beast — 1%",
        "Crystal Scarab — 1.5%",
        "Infernal Goat — 1.5%",
        "Rare Drop — 1.5%",
        "Ruby Heart — 1.5%",
        "VIP Pass — 1.5%",
        "Bank Vault — 2%",
        "6.6Emo Phase — 2%",
        "Fluffy Monster — 2%",
        "Plush Shark — 2%",
        "Road Rebel — 2%",
        "Turtle Shell — 2%",
        "Aquarium — 2.5%",
        "Davy Jones — 2.5%",
        "Fallout — 2.5%",
        "Fashionista — 2.5%",
        "Fast Courier — 2.5%",
        "Fire Show — 2.5%",
        "Gingerbread — 2.5%",
        "Grand Slam — 2.5%",
        "Master Angler — 2.5%",
        "Movie Night — 2.5%",
        "Paladin — 2.5%",
        "Retro Wave — 2.5%",
        "Rock and Roll — 2.5%",
        "Rodeo King — 2.5%",
        "Striker — 2.5%",
        "Wanderer — 2.5%",
        "Angel Wings — 3%",
        "Bloom Pack — 3%",
        "Burger Bag — 3%",
        "Cat Pack — 3%",
        "Croc Sack — 3%",
        "Goth Gir — 3%",
        "Love Letters — 3%",
        "Space Cat — 3%",
        "Star Pupil — 3%",
        "Street Art — 3%"
    ],
    "ViceCream": [
        "any",
        "Dark Lord — 0.5%",
        "Gold Leaf — 0.5%",
        "Red Dragon — 0.5%",
        "Tralashark — 0.5%",
        "Frappuccino — 1%",
        "Punk Rock — 1%",
        "Bumblebee — 1.5%",
        "Crystal — 1.5%",
        "Love Glazed — 1.5%",
        "Mega Scoop — 1.5%",
        "Raspberry — 1.5%",
        "Rock Solid — 1.5%",
        "Sub-Zero — 1.5%",
        "Sundae Drive — 1.5%",
        "Viceroy — 1.5%",
        "Bite Me — 2%",
        "Cavendish — 2%",
        "Champion — 2%",
        "Cup — 2%",
        "Chilly Bones — 2%",
        "Choco Cone — 2%",
        "Gelato Rose — 2%",
        "Gummy Bear — 2%",
        "Porcelain — 2%",
        "Queen's Gambit — 2%",
        "Scoopzilla — 2%",
        "Star Buddy — 2%",
        "Stay Chill — 2%",
        "Taiyaki — 2%",
        "Ube Cream — 2%",
        "Vanilla — 2%",
        "Vanilla Brick — 2%",
        "Vintage Bunny — 2%",
        "Wafflesaurus — 2%",
        "Cold Paws — 2.5%",
        "Dark Sparkle — 2.5%",
        "Iceman — 2.5%",
        "Mermaid — 2.5%",
        "Sushi — 2.5%",
        "Bamboo Ice — 3%",
        "Berry Shake — 3%",
        "Birthday — 3%",
        "Cherry On Top — 3%",
        "Circus — 3%",
        "Classic — 3%",
        "Disco Funk — 3%",
        "Dreamland — 3%",
        "Pine Cone — 3%",
        "Pumpkin Spice — 3%",
        "Unicone — 3%"
    ],
    "PoolFloat": [
        "any",
        "Baywatch — 0.5%",
        "Luxury Yacht — 0.5%",
        "Nessie — 0.5%",
        "Going Merry — 1%",
        "Gzhel — 1%",
        "Khokhloma — 1%",
        "Kitsune — 1%",
        "Pool King — 1%",
        "Pool Pepe — 1%",
        "Stretching — 1%",
        "Anubis — 1.5%",
        "Cash Flow — 1.5%",
        "Crypto Whale — 1.5%",
        "Homer — 1.5%",
        "Hong Long — 1.5%",
        "Los Muertos — 1.5%",
        "Peach Shake — 1.5%",
        "Safari — 1.5%",
        "Skibidi — 1.5%",
        "Slick Track — 1.5%",
        "Water — 1.5%",
        "Water Tank — 1.5%",
        "Bald Eagle — 2%",
        "Disco — 2%",
        "Leonardo — 2%",
        "Motorboat — 2%",
        "Pigeon — 2%",
        "Pool Party — 2%",
        "Stonks — 2%",
        "Duck Boss — 2.5%",
        "Giant Panda — 2.5%",
        "Mojito — 2.5%",
        "Pelican Decoy — 2.5%",
        "Rescue Mission — 2.5%",
        "Royal Peacock — 2.5%",
        "Toucan — 2.5%",
        "Air Bunny — 3%",
        "Alpaca — 3%",
        "Balloon Dog — 3%",
        "Dark Swan — 3%",
        "Giraffe — 3%",
        "Golden Cobra — 3%",
        "Lizard — 3%",
        "Lucky Dragon — 3%",
        "Palm Beach — 3%",
        "Private Jet — 3%",
        "Quacky — 3%",
        "Sebastian — 3%",
        "Show Seal — 3%",
        "Unicorn — 3%"
    ],
    "StellarRocket": [
        "any",
        "Bitcoin — 0.5%",
        "Mission Uranus — 0.5%",
        "To The Moon — 0.5%",
        "Black Wing — 1%",
        "Gunship — 1%",
        "Jewels — 1%",
        "Mega Death — 1%",
        "Normandy — 1%",
        "Nostromo — 1%",
        "Space Bot — 1%",
        "Space Veggie — 1%",
        "Telegram — 1%",
        "Chrome — 1.5%",
        "Knowledge — 1.5%",
        "Pepelatz — 1.5%",
        "Planet Express — 1.5%",
        "Police Box — 1.5%",
        "Submarine — 1.5%",
        "Alien Pizza — 2%",
        "Baby Carrot — 2%",
        "Clever Bird — 2%",
        "Doomsday — 2%",
        "Fishing Cat — 2%",
        "Laika — 2%",
        "Little Journey — 2%",
        "Pencil — 2%",
        "Squirrel — 2%",
        "Worm Gun — 2%",
        "Banana — 2.5%",
        "Cardboard — 2.5%",
        "Checkered — 2.5%",
        "First Step — 2.5%",
        "Flower Power — 2.5%",
        "Jet Bike — 2.5%",
        "Lava Lamp — 2.5%",
        "Malfunction — 2.5%",
        "Rocket Plush — 2.5%",
        "Silver Ride — 2.5%",
        "Soap Bubbles — 2.5%",
        "Astro Peach — 3%",
        "Fireworks — 3%",
        "Green Jelly — 3%",
        "Hornet — 3%",
        "Lollipop — 3%",
        "Neon Fuel — 3%",
        "Popsicle — 3%",
        "Ruby Sparkle — 3%",
        "Sky Ghost — 3%",
        "Unicorn — 3%",
        "Vintage Toy — 3%"
    ]
}

COLLECTION_BACKGROUNDS = {
    "ChillFlame": [
        "any", "Amber", "Aquamarine", "Azure Blue", "Battleship Grey", "Burgundy",
        "Burnt Sienna", "Camo Green", "Cappuccino", "Caramel", "Carmine", "Carrot Juice",
        "Chestnut", "Chocolate", "Cobalt Blue", "Copper", "Coral Red", "Cyberpunk",
        "Dark Green", "Dark Lilac", "Deep Cyan", "Desert Sand", "Electric Indigo",
        "Electric Purple", "Emerald", "English Violet", "Fandango", "Fire Engine",
        "French Blue", "French Violet", "Grape", "Gunship Green", "Hunter Green",
        "Indigo Dye", "Ivory White", "Jade Green", "Khaki Green", "Lavender",
        "Lemongrass", "Light Olive", "Marine Blue", "Mexican Pink", "Midnight Blue",
        "Mint Green", "Moonstone", "Mustard", "Mystic Pearl", "Navy Blue", "Old Gold",
        "Onyx Black", "Pacific Cyan", "Pacific Green", "Persimmon", "Pine Green",
        "Pistachio", "Pure Gold", "Purple", "Ranger Green", "Raspberry", "Rifle Green",
        "Roman Silver", "Sapphire", "Satin Gold", "Seal Brown", "Shamrock Green",
        "Silver Blue", "Sky Blue", "Steel Grey", "Strawberry", "Tactical Pine", "Tomato",
        "Turquoise", "Malachite", "Neon Blue", "Orange", "Platinum", "Feldgrau",
        "Celtic Blue", "Gunmetal", "Rosewood"
    ],
    "MoodPack": [
        "any",
        "Black",
        "Aquamarine",
        "Azure Blue",
        "Burgundy",
        "Burnt Sienna",
        "Camo Green",
        "Cappuccino",
        "Carmine",
        "Carrot Juice",
        "Celtic Blue",
        "Coral Red",
        "Dark Green",
        "Dark Lilac",
        "Deep Cyan",
        "Desert Sand",
        "Electric Indigo",
        "Electric Purple",
        "Emerald",
        "English Violet",
        "Fandango",
        "French Blue",
        "French Violet",
        "Grape",
        "Gunmetal",
        "Gunship Green",
        "Hunter Green",
        "Ivory White",
        "Jade Green",
        "Khaki Green",
        "Lemongrass",
        "Light Olive",
        "Malachite",
        "Mexican Pink",
        "Midnight Blue",
        "Mustard",
        "Mystic Pearl",
        "Navy Blue",
        "Neon Blue",
        "Old Gold",
        "Orange",
        "Pacific Cyan",
        "Persimmon",
        "Pistachio",
        "Pure Gold",
        "Purple",
        "Ranger Green",
        "Roman Silver",
        "Rosewood",
        "Sapphire",
        "Satin Gold",
        "Seal Brown",
        "Shamrock Green",
        "Silver Blue",
        "Steel Grey",
        "Tactical Pine",
        "Tomato",
        "Amber — 1%",
        "Cyberpunk — 1%",
        "Indigo Dye — 1%",
        "Platinum — 1%",
        "Chestnut — 1.2%",
        "Chocolate — 1.2%",
        "Fire Engine — 1.2%",
        "Moonstone — 1.2%",
        "Pacific Green — 1.2%",
        "Rifle Green — 1.2%",
        "Sky Blue — 1.2%",
        "Strawberry — 1.2%",
        "Turquoise — 1.2%",
        "Battleship Grey — 1.5%",
        "Caramel — 1.5%",
        "Cobalt Blue — 1.5%",
        "Copper — 1.5%",
        "Feldgrau — 1.5%",
        "Lavender — 1.5%",
        "Marine Blue — 1.5%",
        "Mint Green — 1.5%",
        "Onyx Black — 1.5%",
        "Pine Green — 1.5%",
        "Raspberry — 1.5%"
    ],
    "ViceCream": [
        "any",
        "Black",
        "Amber",
        "Aquamarine",
        "Azure Blue",
        "Battleship Grey",
        "Burgundy",
        "Camo Green",
        "Cappuccino",
        "Caramel",
        "Carmine",
        "Carrot Juice",
        "Celtic Blue",
        "Chestnut",
        "Chocolate",
        "Cobalt Blue",
        "Copper",
        "Coral Red",
        "Cyberpunk",
        "Dark Green",
        "Dark Lilac",
        "Deep Cyan",
        "Desert Sand",
        "Electric Indigo",
        "Electric Purple",
        "Emerald",
        "English Violet",
        "Fandango",
        "Feldgrau",
        "Fire Engine",
        "French Blue",
        "French Violet",
        "Grape",
        "Gunmetal",
        "Gunship Green",
        "Hunter Green",
        "Indigo Dye",
        "Ivory White",
        "Jade Green",
        "Khaki Green",
        "Lavender",
        "Lemongrass",
        "Light Olive",
        "Malachite",
        "Marine Blue",
        "Mexican Pink",
        "Midnight Blue",
        "Mint Green",
        "Moonstone",
        "Mustard",
        "Mystic Pearl",
        "Navy Blue",
        "Neon Blue",
        "Old Gold",
        "Onyx Black",
        "Orange",
        "Pacific Cyan",
        "Pacific Green",
        "Persimmon",
        "Pine Green",
        "Pistachio",
        "Pure Gold",
        "Purple",
        "Ranger Green",
        "Raspberry",
        "Rifle Green",
        "Roman Silver",
        "Rosewood",
        "Satin Gold",
        "Shamrock Green",
        "Silver Blue",
        "Sky Blue",
        "Steel Grey",
        "Strawberry",
        "Tactical Pine",
        "Tomato",
        "Turquoise",
        "Burnt Sienna",
        "Sapphire — 1.2%",
        "Seal Brown — 1.2%",
        "Platinum — 1.5%"
    ],
    "PoolFloat": [
        "any",
        "Black",
        "Aquamarine",
        "Azure Blue",
        "Burgundy",
        "Burnt Sienna",
        "Camo Green",
        "Cappuccino",
        "Caramel",
        "Carrot Juice",
        "Chestnut",
        "Chocolate",
        "Cobalt Blue",
        "Copper",
        "Dark Green",
        "Dark Lilac",
        "Deep Cyan",
        "Desert Sand",
        "Electric Indigo",
        "Electric Purple",
        "Emerald",
        "English Violet",
        "Feldgrau",
        "Fire Engine",
        "French Blue",
        "French Violet",
        "Grape",
        "Gunmetal",
        "Hunter Green",
        "Ivory White",
        "Jade Green",
        "Khaki Green",
        "Lavender",
        "Light Olive",
        "Malachite",
        "Mexican Pink",
        "Mint Green",
        "Moonstone",
        "Mustard",
        "Mystic Pearl",
        "Navy Blue",
        "Neon Blue",
        "Orange",
        "Pacific Cyan",
        "Pacific Green",
        "Persimmon",
        "Pistachio",
        "Pure Gold",
        "Ranger Green",
        "Raspberry",
        "Rifle Green",
        "Roman Silver",
        "Sapphire",
        "Satin Gold",
        "Seal Brown",
        "Shamrock Green",
        "Sky Blue",
        "Steel Grey",
        "Strawberry",
        "Tactical Pine",
        "Turquoise",
        "Carmine — 1%",
        "Celtic Blue — 1%",
        "Lemongrass — 1%",
        "Midnight Blue — 1%",
        "Purple — 1%",
        "Amber — 1.2%",
        "Battleship Grey — 1.2%",
        "Coral Red — 1.2%",
        "Cyberpunk — 1.2%",
        "Gunship Green — 1.2%",
        "Pine Green — 1.2%",
        "Rosewood — 1.2%",
        "Tomato — 1.2%",
        "Fandango — 1.5%",
        "Indigo Dye — 1.5%",
        "Marine Blue — 1.5%",
        "Old Gold — 1.5%",
        "Onyx Black — 1.5%",
        "Platinum — 1.5%",
        "Silver Blue — 1.5%"
    ],
    "StellarRocket": [
        "any",
        "Black — 1.2%",
        "Aquamarine — 1%",
        "Caramel — 1%",
        "Carmine — 1%",
        "Copper — 1%",
        "Coral Red — 1%",
        "Cyberpunk — 1%",
        "Desert Sand — 1%",
        "Emerald — 1%",
        "English Violet — 1%",
        "Ivory White — 1%",
        "Lavender — 1%",
        "Midnight Blue — 1%",
        "Neon Blue — 1%",
        "Old Gold — 1%",
        "Pacific Green — 1%",
        "Pine Green — 1%",
        "Ranger Green — 1%",
        "Raspberry — 1%",
        "Rifle Green — 1%",
        "Roman Silver — 1%",
        "Sky Blue — 1%",
        "Tomato — 1%",
        "Amber — 1.2%",
        "Burgundy — 1.2%",
        "Celtic Blue — 1.2%",
        "Chocolate — 1.2%",
        "Cobalt Blue — 1.2%",
        "Dark Green — 1.2%",
        "Dark Lilac — 1.2%",
        "Deep Cyan — 1.2%",
        "Electric Purple — 1.2%",
        "Fandango — 1.2%",
        "Fire Engine — 1.2%",
        "French Blue — 1.2%",
        "French Violet — 1.2%",
        "Gunmetal — 1.2%",
        "Jade Green — 1.2%",
        "Khaki Green — 1.2%",
        "Light Olive — 1.2%",
        "Mexican Pink — 1.2%",
        "Mint Green — 1.2%",
        "Moonstone — 1.2%",
        "Mustard — 1.2%",
        "Mystic Pearl — 1.2%",
        "Onyx Black — 1.2%",
        "Persimmon — 1.2%",
        "Platinum — 1.2%",
        "Sapphire — 1.2%",
        "Satin Gold — 1.2%",
        "Seal Brown — 1.2%",
        "Turquoise — 1.2%",
        "Azure Blue — 1.5%",
        "Battleship Grey — 1.5%",
        "Burnt Sienna — 1.5%",
        "Camo Green — 1.5%",
        "Cappuccino — 1.5%",
        "Carrot Juice — 1.5%",
        "Chestnut — 1.5%",
        "Electric Indigo — 1.5%",
        "Feldgrau — 1.5%",
        "Grape — 1.5%",
        "Gunship Green — 1.5%",
        "Hunter Green — 1.5%",
        "Indigo Dye — 1.5%",
        "Lemongrass — 1.5%",
        "Malachite — 1.5%",
        "Marine Blue — 1.5%",
        "Navy Blue — 1.5%",
        "Orange — 1.5%",
        "Pacific Cyan — 1.5%",
        "Pistachio — 1.5%",
        "Pure Gold — 1.5%",
        "Purple — 1.5%",
        "Rosewood — 1.5%",
        "Shamrock Green — 1.5%",
        "Silver Blue — 1.5%",
        "Steel Grey — 1.5%",
        "Strawberry — 1.5%",
        "Tactical Pine — 1.5%"
    ]
}

# Дефолтные опции для остальных коллекций
DEFAULT_MODELS = ["any", "Hong Long", "Alpaca", "Iron Rose", "Red Molotov", "ChillFlame"]
DEFAULT_BACKGROUNDS = ["any", "Black", "French Violet", "Dark Green", "Deep Black", "Silver"]
DEFAULT_SYMBOLS = ["any", "Straw Hat", "Venetian Mask", "Narcissus", "Golden Star", "Ruby Ring"]


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


def hunt_control_keyboard():
    """Клавиатура для контроля поиска (пауза/продолжение)"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏸ Остановить поиск", callback_data="hunt_pause"),
                InlineKeyboardButton(text="▶️ Продолжить поиск", callback_data="hunt_resume")
            ]
        ]
    )


def user_menu_keyboard(is_admin_user: bool):
    buttons = [
        [InlineKeyboardButton(text="⚡ Быстрый старт", callback_data="um_quickstart")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="um_profile"),
            InlineKeyboardButton(text="🏅 Лидерборд", callback_data="um_leaderboard"),
        ],
        [
            InlineKeyboardButton(text="🏆 Мои достижения", callback_data="um_achievements"),
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


def hunt_options_keyboard(prefix: str, options: list[str], page: int = 0):
    """Клавиатура для выбора опций hunt с поддержкой пагинации (по 10 опций на страницу)"""
    items_per_page = 10
    total_pages = (len(options) + items_per_page - 1) // items_per_page
    
    # Безопасность: проверяем границы страницы
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_options = options[start_idx:end_idx]
    
    rows = [[InlineKeyboardButton(text=f"{opt}", callback_data=f"um_{prefix}_{start_idx + idx}")] 
            for idx, opt in enumerate(page_options)]
    
    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"um_{prefix}_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text=f"Страница {page + 1}/{total_pages}", callback_data=f"um_{prefix}_page_{page}"))
        nav_buttons.append(InlineKeyboardButton(text="Далее ➡️", callback_data=f"um_{prefix}_page_{page + 1}"))
    elif page > 0:
        nav_buttons.append(InlineKeyboardButton(text=f"Страница {page + 1}/{total_pages}", callback_data=f"um_{prefix}_page_{page}"))
    
    if nav_buttons:
        rows.append(nav_buttons)
    
    rows.append([InlineKeyboardButton(text="⬅️ В меню hunt", callback_data="um_huntmenu")])
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
    
    # Проверка новый ли пользователь
    subs_count = len(SUBSCRIPTIONS.get(uid, []))
    is_new_user = subs_count == 0
    
    bot_link = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_link}?start=ref_{uid}"
    
    if is_new_user:
        # Специальное приветствие для новых пользователей с рекомендацией туториала
        welcome = (
            "🚀 <b>Добро пожаловать в Gift_NFT_Scaner!</b>\n"
            "────────────────────\n\n"
            "🎓 <b>Это ваш первый визит!</b>\n\n"
            "Мы подготовили для вас полный туториал, который за 5 минут научит:\n"
            "✅ Создавать подписки на NFT\n"
            "✅ Использовать PRO hunts\n"
            "✅ Участвовать в челленджах\n"
            "✅ Соревноваться в лидерборде\n"
            "✅ Приглашать друзей и получать награды\n\n"
            f"<b>Ваша реферальная ссылка:</b> {ref_link}\n"
            "<i>(Приглашайте друзей и получайте бесплатный PRO!)</i>\n\n"
            "<b>🎯 Выберите:</b>"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📖 Начать туториал (5 мин)", callback_data="tut_step_1")],
                [InlineKeyboardButton(text="⚡ Перейти к боту", callback_data="um_home")],
            ]
        )
    else:
        # Обычное приветствие для вернувшихся пользователей
        welcome = (
            "🚀 <b>С возвращением в @Gift_NFT_Scaner!</b>\n\n"
            f"<b>Твоя реферальная ссылка:</b> {ref_link}\n\n"
            "<b>Быстрые команды:</b>\n"
            "<code>/mysubs</code> — подписки\n"
            "<code>/myhunts</code> — PRO hunts\n"
            "<code>/profile</code> — профиль\n"
            "<code>/challenge</code> — челлендж дня\n"
            "<code>/leaderboard</code> — лидерборд\n"
        )
        keyboard = user_menu_keyboard(is_admin(message.from_user.id))
    
    await message.answer(
        welcome,
        parse_mode="HTML",
        disable_web_page_preview=False,
        reply_markup=keyboard,
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
        model_options = COLLECTION_MODELS.get(collection, DEFAULT_MODELS)
        state["model_options"] = model_options
        await call.message.edit_text(
            f"Шаг 2/4: подарок <b>{html.escape(collection)}</b>\nВыберите модель:",
            parse_mode="HTML",
            reply_markup=hunt_options_keyboard("hwm", model_options),
        )
        await call.answer()
        return
    # ======== PAGINATION FOR HUNT OPTIONS ========
    if action.startswith("hwm_page_"):
        page = int(action.replace("hwm_page_", "", 1))
        state = WIZARD_STATE.get(user_id, {})
        model_options = state.get("model_options", DEFAULT_MODELS)
        await call.message.edit_text(
            f"Шаг 2/4: подарок <b>{html.escape(state.get('collection', 'unknown'))}</b>\nВыберите модель:",
            parse_mode="HTML",
            reply_markup=hunt_options_keyboard("hwm", model_options, page),
        )
        await call.answer()
        return
    if action.startswith("hwb_page_"):
        page = int(action.replace("hwb_page_", "", 1))
        state = WIZARD_STATE.get(user_id, {})
        bg_options = state.get("bg_options", DEFAULT_BACKGROUNDS)
        await call.message.edit_text(
            "Шаг 3/4: выберите фон:",
            reply_markup=hunt_options_keyboard("hwb", bg_options, page),
        )
        await call.answer()
        return
    if action.startswith("hws_page_"):
        page = int(action.replace("hws_page_", "", 1))
        state = WIZARD_STATE.get(user_id, {})
        symbol_options = state.get("symbol_options", DEFAULT_SYMBOLS)
        await call.message.edit_text(
            "Шаг 4/4: выберите символ:",
            reply_markup=hunt_options_keyboard("hws", symbol_options, page),
        )
        await call.answer()
        return
    # ======== END PAGINATION ========
    if action.startswith("hwm_"):
        state = WIZARD_STATE.get(user_id, {})
        options = state.get("model_options", DEFAULT_MODELS)
        try:
            idx = int(action.replace("hwm_", "", 1))
            state["model"] = normalize_hunt_value(options[idx])
        except Exception:
            await call.answer("Некорректный выбор модели", show_alert=True)
            return
        collection = state.get("collection", "")
        bg_options = COLLECTION_BACKGROUNDS.get(collection, DEFAULT_BACKGROUNDS)
        state["bg_options"] = bg_options
        await call.message.edit_text(
            "Шаг 3/4: выберите фон:",
            reply_markup=hunt_options_keyboard("hwb", bg_options),
        )
        await call.answer()
        return
    if action.startswith("hwb_"):
        state = WIZARD_STATE.get(user_id, {})
        options = state.get("bg_options", DEFAULT_BACKGROUNDS)
        try:
            idx = int(action.replace("hwb_", "", 1))
            state["bg"] = normalize_hunt_value(options[idx])
        except Exception:
            await call.answer("Некорректный выбор фона", show_alert=True)
            return
        symbol_options = DEFAULT_SYMBOLS
        state["symbol_options"] = symbol_options
        await call.message.edit_text(
            "Шаг 4/4: выберите символ:",
            reply_markup=hunt_options_keyboard("hws", symbol_options),
        )
        await call.answer()
        return
    if action.startswith("hws_"):
        state = WIZARD_STATE.get(user_id, {})
        options = state.get("symbol_options", DEFAULT_SYMBOLS)
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
    elif action == "achievements":
        text = build_achievements_message(user_id)
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="um_profile")]])
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
        if action == "home":
            await bot.delete_message(call.message.chat.id, call.message.message_id)
            await call.message.answer(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=reply_markup)
        else:
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
    text = "👤 <b>Профиль охотника</b>\n────────────────────\n" + build_profile_message(uid, update_activity=False)
    back_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К лидерборду", callback_data="lb_back")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="um_home")],
        ]
    )
    photo_id = await get_user_profile_photo(int(uid))
    try:
        if photo_id and len(text) <= 1024:
            await call.message.reply_photo(photo_id, caption=text, parse_mode="HTML", reply_markup=back_markup)
        else:
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
        await bot.delete_message(call.message.chat.id, call.message.message_id)
        await call.message.answer(build_leaderboard_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
    except TelegramBadRequest as e:
        # If delete fails, try edit
        try:
            await call.message.edit_text(build_leaderboard_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
        except TelegramBadRequest as e2:
            if "message is not modified" not in str(e2).lower():
                raise
    await call.answer()


@dp.callback_query(F.data.startswith("trp_"))
async def toprefs_profile_callback(call: types.CallbackQuery):
    uid = call.data.replace("trp_", "", 1)
    if not uid.isdigit():
        await call.answer("Некорректный ID профиля", show_alert=True)
        return
    text = "👤 <b>Профиль охотника</b>\n────────────────────\n" + build_profile_message(uid, update_activity=False)
    back_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К TOP реферерам", callback_data="tr_back")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="um_home")],
        ]
    )
    photo_id = await get_user_profile_photo(int(uid))
    try:
        if photo_id and len(text) <= 1024:
            await call.message.reply_photo(photo_id, caption=text, parse_mode="HTML", reply_markup=back_markup)
        else:
            await call.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=back_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await call.answer()


@dp.callback_query(F.data == "tr_back")
async def toprefs_back_callback(call: types.CallbackQuery):
    top = get_toprefs_top(10)
    markup = build_toprefs_keyboard(top) if top else user_menu_keyboard(is_admin(call.from_user.id))
    try:
        await bot.delete_message(call.message.chat.id, call.message.message_id)
        await call.message.answer(build_toprefs_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
    except TelegramBadRequest as e:
        # If delete fails, try edit
        try:
            await call.message.edit_text(build_toprefs_message(), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
        except TelegramBadRequest as e2:
            if "message is not modified" not in str(e2).lower():
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
    
    # Calculate positions
    users = GAMIFICATION.get("users", {})
    leaderboard = sorted(users.items(), key=lambda kv: (kv[1].get("rare_alerts_received", 0), kv[1].get("alerts_received", 0)), reverse=True)
    leaderboard_positions = {user_uid: i+1 for i, (user_uid, _) in enumerate(leaderboard)}
    leaderboard_pos = leaderboard_positions.get(uid, "Вне топ 100")
    
    toprefs = sorted(users.items(), key=lambda kv: kv[1].get("weekly_referrals", 0), reverse=True)
    toprefs_positions = {user_uid: i+1 for i, (user_uid, _) in enumerate(toprefs)}
    toprefs_pos = toprefs_positions.get(uid, "Вне топ 100")
    
    rank_score = profile.get("rare_alerts_received", 0)
    if rank_score >= 3000:
        rank = "🏆 Legend"
    elif rank_score >= 1500:
        rank = "🥇 Gold"
    elif rank_score >= 750:
        rank = "🥈 Silver"
    else:
        rank = "🥉 Bronze"
    pro_status = format_pro_status(profile)
    hits = profile.get("hits", [])
    total_nfts = len(hits)
    unique_collections = len(set(h.get("collection") for h in hits)) if hits else 0
    best_percent = min((h.get("model_percent", 100) for h in hits), default=100)
    avg_percent = round(sum(h.get("model_percent", 0) for h in hits) / total_nfts, 2) if total_nfts else 0
    subs_count = len(SUBSCRIPTIONS.get(uid, {}))
    level = profile.get("alerts_received", 0) // 10 + 1

    # achievements
    achievements = []
    if profile.get('streak', 0) >= 7:
        achievements.append("🔥 Огненный стрик!")
    if total_nfts >= 1000:
        achievements.append("🏆 Мастер находок!")
    if unique_collections >= 10:
        achievements.append("🌍 Коллекционер!")
    if profile.get('alerts_received', 0) >= 1000:
        achievements.append("💪 Опытный охотник")
    if profile.get('rare_alerts_received', 0) >= 200:
        achievements.append("✨ Повелитель редких")
    if profile.get('referrals', 0) >= 10:
        achievements.append("🤝 Привлекатор")
    if profile.get('weekly_referrals', 0) >= 5:
        achievements.append("⚡ Реферальная неделя")
    black_hits = sum(1 for h in hits if h.get('black_bg'))
    if black_hits >= 75:
        achievements.append("🖤 Черный охотник")

    achievements_str = " | ".join(achievements) if achievements else "Нет достижений"
    msg = (
        f"🏆 Ранг: <b>{rank}</b>\n"
        f"⭐ Уровень: <b>{level}</b>\n"
        f"📊 Место в лидерборде: <b>{leaderboard_pos}</b>\n"
        f"📈 Место в ТОП рефов: <b>{toprefs_pos}</b>\n"
        f"🔔 Получено алертов: <b>{profile.get('alerts_received', 0)}</b>\n"
        f"🔥 Редких алертов: <b>{profile.get('rare_alerts_received', 0)}</b>\n"
        f"⚡️ Активность (streak): <b>{profile.get('streak', 0)}</b> дн.\n"
        f"📣 Рефералы: <b>{profile.get('referrals', 0)}</b> (за неделю: {profile.get('weekly_referrals', 0)})\n"
        f"💎 PRO Hunters: <b>{pro_status}</b>\n"
        f"🎯 Активных PRO hunts: <b>{len(profile.get('hunts', []))}</b>\n"
        f"🎨 Подписок: <b>{subs_count}</b>\n"
        f"🏅 Найдено NFT: <b>{total_nfts}</b>\n"
        f"🌟 Уникальных коллекций: <b>{unique_collections}</b>\n"
        f"💯 Лучший процент: <b>{best_percent}%</b>\n"
        f"📊 Средний процент: <b>{avg_percent}%</b>\n"
        f"🎖️ <b>Мои Достижения:</b>\n"
        f"───────────────"
        f"<blockquote>{achievements_str}</blockquote>"
    )
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


def build_achievements_message(uid: str):
    profile = get_user_profile(uid)
    hits = profile.get("hits", [])
    total_nfts = len(hits)
    unique_collections = len(set(h.get("collection") for h in hits)) if hits else 0
    black_hits = sum(1 for h in hits if h.get("black_bg"))

    achievements = []
    if profile.get("streak", 0) >= 7:
        achievements.append("🔥 Огненный стрик! (7+ дней активности)")
    else:
        achievements.append("❌ Огненный стрик: пройди 7 дней активности подряд")

    if total_nfts >= 500:
        achievements.append("🏆 Мастер находок! (500+ найденных NFT)")
    else:
        achievements.append("❌ Мастер находок: найди 500+ NFT")

    if unique_collections >= 10:
        achievements.append("🌍 Коллекционер! (10+ уникальных коллекций)")
    else:
        achievements.append("❌ Коллекционер: собери NFT из 10+ коллекций")

    if profile.get("alerts_received", 0) >= 1000:
        achievements.append("💪 Опытный охотник (1000+ алертов)")
    else:
        achievements.append("❌ Опытный охотник: получи 1000+ алертов")

    if profile.get("rare_alerts_received", 0) >= 200:
        achievements.append("✨ Повелитель редких (200+ редких алертов)")
    else:
        achievements.append("❌ Повелитель редких: 200+ редких алертов")

    if profile.get("referrals", 0) >= 10:
        achievements.append("🤝 Привлекатор (10+ рефералов)")
    else:
        achievements.append("❌ Привлекатор: приведи 10+ людей")

    if profile.get("weekly_referrals", 0) >= 5:
        achievements.append("⚡ Реферальная неделя (5+ рефералов за неделю)")
    else:
        achievements.append("❌ Реферальная неделя: 5+ рефералов за неделю")

    if black_hits >= 75:
        achievements.append("🖤 Черный охотник (75+ черных NFT)")
    else:
        achievements.append("❌ Черный охотник: найди 75+ черных NFT")

    text = (
        "🏆 <b>Мои достижения</b>\n"
        "────────────────────\n"
        f"🎯 Уровень: {profile.get('alerts_received', 0)} алертов, {profile.get('rare_alerts_received', 0)} редких\n"
        f"💎 Уникальные коллекции: {unique_collections} / 10\n"
        f"📦 Найдено NFT: {total_nfts} / 500\n"
        f"🖤 Черные NFT: {black_hits} / 75\n"
        "────────────────────\n"
        ".\n".join(achievements)
    )
    return text


async def get_user_profile_photo(user_id: int):
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.photos:
            # Use medium size instead of largest to avoid too big avatar
            sizes = photos.photos[0]
            if len(sizes) > 1:
                return sizes[1].file_id  # medium size
            else:
                return sizes[0].file_id
    except Exception as e:
        logging.warning(f"Failed to get profile photo for {user_id}: {e}")
    return None


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
            f"{i}) <a href=\"tg://user?id={uid}\">{uid}</a> — Rare: {p.get('rare_alerts_received', 0)}, "
            f"Alerts: {p.get('alerts_received', 0)}\n"
        )
    return msg


@dp.message(Command("toprefs"))
async def toprefs_cmd(message: types.Message):
    top = get_toprefs_top(10)
    await message.answer(
        build_toprefs_message(),
        parse_mode="HTML",
        reply_markup=build_toprefs_keyboard(top) if top else None,
    )


def get_toprefs_top(limit: int = 10):
    ensure_weekly_tracking()
    users = GAMIFICATION.get("users", {})
    return sorted(users.items(), key=lambda kv: kv[1].get("weekly_referrals", 0), reverse=True)[:limit]


def build_toprefs_keyboard(top):
    rows = []
    for i, (uid, _) in enumerate(top, 1):
        rows.append([InlineKeyboardButton(text=f"👤 Профиль #{i}", callback_data=f"trp_{uid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="um_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_toprefs_message():
    top = get_toprefs_top(10)
    if not top:
        return "Реферальная таблица пока пустая."
    week_key = GAMIFICATION.get("week_key", current_week_key())
    msg = f"📣 <b>TOP рефереров недели ({week_key})</b>\n"
    for i, (uid, p) in enumerate(top, 1):
        msg += f"{i}) <a href=\"tg://user?id={uid}\">{uid}</a> — week: <b>{p.get('weekly_referrals', 0)}</b> | total: <b>{p.get('referrals', 0)}</b>\n"
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


def tutorial_step_1_keyboard():
    """Шаг 1: Введение в подписки"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Подробнее о подписках", callback_data="tut_step1_learn")],
            [InlineKeyboardButton(text="✨ Создать подписку сейчас", callback_data="um_sub_wizard_start")],
            [InlineKeyboardButton(text="➡️ Следующий шаг", callback_data="tut_step_2")],
            [InlineKeyboardButton(text="⬅️ Пропустить туториал", callback_data="um_home")],
        ]
    )


def tutorial_step_2_keyboard(is_pro: bool):
    """Шаг 2: PRO hunts"""
    buttons = [
        [InlineKeyboardButton(text="📖 Что такое PRO hunt?", callback_data="tut_step2_learn")],
    ]
    if is_pro:
        buttons.append([InlineKeyboardButton(text="🎯 Создать PRO hunt сейчас", callback_data="um_hunt_wizard_start")])
    else:
        buttons.append([InlineKeyboardButton(text="⭐ Как получить PRO?", callback_data="tut_step2_pro")])
    buttons.extend([
        [InlineKeyboardButton(text="➡️ Следующий шаг", callback_data="tut_step_3")],
        [InlineKeyboardButton(text="⬅️ Пропустить туториал", callback_data="um_home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tutorial_step_3_keyboard():
    """Шаг 3: Челленджи"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Как работают челленджи?", callback_data="tut_step3_learn")],
            [InlineKeyboardButton(text="🎮 Открыть челлендж сейчас", callback_data="um_challenge")],
            [InlineKeyboardButton(text="➡️ Следующий шаг", callback_data="tut_step_4")],
            [InlineKeyboardButton(text="⬅️ Пропустить туториал", callback_data="um_home")],
        ]
    )


def tutorial_step_4_keyboard():
    """Шаг 4: Профиль и лидерборд"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="um_profile")],
            [InlineKeyboardButton(text="🏅 Лидерборд", callback_data="um_leaderboard")],
            [InlineKeyboardButton(text="📣 TOP рефов", callback_data="um_toprefs")],
            [InlineKeyboardButton(text="➡️ Завершить туториал", callback_data="tut_step_5")],
            [InlineKeyboardButton(text="⬅️ Пропустить туториал", callback_data="um_home")],
        ]
    )


def tutorial_step_5_keyboard():
    """Шаг 5: Завершение туториала"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Мои подписки", callback_data="um_mysubs")],
            [InlineKeyboardButton(text="🎯 Мои PRO hunts", callback_data="um_myhunts")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="um_profile")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="um_home")],
        ]
    )


def build_tutorial_step_1():
    """Туториал шаг 1: Подписки"""
    return (
        "🎓 <b>ТУТОРИАЛ: Шаг 1 из 5</b>\n"
        "────────────────────\n\n"
        "<b>📌 Что такое ПОДПИСКИ?</b>\n\n"
        "Подписки — это ваш личный фильтр для отслеживания NFT!\n\n"
        "Вы можете создать подписку на:\n"
        "• 🎁 Конкретный подарок (коллекцию) или все сразу\n"
        "• 🧬 Максимальный процент редкости модели (например, ≤1%)\n"
        "• 🖤 Только чёрные фоны (опционально)\n\n"
        "<b>Пример:</b>\n"
        "Подписка: Collection=<code>all</code>, max_percent=<code>1.0%</code>, black_only=<code>Нет</code>\n"
        "↓\n"
        "Вы получите алерт со ВСЕМИ NFT с моделью ≤1% из любой коллекции\n\n"
        "<b>💡 Совет:</b> Начните с простую подписку на все коллекции с max_percent=1-2%"
    )


def build_tutorial_step_1_learn():
    """Детальное объяснение шага 1"""
    return (
        "📖 <b>ПОДРОБНЕЕ О ПОДПИСКАХ</b>\n"
        "────────────────────\n\n"
        "<b>1️⃣ Выбор коллекции:</b>\n"
        "   • <code>all</code> — следить за всеми подарками\n"
        "   • <code>ChillFlame</code> — только за этой коллекцией\n"
        "   • И другие коллекции...\n\n"
        "<b>2️⃣ Выбор max_percent:</b>\n"
        "   • <code>0.5%</code> — 🔥 Супер редкие (очень мало алертов)\n"
        "   • <code>1.0%</code> — 💎 Редкие\n"
        "   • <code>2.0%</code> — ✨ Среднее (много алертов)\n"
        "   • <code>3.0%</code> — 📊 Частые\n\n"
        "<b>3️⃣ Режим чёрного фона:</b>\n"
        "   • <code>Да</code> 🖤 — только NFT с чёрным фоном\n"
        "   • <code>Нет</code> ✨ — все фоны\n\n"
        "<b>📝 Как создать подписку:</b>\n"
        "Используйте кнопку <b>✨ Конструктор подписки</b> в меню\n"
        "или команду: <code>/subscribe all 1.0 0</code>\n\n"
        "💡 <b>Можно создать несколько подписок!</b>\n"
        "Например:\n"
        "  1) all, 1% (очень редкие)\n"
        "  2) ChillFlame, 2% (редкие из ChillFlame)\n"
        "  3) MoodPack, 3%, черный фон только"
    )


def build_tutorial_step_2(is_pro: bool):
    """Туториал шаг 2: PRO hunts"""
    base = (
        "🎓 <b>ТУТОРИАЛ: Шаг 2 из 5</b>\n"
        "────────────────────\n\n"
        "<b>🎯 Что такое PRO HUNTS?</b>\n\n"
        "PRO hunts — это <b>точный поиск по 4 параметрам одновременно</b>:\n"
        "• 🎁 Коллекция\n"
        "• 🧬 Модель (точное имя, например 'Eye of Sauron')\n"
        "• 🎨 Фон (точный цвет, например 'Black')\n"
        "• 🔣 Символ (точный символ)\n\n"
        "<b>Когда PRO hunt срабатывает?</b>\n"
        "Только когда найден NFT, который ТОЧНО совпадает со ВСЕМИ параметрами!\n\n"
        "<b>Пример:</b>\n"
        "PRO hunt: model='Eye of Sauron' + bg='Black' + symbol='Straw Hat'\n"
        "↓\n"
        "Алерт <b>ТОЛЬКО</b> когда все 3 параметра совпадают вместе\n\n"
    )
    
    if is_pro:
        base += (
            "✅ <b>Вы уже имеете активный PRO!</b>\n"
            "Вы можете создать до 20 PRO hunts прямо сейчас."
        )
    else:
        base += (
            "🔒 <b>PRO требуется для этой функции</b>\n\n"
            "<b>Как получить PRO на 30 дней?</b>\n"
            "1) 💰 Купить через Telegram Stars (199 XTR ≈ €1-2)\n"
            "2) 👥 Пригласить 3 друзей через реферальную ссылку → +7 дней\n"
            "3) ⭐ За выполнение ежедневного челленджа → +1-2 дня\n"
            "4) 🎁 Получить от администратора"
        )
    
    return base


def build_tutorial_step_2_pro():
    """Детальное объяснение как получить PRO"""
    return (
        "⭐ <b>КАК ПОЛУЧИТЬ PRO HUNTERS?</b>\n"
        "────────────────────\n\n"
        "<b>Способ 1️⃣ — КУПИТЬ (Самый быстрый)</b>\n"
        "• 199 Telegram Stars (~€1-2)\n"
        "• 30 дней полного доступа\n"
        "• Кнопка <b>⭐ Купить PRO</b> в главном меню\n\n"
        "<b>Способ 2️⃣ — РЕФЕРАЛЫ (Лучший способ)</b>\n"
        "• Пригласите 3 друзей по вашей ссылке\n"
        "• Получите автоматически +7 дней PRO\n"
        "• Каждые следующие 3 друга = ещё +7 дней\n"
        "• Способ: поделитесь реф.ссылкой из <b>Главное меню</b>\n"
        "• Ваша ссылка всегда в <b>Профиле</b> 👤\n\n"
        "<b>Способ 3️⃣ — ЧЕЛЛЕНДЖИ (Ежедневно)</b>\n"
        "• Выполняйте ежедневный челлендж\n"
        "• Награда: +1, +2 дня PRO за выполнение\n"
        "• Челлендж меняется каждый день\n"
        "• Открыть: <b>🎮 Челлендж</b> или команда <code>/challenge</code>\n\n"
        "<b>Способ 4️⃣ — ОТ АДМИНА (Редко)</b>\n"
        "• Администратор может выдать PRO за активность\n"
        "• За помощь боту, отзывы и идеи\n\n"
        "💡 <b>СОВЕТ:</b> Рефералы — самый выгодный способ!\n"
        "Приглашайте друзей и получайте бесконечный PRO"
    )


def build_tutorial_step_3():
    """Туториал шаг 3: Челленджи"""
    ch = current_daily_challenge()
    return (
        "🎓 <b>ТУТОРИАЛ: Шаг 3 из 5</b>\n"
        "────────────────────\n\n"
        "<b>🎮 Что такое ЕЖЕДНЕВНЫЙ ЧЕЛЛЕНДЖ?</b>\n\n"
        "Каждый день на вас ждёт новая миссия с награной в PRO дни!\n\n"
        "<b>Как это работает?</b>\n"
        "1) Вам выдаётся случайный челлендж с условиями\n"
        "2) Вы нажимаете <b>✅ Принять челлендж</b>\n"
        "3) Вы ловите NFT, который подходит условиям\n"
        "4) Приложение автоматически учитывает это\n"
        "5) Когда ловите подходящий NFT → получается награда в PRO\n\n"
        "<b>📋 Пример сегодняшнего челленджа:</b>\n"
        f"<b>Название:</b> {ch.get('title', 'Challenge')}\n"
        f"<b>Описание:</b> <i>{ch.get('tagline', '...')}</i>\n"
        f"<b>Условие:</b> Model ≤ {ch.get('max_percent', 1)}% "
        f"{'+ Black BG' if ch.get('black_only') else ''}\n"
        f"<b>Награда:</b> +{ch.get('reward_days', 1)} дн. PRO\n\n"
        "<b>💡 Совет:</b> Принимайте челлендж каждый день!\n"
        "Это просто — вы ловите NFT как обычно, "
        "просто убедитесь что приняли челлендж"
    )


def build_tutorial_step_4():
    """Туториал шаг 4: Профиль и рейтинг"""
    return (
        "🎓 <b>ТУТОРИАЛ: Шаг 4 из 5</b>\n"
        "────────────────────\n\n"
        "<b>👤 ПРОФИЛЬ И РЕЙТИНГИ</b>\n\n"
        "<b>📊 Что показывает профиль?</b>\n"
        "• 🏆 Ранг (Bronze → Silver → Gold → Legend)\n"
        "• 🔔 Кол-во полученных алертов\n"
        "• 🔥 Кол-во редких находок\n"
        "• ⚡ Streak — дни активности подряд\n"
        "• 📣 Рефераллы — сколько друзей вы пригласили\n"
        "• 💎 PRO статус и до какого числа\n"
        "• 🎯 Активные PRO hunts\n"
        "• 🏆 ТОП 5 найденных редких NFT\n\n"
        "<b>🏅 ЛИДЕРБОРД</b>\n"
        "Глобальный рейтинг охотников на основе:\n"
        "• Количество редких находок (основной критерий)\n"
        "• Всего полученных алертов\n"
        "Поднимайтесь выше и попадите в ТОП! 🎯\n\n"
        "<b>📣 TOP РЕФОВ ЗА НЕДЕЛЮ</b>\n"
        "Рейтинг рефералов за текущую неделю.\n"
        "Приглашайте больше друзей и займите первое место!\n\n"
        "<b>💡 Совет:</b> Ваш ранг зависит от редких находок (🔥)\n"
        "Ловите редкие NFT → растёт ранг → вы в топе!"
    )


def build_tutorial_step_5():
    """Туториал шаг 5: Завершение"""
    return (
        "🎓 <b>ТУТОРИАЛ ЗАВЕРШЕН! 🎉</b>\n"
        "────────────────────\n\n"
        "<b>Вы узнали все основы!</b>\n\n"
        "✅ <b>Подписки</b> — личные фильтры для отслеживания\n"
        "✅ <b>PRO hunts</b> — точный поиск по параметрам\n"
        "✅ <b>Челленджи</b> — получайте PRO день в день\n"
        "✅ <b>Рейтинги</b> — соревнуйтесь с другими охотниками\n\n"
        "<b>🚀 Дальнейшие шаги:</b>\n\n"
        "1) <b>Создайте первую подписку</b>\n"
        "   → Вы начнёте получать алерты\n\n"
        "2) <b>Получите PRO через рефералов</b>\n"
        "   → Пригласите 3 друзей = +7 дней\n\n"
        "3) <b>Примите ежедневный челлендж</b>\n"
        "   → Автоматически получайте PRO дни\n\n"
        "4) <b>Поднимайтесь в лидерборде</b>\n"
        "   → Ловите редкие NFT и займите ТОП место\n\n"
        "<b>📝 Команды для быстрого доступа:</b>\n"
        "<code>/mysubs</code> — управление подписками\n"
        "<code>/myhunts</code> — управление PRO hunts\n"
        "<code>/challenge</code> — текущий челлендж\n"
        "<code>/profile</code> — ваш профиль\n"
        "<code>/leaderboard</code> — лидерборд\n\n"
        "<b>❓ Нужна помощь?</b>\n"
        "Вернитесь к любому шагу туториала через меню "
        "или используйте <b>⚡ Быстрый старт</b> для напоминания."
    )


def quickstart_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Начать туториал", callback_data="tut_step_1")],
            [InlineKeyboardButton(text="✨ Создать подписку", callback_data="um_sub_wizard_start")],
            [InlineKeyboardButton(text="🎮 Открыть челлендж", callback_data="um_challenge")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="um_profile")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="um_home")],
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
        "⚡ <b>Быстрый старт и туториал</b>\n"
        "────────────────────\n\n"
        "<b>📊 Ваш прогресс:</b>\n"
        f"1) Подписки: <b>{subs_count}</b> {'✅' if subs_count else '⬜'}\n"
        f"2) PRO hunts: <b>{hunts_count}</b> {'✅' if hunts_count else '⬜'}\n"
        f"3) PRO статус: <b>{pro_status}</b>\n"
        f"4) Daily streak: <b>{profile.get('streak', 0)}</b> дн.\n\n"
        "<b>🎓 Выберите действие:</b>\n"
        "Нажмите <b>📖 Начать туториал</b> для обучения\n"
        "или прямо создавайте подписку кнопкой ниже"
    )


# ========= TUTORIAL CALLBACKS =========
@dp.callback_query(F.data == "tut_step_1")
async def tutorial_step_1(call: types.CallbackQuery):
    """Туториал шаг 1: Подписки"""
    user_id = str(call.from_user.id)
    TUTORIAL_STATE[user_id] = 1
    await call.message.edit_text(
        build_tutorial_step_1(),
        parse_mode="HTML",
        reply_markup=tutorial_step_1_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step1_learn")
async def tutorial_step1_learn(call: types.CallbackQuery):
    """Подробное объяснение подписок"""
    await call.message.edit_text(
        build_tutorial_step_1_learn(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к шагу 1", callback_data="tut_step_1")]]
        )
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step_2")
async def tutorial_step_2(call: types.CallbackQuery):
    """Туториал шаг 2: PRO hunts"""
    user_id = str(call.from_user.id)
    TUTORIAL_STATE[user_id] = 2
    is_pro = is_pro_active(user_id)
    await call.message.edit_text(
        build_tutorial_step_2(is_pro),
        parse_mode="HTML",
        reply_markup=tutorial_step_2_keyboard(is_pro)
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step2_learn")
async def tutorial_step2_learn(call: types.CallbackQuery):
    """Подробное объяснение PRO hunts"""
    await call.message.edit_text(
        build_tutorial_step_2(is_pro_active(str(call.from_user.id))),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к шагу 2", callback_data="tut_step_2")]]
        )
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step2_pro")
async def tutorial_step2_pro(call: types.CallbackQuery):
    """Объяснение как получить PRO"""
    await call.message.edit_text(
        build_tutorial_step_2_pro(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Купить PRO (Stars)", callback_data="um_buypro_pay")],
                [InlineKeyboardButton(text="⬅️ Назад к шагу 2", callback_data="tut_step_2")],
            ]
        )
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step_3")
async def tutorial_step_3(call: types.CallbackQuery):
    """Туториал шаг 3: Челленджи"""
    user_id = str(call.from_user.id)
    TUTORIAL_STATE[user_id] = 3
    await call.message.edit_text(
        build_tutorial_step_3(),
        parse_mode="HTML",
        reply_markup=tutorial_step_3_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step3_learn")
async def tutorial_step3_learn(call: types.CallbackQuery):
    """Подробное объяснение челленджей"""
    await call.message.edit_text(
        build_tutorial_step_3(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к шагу 3", callback_data="tut_step_3")]]
        )
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step_4")
async def tutorial_step_4(call: types.CallbackQuery):
    """Туториал шаг 4: Профиль и лидерборд"""
    user_id = str(call.from_user.id)
    TUTORIAL_STATE[user_id] = 4
    await call.message.edit_text(
        build_tutorial_step_4(),
        parse_mode="HTML",
        reply_markup=tutorial_step_4_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "tut_step_5")
async def tutorial_step_5(call: types.CallbackQuery):
    """Туториал шаг 5: Завершение"""
    user_id = str(call.from_user.id)
    TUTORIAL_STATE[user_id] = 5
    await call.message.edit_text(
        build_tutorial_step_5(),
        parse_mode="HTML",
        reply_markup=tutorial_step_5_keyboard()
    )
    await call.answer()


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


@dp.callback_query(F.data == "hunt_pause")
async def hunt_pause(call: types.CallbackQuery):
    """Обработчик кнопки паузы PRO hunt"""
    user_id = call.from_user.id
    HUNT_PAUSED_USERS.add(user_id)
    await call.answer("⏸ Поиск остановлен. Вы не будете получать PRO-уведомления.", show_alert=False)


@dp.callback_query(F.data == "hunt_resume")
async def hunt_resume(call: types.CallbackQuery):
    """Обработчик кнопки возобновления PRO hunt"""
    user_id = call.from_user.id
    HUNT_PAUSED_USERS.discard(user_id)
    await call.answer("▶️ Поиск продолжен. Вы снова получаете PRO-уведомления.", show_alert=False)


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
                InlineKeyboardButton(text=f"{'✅ ' if current == 'rare' else ''}Rare", callback_data="owners_sort_rare"),
                InlineKeyboardButton(text=f"{'✅ ' if current == 'total' else ''}Total", callback_data="owners_sort_total"),
            ],
            [
                InlineKeyboardButton(text=f"{'✅ ' if current == 'ratio' else ''}Ratio", callback_data="owners_sort_ratio"),
                InlineKeyboardButton(text=f"{'✅ ' if current == 'black' else ''}Black", callback_data="owners_sort_black"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="dash_refresh")],
        ]
    )


def _global_counts():
    # Статистика берется из OWNER_STATS (нагруженные владельцы), чтобы значения совпадали с рейтингом
    total = sum(info.get("total", 0) for info in OWNER_STATS.values())
    black = sum(info.get("black", 0) for info in OWNER_STATS.values())
    rare = sum(info.get("rare", 0) for info in OWNER_STATS.values())
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
    elif sort_by == "black":
        ranking = sorted(OWNER_STATS.items(), key=lambda kv: kv[1].get("black", 0), reverse=True)[:10]
    else:
        ranking = sorted(OWNER_STATS.items(), key=lambda kv: (kv[1].get("rare", 0), kv[1].get("total", 0)), reverse=True)[:10]

    total, black, rare = _global_counts()

    msg = [
        "👑 <b>OWNERS DASHBOARD v2</b>",
        "────────────────────",
        f"• 📦 Всего NFT: <b>{total}</b>",
        f"• ⚫ Black: <b>{black}</b>",
        f"• 🔥 Rare: <b>{rare}</b>",
        f"• 🔍 Текущий фильтр: <b>{sort_by}</b>",
        "",  # раздел
        "<b>ТОП владельцев</b> (первые 10):",
    ]

    for i, (owner, info) in enumerate(ranking, 1):
        owner_link = info.get("link", "")
        owner_safe = clean_html(owner)
        owner_title = f"<a href='{owner_link}'>{owner_safe}</a>" if owner_link and owner_link.startswith("https://t.me") else owner_safe
        total_owner = info.get("total", 0)
        rare_owner = info.get("rare", 0)
        black_owner = info.get("black", 0)
        ratio = (rare_owner / total_owner) if total_owner else 0
        msg.append(f"{i}) {owner_title}")
        msg.append(f"   🔥 {rare_owner} | 📊 {total_owner} | ⚫ {black_owner} | 📈 {ratio:.2f}")
        msg.append("")

    if msg and msg[-1] == "":
        msg.pop()  # убрать финальный пустой

    msg.append("────────────────────")
    msg.append("📌 Нажмите кнопку фильтра, чтобы переключить сортировку")

    return "\n".join(msg)


@dp.callback_query(F.data.startswith("owners_sort_"))
async def owners_sort_callback(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    sort_by = call.data.replace("owners_sort_", "")
    if sort_by not in ("rare", "total", "ratio", "black"):
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
    for i, nft in enumerate(reversed(latest), 1):
        marker = "⚫" if nft.get("black_bg") else ""
        latest_lines.append(f"{i}) {marker}<a href='{nft['link']}'>#{nft['nft_id']}</a> — {nft.get('model_percent', 0)}%")

    top_rare = sorted(
        [n for n in data if n.get("black_bg") or n.get("model_percent", 100) <= 1],
        key=lambda x: x.get("model_percent", 100),
    )[:5]
    top_lines = []
    for idx, nft in enumerate(top_rare, 1):
        marker = "⚫" if nft.get("black_bg") else ""
        top_lines.append(f"{idx}) {marker}<a href='{nft['link']}'>#{nft['nft_id']}</a> — {nft.get('model_percent', 0)}%")
    if not top_lines:
        top_lines = ["Нет редких NFT"]

    model_values = [n.get("model_percent", 0) for n in data]
    black_cnt = sum(1 for n in data if n.get("black_bg"))
    total = len(data)
    black_ratio = black_cnt / total if total > 0 else 0

    chart = (
        f"📊 Распределение по проценту:\n"
        f"• ≤0.5%: {_bars_single(model_values, 0.5)}\n"
        f"• ≤1%: {_bars_single(model_values, 1)}\n"
        f"• ≤2%: {_bars_single(model_values, 2)}\n"
        f"• ≤3%: {_bars_single(model_values, 3)}\n"
        f"• >3%: {_bars_single(model_values, 3, greater=True)}\n\n"
        f"⚫ Черный фон: {black_cnt}/{total} ({black_ratio*100:.1f}%)"
    )

    return (
        f"📊 <b>{collection}</b> — Быстрая статистика\n"
        f"────────────────────\n\n"
        f"🕒 <b>Последние {len(latest)} NFT:</b>\n" + "\n".join(latest_lines) + "\n\n"
        f"🔥 <b>ТОП редких NFT:</b>\n" + "\n".join(top_lines) + "\n\n"
        f"📈 <b>Статистика:</b>\n{chart}"
    )


def _bars_single(values, threshold, greater=False):
    if not values:
        return "0%"
    if greater:
        count = sum(1 for v in values if v > threshold)
    else:
        count = sum(1 for v in values if v <= threshold)
    return f"{count/len(values)*100:.0f}%"


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
        if int(uid) in HUNT_PAUSED_USERS:
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
                await bot.send_message(int(uid), msg, parse_mode="HTML", disable_web_page_preview=True, reply_markup=hunt_control_keyboard())
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
    OWNER_STATS.setdefault(owner_name, {"total": 0, "rare": 0, "black": 0, "link": ""})
    if "link" not in OWNER_STATS[owner_name]:
        OWNER_STATS[owner_name]["link"] = ""
    if "black" not in OWNER_STATS[owner_name]:
        OWNER_STATS[owner_name]["black"] = 0
    if owner_link.startswith("https://t.me"):
        OWNER_STATS[owner_name]["link"] = owner_link
    OWNER_STATS[owner_name]["total"] += 1
    black_bg = data["bg"][0].lower() in ["black", "черный", "чёрный"]
    if black_bg:
        OWNER_STATS[owner_name]["black"] += 1
    if is_rare(data):
        OWNER_STATS[owner_name]["rare"] += 1
    save_owner_stats()

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
    dashboard_filter = SETTINGS.get("dashboard_filter", "all")
    next_style = "v1" if style == "v2" else "v2"
    autostats = SETTINGS.get("live_stats_enabled", False)
    signal_mode = SETTINGS.get("signal_mode", "balanced")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="dash_refresh"),
                InlineKeyboardButton(text=f"🧩 Стиль: {style} → {next_style}", callback_data="dash_toggle_style"),
            ],
            [
                InlineKeyboardButton(text=f"📡 AutoStats: {'ON' if autostats else 'OFF'}", callback_data="dash_toggle_autostats"),
                InlineKeyboardButton(text=f"🎛 Mode: {signal_mode.capitalize()}", callback_data="dash_cycle_mode"),
            ],
            [
                InlineKeyboardButton(text=f"{'✅ ' if dashboard_filter == 'all' else ''}All", callback_data="dash_filter_all"),
                InlineKeyboardButton(text=f"{'✅ ' if dashboard_filter == 'rare' else ''}Rare", callback_data="dash_filter_rare"),
                InlineKeyboardButton(text=f"{'✅ ' if dashboard_filter == 'black' else ''}Black", callback_data="dash_filter_black"),
            ],
        ]
    )


def build_live_stats_message(style: str | None = None, filter_mode: str | None = None):
    style = style or SETTINGS.get("dashboard_style", "v2")
    filter_mode = filter_mode or SETTINGS.get("dashboard_filter", "all")

    title = "📊 <b>LIVE DASHBOARD v2</b>" if style == "v2" else "📊 <b>ЖИВАЯ статистика</b>"
    lines = [title, "────────────────────"]

    total_all = sum(len(STATS.get(c, [])) for c in COLLECTIONS)
    black_all = sum(1 for c in COLLECTIONS for nft in STATS.get(c, []) if nft.get("black_bg"))
    rare_all = sum(1 for c in COLLECTIONS for nft in STATS.get(c, []) if nft.get("model_percent", 100) <= 1)

    lines.extend([
        f"• 📦 Всего NFT: <b>{total_all}</b>",
        f"• ⚫ Black: <b>{black_all}</b>",
        f"• 🔥 Rare: <b>{rare_all}</b>",
        f"• 🎚️ Фильтр: <b>{filter_mode}</b>",
        "",
        "<b>Коллекции</b>",
    ])

    collections_stats = []
    for c in COLLECTIONS:
        data = STATS.get(c, [])
        total = len(data)
        black = sum(1 for nft in data if nft.get("black_bg"))
        rare = sum(1 for nft in data if nft.get("model_percent", 100) <= 1)
        max_percent = COLLECTIONS[c].get("max_percent", 2.0)
        enabled = "✅" if COLLECTIONS[c].get("enabled") else "❌"
        pct_rare = (rare / total * 100) if total else 0
        pct_black = (black / total * 100) if total else 0

        if style == "v1":
            row = (
                f"{enabled} <b>{c}</b>\n"
                f"  Всего: <b>{total}</b> | ⚫ <b>{black}</b> | 🔥 <b>{rare}</b>\n"
                f"  Фильтр ≤ <b>{max_percent}%</b> | rare(total): <b>{COLLECTIONS[c].get('rare_count', 0)}</b>"
            )
        else:
            row = (
                f"{enabled} <b>{c}</b> | Всего: {total} | ⚫{black}({pct_black:.1f}%) | 🔥{rare}({pct_rare:.1f}%) "
                f"| filter ≤{max_percent}%"
            )

        collections_stats.append((c, total, black, rare, pct_rare, pct_black, row))

    if filter_mode == "rare":
        collections_stats.sort(key=lambda i: i[3], reverse=True)
    elif filter_mode == "black":
        collections_stats.sort(key=lambda i: i[2], reverse=True)
    else:
        collections_stats.sort(key=lambda i: i[1], reverse=True)

    for c, total, black, rare, pct_rare, pct_black, row in collections_stats[:8]:
        lines.append(row)
        lines.append("")

    # убрать лишнюю пустую строку в конце блока коллекций
    if lines and lines[-1] == "":
        lines.pop()

    lines.extend([
        "────────────────────",
        f"⚙️ Mode: <b>{SETTINGS.get('signal_mode', 'balanced')}</b>  |  📡 AutoStats: <b>{'ON' if SETTINGS.get('live_stats_enabled') else 'OFF'}</b>",
    ])
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


@dp.callback_query(F.data == "dash_toggle_autostats")
async def dash_toggle_autostats(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    SETTINGS["live_stats_enabled"] = not SETTINGS.get("live_stats_enabled", False)
    save_settings()
    await call.message.edit_text(
        build_live_stats_message(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=dashboard_keyboard(),
    )
    await call.answer(f"AutoStats: {'ON' if SETTINGS['live_stats_enabled'] else 'OFF'}")


@dp.callback_query(F.data == "dash_cycle_mode")
async def dash_cycle_mode(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    modes = ["conservative", "balanced", "aggressive"]
    current = SETTINGS.get("signal_mode", "balanced")
    next_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "balanced"
    SETTINGS["signal_mode"] = next_mode
    save_settings()
    await call.message.edit_text(
        build_live_stats_message(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=dashboard_keyboard(),
    )
    await call.answer(f"Mode: {next_mode}")


@dp.callback_query(F.data.startswith("dash_filter_"))
async def dash_filter_callback(call: types.CallbackQuery):
    if not await ensure_admin_callback(call):
        return
    filter_mode = call.data.replace("dash_filter_", "")
    if filter_mode not in ("all", "rare", "black"):
        await call.answer("Неизвестный фильтр", show_alert=True)
        return
    SETTINGS["dashboard_filter"] = filter_mode
    save_settings()
    await call.message.edit_text(
        build_live_stats_message(),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=dashboard_keyboard(),
    )
    await call.answer(f"Фильтр: {filter_mode}")


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
