import os
import logging
import requests
import base64
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ── 環境變數 ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
NOTION_TOKEN    = os.environ["NOTION_TOKEN"]
NOTION_DB_ID    = os.environ["NOTION_DATABASE_ID"]
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")

# ── 日誌 ─────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Notion API Header ─────────────────────────────────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

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

# ── 對話狀態 ──────────────────────────────────────────────
(
    ASK_NAME,
    ASK_NAME_PHOTO,       # 等待截圖上傳
    ASK_NAME_CONFIRM,     # Gemini 建議名稱後確認
    ASK_COUNTY,
    ASK_COUNTY_NEW,
    ASK_DISTRICT,
    ASK_DISTRICT_NEW,
    ASK_TYPE,
    ASK_TYPE_NEW,
    ASK_HOURS,
    ASK_FEATURE,
    ASK_RATING,
    ASK_URL,
    CONFIRM,
) = range(14)

# ── 動態讀取 Notion 選項 ───────────────────────────────────
def fetch_notion_options():
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}"
    try:
        resp = requests.get(url, headers=NOTION_HEADERS, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"讀取 Notion 欄位失敗: {resp.text}")
            return [], [], []
        props = resp.json().get("properties", {})
        county_opts   = [o["name"] for o in props.get("縣市", {}).get("select",       {}).get("options", [])]
        district_opts = [o["name"] for o in props.get("市區", {}).get("select",       {}).get("options", [])]
        type_opts     = [o["name"] for o in props.get("種類", {}).get("multi_select", {}).get("options", [])]
        return county_opts, district_opts, type_opts
    except Exception as e:
        logger.error(f"fetch_notion_options 錯誤: {e}")
        return [], [], []

# ── YouTube 標題抓取 ───────────────────────────────────────
def get_youtube_title(url: str) -> str:
    if not YOUTUBE_API_KEY:
        return ""
    import re
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url)
    if not match:
        return ""
    video_id = match.group(1)
    api_url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet&id={video_id}&key={YOUTUBE_API_KEY}"
    )
    try:
        r = requests.get(api_url, timeout=5)
        items = r.json().get("items", [])
        if items:
            return items[0]["snippet"]["title"]
    except Exception as e:
        logger.warning(f"YouTube API 錯誤: {e}")
    return ""

# ── Gemini Vision 圖片辨識 ────────────────────────────────
def analyze_image_with_gemini(image_bytes: bytes) -> str:
    """
    將圖片傳給 Gemini Vision，請它從截圖中辨識餐廳名稱。
    回傳建議的餐廳名稱字串，失敗則回傳空字串。
    """
    if not GEMINI_API_KEY:
        return ""
    try:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {
            "contents": [{
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": image_b64
                        }
                    },
                    {
                        "text": (
                            "這是一張餐廳貼文的截圖。"
                            "請從圖片中辨識餐廳名稱，只回傳餐廳名稱本身，"
                            "不要加任何說明文字。"
                            "如果無法辨識餐廳名稱，只回傳「無法辨識」。"
                        )
                    }
                ]
            }]
        }
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            name = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            if name == "無法辨識":
                return ""
            return name
        else:
            logger.error(f"Gemini API 錯誤: {resp.text}")
            return ""
    except Exception as e:
        logger.error(f"analyze_image_with_gemini 錯誤: {e}")
        return ""

