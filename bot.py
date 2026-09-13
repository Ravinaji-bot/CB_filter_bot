"""
Telegram Auto Filter Bot — with Auto Poster (audio/genre/OTT/quality auto-detect)
-----------------------------------------------------------------------------------
Requirements:
    pip install pyrogram tgcrypto motor requests

Kaam kaise karta hai:
    1. DB_CHANNEL me jo bhi video/file aati hai, uska caption/filename padh kar:
         - Resolution + encode tag detect hota hai (480p, 720p, 720p HEVC, 1080p 10bit...)
         - Audio languages detect hoti hain (Hindi, English, Tamil, Telugu, Marathi...)
         - Source detect hota hai (WEB-DL, BluRay, WebRip, HDRip, DVDRip, HDTC, CAM, Pre-HD, HQ)
         - TMDB se genre + landscape poster + (agar mile to) OTT platform fetch hota hai
    2. POST_CHANNEL me ek structured post banta / update hota hai, jisme har quality ke
       saamne "Click Here" hyperlink hoti hai — jo us EXACT file ka deep-link hai.
    3. User jab "Click Here" dabata hai, bot ke PM me wahi specific file mil jaati hai.

.env / Koyeb env vars:
    API_ID, API_HASH   -> https://my.telegram.org
    BOT_TOKEN          -> @BotFather se
    BOT_USERNAME       -> bot ka username bina @ ke (jaise: MyAutoFilterBot)
    DB_CHANNEL         -> jahan aap files daalte ho (bot admin ho)
    POST_CHANNEL       -> jahan auto-post (poster+details) jayega (bot admin ho)
    MONGO_URI          -> MongoDB connection string
    TMDB_API_KEY       -> themoviedb.org ki free API key
    WATCH_REGION       -> OTT provider ke liye country code (default: IN)
    POWERED_BY_TEXT    -> post ke neeche "Powered By" wala text (default: Your Channel)
    POWERED_BY_LINK    -> us text par lagne wala link (default: https://t.me/)
"""

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from io import BytesIO
from datetime import datetime, timedelta, timezone
import re
import os
import time
import asyncio
import threading
import requests
import psutil
from flask import Flask

