import hashlib
import json
import logging
import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as cffi_requests
    CURL_CFFI_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TradingView MEXC Futures Webhook")

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MEXC_WEB_KEY = os.environ["MEXC_WEB_KEY"]  # WEB key uit browser devtools

FUTURES_URL = "https://futures.mexc.com/api/v1/private/order/create"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# MEXC Futures side codes:
# 1 = Open Long  | 2 = Close Short | 3 = Open Short | 4 = Close Long
SIDE_MAP = {
    "open_long": 1,
    "close_short": 2,
    "open_short": 3,
    "close_long": 4,
}

OPEN_ACTIONS = {"open_long", "open_short"}

ORDER_TYPE_LIMIT = "1"
ORDER_TYPE_MARKET = "5"


def md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def sign_request(key: str, body: dict) -> dict:
    date_now = str(int(time.time() * 1000))
    g = md5(key + date_now)[7:]
    s = json.dumps(body, separators=(",", ":"))
    sign = md5(date_now + s + g)
    return {"time": date_now, "sign": sign}


def place_futures_order(body: dict) -> dict:
    signature = sign_request(MEXC_WEB_KEY, body)
    headers = {
        "Content-Type": "application/json",
        "x-mxc-sign": signature["sign"],
        "x-mxc-nonce": signature["time"],
        "User-Agent": USER_AGENT,
        "Authorization": MEXC_WEB_KEY,
    }
    if CURL_CFFI_AVAILABLE:
        response = cffi_requests.post(
            FUTURES_URL, headers=headers, json=body, impersonate="chrome110"
        )
    else:
        response = cffi_requests.post(FUTURES_URL, headers=headers, json=body)

    result = response.json()
    if not result.get("success"):
        raise Exception(f"MEXC fout: {result}")
    return result


class AlertPayload(BaseModel):
    secret: str
    symbol: str
    action: str
    quantity: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    entry_price: Optional[float] = None
    open_type: int = 1
    leverage: int = 10


@app.post("/webhook")
async def receive_alert(payload: AlertPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    action = payload.action.lower()
    if action not in SIDE_MAP:
        valid = list(SIDE_MAP.keys())
        raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid))

    if action in OPEN_ACTIONS and payload.stop_loss_price is None:
        raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij " + action)

    side = SIDE_MAP[action]
    order_type = ORDER_TYPE_LIMIT if payload.entry_price is not None else ORDER_TYPE_MARKET
    price = payload.entry_price if payload.entry_price is not None else 0

    body = {
        "symbol": payload.symbol.upper(),
        "side": side,
        "openType": payload.open_type,
        "type": order_type,
        "vol": float(payload.quantity),
        "leverage": payload.leverage,
        "price": price,
        "priceProtect": "0",
    }

    if payload.stop_loss_price is not None:
        body["stopLossPrice"] = payload.stop_loss_price
        body["stopLossTrend"] = 1  # 1 = latest price

    if payload.take_profit_price is not None:
        body["takeProfitPrice"] = payload.take_profit_price
        body["takeProfitTrend"] = 1

    logger.info("Order body: %s", body)

    try:
        response = place_futures_order(body)
        logger.info("Order geplaatst: %s", response)
        return {"status": "ok", "order": response}
    except Exception as e:
        logger.error("Order mislukt: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}
