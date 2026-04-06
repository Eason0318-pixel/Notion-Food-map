import os
import logging
import requests
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

# ── 對話狀態（純狀態機） ──────────────────────────────────
(
    ASK_NAME,
    ASK_COUNTY,
    ASK_DISTRICT,
    ASK_TYPE,
    ASK_HOURS,
    ASK_FEATURE,
    ASK_RATING,
    ASK_URL,
    CONFIRM,
) = range(9)

# ── 動態讀取 Notion 選項 ───────────────────────────────────
def fetch_notion_options():
    """從 Notion 動態讀取最新的 select / multi_select 選項"""
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

    # select 欄位只在有值時才加入（避免傳空字串出錯）
    if data.get("縣市"):
        properties["縣市"] = {"select": {"name": data["縣市"]}}
    if data.get("市區"):
        properties["市區"] = {"select": {"name": data["市區"]}}

    # 若有連結，加在標題的 link 上
    link = data.get("連結", "")
    if link:
        properties["Name"]["title"][0]["text"]["link"] = {"url": link}

    body = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": properties,
    }

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

# ── 鍵盤工具 ──────────────────────────────────────────────
def make_keyboard(options: list, cols: int = 2) -> ReplyKeyboardMarkup:
    rows = [options[i:i+cols] for i in range(0, len(options), cols)]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

# ══════════════════════════════════════════════════════════
# 對話流程
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "敏的美食地圖 Bot 啟動！\n\n請輸入餐廳名稱（例如：一蘭拉麵台南店）：",
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["名稱"] = name

    # 一次讀取所有 Notion 選項
    county_opts, district_opts, type_opts = fetch_notion_options()
    context.user_data["_county_opts"]   = county_opts
    context.user_data["_district_opts"] = district_opts
    context.user_data["_type_opts"]     = type_opts

    if county_opts:
        await update.message.reply_text(
            f"已記錄：{name}\n\n請選擇縣市：",
            reply_markup=make_keyboard(county_opts)
        )
    else:
        await update.message.reply_text(
            "請輸入縣市（例如：高雄）：",
            reply_markup=ReplyKeyboardRemove()
        )
    return ASK_COUNTY


async def ask_county(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["縣市"] = update.message.text.strip()
    district_opts = context.user_data.get("_district_opts", [])

    if district_opts:
        await update.message.reply_text(
            "請選擇市區：",
            reply_markup=make_keyboard(district_opts)
        )
    else:
        await update.message.reply_text(
            "請輸入市區（例如：三民區）：",
            reply_markup=ReplyKeyboardRemove()
        )
    return ASK_DISTRICT


async def ask_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["市區"] = update.message.text.strip()
    context.user_data["種類"] = []
    type_opts = context.user_data.get("_type_opts", [])

    if type_opts:
        await update.message.reply_text(
            "請選擇料理種類（可多選）\n選完後點「完成」：",
            reply_markup=make_keyboard(["完成"] + type_opts)
        )
    else:
        await update.message.reply_text(
            "請輸入料理種類（多個用逗號分隔，例如：拉麵,燒肉）：",
            reply_markup=ReplyKeyboardRemove()
        )
    return ASK_TYPE


async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    type_opts = context.user_data.get("_type_opts", [])

    if text == "完成":
        if not context.user_data.get("種類"):
            await update.message.reply_text(
                "至少選一個種類，或直接手動輸入：",
                reply_markup=make_keyboard(["完成"] + type_opts)
            )
            return ASK_TYPE
        # 進入下一步
    elif text in type_opts:
        if text not in context.user_data["種類"]:
            context.user_data["種類"].append(text)
        already = "、".join(context.user_data["種類"])
        await update.message.reply_text(
            f"已選：{already}\n\n繼續選或點「完成」：",
            reply_markup=make_keyboard(["完成"] + type_opts)
        )
        return ASK_TYPE
    else:
        # 手動輸入（逗號分隔）
        context.user_data["種類"] = [t.strip() for t in text.split(",") if t.strip()]

    await update.message.reply_text(
        "請輸入營業時間\n（例如：11:30-21:00，週二公休）\n不清楚請輸入「略過」：",
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_HOURS


async def ask_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["營業時間"] = "" if text == "略過" else text
    await update.message.reply_text(
        "請輸入餐廳特色\n（例如：老宅改造、必點牛舌定食）\n沒有請輸入「略過」："
    )
    return ASK_FEATURE


async def ask_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["特色"] = "" if text == "略過" else text
    await update.message.reply_text(
        "請輸入你的評價或備忘\n（例如：強烈推薦！下次還要去）\n沒有請輸入「略過」："
    )
    return ASK_RATING


async def ask_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["評價"] = "" if text == "略過" else text
    await update.message.reply_text(
        "請貼上相關連結\n（Google Maps / 食記 / YouTube 皆可）\n沒有請輸入「略過」："
    )
    return ASK_URL


async def ask_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "略過":
        context.user_data["連結"] = ""
    else:
        if ("youtube.com" in text or "youtu.be" in text) and not context.user_data.get("名稱"):
            title = get_youtube_title(text)
            if title:
                context.user_data["名稱"] = title
        context.user_data["連結"] = text

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
    await update.message.reply_text(
        summary,
        reply_markup=make_keyboard(["確認儲存", "取消"])
    )
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "確認" in text:
        ok = save_to_notion(context.user_data)
        if ok:
            await update.message.reply_text(
                "已成功儲存到「敏的美食地圖」！\n傳 /start 繼續收藏下一家。",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                "寫入 Notion 失敗，請確認 Integration 已連接資料庫。\n傳 /start 重試。",
                reply_markup=ReplyKeyboardRemove()
            )
    else:
        await update.message.reply_text(
            "已取消。傳 /start 重新開始。",
            reply_markup=ReplyKeyboardRemove()
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "已取消。傳 /start 重新開始。",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# ── 主程式 ────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_COUNTY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_county)],
            ASK_DISTRICT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_district)],
            ASK_TYPE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type)],
            ASK_HOURS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_hours)],
            ASK_FEATURE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_feature)],
            ASK_RATING:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_rating)],
            ASK_URL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_url)],
            CONFIRM:      [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("敏的美食地圖 Bot 啟動中...")
    app.run_polling()


if __name__ == "__main__":
    main()
