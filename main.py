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

# Basis-URL van de MEXC futures web-API (zelfde paden als de officiele contract-API)
BASE_URL = "https://futures.mexc.com/api/v1/private"
ORDER_CREATE = BASE_URL + "/order/create"
ORDER_CANCEL_ALL = BASE_URL + "/order/cancel_all"
OPEN_POSITIONS = BASE_URL + "/position/open_positions"
TPSL_PLACE = BASE_URL + "/stoporder/place"
TPSL_CANCEL_ALL = BASE_URL + "/stoporder/cancel_all"

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
CLOSE_ACTIONS = {"close_long", "close_short"}
ORDER_ACTIONS = OPEN_ACTIONS | CLOSE_ACTIONS

ORDER_TYPE_LIMIT = "1"
ORDER_TYPE_MARKET = "5"

# MEXC position types in open_positions: 1 = long, 2 = short
POSITION_TYPE_LONG = 1
POSITION_TYPE_SHORT = 2


def md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _sign(payload_str: str) -> dict:
    """Bouw de MEXC web-key signature voor een (al geserialiseerde) payload string.

    Voor POST is payload_str de compacte JSON-body, voor GET de gesorteerde
    query-string (key=value&key=value)."""
    date_now = str(int(time.time() * 1000))
    g = md5(MEXC_WEB_KEY + date_now)[7:]
    sign = md5(date_now + payload_str + g)
    return {"time": date_now, "sign": sign}


def _headers(signature: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "x-mxc-sign": signature["sign"],
        "x-mxc-nonce": signature["time"],
        "User-Agent": USER_AGENT,
        "Authorization": MEXC_WEB_KEY,
    }


def mexc_post(url: str, body: dict) -> dict:
    body_str = json.dumps(body, separators=(",", ":"))
    signature = _sign(body_str)
    headers = _headers(signature)
    if CURL_CFFI_AVAILABLE:
        response = cffi_requests.post(url, headers=headers, data=body_str, impersonate="chrome110")
    else:
        response = cffi_requests.post(url, headers=headers, data=body_str)
    result = response.json()
    if not result.get("success"):
        raise Exception(f"MEXC fout ({url}): {result}")
    return result


def mexc_get(url: str, params: Optional[dict] = None) -> dict:
    params = params or {}
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = _sign(query)
    headers = _headers(signature)
    full_url = url + ("?" + query if query else "")
    if CURL_CFFI_AVAILABLE:
        response = cffi_requests.get(full_url, headers=headers, impersonate="chrome110")
    else:
        response = cffi_requests.get(full_url, headers=headers)
    result = response.json()
    if not result.get("success"):
        raise Exception(f"MEXC fout ({url}): {result}")
    return result


# --- ORDER ACTIES ---

def place_entry_order(payload: "AlertPayload") -> dict:
    side = SIDE_MAP[payload.action.lower()]
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

    logger.info("Entry order body: %s", body)
    return mexc_post(ORDER_CREATE, body)


def cancel_all(symbol: str) -> dict:
    """Annuleer alle openstaande (ongevulde) orders voor een contract."""
    body = {"symbol": symbol.upper()}
    logger.info("Cancel-all orders: %s", body)
    return mexc_post(ORDER_CANCEL_ALL, body)


def get_open_position(symbol: str) -> Optional[dict]:
    """Haal de open positie voor een symbool op (positionId, holdVol, positionType)."""
    result = mexc_get(OPEN_POSITIONS, {"symbol": symbol.upper()})
    data = result.get("data") or []
    for pos in data:
        if str(pos.get("symbol", "")).upper() == symbol.upper() and float(pos.get("holdVol", 0)) > 0:
            return pos
    return None


def move_sl_to_breakeven(symbol: str, stop_loss_price: float,
                         take_profit_price: Optional[float]) -> dict:
    """Verplaats de stop-loss naar break-even.

    Stappen: open positie opzoeken -> bestaande TP/SL annuleren ->
    nieuwe TP/SL op de positie plaatsen met SL op de break-even prijs."""
    pos = get_open_position(symbol)
    if pos is None:
        raise Exception(f"Geen open positie gevonden voor {symbol}; break-even overgeslagen.")

    position_id = pos.get("positionId")
    hold_vol = float(pos.get("holdVol", 0))
    position_type = int(pos.get("positionType", 0))

    # Bestaande TP/SL plan-orders weghalen voordat we nieuwe plaatsen
    try:
        mexc_post(TPSL_CANCEL_ALL, {"symbol": symbol.upper()})
    except Exception as e:
        logger.warning("Kon bestaande TP/SL niet annuleren (ga toch door): %s", e)

    body = {
        "symbol": symbol.upper(),
        "positionId": position_id,
        "vol": hold_vol,
        "stopLossPrice": stop_loss_price,
        "lossTrend": 1,      # 1 = latest price
        "profitTrend": 1,
        "priceProtect": "0",
    }
    if take_profit_price is not None:
        body["takeProfitPrice"] = take_profit_price

    logger.info("Break-even TP/SL body (positie %s, %s contracten): %s",
                position_id, hold_vol, body)
    return mexc_post(TPSL_PLACE, body)


# --- PAYLOAD MODEL ---

class AlertPayload(BaseModel):
    secret: str
    action: str
    symbol: str
    quantity: Optional[float] = None
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

    # 1) Entry / exit orders
    if action in ORDER_ACTIONS:
        if payload.quantity is None:
            raise HTTPException(status_code=400, detail="quantity is verplicht bij " + action)
        if action in OPEN_ACTIONS and payload.stop_loss_price is None:
            raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij " + action)
        try:
            response = place_entry_order(payload)
            logger.info("Order geplaatst: %s", response)
            return {"status": "ok", "action": action, "order": response}
        except Exception as e:
            logger.error("Order mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # 2) Ongevulde order annuleren (timeout)
    if action == "cancel":
        try:
            response = cancel_all(payload.symbol)
            logger.info("Orders geannuleerd: %s", response)
            return {"status": "ok", "action": action, "result": response}
        except Exception as e:
            logger.error("Annuleren mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # 3) Stop-loss naar break-even verplaatsen
    if action == "move_sl_be":
        if payload.stop_loss_price is None:
            raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij move_sl_be")
        try:
            response = move_sl_to_breakeven(
                payload.symbol, payload.stop_loss_price, payload.take_profit_price
            )
            logger.info("Break-even gezet: %s", response)
            return {"status": "ok", "action": action, "result": response}
        except Exception as e:
            logger.error("Break-even mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    valid = list(SIDE_MAP.keys()) + ["cancel", "move_sl_be"]
    raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid))


@app.get("/health")
def health():
    return {"status": "ok"}