# ---------------- CONFIG (Koyeb env vars se aayega) ----------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
DB_CHANNEL = int(os.environ.get("DB_CHANNEL", "0"))
POST_CHANNEL = int(os.environ.get("POST_CHANNEL", "0"))
REQUEST_CHANNEL = int(os.environ.get("REQUEST_CHANNEL", "0"))  # admin-only private channel
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # bot owner ka Telegram user ID
SPAM_LOG_CHANNEL = int(os.environ.get("SPAM_LOG_CHANNEL", "0"))  # spam alerts ke liye alag private channel
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0"))  # naye users/groups ka log yahan jaata hai
FORCE_SUB_CHANNEL = int(os.environ.get("FORCE_SUB_CHANNEL", "0"))  # private channel — pehle join karna zaroori
MUTE_MINUTES = int(os.environ.get("MUTE_MINUTES", "30"))  # kitni der ke liye mute karna hai
MONGO_URI = os.environ.get("MONGO_URI", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
WATCH_REGION = os.environ.get("WATCH_REGION", "IN")
POWERED_BY_TEXT = os.environ.get("POWERED_BY_TEXT", "Your Channel")
POWERED_BY_LINK = os.environ.get("POWERED_BY_LINK", "https://t.me/")
PORT = int(os.environ.get("PORT", "8000"))
TOTAL_STORAGE_MB = float(os.environ.get("TOTAL_STORAGE_MB", "512"))  # MongoDB cluster ka total quota (MB)
BOT_START_TIME = time.time()
# ---------------------------------------------------------------------

app = Client("autofilter_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["autofilter"]
files_col = db["files"]     # har file ka record (quality, audio, source, movie_key, etc.)
posts_col = db["posts"]     # movie-wise grouped auto-post tracking (message_id, genres, ott...)
users_col = db["users"]     # per-user state (jaise "movie request" ka pending flow)
settings_col = db["settings"]  # bot-wide settings (jaise cached force-sub invite link)
groups_col = db["authorized_groups"]  # sirf owner-approved groups yahan store hote hain
warnings_col = db["warnings"]  # per-user-per-group spam warning count


# ============================================================
#  DETECTION HELPERS
# ============================================================

# ---- Resolution + encode tag (per-file quality label jo button/line me dikhta hai) ----
QUALITY_PATTERNS = [
    (r"\b2160p\b|\b4k\b", "2160p 4K"),
    (r"\b1080p\b.*?\bhevc\b.*?\b10\s?bit\b|\b10\s?bit\b.*?\b1080p\b.*?\bhevc\b", "1080p HEVC 10bit"),
    (r"\b1080p\b.*?\b10\s?bit\b", "1080p 10bit"),
    (r"\b1080p\b.*?\bhevc\b", "1080p HEVC"),
    (r"\b1080p\b", "1080p"),
    (r"\b720p\b.*?\bhevc\b.*?\b10\s?bit\b|\b10\s?bit\b.*?\b720p\b.*?\bhevc\b", "720p HEVC 10bit"),
    (r"\b720p\b.*?\b10\s?bit\b", "720p 10bit"),
    (r"\b720p\b.*?\bhevc\b", "720p HEVC"),
    (r"\b720p\b", "720p"),
    (r"\b480p\b", "480p"),
    (r"\b360p\b", "360p"),
    (r"\bpre[\s\-]?hd\b", "Pre-HD"),
    (r"\bhdtc\b|\bhd[\s\-]?tc\b", "HDTC"),
    (r"\bhdcam\b|\bcam\b", "CAM"),
    (r"\bhq\b", "HQ"),
]


def detect_quality(text: str) -> str:
    """Caption/filename me se resolution/encode tag detect karta hai."""
    text_l = text.lower()
    for pattern, label in QUALITY_PATTERNS:
        if re.search(pattern, text_l):
            return label
    return "Unknown"


# ---- Source (header me "ǫᴜᴀʟɪᴛʏ : WEB-DL" wali line) ----
SOURCE_PATTERNS = [
    (r"\bweb[\-\s]?dl\b", "WEB-DL"),
    (r"\bweb[\-\s]?rip\b", "WebRip"),
    (r"\bblu[\-\s]?ray\b|\bbrrip\b|\bbdrip\b", "BluRay"),
    (r"\bhdrip\b", "HDRip"),
    (r"\bdvdrip\b", "DVDRip"),
    (r"\bhdtc\b|\bhd[\s\-]?tc\b", "HDTC"),
    (r"\bhdcam\b|\bcam\b", "CAM"),
    (r"\bpre[\s\-]?hd\b", "Pre-HD"),
    (r"\bhq\b", "HQ"),
]


def detect_source(text: str) -> str:
    """Caption/filename se release source (WEB-DL/BluRay/WebRip/etc) detect karta hai."""
    text_l = text.lower()
    for pattern, label in SOURCE_PATTERNS:
        if re.search(pattern, text_l):
            return label
    return "Unknown"


# ---- Audio languages (jitni bhi ho, sab auto-detect) ----
LANGUAGES = [
    "Hindi", "English", "Tamil", "Telugu", "Kannada", "Malayalam",
    "Marathi", "Bengali", "Punjabi", "Gujarati", "Urdu", "Bhojpuri",
]


def detect_audio(text: str) -> list:
    """Caption/filename me jitni bhi languages mile, sab return karta hai (order preserve)."""
    text_l = text.lower()
    found = []
    for lang in LANGUAGES:
        if re.search(rf"\b{lang.lower()}\b", text_l):
            found.append(lang)
    return found


# ---- Known piracy/leak website names jo caption me spam ki tarah aate hain ----
KNOWN_SITES = [
    "SkymoviesHD", "SkymoviesHD.us", "HDHub4u", "hd4hub", "Hub4u", "Filmyzilla",
    "FilmyZilla", "Filmywap", "MoviesFlix", "9xmovies", "Khatrimaza", "Bolly4u",
    "Mp4moviez", "World4ufree", "ExtraMovies", "DownloadHub", "TodayPk",
    "TamilRockers", "Isaimini", "Moviesda", "Vegamovies", "7StarHD", "RdxHD",
    "Katmoviehd", "Moviesmod", "Jalshamoviez", "Bolly4uMovie", "Filmy4wap",
    "Cinevood", "Toxicwap", "Skymovies", "Movierulz", "Uwatchfree", "Filmyhit",
]


def clean_caption(text: str) -> str:
    """
    Caption/filename se sabhi promotional junk hata kar sirf 'Movie Name ... .mkv'
    wala clean hissa nikalta hai:
      - Channel tags jaise [@ClipmateZone], (@PlexRip), @username
      - URLs / t.me links
      - Fancy unicode 'Powered by / 1st on Telegram' jaisi signature lines
      - Known piracy website names (SkymoviesHD, HDHub4u, etc.)
    """
    # 1. Sabse pehle: file extension tak ka hissa hi rakho, uske baad ka
    #    sab kuch (signature block, "Powered by" wagera) discard kar do.
    match = re.search(r"^.*?\.(mkv|mp4|avi|webm|mov)\b", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        text = match.group(0)

    # 2. Channel tags: [@Something], (@Something), ya sirf @Something
    text = re.sub(r"\[\s*@\w+\s*\]", " ", text)
    text = re.sub(r"\(\s*@\w+\s*\)", " ", text)
    text = re.sub(r"「?\s*@\w+\s*」?", " ", text)
    text = re.sub(r"@\w+", " ", text)

    # 3. URLs / telegram links
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"www\.\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"t\.me/\S+", " ", text, flags=re.IGNORECASE)

    # 4. Known piracy website names
    for site in KNOWN_SITES:
        text = re.sub(re.escape(site), " ", text, flags=re.IGNORECASE)

    # 5. Extra spaces/newlines clean karo
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_movie_name(text: str):
    """Caption/filename se junk hata kar clean movie/series name + year nikalta hai."""
    name = text
    name = re.sub(r"\.(mkv|mp4|avi|webm)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\[[^\]]*\]|\{[^\}]*\}", " ", name)

    junk = [
        r"\d{3,4}p", r"hevc", r"10\s?bit", r"x264", r"x265", r"web[\-\s]?dl",
        r"webrip", r"hdrip", r"blu[\-\s]?ray", r"brrip", r"bdrip", r"dvdrip",
        r"hdtc", r"hdcam", r"cam", r"pre[\-\s]?hd", r"hq", r"esub", r"esubs",
        r"dual\s?audio", r"multi\s?audio", r"\bAAC\b", r"\bDD5\.1\b",
        r"\d+MB", r"\d+GB",
        # ---- series-specific junk: season/episode/complete/combined tags ----
        r"\bs\d{1,2}\b", r"\bseason\s?\d{1,2}\b",
        r"\be\d{1,3}\s*-\s*e?\d{1,3}\b", r"\be\d{1,3}\b", r"\bepisode\s?\d{1,3}\b",
        r"\bcombined\b", r"\bcomplete\b", r"#\w+",
    ]
    for lang in LANGUAGES:
        junk.append(lang)
    for j in junk:
        name = re.sub(j, " ", name, flags=re.IGNORECASE)

    year_match = re.search(r"(19|20)\d{2}", name)
    year = year_match.group(0) if year_match else ""
    name = re.sub(r"(19|20)\d{2}", " ", name)

    name = re.sub(r"[._\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name, year


# ---- SERIES DETECTION: Season number + Episode range (E01-E08, E01-E05, wagera) ----
def detect_series_info(text: str):
    """
    Caption/filename se web-series ki info nikalta hai:
      - season number (S01 -> 1, S02 -> 2, ... S20 -> 20)
      - episode range (E01-E08 -> start=1,end=8 ; sirf E05 -> start=end=5)
    Agar season na mile to is_series=False (movie samjha jayega).
    """
    season = None
    s_match = re.search(r"\bS(\d{1,2})\b", text, flags=re.IGNORECASE)
    if not s_match:
        s_match = re.search(r"\bseason\s?(\d{1,2})\b", text, flags=re.IGNORECASE)
    if s_match:
        season = int(s_match.group(1))

    ep_start, ep_end = None, None
    range_match = re.search(r"\bE(\d{1,3})\s*-\s*E?(\d{1,3})\b", text, flags=re.IGNORECASE)
    if range_match:
        ep_start = int(range_match.group(1))
        ep_end = int(range_match.group(2))
    else:
        single_match = re.search(r"\bE(\d{1,3})\b", text, flags=re.IGNORECASE)
        if single_match:
            ep_start = ep_end = int(single_match.group(1))

    is_series = season is not None
    return is_series, season, ep_start, ep_end


def format_episode_label(ep_start, ep_end) -> str:
    if ep_start is None:
        return ""  # season pack jisme episode tag hi nahi mila
    if ep_start == ep_end:
        return f"E{ep_start:02d}"
    return f"E{ep_start:02d}-E{ep_end:02d}"


def quality_sort_key(quality: str):
    """480p < 720p HEVC < 720p < 1080p ... is order me sort karne ke liye."""
    m = re.search(r"(\d{3,4})p", quality)
    res = int(m.group(1)) if m else 99999  # Unknown/Pre-HD/CAM sabse aakhir me
    hevc_rank = 0 if "hevc" in quality.lower() else 1
    tenbit_rank = 0 if "10bit" in quality.lower() else 1
    return (res, hevc_rank, tenbit_rank)


# ---- TMDB: poster + genres + OTT (movie aur TV series dono ke liye) ----
GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War",
    37: "Western",
}

TV_GENRE_MAP = {
    10759: "Action & Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 10762: "Kids",
    9648: "Mystery", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk",
    10768: "War & Politics", 37: "Western",
}


def fetch_tmdb_details(name: str, year: str = "", media_type: str = "movie"):
    """
    TMDB se movie YA tv-series search karke: landscape poster, genres (comma string),
    OTT platform (comma string) return karta hai.
    media_type: "movie" ya "tv"
    Agar TMDB_API_KEY na ho ya kuch na mile to sensible defaults return karta hai.
    """
    poster_url, genres_str, ott_str = None, "N/A", "N/A"

    if not TMDB_API_KEY:
        return poster_url, genres_str, ott_str

    genre_map = TV_GENRE_MAP if media_type == "tv" else GENRE_MAP
    title_field = "name" if media_type == "tv" else "title"

    try:
        params = {"api_key": TMDB_API_KEY, "query": name}
        if year:
            if media_type == "tv":
                params["first_air_date_year"] = year
            else:
                params["year"] = year

        resp = requests.get(
            f"https://api.themoviedb.org/3/search/{media_type}", params=params, timeout=10
        )
        results = (resp.json() or {}).get("results") or []
        if not results:
            return poster_url, genres_str, ott_str

        item = results[0]
        tmdb_id = item.get("id")

        # ---- Landscape (backdrop) poster, HD quality ----
        best_backdrop_path = None
        best_width = 0
        if tmdb_id:
            try:
                img_resp = requests.get(
                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images",
                    params={"api_key": TMDB_API_KEY},
                    timeout=10,
                )
                backdrops = (img_resp.json() or {}).get("backdrops") or []
                for b in backdrops:
                    w = b.get("width", 0)
                    if w > best_width:
                        best_width = w
                        best_backdrop_path = b.get("file_path")
            except Exception as e:
                print(f"TMDB images fetch error: {e}")

        if not best_backdrop_path:
            best_backdrop_path = item.get("backdrop_path")

        if best_backdrop_path:
            poster_url = f"https://image.tmdb.org/t/p/original{best_backdrop_path}"

        genre_ids = item.get("genre_ids") or []
        genre_names = [genre_map.get(gid) for gid in genre_ids if genre_map.get(gid)]
        if genre_names:
            genres_str = ", ".join(genre_names)

        # OTT / watch providers
        if tmdb_id:
            prov_resp = requests.get(
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/watch/providers",
                params={"api_key": TMDB_API_KEY},
                timeout=10,
            )
            prov_data = (prov_resp.json() or {}).get("results", {})
            region_data = prov_data.get(WATCH_REGION, {})
            providers = region_data.get("flatrate") or region_data.get("ads") or region_data.get("rent") or []
            provider_names = [p.get("provider_name") for p in providers if p.get("provider_name")]
            if provider_names:
                ott_str = ", ".join(sorted(set(provider_names)))

        return poster_url, genres_str, ott_str

    except Exception as e:
        print(f"TMDB fetch error: {e}")
        return poster_url, genres_str, ott_str


def download_poster_bytes(url: str):
    """
    Poster ko server-side download karke BytesIO me return karta hai, taaki:
      1. Poster ek REAL uploaded photo ki tarah bheja jaaye (caption ke saath chipka hua),
         sirf ek "link preview" card ki tarah nahi.
      2. Original TMDB image link kahin bhi message me expose na ho — user sirf
         Telegram ka apna uploaded photo dekhta hai, seedha source link nahi milta.
    """
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        bio = BytesIO(resp.content)
        bio.name = "poster.jpg"
        return bio
    except Exception as e:
        print(f"Poster download error: {e}")
        return None


# ============================================================
#  AUTO POST BUILD + SEND/UPDATE
# ============================================================

def format_file_size(size_bytes) -> str:
    """Bytes ko readable MB/GB me convert karta hai (jaise: 645 MB, 1.3 GB, 3.12 GB)."""
    if not size_bytes:
        return "N/A"
    mb = size_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.0f} MB"
    gb = mb / 1024
    gb_str = f"{gb:.2f}".rstrip("0").rstrip(".")
    return f"{gb_str} GB"


def build_movie_caption(movie_name, year, audio_str, genres_str, ott_str, source_str, quality_lines):
    return (
        f"🎬 **{movie_name} {year}**\n"
        f"────•˚•── ✦ ──•˚•────\n"
        f"🔊 ᴀᴜᴅɪᴏ  : {audio_str}\n"
        f"🎭 ɢᴇɴʀᴇs : {genres_str}\n"
        f"🍿 ᴏᴛᴛ : {ott_str}\n"
        f"🚀 ǫᴜᴀʟɪᴛʏ : {source_str}\n"
        f"────•˚•── ✦ ──•˚•────\n\n"
        f"{quality_lines}\n\n"
        f"💢 ᴘᴏᴡᴇʀᴇᴅ ʙʏ : [{POWERED_BY_TEXT}]({POWERED_BY_LINK}) 🤞"
    )


def build_series_caption(movie_name, season, audio_str, genres_str, ott_str, source_str, quality_lines):
    return (
        f"🎬 **{movie_name} S{season:02d} #Combined**\n"
        f"────•˚•── ✦ ──•˚•────\n"
        f"🔊 ᴀᴜᴅɪᴏ  : {audio_str}\n"
        f"🎭 ɢᴇɴʀᴇs : {genres_str}\n"
        f"🍿 ᴏᴛᴛ : {ott_str}\n"
        f"🚀 ǫᴜᴀʟɪᴛʏ : {source_str}\n"
        f"────•˚•── ✦ ──•˚•────\n\n"
        f"{quality_lines}\n\n"
        f"💢 ᴘᴏᴡᴇʀᴇᴅ ʙʏ : [{POWERED_BY_TEXT}]({POWERED_BY_LINK}) 🤞"
    )


async def send_or_update_post(client: Client, movie_key: str):
    """
    Movie/Series ki saari files DB se nikaal kar (audio/quality/source aggregate karke)
    ek structured post banata hai ya, agar pehle se bana hai, use update kar deta hai.
    Web-series ke liye alag template use hota hai (season header + episode-range grouping).
    """
    cursor = files_col.find({"movie_key": movie_key})
    all_files = await cursor.to_list(length=500)
    if not all_files:
        return

    movie_name = all_files[0]["movie_name"]
    year = all_files[0].get("year", "")
    is_series = bool(all_files[0].get("is_series"))
    season = all_files[0].get("season")

    # audio: saari files ke union me se jitni languages mili, sab
    audio_set = []
    for f in all_files:
        for lang in f.get("audio", []):
            if lang not in audio_set:
                audio_set.append(lang)
    audio_str = ", ".join(audio_set) if audio_set else "N/A"

    # source: jo bhi files me mila (unique list)
    sources = sorted(set(f.get("source", "Unknown") for f in all_files) - {"Unknown"})
    source_str = ", ".join(sources) if sources else "Unknown"

    existing = await posts_col.find_one({"movie_key": movie_key})

    if existing and existing.get("poster_url") is not None:
        poster_url = existing.get("poster_url")
        genres_str = existing.get("genres", "N/A")
        ott_str = existing.get("ott", "N/A")
    else:
        media_type = "tv" if is_series else "movie"
        poster_url, genres_str, ott_str = fetch_tmdb_details(movie_name, year, media_type)

    if is_series:
        # ---- Series: quality ke hisaab se group karo, har group ke andar episode-range lines ----
        quality_groups = {}
        for f in all_files:
            q = f.get("quality", "Unknown")
            quality_groups.setdefault(q, []).append(f)

        quality_blocks = []
        for q in sorted(quality_groups.keys(), key=quality_sort_key):
            files_in_q = sorted(
                quality_groups[q], key=lambda x: (x.get("ep_start") if x.get("ep_start") is not None else 0)
            )
            lines = [f"✧ {q} : "]
            for f in files_in_q:
                code = str(f["_id"])
                link = f"https://t.me/{BOT_USERNAME}?start=file_{code}"
                size_str = format_file_size(f.get("file_size"))
                ep_label = f.get("episode_label") or ""
                if ep_label:
                    lines.append(f"{ep_label} [Click Here]({link}) ({size_str})")
                else:
                    lines.append(f"[Click Here]({link}) ({size_str})")
            quality_blocks.append("\n".join(lines))
        quality_lines = "\n\n".join(quality_blocks)

        caption = build_series_caption(
            movie_name, season, audio_str, genres_str, ott_str, source_str, quality_lines
        )
    else:
        # ---- Movie: har file ke liye ek "quality : ➤ Click Here (size)" line ----
        quality_rows = []
        for f in sorted(all_files, key=lambda x: quality_sort_key(x.get("quality", ""))):
            code = str(f["_id"])
            link = f"https://t.me/{BOT_USERNAME}?start=file_{code}"
            size_str = format_file_size(f.get("file_size"))
            quality_rows.append(
                f"{f.get('quality', 'Unknown')} : ➤ [Click Here]({link}) ({size_str})"
            )
        quality_lines = "\n\n".join(quality_rows)

        caption = build_movie_caption(
            movie_name, year, audio_str, genres_str, ott_str, source_str, quality_lines
        )

    # "Request Your Movie" button — har post ke niche, deep-link se bot PM khulta hai
    request_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔍 Request Your Movie", url=f"https://t.me/{BOT_USERNAME}?start=request")]]
    )

    if existing:
        try:
            if existing.get("poster_url"):
                await client.edit_message_caption(
                    chat_id=POST_CHANNEL,
                    message_id=existing["message_id"],
                    caption=caption,
                    reply_markup=request_button,
                )
            else:
                await client.edit_message_text(
                    chat_id=POST_CHANNEL,
                    message_id=existing["message_id"],
                    text=caption,
                    reply_markup=request_button,
                )
        except Exception as e:
            print(f"Edit post error: {e}")

        await posts_col.update_one(
            {"movie_key": movie_key},
            {"$set": {"genres": genres_str, "ott": ott_str}},
        )
        return

    # naya post banao — poster ko pehle download karo, phir asli photo ki tarah upload karo
    # (isse poster caption ke saath "chipka hua" ek hi post banta hai, aur original
    #  TMDB link kahin expose nahi hota)
    sent = None
    if poster_url:
        photo_bytes = download_poster_bytes(poster_url)
        if photo_bytes:
            sent = await client.send_photo(
                chat_id=POST_CHANNEL, photo=photo_bytes, caption=caption, reply_markup=request_button
            )

    if sent is None:
        sent = await client.send_message(chat_id=POST_CHANNEL, text=caption, reply_markup=request_button)

    await posts_col.update_one(
        {"movie_key": movie_key},
        {"$set": {
            "movie_key": movie_key,
            "movie_name": movie_name,
            "is_series": is_series,
            "season": season,
            "message_id": sent.id,
            "poster_url": poster_url,
            "genres": genres_str,
            "ott": ott_str,
        }},
        upsert=True,
    )


# ============================================================
#  BOT HANDLERS
# ============================================================

# ---------- 1. Auto Index: DB channel me jo bhi file aaye usse save karo ----------
@app.on_message(filters.chat(DB_CHANNEL) & (filters.document | filters.video | filters.audio))
async def index_file(client: Client, message: Message):
    media = message.document or message.video or message.audio
    raw_file_name = getattr(media, "file_name", None) or message.caption or "unknown"
    raw_caption = message.caption or raw_file_name

    # sabse pehle: channel tags / links / "Powered by" signature / piracy site
    # names sab hata kar sirf saaf "Movie Name ... .mkv" wala hissa nikalo
    caption_text = clean_caption(raw_caption)
    file_name = clean_caption(raw_file_name)

    quality = detect_quality(caption_text)
    source = detect_source(caption_text)
    audio = detect_audio(caption_text)

    # ---- Web-series detection: season + episode range ----
    is_series, season, ep_start, ep_end = detect_series_info(caption_text)
    episode_label = format_episode_label(ep_start, ep_end) if is_series else ""

    movie_name, year = clean_movie_name(caption_text)

    if is_series:
        # har season ka alag post banega: "seriesname_s01", "seriesname_s02"...
        movie_key = f"{movie_name.lower().strip()}_s{season:02d}"
    else:
        movie_key = movie_name.lower().strip()

    if not movie_name.strip():
        print("Movie/Series name detect nahi hui, skip kar rahe hain.")
        return

    result = await files_col.find_one_and_update(
        {"file_id": media.file_id},
        {"$set": {
            "file_id": media.file_id,
            "file_name": file_name,
            "file_size": media.file_size,
            "caption": caption_text,
            "message_id": message.id,
            "quality": quality,
            "source": source,
            "audio": audio,
            "movie_name": movie_name,
            "movie_key": movie_key,
            "year": year,
            "is_series": is_series,
            "season": season,
            "ep_start": ep_start,
            "ep_end": ep_end,
            "episode_label": episode_label,
        }},
        upsert=True,
        return_document=True,
    )
    print(
        f"Indexed: {file_name} | {'Series' if is_series else 'Movie'}: {movie_name} "
        f"{'S%02d' % season if is_series else ''} {episode_label} | Quality: {quality} | "
        f"Source: {source} | Audio: {audio}"
    )

    if POST_CHANNEL and BOT_USERNAME:
        try:
            await send_or_update_post(client, movie_key)
        except Exception as e:
            print(f"Auto-post error: {e}")


# ---------- SECURITY: sirf owner-authorized groups me hi bot kaam karega ----------
async def is_group_authorized(chat_id: int) -> bool:
    doc = await groups_col.find_one({"chat_id": chat_id})
    return doc is not None


# ---------- ANTI-SPAM: links / @mentions / "bot" keyword / lambe spam words detect ----------
SPAM_PATTERNS = [
    r"https?://\S+",       # http:// ya https:// wale links
    r"www\.\S+",            # www. wale links
    r"t\.me/\S+",            # telegram invite/deep links
    r"@[a-zA-Z0-9_]{4,}",    # @username mentions
    r"\bbot\b",              # "bot" keyword
    r"\S{25,}",              # ek hi lamba bina-space word (spam/promo code jaisa)
]


def is_spam_message(text: str) -> bool:
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


async def is_chat_admin(client: Client, chat_id: int, user_id: int) -> bool:
    """Group admins/owner ko spam-check se exempt rakhne ke liye."""
    if user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def handle_spam(client: Client, message: Message) -> bool:
    """
    Agar message spam ho to: delete karo, warning do (1/2/3), 3+ warnings par
    MUTE_MINUTES ke liye mute karo, aur SPAM_LOG_CHANNEL me admin ko batao.
    Return True agar spam tha (aage process na kiya jaaye), warna False.
    """
    if not message.text or not is_spam_message(message.text):
        return False

    if await is_chat_admin(client, message.chat.id, message.from_user.id):
        return False  # admins/owner ko chhodo

    user = message.from_user
    chat = message.chat

    # message delete karo
    try:
        await message.delete()
    except Exception as e:
        print(f"Spam delete error: {e}")

    # warning count badhao
    record = await warnings_col.find_one({"chat_id": chat.id, "user_id": user.id})
    count = (record.get("count", 0) if record else 0) + 1
    await warnings_col.update_one(
        {"chat_id": chat.id, "user_id": user.id},
        {"$set": {"chat_id": chat.id, "user_id": user.id, "count": count}},
        upsert=True,
    )

    action_text = ""
    if count <= 3:
        try:
            await client.send_message(
                chat.id,
                f"⚠️ [{user.mention}](tg://user?id={user.id}), spam/link/mention allowed nahi hai!\n"
                f"Warning **{count}/3**.",
            )
        except Exception:
            pass
        action_text = f"Warning {count}/3 di gayi"
    else:
        until_dt = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
        try:
            await client.restrict_chat_member(
                chat.id,
                user.id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                ),
                until_date=until_dt,
            )
            await client.send_message(
                chat.id,
                f"🚫 [{user.mention}](tg://user?id={user.id}) ko baar-baar spam karne par "
                f"**{MUTE_MINUTES} minutes** ke liye mute kar diya gaya hai.",
            )
        except Exception as e:
            print(f"Mute error: {e}")
        # count reset karo taaki agli baar fresh se 1 se shuru ho
        await warnings_col.update_one(
            {"chat_id": chat.id, "user_id": user.id}, {"$set": {"count": 0}}
        )
        action_text = f"{MUTE_MINUTES} minutes ke liye MUTE kar diya gaya"

    # admin ko alag private channel me batao
    if SPAM_LOG_CHANNEL:
        try:
            await client.send_message(
                SPAM_LOG_CHANNEL,
                (
                    f"🚨 **Spam Detected**\n\n"
                    f"👤 User: {user.first_name or ''} (@{user.username or 'no_username'})\n"
                    f"🆔 User ID: `{user.id}`\n"
                    f"💬 Group: {chat.title or chat.id}\n"
                    f"🆔 Group ID: `{chat.id}`\n"
                    f"📝 Message: {message.text[:200]}\n"
                    f"⚙️ Action: {action_text}"
                ),
            )
        except Exception as e:
            print(f"Spam log error: {e}")

    return True


