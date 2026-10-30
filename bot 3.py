import asyncio
import html
import hashlib
import io
import json
import logging
import os
import re
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- Render health server --------------------

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Anime Poster Bot is running!", 200


@web_app.route("/health")
def health():
    return "OK", 200


def run_web_server():
    port = int(os.environ.get("PORT", "10000"))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)


# -------------------- Configuration --------------------

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANILIST_API_URL = os.getenv("ANILIST_API_URL", "https://graphql.anilist.co").strip()

WATCH_NOW_URL = os.getenv(
    "WATCH_NOW_URL", "https://t.me/YourChannelUsername"
).strip()
JOIN_TELEGRAM_URL = os.getenv(
    "JOIN_TELEGRAM_URL", "https://t.me/YourChannelUsername"
).strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@YourChannelUsername").strip()

LOGO = Path(__file__).parent / "assets" / "channel_logo.jpg"
_configured_templates_dir = Path(os.getenv("TEMPLATES_DIR", str(Path(__file__).parent / "assets" / "templates")))
try:
    _configured_templates_dir.mkdir(parents=True, exist_ok=True)
    _probe = _configured_templates_dir / ".write_test"
    _probe.write_text("ok", encoding="utf-8")
    _probe.unlink(missing_ok=True)
    TEMPLATES_DIR = _configured_templates_dir
except OSError:
    # Render's /var/data is writable only when a persistent disk is mounted.
    # Keep the bot usable on instances without a disk.
    TEMPLATES_DIR = Path("/tmp/aniflix_templates")
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_FILE = TEMPLATES_DIR / "templates.json"
SETTINGS_FILE = TEMPLATES_DIR / "bot_settings.json"
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

W, H = 1920, 1080
DEFAULT_QUALITIES = ["480P", "720P", "1080P"]
DEFAULT_QUALITY = "  •  ".join(DEFAULT_QUALITIES)
DEFAULT_AUDIO = "Tamil Audio"
DEFAULT_JOIN = f"Telegram Join → {CHANNEL_USERNAME}"
DEFAULT_BRAND = "ANIFLIX INFINITY"


def load_bot_settings():
    """Load editable channel details, keeping environment values as defaults."""
    defaults = {
        "channel_username": CHANNEL_USERNAME,
        "watch_now_url": WATCH_NOW_URL,
        "join_telegram_url": JOIN_TELEGRAM_URL,
        "brand_name": DEFAULT_BRAND,
    }
    try:
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                defaults.update({key: str(value).strip() for key, value in saved.items() if key in defaults and value is not None})
    except (OSError, ValueError, json.JSONDecodeError):
        log.exception("Could not load bot settings")
    return defaults


def save_bot_settings(settings):
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = SETTINGS_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temp_file.replace(SETTINGS_FILE)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ANIFLIX-INFINITY")

http = requests.Session()
http.headers["Accept"] = "application/json"
# -------------------- AniList helpers --------------------

