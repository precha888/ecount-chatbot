from fastapi import FastAPI, Request
from pydantic import BaseModel
from rapidfuzz import process, fuzz
import csv
import os
import re
import requests
from datetime import datetime
import json

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

app = FastAPI()

# ---------- File path ----------
DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "items_master.csv")

# ---------- ECOUNT CONFIG ----------
# For production use oapiia; for sandbox/testing you may use sboapiia.
ECOUNT_BASE_URL = "https://sboapiia.ecount.com/OAPI/V2"
# TODO: put your real SESSION_ID from Ecount API login here
ECOUNT_SESSION_ID = "3930373738337c505245434841:IA-EStEUmGRPMqcE"


LINE_CHANNEL_SECRET = os.getenv("0ca62460a116b780a7831b6f0f19f4d7", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("/OhTZkqMfVDM19ksNB/Tm94G+gRqkf9bJcuxrPF8X1WdDODU/64p2r+CfIo4GILGLjWdzhzWgSC62d+cCpE/yysC3mZsKDlZ8GvKLz4DAguVysFDIhyMk0IcN321RXLLvnLFiB2ARKyB+TrUrg+KfAdB04t89/1O/w1cDnyilFU=", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
parser = WebhookParser(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ---------- In-memory product list ----------
products: list[dict] = []


def normalize(text: str) -> str:
    """Lowercase and remove spaces, dashes, punctuation for fuzzy match."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def load_products():
    """
    Load items_master.csv and add normalized MODEL for fuzzy search.
    Uses utf-8-sig to remove BOM (Excel quirk).
    """
    global products
    products = []
    try:
        with open(DATA_FILE, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Clean headers/values (strip spaces, BOM, etc.)
                row = { (k or "").strip(): (v or "").strip() for k, v in row.items() }
                model = row.get("MODEL", "")
                row["normalized_model"] = normalize(model)
                products.append(row)
        print(f"✅ Loaded {len(products)} products from {DATA_FILE}")
    except FileNotFoundError:
        print(f"⚠️ CSV file not found: {DATA_FILE}")


def find_best_product(query: str):
    """Return best-match product dict and score (0–100)."""
    if not products:
        return None, 0

    query_norm = normalize(query)
    choices = [p["normalized_model"] for p in products]

    match, score, idx = process.extractOne(
        query_norm,
        choices,
        scorer=fuzz.WRatio
    )
    product = products[idx]
    print(f"🎯 Matched product: {product} (score={score})")
    return product, score


# ---------- Ecount helpers ----------

def get_stock_from_ecount(item_code: str) -> float | str:
    """
    Get stock quantity from Ecount InventoryBalance/ViewInventoryBalanceStatus API.
    """

    stock_url = (
        f"{ECOUNT_BASE_URL}/InventoryBalance/ViewInventoryBalanceStatus"
        f"?SESSION_ID={ECOUNT_SESSION_ID}"
    )

    base_date = datetime.today().strftime("%Y%m%d")

    payload = {
        "PROD_CD": item_code,
        "WH_CD": "",
        "BASE_DATE": base_date
    }

    headers = {"Content-Type": "application/json"}

    # Debug logging
    print("\n[STOCK] Request:", stock_url, payload)
    resp = requests.post(stock_url, json=payload, headers=headers, timeout=10)
    print("[STOCK] Status:", resp.status_code)
    print("[STOCK] Body:", resp.text[:500])

    resp.raise_for_status()
    data = resp.json()

    data_block = data.get("Data", {})
    result_list = data_block.get("Result", [])

    if not result_list:
        print("[STOCK] No Result list in response:", data_block)
        return 0

    bal_str = result_list[0].get("BAL_QTY", "0")
    try:
        return float(bal_str)
    except (ValueError, TypeError):
        return bal_str


def get_price_from_ecount(item_code: str) -> float | str:
    """
    Get selling price from Ecount InventoryBasic/ViewBasicProduct API.
    Uses OUT_PRICE as main selling price, with fallbacks.
    """

    price_url = (
        f"{ECOUNT_BASE_URL}/InventoryBasic/ViewBasicProduct"
        f"?SESSION_ID={ECOUNT_SESSION_ID}"
    )

    payload = {
        "PROD_CD": item_code,
        "PROD_TYPE": ""
    }

    headers = {"Content-Type": "application/json"}

    # Debug logging
    print("\n[PRICE] Request:", price_url, payload)
    resp = requests.post(price_url, json=payload, headers=headers, timeout=10)
    print("[PRICE] Status:", resp.status_code)
    print("[PRICE] Body:", resp.text[:500])

    resp.raise_for_status()
    data = resp.json()

    data_block = data.get("Data", {})
    raw_result = data_block.get("Result")

    # Result may be a list or a JSON string "[{...}]"
    result_list = []
    if isinstance(raw_result, list):
        result_list = raw_result
    elif isinstance(raw_result, str):
        try:
            result_list = json.loads(raw_result)
        except Exception as e:
            print("[PRICE] Could not parse Result string:", e, raw_result)

    if not result_list:
        print("[PRICE] No Result found in response:", data_block)
        return "ไม่พบราคา"

    row = result_list[0]

    price_str = (
        row.get("OUT_PRICE")
        or row.get("OUT_PRICE1")
        or row.get("OUTSIDE_PRICE")
        or "0"
    )

    try:
        return float(price_str)
    except (ValueError, TypeError):
        return price_str


def get_price_and_stock(item_code: str):
    """Wrapper that returns (price, stock) using both Ecount APIs."""
    price = get_price_from_ecount(item_code)
    stock = get_stock_from_ecount(item_code)
    return price, stock

def generate_reply(text: str) -> str:
    """From user text → fuzzy match → Ecount price & stock → reply string."""

    # Find something that looks like a model/item code in the message
    m = re.search(r"[A-Za-z0-9\-]{4,}", text)
    if not m:
        return "กรุณาพิมพ์รหัสสินค้าหรือรุ่น เช่น MY2N24VDC หรือ 2961105"

    query_model = m.group()
    product, score = find_best_product(query_model)

    if not product or score < 70:
        return (
            f"ยังไม่พบรุ่นใกล้เคียงกับ '{query_model}' "
            f"(score={score:.1f}) รบกวนตรวจสอบรหัสอีกครั้งครับ"
        )

    # Extract info from CSV row
    item_code = product.get("ITEM_CODE", "")
    model = product.get("MODEL", "")
    name = product.get("ITEM_NAME", "")
    spec = product.get("SPEC", "")
    unit = product.get("UNIT", "")

    print("ITEM_CODE from CSV:", item_code)

    try:
        price, stock_qty = get_price_and_stock(item_code)
        price_text = f"{price:.2f}" if isinstance(price, (int, float)) else str(price)
    except Exception as e:
        price_text = "ไม่สามารถดึงราคา/สต็อกได้"
        stock_qty = "ไม่ทราบ"
        print("Ecount API error:", e)

    reply = (
        f"พบรุ่นที่ใกล้เคียงที่สุด:\n"
        f"🔹 MODEL: {model}\n"
        f"🔹 ITEM_CODE: {item_code}\n"
        f"🔹 ชื่อสินค้า: {name}\n"
        f"🔹 รายละเอียด: {spec}\n"
        f"🔹 หน่วยขาย: {unit}\n"
        f"🔹 ราคา: {price_text} ต่อ {unit}\n"
        f"🔹 สต๊อกคงเหลือ: {stock_qty} {unit}\n"
    )

    return reply
# ---------- Load CSV at startup ----------
load_products()

# ---------- API models ----------

class ChatRequest(BaseModel):
    message: str


# ---------- Endpoints ----------

@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "Ecount chatbot is running. Visit /docs for the API, or POST /chat."
    }

@app.get("/health")
def health():
    return {"status": "ok", "products_loaded": len(products)}


@app.post("/chat")
def chat(req: ChatRequest):
    reply = generate_reply(req.message)
    return {"reply": reply}
  
@app.post("/line-webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(default=None)
):
    if parser is None or line_bot_api is None:
        raise HTTPException(status_code=500, detail="LINE not configured on server")

    body = await request.body()
    body_text = body.decode("utf-8")
    print("LINE webhook raw body:", body_text)

    try:
        events = parser.parse(body_text, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessage):
            user_text = event.message.text
            reply_text = generate_reply(user_text)   # 🔁 reuse your logic

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )

    return "OK"