# ---------- 1a. Bot ko kisi group me add karte hi check karo, agar unauthorized to leave karo ----------
@app.on_message(filters.new_chat_members)
async def guard_new_group(client: Client, message: Message):
    added_ids = [m.id for m in (message.new_chat_members or [])]
    me = await client.get_me()
    if me.id not in added_ids:
        return  # kisi aur member ko add kiya gaya hai, bot ko nahi

    chat = message.chat
    chat_id = chat.id

    # ---- LOG: har naye group ki entry LOG_CHANNEL me, chahe authorized ho ya na ho ----
    if LOG_CHANNEL:
        invite_link = "N/A"
        try:
            if chat.username:
                invite_link = f"https://t.me/{chat.username}"
            else:
                invite_link = await client.export_chat_invite_link(chat_id)
        except Exception:
            invite_link = "N/A (bot ko admin banane ke baad milega)"

        try:
            await client.send_message(
                LOG_CHANNEL,
                (
                    f"#new_group\n\n"
                    f"👥 Group: {chat.title or 'Unknown'}\n"
                    f"🆔 Group ID: `{chat_id}`\n"
                    f"🔗 Link: {invite_link}"
                ),
            )
        except Exception as e:
            print(f"Group log error: {e}")

    if await is_group_authorized(chat_id):
        return  # owner ne pehle se allow kar rakha hai

    try:
        await message.reply_text(
            "⚠️ Ye bot sirf **owner ki permission** se kaam karta hai.\n"
            "Is group ko authorize nahi kiya gaya, isliye main abhi leave kar raha hoon."
        )
    except Exception:
        pass

    try:
        await client.leave_chat(chat_id)
    except Exception as e:
        print(f"Leave chat error: {e}")