def anilist_request(query, variables):
    response = http.post(
        ANILIST_API_URL,
        json={"query": query, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError("AniList lookup failed: " + str(payload["errors"][0].get("message", "unknown error")))
    return payload.get("data", {})


def search(query):
    gql = """
    query ($search: String) {
      Page(page: 1, perPage: 8) {
        media(search: $search, type: ANIME, sort: [SEARCH_MATCH, POPULARITY_DESC]) {
          id
          title { romaji english native }
          startDate { year }
          coverImage { extraLarge large }
          averageScore
        }
      }
    }
    """
    data = anilist_request(gql, {"search": query})
    results = []
    for media in data.get("Page", {}).get("media", []):
        titles = media.get("title") or {}
        title = titles.get("english") or titles.get("romaji") or titles.get("native") or "Untitled"
        cover = media.get("coverImage") or {}
        results.append({
            "id": media["id"],
            "title": title,
            "date": str((media.get("startDate") or {}).get("year") or ""),
            "poster_url": cover.get("extraLarge") or cover.get("large"),
        })
    return [item for item in results if item.get("poster_url")]


def details(item):
    gql = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id
        title { romaji english native }
        description(asHtml: false)
        startDate { year }
        averageScore
        genres
        coverImage { extraLarge large }
      }
    }
    """
    media = anilist_request(gql, {"id": int(item["id"])}).get("Media") or {}
    titles = media.get("title") or {}
    score = media.get("averageScore") or 0
    cover = media.get("coverImage") or {}
    return {
        "title": titles.get("english") or titles.get("romaji") or titles.get("native") or item["title"],
        "overview": re.sub(r"<[^>]+>", "", media.get("description") or "Synopsis unavailable."),
        "year": str((media.get("startDate") or {}).get("year") or item.get("date") or ""),
        "rating": round(float(score) / 10, 1) if score else 0,
        "genres": (media.get("genres") or [])[:3],
        "poster_url": cover.get("extraLarge") or cover.get("large") or item.get("poster_url"),
    }


# -------------------- Image helpers --------------------

def font(size, bold=False):
    paths = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ),
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(draw, text, fnt, maxw):
    lines, current = [], ""
    for word in text.split():
        test = (current + " " + word).strip()
        if draw.textbbox((0, 0), test, font=fnt)[2] <= maxw:
            current = test
        else:
            if current:
                lines.append(current)
            if draw.textbbox((0, 0), word, font=fnt)[2] > maxw:
                current = ""
                for char in word:
                    candidate = current + char
                    if draw.textbbox((0, 0), candidate, font=fnt)[2] <= maxw:
                        current = candidate
                    else:
                        if current:
                            lines.append(current)
                        current = char
            else:
                current = word
    if current:
        lines.append(current)
    return lines


def put_lines(draw, lines, xy, fnt, fill, gap=12, stroke=0, stroke_fill="#000"):
    x, y = xy
    for line in lines:
        draw.text(
            (x, y), line, font=fnt, fill=fill,
            stroke_width=stroke, stroke_fill=stroke_fill,
        )
        bounds = draw.textbbox((x, y), line, font=fnt, stroke_width=stroke)
        y += bounds[3] - bounds[1] + gap
    return y


def centered_text(draw, box, text, fnt, fill):
    x1, y1, x2, y2 = box
    bounds = draw.textbbox((0, 0), text, font=fnt)
    text_w = bounds[2] - bounds[0]
    text_h = bounds[3] - bounds[1]
    x = x1 + (x2 - x1 - text_w) // 2
    y = y1 + (y2 - y1 - text_h) // 2 - bounds[1]
    draw.text((x, y), text, font=fnt, fill=fill)


def draw_quality_buttons(draw, x, y, max_width):
    """Large, individually readable 480P / 720P / 1080P badges."""
    labels = DEFAULT_QUALITIES
    gap = 20
    button_h = 70
    button_w = 190
    label_font = font(30, True)

    total_width = len(labels) * button_w + (len(labels) - 1) * gap
    if total_width > max_width:
        button_w = (max_width - gap * (len(labels) - 1)) // len(labels)

    for index, label in enumerate(labels):
        x1 = x + index * (button_w + gap)
        x2 = x1 + button_w
        draw.rounded_rectangle(
            (x1, y, x2, y + button_h),
            radius=16,
            fill="#FFC857",
            outline="#FFFFFF",
            width=3,
        )
        centered_text(
            draw, (x1, y, x2, y + button_h), label, label_font, "#171923"
        )


def draw_telegram_join(draw, x, y, width, channel_username):
    """Draw a large blue Telegram-style banner with a paper-plane mark."""
    height = 82
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=19,
        fill="#168DE2",
        outline="#FFFFFF",
        width=3,
    )

    cx, cy, radius = x + 51, y + height // 2, 29
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill="#FFFFFF",
    )
    draw.polygon(
        [
            (cx - 17, cy - 10),
            (cx + 19, cy - 19),
            (cx + 8, cy + 19),
            (cx, cy + 5),
            (cx - 9, cy + 12),
            (cx - 7, cy - 1),
        ],
        fill="#168DE2",
    )

    label = f"JOIN TELEGRAM  {channel_username}"
    label_font = font(30, True)
    available_width = width - 105
    while draw.textbbox((0, 0), label, font=label_font)[2] > available_width:
        if label_font.size <= 18:
            break
        label_font = font(label_font.size - 1, True)

    bounds = draw.textbbox((0, 0), label, font=label_font)
    text_h = bounds[3] - bounds[1]
    draw.text(
        (x + 96, y + (height - text_h) // 2 - bounds[1]),
        label,
        font=label_font,
        fill="#FFFFFF",
    )


def _hexagon_points(cx, cy, radius):
    import math
    return [
        (int(cx + radius * math.cos(math.radians(60 * i - 30))),
         int(cy + radius * math.sin(math.radians(60 * i - 30))))
        for i in range(6)
    ]


def _draw_hex_image(canvas, source, cx, cy, radius, crop_shift=0.5):
    """Crop an anime image into a glowing hexagonal collage tile."""
    size = radius * 2 + 12
    tile = ImageOps.fit(
        source.convert("RGB"), (size, size), method=Image.Resampling.LANCZOS,
        centering=(crop_shift, 0.5)
    ).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    pts = _hexagon_points(size // 2, size // 2, radius - 2)
    md.polygon(pts, fill=255)
    tile.putalpha(mask)
    # Cyan glow behind each tile.
    glow = Image.new("RGBA", (size + 36, size + 36), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    shifted = [(x + 18, y + 18) for x, y in pts]
    gd.polygon(shifted, fill=(0, 235, 255, 115))
    glow = glow.filter(ImageFilter.GaussianBlur(13))
    canvas.alpha_composite(glow, (cx - size // 2 - 18, cy - size // 2 - 18))
    canvas.alpha_composite(tile, (cx - size // 2, cy - size // 2))
    ImageDraw.Draw(canvas).polygon(
        [(x + cx - size // 2, y + cy - size // 2) for x, y in pts],
        outline=(0, 235, 255, 255), width=7
    )


def _draw_honeycomb(draw):
    """Draw the fixed dark teal hexagon preset background."""
    draw.rectangle((0, 0, W, H), fill="#031014")
    import math
    radius = 67
    step_x = int(radius * 1.5)
    step_y = int(radius * math.sqrt(3))
    for row, cy in enumerate(range(-70, H + step_y, step_y)):
        offset = 0 if row % 2 == 0 else int(step_x / 2)
        for cx in range(-100 + offset, W + step_x, step_x):
            pts = _hexagon_points(cx, cy, radius)
            draw.polygon(pts, fill="#06191e", outline="#10383d", width=3)
            inner = _hexagon_points(cx, cy, radius - 5)
            draw.line(inner + [inner[0]], fill="#071216", width=2)
    # Bright cyan edge accents and corner geometry.
    draw.line([(0, 115), (90, 0), (112, 0)], fill="#00eaff", width=10)
    draw.line([(0, 165), (48, 112)], fill="#00aebd", width=4)
    draw.line([(W - 115, 0), (W, 110)], fill="#00eaff", width=8)
    draw.polygon([(W - 210, 0), (W - 70, 0), (W - 145, 75)], fill="#00cbd5")
    draw.polygon([(W - 150, H), (W, H - 145), (W, H)], fill="#00cbd5")


def make_poster(info, template_path=None):
    """Render the ANIFLIX teal/black honeycomb preset with AniList content.

    A selected uploaded template remains supported as a legacy option; without
    one, the bot uses the built-in ANIFLIX preset layout.
    """
    settings = load_bot_settings()
    if not info.get("poster_url"):
        raise RuntimeError("No poster artwork found.")
    response = http.get(info["poster_url"], timeout=30)
    response.raise_for_status()
    poster = Image.open(io.BytesIO(response.content)).convert("RGB")

    if template_path and Path(template_path).is_file():
        with Image.open(template_path) as template_image:
            canvas = ImageOps.fit(template_image.convert("RGB"), (W, H), method=Image.Resampling.LANCZOS).convert("RGBA")
    else:
        canvas = Image.new("RGBA", (W, H), (3, 16, 20, 255))
        _draw_honeycomb(ImageDraw.Draw(canvas))

    draw = ImageDraw.Draw(canvas)
    cyan = "#00EAF2"
    white = "#F4FCFF"
    muted = "#C6E1E5"

    # Brand header.
    draw.polygon([(83, 88), (121, 46), (160, 88), (143, 88), (121, 65), (101, 88)], outline=cyan, fill=None, width=7)
    draw.text((190, 48), "ANIFLIX", font=font(65, True), fill=white)
    draw.text((194, 119), "N E T W O R K   T A M I L", font=font(22, True), fill=cyan)

    # Main title and genres.
    title = re.sub(r"\s+", " ", info.get("title", "ANIME TITLE")).strip().upper()
    title_font = font(68 if len(title) <= 18 else 58, True)
    title_lines = wrap(draw, title, title_font, 850)[:2]
    title_y = 190
    for line in title_lines:
        draw.text((68, title_y), line, font=title_font, fill=white, stroke_width=2, stroke_fill="#007F8A")
        title_y += 70

    genres = info.get("genres") or []
    genre_text = "   •   ".join(str(g).upper() for g in genres[:4]) or "ACTION   •   ADVENTURE   •   FANTASY"
    genre_font = font(24, True)
    while draw.textbbox((0, 0), genre_text, font=genre_font)[2] > 820 and genre_font.size > 17:
        genre_font = font(genre_font.size - 1, True)
    draw.rounded_rectangle((68, title_y + 3, 880, title_y + 49), radius=14, outline=cyan, width=3, fill=(0, 33, 39, 225))
    draw.text((88, title_y + 11), genre_text, font=genre_font, fill=white)

    # Synopsis panel.
    panel_y = title_y + 62
    panel_bottom = panel_y + 150
    draw.rounded_rectangle((68, panel_y, 950, panel_bottom), radius=22, fill=(1, 20, 25, 235), outline=cyan, width=4)
    draw.text((102, panel_y + 15), "▤  SYNOPSIS", font=font(30, True), fill=cyan)
    synopsis = re.sub(r"\s+", " ", info.get("overview", "Synopsis unavailable.")).strip()
    if len(synopsis) > 300:
        synopsis = synopsis[:297].rsplit(" ", 1)[0] + "…"
    syn_font = font(24, True)
    syn_lines = wrap(draw, synopsis, syn_font, 800)[:3]
    put_lines(draw, syn_lines, (105, panel_y + 65), syn_font, white, 9)

    # Year, score and Tamil audio row.
    meta_y = panel_bottom + 18
    draw.rounded_rectangle((68, meta_y, 580, meta_y + 68), radius=17, fill=(0, 24, 30, 235), outline=cyan, width=3)
    year = str(info.get("year") or "----")
    rating = info.get("rating")
    rating_text = f"{float(rating):.1f}/10" if rating else "--/10"
    draw.text((100, meta_y + 12), f"▦  {year}", font=font(27, True), fill=white)
    draw.line((315, meta_y + 10, 315, meta_y + 58), fill=cyan, width=3)
    draw.text((345, meta_y + 12), f"★  {rating_text}", font=font(27, True), fill=white)
    draw.rounded_rectangle((605, meta_y, 950, meta_y + 68), radius=17, fill=(0, 24, 30, 235), outline=cyan, width=3)
    draw.text((640, meta_y + 15), "♫  TAMIL AUDIO", font=font(25, True), fill=cyan)

    # Quality selector.
    qy = meta_y + 82
    draw.rounded_rectangle((68, qy, 950, qy + 88), radius=18, fill=(0, 24, 30, 235), outline=cyan, width=3)
    draw.text((94, qy + 8), "HD  QUALITY", font=font(22, True), fill=cyan)
    for i, label in enumerate(DEFAULT_QUALITIES):
        x1 = 150 + i * 260
        draw.rounded_rectangle((x1, qy + 36, x1 + 225, qy + 78), radius=13, fill=(3, 36, 43, 255), outline=cyan, width=3)
        centered_text(draw, (x1, qy + 36, x1 + 225, qy + 78), "⬇  " + label, font(23, True), white)

    # Action buttons and attribution.
    by = qy + 108
    draw.rounded_rectangle((75, by, 505, by + 70), radius=20, fill="#00A94F", outline="#19FF9B", width=5)
    centered_text(draw, (75, by, 505, by + 70), "⬇  DOWNLOAD NOW", font(31, True), white)
    draw.rounded_rectangle((540, by, 950, by + 70), radius=20, fill="#008DD7", outline="#00DFFF", width=5)
    centered_text(draw, (540, by, 950, by + 70), "➤  JOIN NOW", font(31, True), white)
    draw.text((78, by + 82), f"Powered By : {settings['channel_username']}", font=font(22, True), fill=white)

    # Right-side hexagonal anime collage, generated from the AniList cover.
    positions = [
        (1190, 125, 112, 0.48), (1430, 120, 105, 0.25), (1650, 235, 120, 0.75),
        (1085, 375, 165, 0.1), (1370, 390, 175, 0.55), (1690, 510, 130, 0.9),
        (1110, 690, 105, 0.35), (1375, 735, 145, 0.7), (1640, 790, 155, 0.2),
        (1450, 980, 110, 0.5)
    ]
    for cx, cy, radius, shift in positions:
        _draw_hex_image(canvas, poster, cx, cy, radius, shift)

    # Small geometric cyan markers along the collage edge.
    draw = ImageDraw.Draw(canvas)
    for ymark in (95, 145, 195, 645, 695):
        draw.rectangle((1000, ymark, 1012, ymark + 12), fill=cyan)
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=94, optimize=True)
    output.seek(0)
    safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "-", info.get("title", "anime")).strip("-").lower()
    output.name = (safe_title or "anime") + "-ANIFLIX.jpg"
    return output


def poster_keyboard():
    """Real clickable Telegram buttons shown below the poster photo."""
    settings = load_bot_settings()
    rows = []
    if settings["watch_now_url"]:
        rows.append([
            InlineKeyboardButton("▶️ Watch Now", url=settings["watch_now_url"])
        ])
    if settings["join_telegram_url"]:
        rows.append([
            InlineKeyboardButton("✈️ Join Telegram", url=settings["join_telegram_url"])
        ])
    return InlineKeyboardMarkup(rows) if rows else None


# -------------------- Saved poster templates --------------------

def load_template_data():
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    if not TEMPLATES_FILE.exists():
        return {"templates": {}, "active": None}
    try:
        data = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid template data")
        data.setdefault("templates", {})
        data.setdefault("active", None)
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        log.exception("Could not read saved template data")
        return {"templates": {}, "active": None}


def save_template_data(data):
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = TEMPLATES_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp_file.replace(TEMPLATES_FILE)


def is_template_admin(update):
    if not ADMIN_USER_ID:
        return False
    user = update.effective_user
    return bool(user and str(user.id) == ADMIN_USER_ID)


def template_key(name):
    return re.sub(r"\s+", " ", name).strip().casefold()


def template_keyboard(action):
    data = load_template_data()
    rows = []
    for key, item in sorted(data["templates"].items(), key=lambda pair: pair[1]["name"].casefold()):
        token = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        rows.append([InlineKeyboardButton(item["name"], callback_data=f"template:{action}:{token}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def addtemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_template_admin(update):
        await update.effective_message.reply_text(
            "⛔ Template management is admin-only. Set ADMIN_USER_ID in Render Environment."
        )
        return
    name = " ".join(context.args).strip()
    if not name:
        context.user_data["template_state"] = "name"
        await update.effective_message.reply_text("Send a name for this template.\nSend /cancel to stop.")
        return
    await begin_template_image(update, context, name)


async def begin_template_image(update, context, name):
    if len(name) > 50:
        await update.effective_message.reply_text("Template names must be 50 characters or fewer.")
        return
    key = template_key(name)
    if not key:
        await update.effective_message.reply_text("Please send a valid template name.")
        return
    data = load_template_data()
    if key in data["templates"]:
        await update.effective_message.reply_text("That template name already exists. Choose another name or delete the old one first.")
        return
    context.user_data["template_pending_name"] = name.strip()
    context.user_data["template_state"] = "image"
    await update.effective_message.reply_text(
        f"🖼 Send the blank template image for **{name.strip()}** now.\n\n"
        "Use a 16:9 image with an empty artwork area on the left and space for title/details on the right.\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def template_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("template_state") != "image":
        return
    if not is_template_admin(update):
        return
    message = update.effective_message
    photo = message.photo[-1] if message.photo else None
    document = message.document if message.document and (message.document.mime_type or "").startswith("image/") else None
    if not photo and not document:
        await message.reply_text("Please send the template as a photo or image file.")
        return
    file_id = photo.file_id if photo else document.file_id
    try:
        tg_file = await context.bot.get_file(file_id)
        image_bytes = io.BytesIO()
        await tg_file.download_to_memory(out=image_bytes)
        image_bytes.seek(0)
        with Image.open(image_bytes) as check:
            check.verify()
        image_bytes.seek(0)
        with Image.open(image_bytes) as check:
            if check.width < 640 or check.height < 360:
                await message.reply_text("Image is too small. Please send at least 640×360 pixels.")
                return
            check.convert("RGB").save(image_bytes := io.BytesIO(), format="PNG", optimize=True)
        name = context.user_data.get("template_pending_name", "Template")
        key = template_key(name)
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        image_path = TEMPLATES_DIR / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:20] + ".png")
        image_bytes.seek(0)
        image_path.write_bytes(image_bytes.getvalue())
        data = load_template_data()
        data["templates"][key] = {"name": name, "file": image_path.name}
        save_template_data(data)
        context.user_data.pop("template_state", None)
        context.user_data.pop("template_pending_name", None)
        await message.reply_text(
            f"✅ Template **{name}** saved!\nUse /templates to view it and /settemplate to activate it.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as error:
        log.exception("Template upload failed")
        await message.reply_text(f"⚠️ Could not save that image: {error}")


async def templates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_template_data()
    if not data["templates"]:
        await update.effective_message.reply_text("No saved templates yet. Use /addtemplate to add one.")
        return
    active = data.get("active")
    lines = ["🎨 Saved templates:"]
    for key, item in sorted(data["templates"].items(), key=lambda pair: pair[1]["name"].casefold()):
        marker = " ⭐ ACTIVE" if key == active else ""
        lines.append(f"• {item['name']}{marker}")
    await update.effective_message.reply_text("\n".join(lines))


async def settemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_template_admin(update):
        await update.effective_message.reply_text("⛔ Only the bot admin can change the active template.")
        return
    data = load_template_data()
    if not data["templates"]:
        await update.effective_message.reply_text("No templates saved. Use /addtemplate first.")
        return
    name = " ".join(context.args).strip()
    if name:
        key = template_key(name)
        if key not in data["templates"]:
            await update.effective_message.reply_text("Template not found. Use /templates to see saved names.")
            return
        data["active"] = key
        save_template_data(data)
        await update.effective_message.reply_text(f"✅ Active template: {data['templates'][key]['name']}")
        return
    await update.effective_message.reply_text("Choose the template to activate:", reply_markup=template_keyboard("set"))


async def deletetemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_template_admin(update):
        await update.effective_message.reply_text("⛔ Only the bot admin can delete templates.")
        return
    data = load_template_data()
    if not data["templates"]:
        await update.effective_message.reply_text("No saved templates to delete.")
        return
    name = " ".join(context.args).strip()
    if name:
        await delete_template_by_key(update, context, template_key(name))
        return
    await update.effective_message.reply_text("Choose a template to delete:", reply_markup=template_keyboard("delete"))


async def delete_template_by_key(update, context, key):
    data = load_template_data()
    item = data["templates"].pop(key, None)
    if not item:
        await update.effective_message.reply_text("Template not found.")
        return
    image_path = TEMPLATES_DIR / item.get("file", "")
    if image_path.is_file() and image_path.parent == TEMPLATES_DIR:
        image_path.unlink(missing_ok=True)
    if data.get("active") == key:
        data["active"] = None
    save_template_data(data)
    await update.effective_message.reply_text(f"🗑 Deleted template: {item['name']}")


async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_template_admin(update):
        await query.answer("Admin only", show_alert=True)
        return
    await query.answer()
    _, action, token = query.data.split(":", 2)
    data = load_template_data()
    key = next((stored_key for stored_key in data["templates"] if hashlib.sha256(stored_key.encode("utf-8")).hexdigest()[:16] == token), None)
    if key is None:
        await query.edit_message_text("That template no longer exists.")
        return
    if action == "set":
        data["active"] = key
        save_template_data(data)
        await query.edit_message_text(f"✅ Active template set to: {data['templates'][key]['name']}")
    elif action == "delete":
        item = data["templates"].pop(key)
        image_path = TEMPLATES_DIR / item.get("file", "")
        if image_path.is_file() and image_path.parent == TEMPLATES_DIR:
            image_path.unlink(missing_ok=True)
        if data.get("active") == key:
            data["active"] = None
        save_template_data(data)
        await query.edit_message_text(f"🗑 Deleted template: {item['name']}")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("template_state", None)
    context.user_data.pop("template_pending_name", None)
    await update.effective_message.reply_text("Template setup cancelled.")


async def template_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("template_state") != "name":
        await text_fallback(update, context)
        return
    if not is_template_admin(update):
        await update.effective_message.reply_text("⛔ Only the bot admin can add templates.")
        return
    await begin_template_image(update, context, update.effective_message.text.strip())


async def setusername_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_template_admin(update):
        await update.effective_message.reply_text("⛔ Only the bot admin can change usernames. Set ADMIN_USER_ID in Render Environment.")
        return
    username = " ".join(context.args).strip()
    if not username:
        await update.effective_message.reply_text("Usage: /setusername @YourChannelUsername")
        return
    if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", username):
        await update.effective_message.reply_text("Please send a valid Telegram username, for example: /setusername @ANIFLIXANIMETAMIL")
        return
    if not username.startswith("@"): 
        username = "@" + username
    settings = load_bot_settings()
    settings["channel_username"] = username
    save_bot_settings(settings)
    await update.effective_message.reply_text(f"✅ Poster username updated to {username}")


async def setlinks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_template_admin(update):
        await update.effective_message.reply_text("⛔ Only the bot admin can change poster links.")
        return
    raw = " ".join(context.args).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 2 or not all(parts):
        await update.effective_message.reply_text("Usage:\n/setlinks https://t.me/WatchChannel | https://t.me/JoinChannel\nFirst link = Watch Now button; second link = Join Telegram button.")
        return
    if not all(re.match(r"^https://", part, re.I) for part in parts):
        await update.effective_message.reply_text("Both links must start with https://")
        return
    settings = load_bot_settings()
    settings["watch_now_url"], settings["join_telegram_url"] = parts
    save_bot_settings(settings)
    await update.effective_message.reply_text("✅ Watch Now and Join Telegram links updated.")


async def setbrand_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_template_admin(update):
        await update.effective_message.reply_text("⛔ Only the bot admin can change the poster brand text.")
        return
    brand = " ".join(context.args).strip()
    if not brand or len(brand) > 32:
        await update.effective_message.reply_text("Usage: /setbrand ANIFLIX INFINITY (up to 32 characters)")
        return
    settings = load_bot_settings()
    settings["brand_name"] = brand
    save_bot_settings(settings)
    await update.effective_message.reply_text(f"✅ Poster watermark text updated to: {brand}")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = load_bot_settings()
    await update.effective_message.reply_text(
        "⚙️ Current poster settings\n"
        f"Username: {settings['channel_username']}\n"
        f"Watch Now: {settings['watch_now_url']}\n"
        f"Join Telegram: {settings['join_telegram_url']}\n"
        f"Watermark: {settings['brand_name']}\n\n"
        "Admin commands: /setusername, /setlinks, /setbrand"
    )


# -------------------- Telegram commands --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎌 Welcome to ANIFLIX INFINITY Poster Bot!\n\n"
        "Create 16:9 (1920×1080) anime posters using the fixed ANIFLIX teal-and-black "
        "honeycomb design, AniList details, hexagonal anime collage, quality labels, "
        "and Download / Join buttons.\n\n"
        "Commands:\n"
        "/poster <anime name>\n"
        "/search <anime name>\n"
        "/synopsis <anime name>\n"
        "/addtemplate [name] (admin)\n"
        "/templates\n"
        "/settemplate [name] (admin)\n"
        "/deletetemplate [name] (admin)\n"
        "/setusername @username (admin)\n"
        "/setlinks watch_url | join_url (admin)\n"
        "/setbrand [text] (admin)\n"
        "/settings\n"
        "/help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Commands:\n"
        "/poster <anime name> — create a 16:9 poster\n"
        "/search <anime name> — search titles\n"
        "/synopsis <anime name> — get synopsis and metadata\n"
        "/addtemplate [name] — save a blank poster template (admin)\n"
        "/templates — list saved templates\n"
        "/settemplate [name] — select active template (admin)\n"
        "/deletetemplate [name] — delete a template (admin)\n"
        "/cancel — cancel template setup\n"
        "/setusername @username — edit displayed channel username (admin)\n"
        "/setlinks watch_url | join_url — edit poster buttons (admin)\n"
        "/setbrand [text] — edit watermark text (admin)\n"
        "/settings — show current poster settings\n"
        "/help — show commands\n\n"
        "Example: /poster Naruto Shippuden"
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "Usage: /poster <anime name>\nExample: /poster Solo Leveling"
        )
        return

    message = await update.effective_message.reply_text("🔎 Searching AniList…")
    try:
        items = search(query)
        if not items:
            await message.edit_text(
                "No poster results found. Try another spelling."
            )
            return

        context.user_data["results"] = {
            str(index): item for index, item in enumerate(items)
        }
        buttons = []
        for index, item in enumerate(items):
            label = item["title"] + (
                f" ({item['date'][:4]})" if item["date"] else ""
            )
            buttons.append([
                InlineKeyboardButton(
                    label[:58], callback_data=f"poster:{index}"
                )
            ])
        await message.edit_text(
            "Choose the correct title:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as error:
        log.exception("Search failed")
        await message.edit_text(f"⚠️ Search failed: {error}")


async def synopsis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "Usage: /synopsis <anime name>"
        )
        return

    try:
        items = search(query)
        if not items:
            await update.effective_message.reply_text(
                "No matching title found."
            )
            return

        data = details(items[0])
        message = f"📺 {data['title']}\n\n{data['overview'][:900]}"
        if data["year"]:
            message += f"\n\n📅 Year: {data['year']}"
        if data["rating"]:
            message += f"\n⭐ AniList rating: {float(data['rating']):.1f}/10"
        if data["genres"]:
            message += "\n🎭 Genres: " + ", ".join(data["genres"])
        settings = load_bot_settings()
        message += (
            f"\n\n🎞 {DEFAULT_QUALITY}\n"
            f"🔊 {DEFAULT_AUDIO}\n"
            f"✈️ Telegram Join → {settings['channel_username']}"
        )
        await update.effective_message.reply_text(message)
    except Exception as error:
        log.exception("Synopsis lookup failed")
        await update.effective_message.reply_text(
            f"⚠️ Could not retrieve synopsis: {error}"
        )


async def poster_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    index = query.data.split(":", 1)[1]
    item = context.user_data.get("results", {}).get(index)
    if not item:
        await query.edit_message_text(
            "Search expired. Please run /poster again."
        )
        return

    await query.edit_message_text(
        f"🎨 Creating 1920×1080 poster for {item['title']}…"
    )
    try:
        data = details(item)
        # Use the built-in ANIFLIX teal/black preset consistently for every poster.
        # Previously selected custom backgrounds are left saved but do not override it.
        image = make_poster(data, template_path=None)

        title = html.escape(data["title"])
        caption = f"🎌 <b>{title}</b>"
        if data["genres"]:
            caption += f"\n🎭 {html.escape(', '.join(data['genres']))}"
        if data["year"]:
            caption += f"\n📅 {html.escape(data['year'])}"
        if data["rating"]:
            caption += f"  ⭐ {float(data['rating']):.1f}/10"

        settings = load_bot_settings()
        quote = (
            f"🎞 {html.escape(DEFAULT_QUALITY)}\n"
            f"🔊 {html.escape(DEFAULT_AUDIO)}\n"
            f"✈️ Telegram Join → {html.escape(settings['channel_username'])}\n\n"
            f"{html.escape(settings['brand_name'])}"
        )
        caption += f"\n\n<blockquote expandable>{quote}</blockquote>"

        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=image,
            caption=caption[:1024],
            parse_mode=ParseMode.HTML,
            reply_markup=poster_keyboard(),
        )
        await query.message.reply_text(
            "✅ Poster ready! Genres are displayed directly below the anime "
            "title. The Watch Now and Join Telegram buttons below the image "
            "are clickable."
        )
    except Exception as error:
        log.exception("Poster creation failed")
        await query.message.reply_text(f"⚠️ Couldn't create poster: {error}")


async def text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Use /poster followed by an anime name.\n"
        "Example: /poster Naruto Shippuden"
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN in Render Environment.")
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("poster", search_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("synopsis", synopsis_cmd))
    app.add_handler(CommandHandler("addtemplate", addtemplate_cmd))
    app.add_handler(CommandHandler("templates", templates_cmd))
    app.add_handler(CommandHandler("settemplate", settemplate_cmd))
    app.add_handler(CommandHandler("deletetemplate", deletetemplate_cmd))
    app.add_handler(CommandHandler("setusername", setusername_cmd))
    app.add_handler(CommandHandler("setlinks", setlinks_cmd))
    app.add_handler(CommandHandler("setbrand", setbrand_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(template_callback, pattern=r"^template:(set|delete):"))
    app.add_handler(CallbackQueryHandler(poster_callback, pattern=r"^poster:\d+$"))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE), template_upload_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, template_name_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback))

    log.info("ANIFLIX INFINITY Poster Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()
