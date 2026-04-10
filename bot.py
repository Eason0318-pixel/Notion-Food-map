import os
import logging
import re
import unicodedata
import requests
import base64
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ── 環境變數 ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
NOTION_TOKEN    = os.environ["NOTION_TOKEN"]
NOTION_DB_ID    = os.environ["NOTION_DATABASE_ID"]
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")

# ── 特殊按鈕常數 ──────────────────────────────────────────
BTN_DONE         = "✅ 完成選擇"
BTN_UNDO         = "🗑️ 清除上一個"
BTN_BACK         = "↩️ 返回上一步"
BTN_CANCEL       = "❌ 取消本次動作"
BTN_SKIP         = "⏭️ 略過"
BTN_CONFIRM      = "✅ 確認儲存"
BTN_UPLOAD_IMG   = "📸 上傳截圖由 AI 辨識"
BTN_ADD_COUNTY   = "➕ 新增縣市"
BTN_ADD_DISTRICT = "➕ 新增市區"
BTN_ADD_TYPE     = "➕ 新增種類"
PLACEHOLDER      = "（待新增）"

# ── 狀態常數 ─────────────────────────────────────────────
ST_WAIT_LINK       = 1
ST_INPUT_NAME      = 2
ST_WAIT_PHOTO      = 3
ST_SELECT_COUNTY   = 4
ST_INPUT_NEW_COUNTY = 5
ST_SELECT_DISTRICT = 6
ST_INPUT_NEW_DISTRICT = 7
ST_SELECT_TYPE     = 8
ST_INPUT_NEW_TYPE  = 9
ST_CONFIRM_SIMILAR = 10   # ← 新增：相似選項確認狀態
ST_INPUT_HOURS     = 11
ST_INPUT_FEATURE   = 12
ST_INPUT_REVIEW    = 13
ST_FINAL_CONFIRM   = 14

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 全域選項快取 ──────────────────────────────────────────
GLOBAL_COUNTY   = ["台南", "高雄"]
GLOBAL_DISTRICT = ["中西區", "東區", "永康區", "南區", "三民區", "北區"]
GLOBAL_TYPE     = [
    "甜點", "義式料理", "泡芙", "燒肉", "牛舌", "焗烤", "火鍋", "拉麵",
    "鴨肉飯", "印度咖喱", "烤餅", "丼飯", "早午餐", "拼盤", "鐵鍋燉",
    "歐姆蛋包飯", "泰式", "牛排", "四川麻辣", "和牛", "泰式料理",
    "麻辣鍋", "手搖", "壽喜燒"
]

def normalize(text: str) -> str:
    """標準化文字：去除空白、轉全形為半形、轉小寫"""
    text = text.strip()
    text = unicodedata.normalize("NFKC", text)
    return text.lower()

def find_similar(new_text: str, options: list) -> list:
    """
    方案B：找出相似選項
    - 完全相符（normalize後）→ 回傳該選項（完全相符flag）
    - 相似（其中一個包含另一個，或共用2個以上字符）→ 回傳相似清單
    """
    norm_new = normalize(new_text)
    exact = []
    similar = []

    for opt in options:
        norm_opt = normalize(opt)
        if norm_new == norm_opt:
            exact.append(opt)
        elif norm_new in norm_opt or norm_opt in norm_new:
            similar.append(opt)
        else:
            # 計算共用字符數（中文字）
            common_chars = sum(1 for c in norm_new if c in norm_opt)
            if common_chars >= 2:
                similar.append(opt)

    return exact, similar

# ── 從 Notion 載入選項 ────────────────────────────────────
def load_options_from_notion():
    global GLOBAL_COUNTY, GLOBAL_DISTRICT, GLOBAL_TYPE
    try:
        r = requests.get(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28"
            },
            timeout=15
        )
        r.raise_for_status()
        props = r.json().get("properties", {})

        county   = [o["name"] for o in props.get("縣市", {}).get("select", {}).get("options", [])]
        district = [o["name"] for o in props.get("市區", {}).get("select", {}).get("options", [])]
        types    = [o["name"] for o in props.get("種類", {}).get("multi_select", {}).get("options", [])]

        if county:   GLOBAL_COUNTY   = county
        if district: GLOBAL_DISTRICT = district
        if types:    GLOBAL_TYPE     = types
        logger.info("Notion 選項載入完成")
    except Exception as e:
        logger.error(f"載入 Notion 選項失敗: {e}")