# ---------- 1b. Owner-only commands: group ko authorize/unauthorize karna ----------
@app.on_message(filters.command("addgroup") & filters.user(OWNER_ID))
async def add_group(client: Client, message: Message):
    if len(message.command) > 1:
        try:
            chat_id = int(message.command[1])
        except ValueError:
            await message.reply_text("Usage: `/addgroup <chat_id>` ya group ke andar bhej ke `/addgroup`")
            return
    else:
        chat_id = message.chat.id

    await groups_col.update_one(
        {"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True
    )
    await message.reply_text(f"✅ Group `{chat_id}` authorize ho gaya. Ab bot yahan kaam karega.")


@app.on_message(filters.command("removegroup") & filters.user(OWNER_ID))
async def remove_group(client: Client, message: Message):
    if len(message.command) > 1:
        try:
            chat_id = int(message.command[1])
        except ValueError:
            await message.reply_text("Usage: `/removegroup <chat_id>` ya group ke andar bhej ke `/removegroup`")
            return
    else:
        chat_id = message.chat.id

    await groups_col.delete_one({"chat_id": chat_id})
    await message.reply_text(f"❌ Group `{chat_id}` unauthorize kar diya gaya.")

    try:
        await client.leave_chat(chat_id)
    except Exception:
        pass


@app.on_message(filters.command("listgroups") & filters.user(OWNER_ID))
async def list_groups(client: Client, message: Message):
    cursor = groups_col.find({})
    groups = await cursor.to_list(length=500)
    if not groups:
        await message.reply_text("Abhi koi bhi group authorized nahi hai.")
        return
    text = "\n".join(f"• `{g['chat_id']}`" for g in groups)
    await message.reply_text(f"**Authorized Groups:**\n{text}")


# ---------- FORCE SUBSCRIBE: user pehle private channel join kare, tabhi bot use ho ----------
async def get_force_sub_invite_link(client: Client):
    """
    Ek 'join request required' invite link banata hai (ya cache se deta hai), jisse
    private channel me user seedha add nahi hota — pehle admin ki approval lagti hai.
    """
    doc = await settings_col.find_one({"key": "force_sub_link"})
    if doc and doc.get("link"):
        return doc["link"]

    try:
        link_obj = await client.create_chat_invite_link(
            FORCE_SUB_CHANNEL, creates_join_request=True
        )
        link = link_obj.invite_link
        await settings_col.update_one(
            {"key": "force_sub_link"}, {"$set": {"link": link}}, upsert=True
        )
        return link
    except Exception as e:
        print(f"Force-sub link error: {e}")
        return None


async def is_force_sub_member(client: Client, user_id: int) -> bool:
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await client.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        return False


async def send_force_sub_message(client: Client, message: Message):
    link = await get_force_sub_invite_link(client)
    if not link:
        await message.reply_text(
            "⚠️ Force-subscribe channel sahi se setup nahi hai. Admin se contact karein."
        )
        return

    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔐 Send Join Request", url=link)]]
    )
    await message.reply_text(
        "🔒 **Access Denied**\n\n"
        "• **Private Channel:** Access requires approval. Send join request.\n\n"
        "✅ After joining, please press /start again.",
        reply_markup=buttons,
    )