# ── 寫入 Notion ────────────────────────────────────────────
def save_to_notion(data: dict) -> bool:
    url = "https://api.notion.com/v1/pages"
    properties = {
        "Name": {
            "title": [{"text": {"content": data.get("名稱", "未命名")}}]
        },
        "種類": {
            "multi_select": [{"name": t} for t in data.get("種類", [])]
        },
        "營業時間": {
            "rich_text": [{"text": {"content": data.get("營業時間", "")}}]
        },
        "特色": {
            "rich_text": [{"text": {"content": data.get("特色", "")}}]
        },
        "評價": {
            "rich_text": [{"text": {"content": data.get("評價", "")}}]
        },
    }
    if data.get("縣市"):
        properties["縣市"] = {"select": {"name": data["縣市"]}}
    if data.get("市區"):
        properties["市區"] = {"select": {"name": data["市區"]}}
    link = data.get("連結", "")
    if link:
        properties["Name"]["title"][0]["text"]["link"] = {"url": link}
    body = {"parent": {"database_id": NOTION_DB_ID}, "properties": properties}
    try:
        resp = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=10)
        if resp.status_code == 200:
            logger.info("成功寫入 Notion")
            return True
        else:
            logger.error(f"Notion 寫入失敗: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"save_to_notion 錯誤: {e}")
        return False

# ══════════════════════════════════════════════════════════
# 鍵盤產生器
# ══════════════════════════════════════════════════════════

def _opts_rows(opts: list) -> list:
    """每排兩個，單數補 PLACEHOLDER 佔位"""
    padded = opts[:]
    if len(padded) % 2 != 0:
        padded.append(PLACEHOLDER)
    rows = []
    for i in range(0, len(padded), 2):
        rows.append(padded[i:i+2])
    return rows

def name_keyboard() -> ReplyKeyboardMarkup:
    """
    [ 📸 上傳截圖由 AI 辨識 ]
    [ ⏭️ 略過 ]  [ ❌ 取消本次動作 ]
    """
    rows = [
        [BTN_UPLOAD_IMG],
        [BTN_SKIP, BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def name_confirm_keyboard() -> ReplyKeyboardMarkup:
    """Gemini 建議名稱後的確認鍵盤"""
    rows = [
        [BTN_CONFIRM],
        [BTN_SKIP, BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def county_keyboard(county_opts: list) -> ReplyKeyboardMarkup:
    rows = [[BTN_BACK]]
    rows += _opts_rows(county_opts)
    rows.append([BTN_ADD_COUNTY])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def district_keyboard(district_opts: list) -> ReplyKeyboardMarkup:
    rows = [[BTN_BACK]]
    rows += _opts_rows(district_opts)
    rows.append([BTN_ADD_DISTRICT])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def type_keyboard(type_opts: list, has_selection: bool) -> ReplyKeyboardMarkup:
    rows = []
    if has_selection:
        rows.append([BTN_DONE])
    rows.append([BTN_UNDO, BTN_BACK])
    rows += _opts_rows(type_opts)
    rows.append([BTN_ADD_TYPE])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def text_input_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_SKIP],
        [BTN_BACK, BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def confirm_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_CONFIRM],
        [BTN_BACK, BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

# ── 工具函數 ──────────────────────────────────────────────
def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")

async def do_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "已取消本次動作。直接傳連結或輸入名稱可重新開始。",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def _load_notion_and_go_county(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """讀取 Notion 選項後跳往縣市選擇"""
    county_opts, district_opts, type_opts = fetch_notion_options()
    context.user_data["_county_opts"]   = county_opts
    context.user_data["_district_opts"] = district_opts
    context.user_data["_type_opts"]     = type_opts
    name = context.user_data.get("名稱", "未命名")
    await update.message.reply_text(
        f"已記錄：{name}\n\n請選擇縣市：",
        reply_markup=county_keyboard(county_opts)
    )
    return ASK_COUNTY

# ══════════════════════════════════════════════════════════
# 對話流程
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "敏的美食地圖 Bot！\n\n直接傳入餐廳連結，或輸入餐廳名稱開始：",
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_NAME


async def receive_url_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """偵測到網址，自動啟動流程"""
    context.user_data.clear()
    text = update.message.text.strip()
    context.user_data["連結"] = text

    if "youtube.com" in text or "youtu.be" in text:
        title = get_youtube_title(text)
        if title:
            context.user_data["名稱"] = title
            await update.message.reply_text(
                f"偵測到 YouTube！已自動抓取標題：{title}\n\n"
                "若想修改名稱請輸入新名稱，否則點「略過」繼續：",
                reply_markup=name_keyboard()
            )
        else:
            await update.message.reply_text(
                "偵測到 YouTube 連結，請輸入餐廳名稱：",
                reply_markup=name_keyboard()
            )
    else:
        await update.message.reply_text(
            "已收到連結！\n請輸入餐廳名稱，或選擇下方選項：",
            reply_markup=name_keyboard()
        )
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_CANCEL:
        return await do_cancel(update, context)

    if text == BTN_UPLOAD_IMG:
        await update.message.reply_text(
            "請上傳餐廳貼文的截圖，AI 將自動辨識餐廳名稱：",
            reply_markup=ReplyKeyboardRemove()
        )
        return ASK_NAME_PHOTO

    if is_url(text):
        context.user_data["連結"] = text
        if "youtube.com" in text or "youtu.be" in text:
            title = get_youtube_title(text)
            if title:
                context.user_data["名稱"] = title
        await update.message.reply_text(
            "已記錄連結！\n請輸入餐廳名稱，或選擇下方選項：",
            reply_markup=name_keyboard()
        )
        return ASK_NAME

    if text in (BTN_SKIP, "略過"):
        if not context.user_data.get("名稱"):
            context.user_data["名稱"] = "未命名"
    else:
        context.user_data["名稱"] = text

    return await _load_notion_and_go_county(update, context)


async def ask_name_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """等待截圖上傳，收到圖片後丟給 Gemini 分析"""

    # 如果使用者傳的是文字而不是圖片
    if not update.message.photo:
        await update.message.reply_text(
            "請直接傳送圖片截圖（不是連結），或點「取消」返回：",
            reply_markup=ReplyKeyboardMarkup(
                [[BTN_CANCEL]], one_time_keyboard=True, resize_keyboard=True
            )
        )
        return ASK_NAME_PHOTO

    await update.message.reply_text("正在分析截圖，請稍候...")

    # 取最高畫質的圖片
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    suggested_name = analyze_image_with_gemini(bytes(image_bytes))

    if suggested_name:
        context.user_data["_gemini_suggestion"] = suggested_name
        await update.message.reply_text(
            f"AI 辨識結果：\n\n「{suggested_name}」\n\n"
            "點「✅ 確認儲存」使用此名稱，或直接輸入修改後的名稱：",
            reply_markup=name_confirm_keyboard()
        )
        return ASK_NAME_CONFIRM
    else:
        await update.message.reply_text(
            "AI 無法從截圖中辨識餐廳名稱，請手動輸入：",
            reply_markup=name_keyboard()
        )
        return ASK_NAME


async def ask_name_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用者確認或修改 Gemini 建議的名稱"""
    text = update.message.text.strip()

    if text == BTN_CANCEL:
        return await do_cancel(update, context)

    if text == BTN_CONFIRM:
        # 使用 Gemini 建議的名稱
        context.user_data["名稱"] = context.user_data.get("_gemini_suggestion", "未命名")
    elif text in (BTN_SKIP, "略過"):
        context.user_data["名稱"] = "未命名"
    else:
        # 使用者手動輸入修改後的名稱
        context.user_data["名稱"] = text

    return await _load_notion_and_go_county(update, context)


async def ask_county(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "請重新輸入餐廳名稱，或選擇下方選項：",
            reply_markup=name_keyboard()
        )
        return ASK_NAME
    if text == BTN_ADD_COUNTY:
        await update.message.reply_text("請輸入新的縣市名稱：", reply_markup=ReplyKeyboardRemove())
        return ASK_COUNTY_NEW

    context.user_data["縣市"] = text
    return await _go_to_district(update, context)


async def ask_county_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    context.user_data["縣市"] = text
    await update.message.reply_text(f"已新增縣市：{text}")
    return await _go_to_district(update, context)


async def _go_to_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    district_opts = context.user_data.get("_district_opts", [])
    await update.message.reply_text(
        "請選擇市區：",
        reply_markup=district_keyboard(district_opts)
    )
    return ASK_DISTRICT


async def ask_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    county_opts = context.user_data.get("_county_opts", [])

    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "請重新選擇縣市：",
            reply_markup=county_keyboard(county_opts)
        )
        return ASK_COUNTY
    if text == BTN_ADD_DISTRICT:
        await update.message.reply_text("請輸入新的市區名稱：", reply_markup=ReplyKeyboardRemove())
        return ASK_DISTRICT_NEW

    context.user_data["市區"] = text
    return await _go_to_type(update, context)


async def ask_district_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    context.user_data["市區"] = text
    await update.message.reply_text(f"已新增市區：{text}")
    return await _go_to_type(update, context)


async def _go_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["種類"] = []
    type_opts = context.user_data.get("_type_opts", [])
    await update.message.reply_text(
        "請選擇料理種類（可多選）：",
        reply_markup=type_keyboard(type_opts, has_selection=False)
    )
    return ASK_TYPE


async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    type_opts     = context.user_data.get("_type_opts", [])
    district_opts = context.user_data.get("_district_opts", [])
    selected      = context.user_data.get("種類", [])

    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "請重新選擇市區：",
            reply_markup=district_keyboard(district_opts)
        )
        return ASK_DISTRICT
    if text == BTN_UNDO:
        if selected:
            removed = selected.pop()
            context.user_data["種類"] = selected
            already = "、".join(selected) if selected else "（尚無選擇）"
            await update.message.reply_text(
                f"已清除：{removed}\n目前已選：{already}",
                reply_markup=type_keyboard(type_opts, has_selection=bool(selected))
            )
        else:
            await update.message.reply_text(
                "目前沒有已選的種類可以清除。",
                reply_markup=type_keyboard(type_opts, has_selection=False)
            )
        return ASK_TYPE
    if text == BTN_DONE:
        if not selected:
            await update.message.reply_text(
                "至少選一個種類，或點「➕ 新增種類」手動輸入：",
                reply_markup=type_keyboard(type_opts, has_selection=False)
            )
            return ASK_TYPE
        await update.message.reply_text(
            "請輸入營業時間\n（例如：11:30-21:00，週二公休）",
            reply_markup=text_input_keyboard()
        )
        return ASK_HOURS
    if text == BTN_ADD_TYPE:
        await update.message.reply_text("請輸入新的種類名稱：", reply_markup=ReplyKeyboardRemove())
        return ASK_TYPE_NEW
    if text == PLACEHOLDER:
        await update.message.reply_text(
            "請選擇一個選項：",
            reply_markup=type_keyboard(type_opts, has_selection=bool(selected))
        )
        return ASK_TYPE
    if text in type_opts:
        if text not in selected:
            selected.append(text)
            context.user_data["種類"] = selected
        already = "、".join(selected)
        await update.message.reply_text(
            f"已選：{already}",
            reply_markup=type_keyboard(type_opts, has_selection=True)
        )
        return ASK_TYPE

    # 手動輸入（逗號分隔）
    context.user_data["種類"] = [t.strip() for t in text.split(",") if t.strip()]
    await update.message.reply_text(
        "請輸入營業時間\n（例如：11:30-21:00，週二公休）",
        reply_markup=text_input_keyboard()
    )
    return ASK_HOURS


async def ask_type_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    type_opts = context.user_data.get("_type_opts", [])
    selected  = context.user_data.get("種類", [])
    if text and text not in selected:
        selected.append(text)
        context.user_data["種類"] = selected
    if text and text not in type_opts:
        type_opts.append(text)
        context.user_data["_type_opts"] = type_opts
    already = "、".join(selected)
    await update.message.reply_text(
        f"已新增種類：{text}\n目前已選：{already}",
        reply_markup=type_keyboard(type_opts, has_selection=True)
    )
    return ASK_TYPE


async def ask_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        type_opts = context.user_data.get("_type_opts", [])
        selected  = context.user_data.get("種類", [])
        await update.message.reply_text(
            "請重新選擇料理種類：",
            reply_markup=type_keyboard(type_opts, has_selection=bool(selected))
        )
        return ASK_TYPE
    context.user_data["營業時間"] = "" if text in (BTN_SKIP, "略過") else text
    await update.message.reply_text(
        "請輸入餐廳特色\n（例如：老宅改造、必點牛舌定食）",
        reply_markup=text_input_keyboard()
    )
    return ASK_FEATURE


async def ask_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "請重新輸入營業時間：",
            reply_markup=text_input_keyboard()
        )
        return ASK_HOURS
    context.user_data["特色"] = "" if text in (BTN_SKIP, "略過") else text
    await update.message.reply_text(
        "請輸入你的評價或備忘\n（例如：強烈推薦！下次還要去）",
        reply_markup=text_input_keyboard()
    )
    return ASK_RATING


async def ask_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "請重新輸入餐廳特色：",
            reply_markup=text_input_keyboard()
        )
        return ASK_FEATURE
    context.user_data["評價"] = "" if text in (BTN_SKIP, "略過") else text

    if context.user_data.get("連結"):
        return await _show_confirm(update, context)

    await update.message.reply_text(
        "請貼上相關連結\n（Google Maps / 食記 / YouTube 皆可）",
        reply_markup=text_input_keyboard()
    )
    return ASK_URL


async def ask_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        await update.message.reply_text(
            "請重新輸入評價：",
            reply_markup=text_input_keyboard()
        )
        return ASK_RATING
    context.user_data["連結"] = "" if text in (BTN_SKIP, "略過") else text
    return await _show_confirm(update, context)


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    types_str = "、".join(d.get("種類", [])) or "（未填）"
    summary = (
        "請確認以下資訊：\n\n"
        f"名稱：{d.get('名稱', '')}\n"
        f"位置：{d.get('縣市', '')} {d.get('市區', '')}\n"
        f"種類：{types_str}\n"
        f"營業：{d.get('營業時間', '') or '（未填）'}\n"
        f"特色：{d.get('特色', '') or '（未填）'}\n"
        f"評價：{d.get('評價', '') or '（未填）'}\n"
        f"連結：{d.get('連結', '') or '（未填）'}"
    )
    await update.message.reply_text(summary, reply_markup=confirm_keyboard())
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_CANCEL:
        return await do_cancel(update, context)
    if text == BTN_BACK:
        if context.user_data.get("連結"):
            await update.message.reply_text(
                "請重新輸入評價：",
                reply_markup=text_input_keyboard()
            )
            return ASK_RATING
        else:
            await update.message.reply_text(
                "請重新輸入連結：",
                reply_markup=text_input_keyboard()
            )
            return ASK_URL
    if text == BTN_CONFIRM:
        ok = save_to_notion(context.user_data)
        if ok:
            await update.message.reply_text(
                "已成功儲存到「敏的美食地圖」！\n\n下次直接傳連結即可繼續收藏。",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                "寫入 Notion 失敗，請確認 Integration 已連接資料庫。\n傳 /start 重試。",
                reply_markup=ReplyKeyboardRemove()
            )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text("請點選上方按鈕操作。", reply_markup=confirm_keyboard())
    return CONFIRM


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await do_cancel(update, context)

# ── 主程式 ────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & filters.Regex(r"https?://") & ~filters.COMMAND, receive_url_direct),
        ],
        states={
            ASK_NAME:         [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_NAME_PHOTO:   [
                MessageHandler(filters.PHOTO, ask_name_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name_photo),
            ],
            ASK_NAME_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name_confirm)],
            ASK_COUNTY:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_county)],
            ASK_COUNTY_NEW:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_county_new)],
            ASK_DISTRICT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_district)],
            ASK_DISTRICT_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_district_new)],
            ASK_TYPE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type)],
            ASK_TYPE_NEW:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type_new)],
            ASK_HOURS:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_hours)],
            ASK_FEATURE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_feature)],
            ASK_RATING:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_rating)],
            ASK_URL:          [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_url)],
            CONFIRM:          [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("敏的美食地圖 Bot 啟動中...")
    app.run_polling()


if __name__ == "__main__":
    main()
