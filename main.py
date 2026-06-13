import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
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

app = FastAPI(title="TradingView Futures Webhook")  # ORB -> MEXC + OKX EU

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
TPSL_OPEN_ORDERS = BASE_URL + "/stoporder/open_orders"        # actieve TP/SL orders ophalen
TPSL_CHANGE_PRICE = BASE_URL + "/stoporder/change_price"      # TP/SL van een limit-order wijzigen
TPSL_CHANGE_PLAN_PRICE = BASE_URL + "/stoporder/change_plan_price"  # TP/SL van een positie-order wijzigen

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


def check_web_key() -> dict:
    """Controleer of de MEXC web-key nog geldig is via een lichte authenticated call.

    Onderscheid drie uitkomsten:
      valid = True   -> key werkt
      valid = False  -> MEXC antwoordde, maar wees de aanvraag af (key verlopen/ongeldig)
      valid = None   -> MEXC niet bereikbaar (netwerk/timeout), status onbekend"""
    url = ACCOUNT_ASSET + MARGIN_CURRENCY.upper()
    try:
        signature = _sign("")
        headers = _headers(signature)
        if CURL_CFFI_AVAILABLE:
            resp = cffi_requests.get(url, headers=headers, impersonate="chrome110")
        else:
            resp = cffi_requests.get(url, headers=headers)
    except Exception as e:
        return {"valid": None, "status": "unreachable", "reason": str(e)}

    try:
        result = resp.json()
    except Exception:
        return {"valid": None, "status": "bad_response", "reason": f"HTTP {getattr(resp, 'status_code', '?')}"}

    if result.get("success"):
        return {"valid": True, "status": "ok"}
    return {
        "valid": False,
        "status": "invalid",
        "code": result.get("code"),
        "reason": result.get("message") or result.get("msg") or str(result),
    }


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


def get_active_tpsl(symbol: str) -> list:
    """Haal de actieve (nog niet getriggerde) TP/SL-orders voor een symbool op."""
    result = mexc_get(TPSL_OPEN_ORDERS, {"symbol": symbol.upper()})
    data = result.get("data") or []
    active = []
    for o in data:
        if str(o.get("symbol", "")).upper() != symbol.upper():
            continue
        # state 1 = nog niet getriggerd; isFinished 0 = niet in eindstatus
        if int(o.get("state", 0)) == 1 and int(o.get("isFinished", 0)) == 0:
            active.append(o)
    return active


def move_sl_to_breakeven(symbol: str, stop_loss_price: float,
                         take_profit_price: Optional[float]) -> dict:
    """Verplaats de stop-loss naar break-even DOOR de bestaande TP/SL-order te wijzigen.

    We annuleren niets en plaatsen niets nieuws: we passen alleen de SL-prijs aan op de
    order die al op MEXC staat, en houden de bestaande take-profit intact. Zo is er geen
    moment zonder bescherming."""
    orders = get_active_tpsl(symbol)
    if not orders:
        raise Exception(
            f"Geen actieve TP/SL-order gevonden voor {symbol}; break-even overgeslagen "
            f"(positie mogelijk al gesloten of nog geen SL/TP aanwezig)."
        )

    order = orders[0]
    limit_order_id = order.get("orderId")
    plan_order_id = order.get("id")

    # Take-profit behouden: gebruik de meegestuurde TP, anders de bestaande van de order
    tp = take_profit_price if take_profit_price is not None else order.get("takeProfitPrice")

    # MEXC: stuur altijd zowel de nieuwe SL als de (bestaande) TP mee, anders kan de
    # TP/SL gewist worden.
    def _sl_body(extra: dict) -> dict:
        b = dict(extra)
        b["stopLossPrice"] = stop_loss_price
        b["lossTrend"] = 1
        b["profitTrend"] = 1
        if tp is not None:
            b["takeProfitPrice"] = tp
        return b

    # Na een fill hoort de TP/SL bij de POSITIE -> wijzigen via change_plan_price
    # (stopPlanOrderId). Zolang de limit-order nog niet gevuld is, hangt de TP/SL aan de
    # ORDER -> change_price (orderId). Het orderId-veld blijft ook na de fill gevuld, dus
    # we kunnen niet op dat veld vertrouwen: we proberen de positie-variant eerst en vallen
    # terug op de order-variant.
    attempts = []
    if plan_order_id:
        attempts.append(("change_plan_price", TPSL_CHANGE_PLAN_PRICE,
                         _sl_body({"stopPlanOrderId": int(plan_order_id)})))
    if limit_order_id and int(limit_order_id) != 0:
        attempts.append(("change_price", TPSL_CHANGE_PRICE,
                         _sl_body({"orderId": int(limit_order_id)})))

    if not attempts:
        raise Exception(f"Geen bruikbare order-id gevonden om de SL te wijzigen voor {symbol}.")

    errors = []
    for name, url, body in attempts:
        try:
            logger.info("Break-even via %s: %s", name, body)
            return mexc_post(url, body)
        except Exception as e:
            logger.warning("Break-even via %s mislukt, probeer volgende: %s", name, e)
            errors.append(f"{name}: {e}")
    raise Exception("Break-even via alle endpoints mislukt -> " + " | ".join(errors))


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