def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    return f"{d}d {h}h {m}m {s}s"


@app.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_cmd(client: Client, message: Message):
    all_users = await users_col.count_documents({})
    all_groups = await groups_col.count_documents({})
    premium_users = await users_col.count_documents({"premium": True})  # abhi ke liye 0 (premium system nahi hai)
    all_files = await files_col.count_documents({})

    try:
        db_stats = await db.command("dbStats")
        used_mb = db_stats.get("dataSize", 0) / (1024 * 1024)
    except Exception as e:
        print(f"dbStats error: {e}")
        used_mb = 0

    free_mb = max(TOTAL_STORAGE_MB - used_mb, 0)

    uptime_str = format_uptime(time.time() - BOT_START_TIME)
    ram_percent = psutil.virtual_memory().percent
    cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 1)

    text = (
        "╭────[ 🗃 ᴅᴀᴛᴀʙᴀsᴇ 🗃 ]────⍟\n"
        "│\n"
        f"├⋟ ᴀʟʟ ᴜsᴇʀs ⋟ {all_users}\n"
        f"├⋟ ᴀʟʟ ɢʀᴏᴜᴘs ⋟ {all_groups}\n"
        f"├⋟ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ⋟ {premium_users}\n"
        f"├⋟ ᴀʟʟ ꜰɪʟᴇs ⋟ {all_files}\n"
        f"├⋟ ᴜsᴇᴅ sᴛᴏʀᴀɢᴇ ⋟ {used_mb:.2f} MB\n"
        f"├⋟ ꜰʀᴇᴇ sᴛᴏʀᴀɢᴇ ⋟ {free_mb:.2f} MB\n"
        "│\n"
        "├────[ 🤖 ʙᴏᴛ ᴅᴇᴛᴀɪʟs 🤖 ]────⍟   \n"
        "│\n"
        f"├⋟ ᴜᴘᴛɪᴍᴇ ⋟ {uptime_str}\n"
        f"├⋟ ʀᴀᴍ ⋟ {ram_percent}%\n"
        f"├⋟ ᴄᴘᴜ ⋟ {cpu_percent}%   \n"
        "│\n"
        "╰─────────────────────⍟"
    )
    await message.reply_text(text)