# ── YouTube 標題抓取 ──────────────────────────────────────
def extract_yt_id(url: str):
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]+)", url)
    if m: return m.group(1)
    m = re.search(r"youtube\.com/watch.*v=([A-Za-z0-9_-]+)", url)
    if m: return m.group(1)
    return None

def fetch_youtube_title(url: str) -> str:
    vid = extract_yt_id(url)
    if not vid or not YOUTUBE_API_KEY:
        return ""
    try:
        r = requests.get(
            f"https://www.googleapis.com/youtube/v3/videos?part=snippet&id={vid}&key={YOUTUBE_API_KEY}",
            timeout=15
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items: return ""
        return items[0]["snippet"].get("title", "")
    except Exception as e:
        logger.error(f"YouTube 失敗: {e}")
        return ""

# ── Gemini Vision 截圖辨識 ────────────────────────────────
def gemini_recognize_name(photo_bytes: bytes) -> str:
    if not GEMINI_API_KEY:
        return ""
    try:
        b64 = base64.b64encode(photo_bytes).decode()
        prompt = (
            "這是一張餐廳或美食相關的貼文截圖，請幫我辨識餐廳名稱。"
            "請依序檢查：1.引號、書名號、括號標示的文字 2.hashtag中的店名 "
            "3.貼文開頭或結尾的店名標示 4.圖片上的店招牌或Logo 5.內文中像店名的專有名詞。"
            "請只回傳餐廳名稱，不要其他說明。"
        )
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                        {"text": prompt}
                    ]
                }]
            },
            timeout=30
        )
        r.raise_for_status()
        candidates = r.json().get("candidates", [])
        if candidates:
            return candidates[0]["content"]["parts"][0].get("text", "").strip()
    except Exception as e:
        logger.error(f"Gemini 失敗: {e}")
    return ""

# ── 寫入 Notion ───────────────────────────────────────────
def write_notion(name, url, county, district, types, hours, feature, review) -> bool:
    try:
        props = {
            "Name": {"title": [{"type": "text", "text": {"content": name}}]},
            "縣市": {"select": {"name": county}} if county else {},
            "市區": {"select": {"name": district}} if district else {},
            "種類": {"multi_select": [{"name": t} for t in types]},
        }
        if hours:   props["營業時間"] = {"rich_text": [{"text": {"content": hours}}]}
        if feature: props["特色"]    = {"rich_text": [{"text": {"content": feature}}]}
        if review:  props["評價"]    = {"rich_text": [{"text": {"content": review}}]}
        # URL 存入頁面 icon 備用欄位（如有 URL 欄位可改這裡）
        # 這裡把連結加到 Name 的 link
        if url:
            props["Name"] = {"title": [{"type": "text", "text": {"content": name, "link": {"url": url}}}]}

        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28"
            },
            json={
                "parent": {"database_id": NOTION_DB_ID},
                "properties": props
            },
            timeout=15
        )
        r.raise_for_status()
        load_options_from_notion()
        return True
    except Exception as e:
        logger.error(f"Notion 失敗: {e}")
        return False

# ── 鍵盤工具函式 ──────────────────────────────────────────
def _pair_rows(opts):
    padded = opts[:]
    if len(padded) % 2 != 0:
        padded.append(PLACEHOLDER)
    rows = []
    for i in range(0, len(padded), 2):
        rows.append(padded[i:i+2])
    return rows

def make_single_kb(opts, add_btn):
    rows = [[BTN_BACK]]
    rows += _pair_rows(opts)
    rows.append([add_btn])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def make_multi_kb(opts, selected, add_btn):
    rows = []
    if selected:
        rows.append([BTN_DONE])
    rows.append([BTN_UNDO, BTN_BACK])
    rows += _pair_rows(opts)
    rows.append([add_btn])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def make_confirm_kb():
    return ReplyKeyboardMarkup(
        [[BTN_CONFIRM], [BTN_BACK, BTN_CANCEL]],
        one_time_keyboard=True, resize_keyboard=True
    )

