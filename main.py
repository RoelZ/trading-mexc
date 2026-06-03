import hashlib
import json
import logging
import math
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

app = FastAPI(title="TradingView MEXC Futures Webhook")  # ORB -> MEXC

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MEXC_WEB_KEY = os.environ["MEXC_WEB_KEY"]  # WEB key uit browser devtools

# Munt waarin je futures-marge wordt aangehouden (voor saldo-opvraag)
MARGIN_CURRENCY = os.environ.get("MARGIN_CURRENCY", "USDT")

# Basis-URL van de MEXC futures web-API (zelfde paden als de officiele contract-API)
BASE_URL = "https://futures.mexc.com/api/v1/private"
ORDER_CREATE = BASE_URL + "/order/create"
ORDER_CANCEL_ALL = BASE_URL + "/order/cancel_all"
OPEN_POSITIONS = BASE_URL + "/position/open_positions"
ACCOUNT_ASSET = BASE_URL + "/account/asset/"  # + currency
TPSL_PLACE = BASE_URL + "/stoporder/place"
TPSL_CANCEL_ALL = BASE_URL + "/stoporder/cancel_all"

# Publieke endpoint (geen signing) om de contractgrootte op te halen
CONTRACT_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"

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


# --- SALDO & POSITIEGROOTTE ---

def get_contract_size(symbol: str, fallback: float) -> float:
    """Haal de contractgrootte (coins per contract) op via de publieke MEXC-API.

    Dit voorkomt verkeerde positiegroottes door een handmatig foute waarde.
    Lukt de opvraag niet, dan wordt de meegestuurde waarde als fallback gebruikt."""
    try:
        url = CONTRACT_DETAIL + "?symbol=" + symbol.upper()
        if CURL_CFFI_AVAILABLE:
            response = cffi_requests.get(url, impersonate="chrome110")
        else:
            response = cffi_requests.get(url)
        result = response.json()
        if result.get("success") and result.get("data"):
            cs = result["data"].get("contractSize")
            if cs:
                return float(cs)
    except Exception as e:
        logger.warning("Kon contractSize niet ophalen voor %s (gebruik fallback %s): %s",
                       symbol, fallback, e)
    return fallback


def get_account_equity(currency: str = MARGIN_CURRENCY) -> float:
    """Haal je beschikbare futures-saldo op (in USDT)."""
    result = mexc_get(ACCOUNT_ASSET + currency.upper())
    data = result.get("data") or {}
    for field in ("equity", "availableBalance", "cashBalance"):
        value = data.get(field)
        if value is not None:
            return float(value)
    raise Exception(f"Kon saldo niet bepalen uit MEXC-respons: {data}")


def compute_position(balance: float, entry: float, stop_loss: float,
                     risk_pct: float, max_cost: float, max_leverage: int,
                     contract_size: float) -> dict:
    """Bereken contracten, leverage en kosten op basis van risico + max kosten.

    - Positiegrootte volgt uit het risico: bij SL verlies je precies risk_pct van je saldo.
    - Leverage is de kleinste hele hefboom zodat de marge (kosten) <= max_cost blijft,
      afgetopt op max_leverage. Wordt de cap geraakt, dan kunnen de kosten hoger uitvallen
      dan max_cost, maar het risico blijft op risk_pct."""
    sl_dist = abs(entry - stop_loss)
    if sl_dist <= 0:
        raise Exception("Stop-loss afstand is 0; kan positiegrootte niet berekenen.")
    if contract_size <= 0:
        raise Exception("contract_size moet groter dan 0 zijn.")

    risk_amount = balance * (risk_pct / 100.0)
    coins_raw = risk_amount / sl_dist
    contracts = max(1, int(round(coins_raw / contract_size)))

    coins = contracts * contract_size
    notional = coins * entry
    leverage = max(1, min(int(max_leverage), math.ceil(notional / max_cost)))
    cost = notional / leverage
    actual_risk = coins * sl_dist

    return {
        "balance": round(balance, 4),
        "risk_pct": risk_pct,
        "risk_amount_target": round(risk_amount, 4),
        "sl_distance": sl_dist,
        "contracts": contracts,
        "coins": coins,
        "notional": round(notional, 4),
        "leverage": leverage,
        "cost": round(cost, 4),
        "actual_risk": round(actual_risk, 4),
        "cost_capped": cost > max_cost,
    }


# --- ORDER ACTIES ---

def place_entry_order(payload: "AlertPayload") -> dict:
    action = payload.action.lower()
    side = SIDE_MAP[action]

    # Hoeveelheid + leverage: handmatig meegegeven, anders automatisch op live saldo
    if payload.quantity is not None:
        contracts = float(payload.quantity)
        leverage = payload.leverage if payload.leverage is not None else 10
        sizing = {"mode": "manual", "contracts": contracts, "leverage": leverage}
    else:
        if payload.entry_price is None or payload.stop_loss_price is None:
            raise Exception("entry_price en stop_loss_price zijn nodig voor automatische sizing.")
        balance = get_account_equity()
        contract_size = get_contract_size(payload.symbol, payload.contract_size)
        sizing = compute_position(
            balance=balance,
            entry=payload.entry_price,
            stop_loss=payload.stop_loss_price,
            risk_pct=payload.risk_pct,
            max_cost=payload.max_cost,
            max_leverage=payload.max_leverage,
            contract_size=contract_size,
        )
        sizing["contract_size"] = contract_size
        sizing["mode"] = "auto"
        contracts = sizing["contracts"]
        leverage = sizing["leverage"]
        logger.info("Auto-sizing: %s", sizing)

    order_type = ORDER_TYPE_LIMIT if payload.entry_price is not None else ORDER_TYPE_MARKET
    price = payload.entry_price if payload.entry_price is not None else 0

    body = {
        "symbol": payload.symbol.upper(),
        "side": side,
        "openType": payload.open_type,
        "type": order_type,
        "vol": float(contracts),
        "leverage": int(leverage),
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
    response = mexc_post(ORDER_CREATE, body)
    return {"order": response, "sizing": sizing}


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
    entry_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    open_type: int = 1
    # Automatische sizing (gebruikt als 'quantity' niet is meegegeven)
    risk_pct: float = 1.0
    max_cost: float = 400.0
    max_leverage: int = 20
    contract_size: float = 0.0001
    # Handmatige override (optioneel)
    quantity: Optional[float] = None
    leverage: Optional[int] = None


@app.post("/webhook")
async def receive_alert(payload: AlertPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    action = payload.action.lower()

    # 1) Entry / exit orders
    if action in ORDER_ACTIONS:
        if action in OPEN_ACTIONS and payload.stop_loss_price is None:
            raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij " + action)
        try:
            response = place_entry_order(payload)
            logger.info("Order geplaatst: %s", response)
            return {"status": "ok", "action": action, **response}
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