# ---------- BROADCAST: sabhi users + groups ko message bhejo, auto-delete timer ke saath ----------
async def get_broadcast_delete_minutes() -> int:
    doc = await settings_col.find_one({"key": "broadcast_delete_minutes"})
    return doc.get("value", 0) if doc else 0


async def auto_delete_message(client: Client, chat_id: int, message_id: int, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception as e:
        print(f"Auto-delete broadcast msg error: {e}")


@app.on_message(filters.command("setdeletetime") & filters.user(OWNER_ID))
async def set_delete_time(client: Client, message: Message):
    if len(message.command) < 2:
        current = await get_broadcast_delete_minutes()
        await message.reply_text(
            f"Abhi ka auto-delete time: **{current} minutes** "
            f"({'OFF' if current == 0 else 'ON'})\n\n"
            "Change karne ke liye: `/setdeletetime <minutes>` (0 = auto-delete OFF)"
        )
        return

    try:
        minutes = int(message.command[1])
    except ValueError:
        await message.reply_text("Minutes ek number hona chahiye. Usage: `/setdeletetime <minutes>`")
        return

    await settings_col.update_one(
        {"key": "broadcast_delete_minutes"},
        {"$set": {"key": "broadcast_delete_minutes", "value": minutes}},
        upsert=True,
    )
    if minutes > 0:
        await message.reply_text(f"✅ Broadcast messages ab **{minutes} minutes** baad auto-delete ho jayenge.")
    else:
        await message.reply_text("✅ Auto-delete **OFF** kar diya gaya — broadcast messages hamesha rahenge.")


@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text(
            "Jo message broadcast karna hai, usi par **reply** karke `/broadcast` bhejo.\n"
            "(Text, photo, video — kuch bhi ho sakta hai)"
        )
        return

    target = message.reply_to_message
    delete_minutes = await get_broadcast_delete_minutes()

    all_users = await users_col.find({}).to_list(length=200000)
    all_groups = await groups_col.find({}).to_list(length=200000)
    destinations = [u["user_id"] for u in all_users] + [g["chat_id"] for g in all_groups]

    status_msg = await message.reply_text(
        f"📢 Broadcast shuru ho raha hai... (0/{len(destinations)})"
    )

    success = 0
    failed = 0

    for i, chat_id in enumerate(destinations, start=1):
        try:
            sent = await target.copy(chat_id)
            success += 1
            if delete_minutes > 0:
                asyncio.create_task(
                    auto_delete_message(client, chat_id, sent.id, delete_minutes * 60)
                )
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                sent = await target.copy(chat_id)
                success += 1
                if delete_minutes > 0:
                    asyncio.create_task(
                        auto_delete_message(client, chat_id, sent.id, delete_minutes * 60)
                    )
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if i % 25 == 0 or i == len(destinations):
            try:
                await status_msg.edit_text(f"📢 Broadcast ja raha hai... ({i}/{len(destinations)})")
            except Exception:
                pass

        await asyncio.sleep(0.05)  # Telegram rate-limit se bachne ke liye halka sa gap

    delete_note = f"{delete_minutes} minutes baad auto-delete hoga" if delete_minutes > 0 else "auto-delete OFF hai"
    await status_msg.edit_text(
        f"✅ **Broadcast complete!**\n\n"
        f"📤 Sent: {success}\n"
        f"❌ Failed: {failed}\n"
        f"🗑 {delete_note}"
    )


# ---------- 2. Group me koi bhi text aaye to usse "query" maan kar search karo ----------
@app.on_message(filters.group & filters.text & ~filters.command(["start", "help", "addgroup", "removegroup", "listgroups", "stats", "broadcast", "setdeletetime"]))
async def auto_filter(client: Client, message: Message):
    # SECURITY: agar ye group owner-authorized nahi hai to kuch na karo
    if not await is_group_authorized(message.chat.id):
        return

    # ANTI-SPAM: link / @mention / bot-keyword / lambe spam word check
    if await handle_spam(client, message):
        return  # spam tha, delete/warn/mute ho chuka — aage search process mat karo

    query = message.text.strip()
    if len(query) < 2:
        return

    pattern = re.escape(query)
    cursor = files_col.find(
        {"file_name": {"$regex": pattern, "$options": "i"}}
    ).limit(10)
    results = await cursor.to_list(length=10)

    if not results:
        return

    buttons = []
    for r in results:
        label = f"{r.get('movie_name', r['file_name'])[:40]} ({r.get('quality', '')})"
        buttons.append(
            [InlineKeyboardButton(label, url=f"https://t.me/{BOT_USERNAME}?start=file_{r['_id']}")]
        )

    await message.reply_text(
        f"🔍 Found {len(results)} result(s) for: **{query}**",
        reply_markup=InlineKeyboardMarkup(buttons),
        quote=True,
    )


# ---------- 3. /start (deep-link se aayi file bhejne ke liye) ----------
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    # ---- LOG: agar ye user pehli baar bot use kar raha hai ----
    user = message.from_user
    existing_user = await users_col.find_one({"user_id": user.id})
    if not existing_user or not existing_user.get("logged"):
        await users_col.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "logged": True}},
            upsert=True,
        )
        if LOG_CHANNEL:
            try:
                await client.send_message(
                    LOG_CHANNEL,
                    (
                        f"#new_user\n\n"
                        f"👤 Name: {user.first_name or ''} {user.last_name or ''}\n"
                        f"🔗 Username: @{user.username or 'no_username'}\n"
                        f"🆔 User ID: `{user.id}`"
                    ),
                )
            except Exception as e:
                print(f"User log error: {e}")

    # ---- FORCE SUBSCRIBE: pehle private channel join karna zaroori ----
    if FORCE_SUB_CHANNEL and not await is_force_sub_member(client, user.id):
        await send_force_sub_message(client, message)
        return

    if len(message.command) > 1 and message.command[1].startswith("file_"):
        code = message.command[1].split("_", 1)[1]
        try:
            doc = await files_col.find_one({"_id": ObjectId(code)})
        except Exception:
            doc = None

        if not doc:
            await message.reply_text("⚠️ Ye file link expire ho chuka hai ya invalid hai.")
            return

        try:
            await client.send_cached_media(
                chat_id=message.chat.id,
                file_id=doc["file_id"],
                caption=(
                    f"🎬 {doc.get('movie_name', '')}\n"
                    f"📀 Quality: {doc.get('quality', '')}\n"
                    f"🔊 Audio: {', '.join(doc.get('audio', [])) or 'N/A'}"
                ),
            )
        except Exception as e:
            await message.reply_text(f"⚠️ File bhejne me error aaya: {e}")
        return

    # "🔍 Request Your Movie" button se aaya user
    if len(message.command) > 1 and message.command[1] == "request":
        await users_col.update_one(
            {"user_id": message.from_user.id},
            {"$set": {"user_id": message.from_user.id, "awaiting_request": True}},
            upsert=True,
        )
        await message.reply_text(
            "🎬 Jo movie/series chahiye uska **naam aur year** yahan type karke bhejo.\n"
            "Aapki request seedhe admin tak pahunch jayegi."
        )
        return

    await message.reply_text(
        "Namaste! Main ek Auto Filter Bot hoon.\n"
        "Mujhe kisi group me add karo aur movie/file ka naam type karo, "
        "main search karke result de dunga."
    )