def make_skip_kb():
    return ReplyKeyboardMarkup(
        [[BTN_SKIP], [BTN_BACK, BTN_CANCEL]],
        one_time_keyboard=True, resize_keyboard=True
    )

def make_name_kb():
    return ReplyKeyboardMarkup(
        [[BTN_UPLOAD_IMG], [BTN_SKIP, BTN_CANCEL]],
        one_time_keyboard=True, resize_keyboard=True
    )

# ── 顯示各步驟 ────────────────────────────────────────────
def get_st(ctx):    return ctx.user_data.get("st", ST_WAIT_LINK)
def set_st(ctx, s): ctx.user_data["st"] = s

async def show_name(update, ctx):
    set_st(ctx, ST_INPUT_NAME)
    await update.message.reply_text(
        "🍽️ 請輸入餐廳名稱：\n（或上傳貼文截圖讓 AI 辨識，也可略過）",
        reply_markup=make_name_kb()
    )

async def show_county(update, ctx):
    set_st(ctx, ST_SELECT_COUNTY)
    opts = list(GLOBAL_COUNTY)
    await update.message.reply_text(
        "🗺️ 請選擇縣市：",
        reply_markup=make_single_kb(opts, BTN_ADD_COUNTY)
    )

async def show_district(update, ctx):
    set_st(ctx, ST_SELECT_DISTRICT)
    opts = list(GLOBAL_DISTRICT)
    await update.message.reply_text(
        "📍 請選擇市區：",
        reply_markup=make_single_kb(opts, BTN_ADD_DISTRICT)
    )

async def show_type(update, ctx):
    set_st(ctx, ST_SELECT_TYPE)
    sel = ctx.user_data.get("sel_types", [])
    all_types = list(GLOBAL_TYPE)
    for t in ctx.user_data.get("extra_types", []):
        if t not in all_types:
            all_types.append(t)
    rem = [t for t in all_types if t not in sel]
    sel_text = "、".join(sel) if sel else "（尚未選擇）"
    await update.message.reply_text(
        f"🍜 請選擇料理種類（可多選）\n已選：{sel_text}",
        reply_markup=make_multi_kb(rem, sel, BTN_ADD_TYPE)
    )

async def show_hours(update, ctx):
    set_st(ctx, ST_INPUT_HOURS)
    await update.message.reply_text(
        "🕐 請輸入營業時間：\n（例：11:00–21:00，週一公休）",
        reply_markup=make_skip_kb()
    )

async def show_feature(update, ctx):
    set_st(ctx, ST_INPUT_FEATURE)
    await update.message.reply_text(
        "✨ 請輸入餐廳特色：",
        reply_markup=make_skip_kb()
    )

async def show_review(update, ctx):
    set_st(ctx, ST_INPUT_REVIEW)
    await update.message.reply_text(
        "⭐ 請輸入你的評價：",
        reply_markup=make_skip_kb()
    )

async def show_confirm(update, ctx):
    set_st(ctx, ST_FINAL_CONFIRM)
    d = ctx.user_data
    url_line = f"\n🔗 連結：{d.get('url','')}" if d.get('url') else ""
    types_text = "、".join(d.get("sel_types", [])) or "（未選）"
    await update.message.reply_text(
        f"📋 請確認以下資料：\n\n"
        f"🍽️ 名稱：{d.get('name','（未填）')}{url_line}\n"
        f"🗺️ 縣市：{d.get('county','（未選）')}\n"
        f"📍 市區：{d.get('district','（未選）')}\n"
        f"🍜 種類：{types_text}\n"
        f"🕐 營業時間：{d.get('hours','（略過）')}\n"
        f"✨ 特色：{d.get('feature','（略過）')}\n"
        f"⭐ 評價：{d.get('review','（略過）')}\n\n"
        f"確認存入 Notion 嗎？",
        reply_markup=make_confirm_kb()
    )

async def do_cancel(update, ctx):
    ctx.user_data.clear()
    set_st(ctx, ST_WAIT_LINK)
    await update.message.reply_text("已取消本次動作。", reply_markup=ReplyKeyboardRemove())