@app.get("/keycheck")
def keycheck(secret: str = "", x_webhook_secret: str = Header(default="")):
    """Controleer of de MEXC web-key nog geldig is. Beveiligd met de webhook-secret.

    De secret mag je meegeven via de header 'x-webhook-secret' (aanbevolen) of via de
    query-parameter 'secret'. De waarde moet gelijk zijn aan de WEBHOOK_SECRET env-var.

    Antwoord (altijd HTTP 200) bevat 'valid': true (ok), false (key ongeldig/verlopen)
    of null (MEXC onbereikbaar). Bedoeld om periodiek door n8n te laten pollen."""
    provided = x_webhook_secret or secret
    if provided != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = check_web_key()
    logger.info("Keycheck: %s", result)
    return result


# ============================================================================
# --- OKX EU (eea.okx.com, X-Perps) ---
#
# Officiele V5 API met echte API key (geen web-key-hack zoals bij MEXC).
# Key aanmaken: Profiel > API and connections > Create API key, permissie
# "Trade", plus zelfgekozen passphrase. LET OP: zet het server-IP in de
# allowlist, anders vervalt een trade-key na 14 dagen inactiviteit.
# Base URL MOET eea.okx.com zijn voor EU-accounts (www.okx.com -> error 50119).
# ============================================================================

OKX_BASE_URL = os.environ.get("OKX_BASE_URL", "https://eea.okx.com")
OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_API_SECRET = os.environ.get("OKX_API_SECRET", "")
OKX_API_PASSPHRASE = os.environ.get("OKX_API_PASSPHRASE", "")
OKX_MARGIN_CURRENCY = os.environ.get("OKX_MARGIN_CURRENCY", "USDC")


def _okx_num(x: float) -> str:
    """Float naar compacte string zonder wetenschappelijke notatie/artefacten."""
    return f"{float(x):.10g}"


def _okx_check_config():
    if not (OKX_API_KEY and OKX_API_SECRET and OKX_API_PASSPHRASE):
        raise Exception("OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE env-vars niet gezet.")