# ---------- 4. Private chat me movie request receive karna (sirf jab pending ho) ----------
@app.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def receive_movie_request(client: Client, message: Message):
    user = await users_col.find_one({"user_id": message.from_user.id})
    if not user or not user.get("awaiting_request"):
        return  # is user ka koi pending request nahi, ignore karo

    if not REQUEST_CHANNEL:
        await message.reply_text("⚠️ Request system abhi setup nahi hua hai, admin se contact karein.")
        return

    requester = message.from_user
    request_text = (
        f"📩 **Nayi Movie Request**\n\n"
        f"👤 User: {requester.first_name or ''} (@{requester.username or 'no_username'})\n"
        f"🆔 User ID: `{requester.id}`\n\n"
        f"🎬 Request: {message.text.strip()}"
    )

    try:
        await client.send_message(chat_id=REQUEST_CHANNEL, text=request_text)
        await message.reply_text("✅ Aapki request admin ko bhej di gayi hai. Dhanyavaad!")
    except Exception as e:
        await message.reply_text(f"⚠️ Request bhejne me error aaya: {e}")

    # ek hi request lene ke baad flag clear kar do
    await users_col.update_one(
        {"user_id": message.from_user.id}, {"$set": {"awaiting_request": False}}
    )


# ============================================================
#  KOYEB HEALTH CHECK WEB SERVER
# ============================================================
web = Flask(__name__)


@web.route("/")
def home():
    return "Auto Filter Bot is running!"


def run_web():
    web.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    print("Bot starting...")
    app.run()
  