# ── 主訊息處理 ────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    st   = get_st(ctx)

    if text == BTN_CANCEL:
        await do_cancel(update, ctx)
        return

    # ── 等待連結 ──
    if st == ST_WAIT_LINK:
        if re.search(r"https?://", text, re.I):
            ctx.user_data["url"] = text
            ctx.user_data["sel_types"] = []
            ctx.user_data["extra_types"] = []
            if re.search(r"youtube\.com/watch|youtu\.be/", text, re.I):
                await update.message.reply_text("▶️ YouTube 正在抓取標題...")
                title = fetch_youtube_title(text)
                ctx.user_data["name"] = title
                msg = f"找到影片：{title}" if title else "連結已記住（未能抓到標題）"
                await update.message.reply_text(msg)
            else:
                await update.message.reply_text("✅ 連結已記住！")
            await show_name(update, ctx)
        else:
            await update.message.reply_text("請傳給我一個連結（IG / Threads / YouTube 等）")

    # ── 輸入名稱 ──
    elif st == ST_INPUT_NAME:
        if text == BTN_UPLOAD_IMG:
            set_st(ctx, ST_WAIT_PHOTO)
            await update.message.reply_text(
                "📸 請上傳截圖，AI 將幫你辨識餐廳名稱：",
                reply_markup=ReplyKeyboardRemove()
            )
        elif text == BTN_SKIP:
            ctx.user_data["name"] = ctx.user_data.get("name", "")
            await show_county(update, ctx)
        else:
            ctx.user_data["name"] = text
            await show_county(update, ctx)

    # ── 等待截圖 ──
    elif st == ST_WAIT_PHOTO:
        await update.message.reply_text("請上傳「圖片」，不是文字訊息喔～")

    # ── 選縣市 ──
    elif st == ST_SELECT_COUNTY:
        if text == BTN_BACK:
            await show_name(update, ctx)
        elif text == BTN_ADD_COUNTY:
            set_st(ctx, ST_INPUT_NEW_COUNTY)
            await update.message.reply_text("✏️ 請輸入新縣市名稱：", reply_markup=make_skip_kb())
        elif text == PLACEHOLDER:
            await update.message.reply_text("請從現有選項選擇，或點「➕ 新增縣市」")
            await show_county(update, ctx)
        elif text in GLOBAL_COUNTY:
            ctx.user_data["county"] = text
            await show_district(update, ctx)
        else:
            await show_county(update, ctx)

    # ── 新增縣市 ──
    elif st == ST_INPUT_NEW_COUNTY:
        if text == BTN_BACK:
            await show_county(update, ctx)
        else:
            if text not in GLOBAL_COUNTY:
                GLOBAL_COUNTY.append(text)
            ctx.user_data["county"] = text
            await update.message.reply_text(f"✅ 已新增縣市「{text}」")
            await show_district(update, ctx)

    # ── 選市區 ──
    elif st == ST_SELECT_DISTRICT:
        if text == BTN_BACK:
            await show_county(update, ctx)
        elif text == BTN_ADD_DISTRICT:
            set_st(ctx, ST_INPUT_NEW_DISTRICT)
            await update.message.reply_text("✏️ 請輸入新市區名稱：", reply_markup=make_skip_kb())
        elif text == PLACEHOLDER:
            await update.message.reply_text("請從現有選項選擇，或點「➕ 新增市區」")
            await show_district(update, ctx)
        elif text in GLOBAL_DISTRICT:
            ctx.user_data["district"] = text
            await show_type(update, ctx)
        else:
            await show_district(update, ctx)

    # ── 新增市區 ──
    elif st == ST_INPUT_NEW_DISTRICT:
        if text == BTN_BACK:
            await show_district(update, ctx)
        else:
            if text not in GLOBAL_DISTRICT:
                GLOBAL_DISTRICT.append(text)
            ctx.user_data["district"] = text
            await update.message.reply_text(f"✅ 已新增市區「{text}」")
            await show_type(update, ctx)

    # ── 選種類（多選）──
    elif st == ST_SELECT_TYPE:
        if text == BTN_DONE:
            if not ctx.user_data.get("sel_types"):
                await update.message.reply_text("⚠️ 請至少選一個種類！")
                await show_type(update, ctx)
            else:
                await show_hours(update, ctx)
        elif text == BTN_UNDO:
            sel = ctx.user_data.get("sel_types", [])
            if sel:
                removed = sel.pop()
                ctx.user_data["sel_types"] = sel
                await update.message.reply_text(f"已移除「{removed}」")
            else:
                await update.message.reply_text("目前尚無已選種類。")
            await show_type(update, ctx)
        elif text == BTN_BACK:
            await show_district(update, ctx)
        elif text == BTN_ADD_TYPE:
            set_st(ctx, ST_INPUT_NEW_TYPE)
            await update.message.reply_text(
                "✏️ 請輸入新種類名稱：",
                reply_markup=ReplyKeyboardMarkup([[BTN_BACK, BTN_CANCEL]], resize_keyboard=True)
            )
        elif text == PLACEHOLDER:
            await update.message.reply_text("請從現有選項選擇，或點「➕ 新增種類」")
            await show_type(update, ctx)
        else:
            all_types = list(GLOBAL_TYPE) + ctx.user_data.get("extra_types", [])
            if text in all_types and text not in ctx.user_data.get("sel_types", []):
                ctx.user_data.setdefault("sel_types", []).append(text)
            await show_type(update, ctx)

    # ── 輸入新種類（方案B在這裡發動）──
    elif st == ST_INPUT_NEW_TYPE:
        if text == BTN_BACK:
            await show_type(update, ctx)
        else:
            all_types = list(GLOBAL_TYPE) + ctx.user_data.get("extra_types", [])
            exact, similar = find_similar(text, all_types)

            if exact:
                # 完全相符 → 自動選取，不新增
                matched = exact[0]
                if matched not in ctx.user_data.get("sel_types", []):
                    ctx.user_data.setdefault("sel_types", []).append(matched)
                await update.message.reply_text(
                    f"⚠️ 「{text}」與現有選項「{matched}」完全相同，已自動選取，未新增重複選項。"
                )
                await show_type(update, ctx)

            elif similar:
                # 有相似選項 → 列出讓使用者確認
                ctx.user_data["pending_new_type"] = text
                ctx.user_data["similar_types"] = similar
                set_st(ctx, ST_CONFIRM_SIMILAR)

                # 建立相似選項鍵盤
                number_emoji = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]
                rows = []
                similar_lines = []
                for i, s in enumerate(similar[:5]):
                    label = f"{number_emoji[i]} {s}"
                    rows.append([label])
                    similar_lines.append(f"  {number_emoji[i]} {s}")
                rows.append([f"➕ 確認新增「{text}」"])
                rows.append([BTN_CANCEL])
                kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

                similar_text = "\n".join(similar_lines)
                await update.message.reply_text(
                    f"⚠️ 找到以下相似選項，請確認：\n{similar_text}\n\n"
                    f"請選擇其中一個，或點下方按鈕確認新增「{text}」：",
                    reply_markup=kb
                )
            else:
                # 無相似 → 直接新增
                ctx.user_data.setdefault("extra_types", []).append(text)
                ctx.user_data.setdefault("sel_types", []).append(text)
                await update.message.reply_text(f"✅ 已新增種類「{text}」")
                await show_type(update, ctx)

    # ── 確認相似選項（方案B的回應）──
    elif st == ST_CONFIRM_SIMILAR:
        pending = ctx.user_data.get("pending_new_type", "")
        similar = ctx.user_data.get("similar_types", [])
        confirm_btn = f"➕ 確認新增「{pending}」"

        if text == confirm_btn:
            # 確認新增
            ctx.user_data.setdefault("extra_types", []).append(pending)
            ctx.user_data.setdefault("sel_types", []).append(pending)
            ctx.user_data.pop("pending_new_type", None)
            ctx.user_data.pop("similar_types", None)
            await update.message.reply_text(f"✅ 已新增種類「{pending}」")
            await show_type(update, ctx)
        else:
            # 嘗試比對使用者選的相似選項按鈕
            number_emoji = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]
            chosen = None
            for i, s in enumerate(similar[:5]):
                if text == f"{number_emoji[i]} {s}":
                    chosen = s
                    break
            if chosen:
                if chosen not in ctx.user_data.get("sel_types", []):
                    ctx.user_data.setdefault("sel_types", []).append(chosen)
                ctx.user_data.pop("pending_new_type", None)
                ctx.user_data.pop("similar_types", None)
                await update.message.reply_text(f"✅ 已選取「{chosen}」")
                await show_type(update, ctx)
            else:
                await update.message.reply_text("請點選上方按鈕選擇，或確認新增。")

    # ── 輸入營業時間 ──
    elif st == ST_INPUT_HOURS:
        if text == BTN_BACK:
            await show_type(update, ctx)
        elif text == BTN_SKIP:
            ctx.user_data["hours"] = ""
            await show_feature(update, ctx)
        else:
            ctx.user_data["hours"] = text
            await show_feature(update, ctx)

    # ── 輸入特色 ──
    elif st == ST_INPUT_FEATURE:
        if text == BTN_BACK:
            await show_hours(update, ctx)
        elif text == BTN_SKIP:
            ctx.user_data["feature"] = ""
            await show_review(update, ctx)
        else:
            ctx.user_data["feature"] = text
            await show_review(update, ctx)

    # ── 輸入評價 ──
    elif st == ST_INPUT_REVIEW:
        if text == BTN_BACK:
            await show_feature(update, ctx)
        elif text == BTN_SKIP:
            ctx.user_data["review"] = ""
            await show_confirm(update, ctx)
        else:
            ctx.user_data["review"] = text
            await show_confirm(update, ctx)

    # ── 最終確認 ──
    elif st == ST_FINAL_CONFIRM:
        if text == BTN_CONFIRM:
            await update.message.reply_text("⏳ 寫入 Notion 中...")
            d = ctx.user_data
            ok = write_notion(
                d.get("name", ""),
                d.get("url", ""),
                d.get("county", ""),
                d.get("district", ""),
                d.get("sel_types", []),
                d.get("hours", ""),
                d.get("feature", ""),
                d.get("review", "")
            )
            if ok:
                await update.message.reply_text(
                    "🎉 成功存入 Notion！下一間傳過來就好 👋",
                    reply_markup=ReplyKeyboardRemove()
                )
            else:
                await update.message.reply_text(
                    "❌ 寫入失敗，請確認 NOTION_TOKEN 是否正確。",
                    reply_markup=ReplyKeyboardRemove()
                )
            ctx.user_data.clear()
            set_st(ctx, ST_WAIT_LINK)
        elif text == BTN_BACK:
            await show_review(update, ctx)
        else:
            await show_confirm(update, ctx)

    else:
        ctx.user_data.clear()
        set_st(ctx, ST_WAIT_LINK)
        await update.message.reply_text("已重置。請重新傳連結給我：", reply_markup=ReplyKeyboardRemove())

# ── 處理照片（Gemini 截圖辨識）────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = get_st(ctx)
    if st != ST_WAIT_PHOTO:
        await update.message.reply_text("請先傳連結給我，再開始流程。")
        return

    await update.message.reply_text("🔍 AI 辨識中，請稍候...")
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()
    name = gemini_recognize_name(bytes(photo_bytes))

    if name:
        ctx.user_data["name"] = name
        await update.message.reply_text(f"✅ 辨識結果：「{name}」\n\n若正確請繼續，若要修改請重新輸入文字。")
    else:
        await update.message.reply_text("⚠️ 無法辨識，請手動輸入餐廳名稱：")
        await show_name(update, ctx)
        return

    await show_county(update, ctx)

# ── 指令 ─────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    set_st(ctx, ST_WAIT_LINK)
    load_options_from_notion()
    await update.message.reply_text(
        "👋 嗨！我是你的美食地圖收藏助手。\n\n"
        "傳給我任何餐廳的連結：\n"
        "• IG / Threads / 其他連結 → 直接填資料\n"
        "• YouTube 連結 → 自動抓標題",
        reply_markup=ReplyKeyboardRemove()
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await do_cancel(update, ctx)

def main():
    load_options_from_notion()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    logger.info("Bot v15 starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