def _okx_timestamp() -> str:
    # ISO8601 UTC met milliseconden, bv. 2026-06-12T14:03:05.123Z
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _okx_headers(method: str, request_path: str, body_str: str) -> dict:
    """OKX V5 signing: Base64(HMAC-SHA256(timestamp + METHOD + path + body))."""
    _okx_check_config()
    ts = _okx_timestamp()
    message = ts + method.upper() + request_path + body_str
    sign = base64.b64encode(
        hmac.new(OKX_API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
    }


def okx_request(method: str, path: str, params: Optional[dict] = None,
                body: Optional[object] = None, auth: bool = True) -> dict:
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    request_path = path + ("?" + query if query else "")
    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    headers = _okx_headers(method, request_path, body_str) if auth else {"Content-Type": "application/json"}
    url = OKX_BASE_URL + request_path
    kwargs = {"impersonate": "chrome110"} if CURL_CFFI_AVAILABLE else {}
    if method.upper() == "GET":
        response = cffi_requests.get(url, headers=headers, **kwargs)
    else:
        response = cffi_requests.post(url, headers=headers, data=body_str, **kwargs)
    result = response.json()
    if result.get("code") != "0":
        raise Exception(f"OKX fout ({path}): {result}")
    # Order-endpoints geven per item een sCode terug; "0" = ok
    data = result.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("sCode") not in (None, "", "0"):
                raise Exception(f"OKX order-fout ({path}): {item}")
    return result


# --- INSTRUMENT, SALDO & LEVERAGE (OKX) ---

_okx_instrument_cache: dict = {}


def okx_get_instrument(symbol: str) -> dict:
    """Resolve een symbool naar het live X-perp instrument.

    Accepteert: "ETH" of "ETH-USD_UM_XPERP" (familie -> nieuwste live contract,
    robuust tegen contract-rollover) of een volledige instId zoals
    "ETH-USD_UM_XPERP-310404" (exact dat contract). Cache: 1 uur."""
    key = symbol.upper()
    cached = _okx_instrument_cache.get(key)
    if cached and time.time() - cached["ts"] < 3600:
        return cached["inst"]

    if re.search(r"-\d{6}$", key):
        params = {"instType": "FUTURES", "instId": key}
    else:
        family = key if "XPERP" in key else key.split("-")[0].split("_")[0] + "-USD_UM_XPERP"
        params = {"instType": "FUTURES", "instFamily": family}
    result = okx_request("GET", "/api/v5/public/instruments", params=params, auth=False)
    live = [i for i in (result.get("data") or []) if i.get("state") == "live"]
    if not live:
        raise Exception(f"Geen live OKX-instrument gevonden voor '{symbol}' ({params}).")
    # Bij rollover staan kort 2 contracten live; pak het nieuwst gelistte
    live.sort(key=lambda i: int(i.get("listTime") or 0), reverse=True)
    inst = live[0]
    _okx_instrument_cache[key] = {"inst": inst, "ts": time.time()}
    return inst


def okx_get_equity(ccy: str = OKX_MARGIN_CURRENCY) -> float:
    """Beschikbaar saldo in de trading account (standaard USDC)."""
    result = okx_request("GET", "/api/v5/account/balance", params={"ccy": ccy.upper()})
    data = (result.get("data") or [{}])[0]
    for d in data.get("details") or []:
        if str(d.get("ccy", "")).upper() == ccy.upper():
            for field in ("availEq", "eq", "cashBal"):
                value = d.get(field)
                if value not in (None, ""):
                    return float(value)
    raise Exception(f"Kon {ccy}-saldo niet bepalen uit OKX-respons: {data}")


def okx_set_leverage(inst_id: str, lever: int, mgn_mode: str) -> dict:
    body = {"instId": inst_id, "lever": str(int(lever)), "mgnMode": mgn_mode}
    logger.info("OKX set-leverage: %s", body)
    return okx_request("POST", "/api/v5/account/set-leverage", body=body)


# --- ORDER ACTIES (OKX) ---

def okx_place_entry(payload: "OkxAlertPayload") -> dict:
    action = payload.action.lower()
    side = "buy" if action == "open_long" else "sell"
    td_mode = payload.effective_td_mode()

    inst = okx_get_instrument(payload.symbol)
    inst_id = inst["instId"]
    ct_val = float(inst["ctVal"])  # bv. 0.001 ETH per contract
    inst_max_lever = int(float(inst.get("lever") or payload.max_leverage))

    if payload.quantity is not None:
        contracts = int(payload.quantity)
        leverage = payload.leverage if payload.leverage is not None else min(10, inst_max_lever)
        sizing = {"mode": "manual", "contracts": contracts, "leverage": leverage}
    else:
        if payload.entry_price is None or payload.stop_loss_price is None:
            raise Exception("entry_price en stop_loss_price zijn nodig voor automatische sizing.")
        balance = okx_get_equity()
        sizing = compute_position(
            balance=balance,
            entry=payload.entry_price,
            stop_loss=payload.stop_loss_price,
            risk_pct=payload.risk_pct,
            max_cost=payload.max_cost,
            max_leverage=min(payload.max_leverage, inst_max_lever),
            contract_size=ct_val,
        )
        sizing["contract_size"] = ct_val
        sizing["mode"] = "auto"
        contracts = sizing["contracts"]
        leverage = sizing["leverage"]
        logger.info("OKX auto-sizing: %s", sizing)

    okx_set_leverage(inst_id, leverage, td_mode)

    body = {
        "instId": inst_id,
        "tdMode": td_mode,
        "side": side,
        "ordType": "limit" if payload.entry_price is not None else "market",
        "sz": str(int(contracts)),
    }
    if payload.entry_price is not None:
        body["px"] = _okx_num(payload.entry_price)
    attach = {}
    if payload.stop_loss_price is not None:
        attach["slTriggerPx"] = _okx_num(payload.stop_loss_price)
        attach["slOrdPx"] = "-1"  # -1 = market-uitvoering bij trigger
        attach["slTriggerPxType"] = "last"
    if payload.take_profit_price is not None:
        attach["tpTriggerPx"] = _okx_num(payload.take_profit_price)
        attach["tpOrdPx"] = "-1"
        attach["tpTriggerPxType"] = "last"
    if attach:
        body["attachAlgoOrds"] = [attach]

    logger.info("OKX entry order body: %s", body)
    response = okx_request("POST", "/api/v5/trade/order", body=body)
    return {"order": response, "sizing": sizing, "instId": inst_id}


def okx_close_position(symbol: str, td_mode: str) -> dict:
    """Sluit de volledige positie tegen marktprijs (net mode)."""
    inst_id = okx_get_instrument(symbol)["instId"]
    body = {"instId": inst_id, "mgnMode": td_mode, "autoCxl": True}
    logger.info("OKX close-position: %s", body)
    return okx_request("POST", "/api/v5/trade/close-position", body=body)


def okx_cancel_all(symbol: str) -> dict:
    """Annuleer alle openstaande (ongevulde) orders voor het instrument."""
    inst_id = okx_get_instrument(symbol)["instId"]
    result = okx_request("GET", "/api/v5/trade/orders-pending",
                         params={"instType": "FUTURES", "instId": inst_id})
    orders = result.get("data") or []
    if not orders:
        return {"cancelled": 0, "instId": inst_id}
    body = [{"instId": inst_id, "ordId": o["ordId"]} for o in orders]
    logger.info("OKX cancel-batch-orders: %s", body)
    response = okx_request("POST", "/api/v5/trade/cancel-batch-orders", body=body)
    return {"cancelled": len(body), "instId": inst_id, "result": response}


def okx_move_sl_to_breakeven(symbol: str, stop_loss_price: float,
                             take_profit_price: Optional[float]) -> dict:
    """Verplaats de SL door de bestaande TP/SL te wijzigen (niets annuleren).

    Zelfde tweetraps-aanpak als bij MEXC:
      1) Na een fill leeft de TP/SL als losse algo-order (oco/conditional)
         -> amend-algo-order met newSlTriggerPx.
      2) Voor de fill hangt de TP/SL nog aan de limit-order
         -> amend-order met attachAlgoOrds."""
    inst_id = okx_get_instrument(symbol)["instId"]
    new_sl = _okx_num(stop_loss_price)
    errors = []

    # 1) Positie-variant: losse algo-order na fill
    for ord_type in ("oco", "conditional"):
        try:
            result = okx_request("GET", "/api/v5/trade/orders-algo-pending",
                                 params={"ordType": ord_type, "instId": inst_id})
            algos = result.get("data") or []
        except Exception as e:
            errors.append(f"orders-algo-pending {ord_type}: {e}")
            continue
        for algo in algos:
            try:
                body = {"instId": inst_id, "algoId": algo["algoId"],
                        "newSlTriggerPx": new_sl, "newSlOrdPx": "-1"}
                # TP behouden: meegestuurde TP, anders de bestaande van de order
                tp = take_profit_price if take_profit_price is not None else algo.get("tpTriggerPx")
                if tp not in (None, ""):
                    body["newTpTriggerPx"] = _okx_num(float(tp))
                    body["newTpOrdPx"] = "-1"
                logger.info("Break-even via amend-algo-order: %s", body)
                return okx_request("POST", "/api/v5/trade/amend-algo-order", body=body)
            except Exception as e:
                logger.warning("Break-even via amend-algo-order mislukt: %s", e)
                errors.append(f"amend-algo-order {algo.get('algoId')}: {e}")

    # 2) Order-variant: TP/SL hangt nog aan de ongevulde limit-order
    try:
        result = okx_request("GET", "/api/v5/trade/orders-pending",
                             params={"instType": "FUTURES", "instId": inst_id})
        orders = result.get("data") or []
    except Exception as e:
        orders = []
        errors.append(f"orders-pending: {e}")
    for order in orders:
        attached = order.get("attachAlgoOrds") or []
        if not attached:
            continue
        try:
            amend_attach = []
            for a in attached:
                item = {"attachAlgoId": a.get("attachAlgoId"),
                        "newSlTriggerPx": new_sl, "newSlOrdPx": "-1"}
                tp = take_profit_price if take_profit_price is not None else a.get("tpTriggerPx")
                if tp not in (None, ""):
                    item["newTpTriggerPx"] = _okx_num(float(tp))
                    item["newTpOrdPx"] = "-1"
                amend_attach.append(item)
            body = {"instId": inst_id, "ordId": order["ordId"], "attachAlgoOrds": amend_attach}
            logger.info("Break-even via amend-order: %s", body)
            return okx_request("POST", "/api/v5/trade/amend-order", body=body)
        except Exception as e:
            logger.warning("Break-even via amend-order mislukt: %s", e)
            errors.append(f"amend-order {order.get('ordId')}: {e}")

    raise Exception("Break-even mislukt op OKX -> " +
                    (" | ".join(errors) if errors else f"geen TP/SL-order of positie gevonden voor {inst_id}"))


def okx_check_key() -> dict:
    """Controleer of de OKX API key werkt (zelfde semantiek als MEXC /keycheck)."""
    try:
        _okx_check_config()
    except Exception as e:
        return {"valid": False, "status": "not_configured", "reason": str(e)}
    try:
        okx_get_equity()
        return {"valid": True, "status": "ok"}
    except Exception as e:
        message = str(e)
        if "OKX fout" in message or "OKX order-fout" in message:
            return {"valid": False, "status": "invalid", "reason": message}
        return {"valid": None, "status": "unreachable", "reason": message}


# --- PAYLOAD MODEL & ENDPOINTS (OKX) ---

class OkxAlertPayload(BaseModel):
    secret: str
    action: str
    symbol: str = "ETH-USD_UM_XPERP"
    entry_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    # Marge-modus: "isolated" of "cross"; open_type (1/2) werkt ook (MEXC-compat)
    td_mode: str = "isolated"
    open_type: Optional[int] = None
    # Automatische sizing (gebruikt als 'quantity' niet is meegegeven)
    risk_pct: float = 1.0
    max_cost: float = 400.0
    max_leverage: int = 10  # X-perps op OKX EU retail: max 10x
    # Genegeerd (contractgrootte komt live van OKX); aanwezig voor Pine-compat
    contract_size: Optional[float] = None
    # Handmatige override (optioneel)
    quantity: Optional[float] = None
    leverage: Optional[int] = None

    def effective_td_mode(self) -> str:
        if self.open_type is not None:
            return "isolated" if int(self.open_type) == 1 else "cross"
        return self.td_mode


@app.post("/okx/webhook")
async def receive_okx_alert(payload: OkxAlertPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    action = payload.action.lower()

    # 1) Entry orders
    if action in OPEN_ACTIONS:
        if payload.stop_loss_price is None:
            raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij " + action)
        try:
            response = okx_place_entry(payload)
            logger.info("OKX order geplaatst: %s", response)
            return {"status": "ok", "action": action, **response}
        except Exception as e:
            logger.error("OKX order mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # 2) Positie sluiten (market)
    if action in CLOSE_ACTIONS:
        try:
            response = okx_close_position(payload.symbol, payload.effective_td_mode())
            logger.info("OKX positie gesloten: %s", response)
            return {"status": "ok", "action": action, "result": response}
        except Exception as e:
            logger.error("OKX sluiten mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # 3) Ongevulde order annuleren (timeout)
    if action == "cancel":
        try:
            response = okx_cancel_all(payload.symbol)
            logger.info("OKX orders geannuleerd: %s", response)
            return {"status": "ok", "action": action, "result": response}
        except Exception as e:
            logger.error("OKX annuleren mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # 4) Stop-loss naar break-even
    if action == "move_sl_be":
        if payload.stop_loss_price is None:
            raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij move_sl_be")
        try:
            response = okx_move_sl_to_breakeven(
                payload.symbol, payload.stop_loss_price, payload.take_profit_price
            )
            logger.info("OKX break-even gezet: %s", response)
            return {"status": "ok", "action": action, "result": response}
        except Exception as e:
            logger.error("OKX break-even mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    valid = sorted(ORDER_ACTIONS) + ["cancel", "move_sl_be"]
    raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid))


@app.get("/okx/keycheck")
def okx_keycheck(secret: str = "", x_webhook_secret: str = Header(default="")):
    """Controleer of de OKX API key geldig is. Beveiligd met de webhook-secret."""
    provided = x_webhook_secret or secret
    if provided != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = okx_check_key()
    logger.info("OKX keycheck: %s", result)
    return result
