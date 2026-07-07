"""OKX EU (eea.okx.com) exchange-module — X-Perps via officiele V5 API.

Routes worden geregistreerd als APIRouter en in main.py ingevoegd met
prefix="/okx", waardoor de paden /okx/webhook en /okx/keycheck worden.

Key aanmaken: Profiel > API and connections > Create API key, permissie
"Trade" + passphrase. LET OP: zet het server-IP in de allowlist, anders
vervalt een trade-key na 14 dagen inactiviteit.
Base URL MOET eea.okx.com zijn voor EU-accounts (www.okx.com -> fout 50119).
"""

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
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as cffi_requests  # type: ignore[no-redef]
    CURL_CFFI_AVAILABLE = False

from utils import compute_position, OPEN_ACTIONS, CLOSE_ACTIONS, ORDER_ACTIONS

logger = logging.getLogger(__name__)

# --- Configuratie (env-vars) ---

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
OKX_BASE_URL = os.environ.get("OKX_BASE_URL", "https://eea.okx.com")
OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_API_SECRET = os.environ.get("OKX_API_SECRET", "")
OKX_API_PASSPHRASE = os.environ.get("OKX_API_PASSPHRASE", "")
OKX_MARGIN_CURRENCY = os.environ.get("OKX_MARGIN_CURRENCY", "USDC")


# --- Interne helpers ---

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


# --- API-client ---

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


# --- Instrument, saldo & leverage ---

_instrument_cache: dict = {}


def okx_get_instrument(symbol: str) -> dict:
    """Resolve een symbool naar het live X-perp instrument.

    Accepteert: "ETH" of "ETH-USD_UM_XPERP" (familie -> nieuwste live contract,
    robuust tegen contract-rollover) of een volledige instId zoals
    "ETH-USD_UM_XPERP-310404" (exact dat contract). Cache: 1 uur."""
    key = symbol.upper()
    cached = _instrument_cache.get(key)
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
    _instrument_cache[key] = {"inst": inst, "ts": time.time()}
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


def okx_set_leverage(inst_id: str, lever: int, mgn_mode: str, pos_side: str = "net") -> dict:
    """Zet leverage voor een instrument.

    pos_side is verplicht in hedge-mode (long/short positiemodus):
      "long"  -> voor een long positie
      "short" -> voor een short positie
      "net"   -> voor net/one-way mode (geen posSide vereist)

    In net-mode sturen we posSide NIET mee (anders error 51000).
    In hedge-mode sturen we "long" of "short" mee."""
    body: dict = {"instId": inst_id, "lever": str(int(lever)), "mgnMode": mgn_mode}
    if pos_side in ("long", "short"):
        body["posSide"] = pos_side
    logger.info("OKX set-leverage: %s", body)
    try:
        return okx_request("POST", "/api/v5/account/set-leverage", body=body)
    except Exception as e:
        if "51000" in str(e) and pos_side == "net":
            logger.warning("set-leverage net-mode faalde (account in hedge-mode?), "
                           "retry met posSide=long/short niet mogelijk hier — geef pos_side door via caller")
        raise


# --- Order acties ---

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
        if payload.max_cost_pct is not None:
            max_cost = balance * payload.max_cost_pct / 100.0
        else:
            max_cost = payload.max_cost
        sizing = compute_position(
            balance=balance,
            entry=payload.entry_price,
            stop_loss=payload.stop_loss_price,
            risk_pct=payload.risk_pct,
            max_cost=max_cost,
            max_leverage=min(payload.max_leverage, inst_max_lever),
            contract_size=ct_val,
        )
        sizing["contract_size"] = ct_val
        sizing["mode"] = "auto"
        contracts = sizing["contracts"]
        leverage = sizing["leverage"]
        logger.info("OKX auto-sizing: %s", sizing)

    # posSide voor set-leverage: "long"/"short" in hedge-mode, "net" in one-way mode.
    # We proberen eerst net (one-way). Als OKX 51000 teruggeeft is de account in
    # hedge-mode en sturen we de richting mee.
    pos_side_for_lev = "long" if action == "open_long" else "short"
    is_hedge_mode = False
    try:
        okx_set_leverage(inst_id, leverage, td_mode, pos_side="net")
    except Exception as e:
        if "51000" in str(e):
            logger.info("set-leverage net mislukt (hedge-mode account), retry met posSide=%s", pos_side_for_lev)
            okx_set_leverage(inst_id, leverage, td_mode, pos_side=pos_side_for_lev)
            is_hedge_mode = True
        else:
            raise

    body: dict = {
        "instId": inst_id,
        "tdMode": td_mode,
        "side": side,
        "ordType": "limit" if payload.entry_price is not None else "market",
        "sz": str(int(contracts)),
    }
    if is_hedge_mode:
        body["posSide"] = pos_side_for_lev
    if payload.entry_price is not None:
        body["px"] = _okx_num(payload.entry_price)
    attach: dict = {}
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


def okx_close_position(symbol: str, td_mode: str, action: str = "") -> dict:
    """Sluit de volledige positie tegen marktprijs.

    In hedge-mode is posSide verplicht; we leiden die af van de action
    (close_long -> long, close_short -> short)."""
    inst_id = okx_get_instrument(symbol)["instId"]
    body: dict = {"instId": inst_id, "mgnMode": td_mode, "autoCxl": True}
    if action == "close_long":
        body["posSide"] = "long"
    elif action == "close_short":
        body["posSide"] = "short"
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

    Tweetraps-aanpak:
      1) Na een fill leeft de TP/SL als losse algo-order (oco/conditional)
         -> amend-algo-order met newSlTriggerPx.
         Valt amend terug op 404 (niet beschikbaar op EEA/X-perp), dan
         cancel + herplaatsen via order-algo.
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
            algo_id = algo["algoId"]
            tp = take_profit_price if take_profit_price is not None else algo.get("tpTriggerPx")
            try:
                body: dict = {"instId": inst_id, "algoId": algo_id,
                              "newSlTriggerPx": new_sl, "newSlOrdPx": "-1"}
                if tp not in (None, ""):
                    body["newTpTriggerPx"] = _okx_num(float(tp))
                    body["newTpOrdPx"] = "-1"
                logger.info("Break-even via amend-algo-order: %s", body)
                return okx_request("POST", "/api/v5/trade/amend-algo-order", body=body)
            except Exception as e:
                err_str = str(e)
                if "404" in err_str or "Not Found" in err_str:
                    # amend-algo-order niet beschikbaar op EEA/X-perp -> cancel + herplaatsen
                    logger.info("amend-algo-order 404, cancel+herplaats voor algoId=%s", algo_id)
                    try:
                        okx_request("POST", "/api/v5/trade/cancel-algo-orders",
                                    body=[{"instId": inst_id, "algoId": algo_id}])
                        new_ord: dict = {
                            "instId": inst_id,
                            "tdMode": algo.get("tdMode", "isolated"),
                            "side": algo.get("side", "sell"),
                            "sz": algo.get("sz", "1"),
                            "slTriggerPx": new_sl,
                            "slOrdPx": "-1",
                            "slTriggerPxType": "last",
                        }
                        pos_side = algo.get("posSide")
                        if pos_side and pos_side != "net":
                            new_ord["posSide"] = pos_side
                        if tp not in (None, ""):
                            new_ord["tpTriggerPx"] = _okx_num(float(tp))
                            new_ord["tpOrdPx"] = "-1"
                            new_ord["tpTriggerPxType"] = "last"
                            new_ord["ordType"] = "oco"
                        else:
                            new_ord["ordType"] = "conditional"
                        logger.info("Break-even via cancel+herplaats: %s", new_ord)
                        return okx_request("POST", "/api/v5/trade/order-algo", body=new_ord)
                    except Exception as e2:
                        logger.warning("cancel+herplaats mislukt voor algoId=%s: %s", algo_id, e2)
                        errors.append(f"cancel+herplaats {algo_id}: {e2}")
                else:
                    logger.warning("Break-even via amend-algo-order mislukt: %s", e)
                    errors.append(f"amend-algo-order {algo_id}: {e}")

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
                item: dict = {"attachAlgoId": a.get("attachAlgoId"),
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
    """Controleer of de OKX API key werkt.

    Gebruikt /api/v5/account/balance als lightweight auth-check. Als de response
    code=0 is maar details leeg (nieuw account / geen saldo), geldt de key toch
    als geldig — want OKX heeft de request geauthenticeerd."""
    try:
        _okx_check_config()
    except Exception as e:
        return {"valid": False, "status": "not_configured", "reason": str(e)}
    try:
        result = okx_request("GET", "/api/v5/account/balance",
                             params={"ccy": OKX_MARGIN_CURRENCY.upper()})
        data = (result.get("data") or [{}])[0]
        total_eq = data.get("totalEq", "")
        details = data.get("details") or []
        balance_info = {}
        for d in details:
            if str(d.get("ccy", "")).upper() == OKX_MARGIN_CURRENCY.upper():
                balance_info = {"ccy": OKX_MARGIN_CURRENCY,
                                "availEq": d.get("availEq"), "eq": d.get("eq")}
                break
        return {"valid": True, "status": "ok",
                "totalEq": total_eq or "0", "balance": balance_info or
                f"geen {OKX_MARGIN_CURRENCY} in trading-account (stort USDC of zet account-mode op Futures)"}
    except Exception as e:
        message = str(e)
        if "OKX fout" in message or "OKX order-fout" in message:
            return {"valid": False, "status": "invalid", "reason": message}
        return {"valid": None, "status": "unreachable", "reason": message}


# --- Payload model ---

class OkxAlertPayload(BaseModel):
    secret: str
    action: str
    symbol: str = "ETH-USD_UM_XPERP"
    entry_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    # Marge-modus: "isolated" of "cross"; open_type (1/2) werkt ook als alias
    td_mode: str = "isolated"
    open_type: Optional[int] = None
    # Automatische sizing (gebruikt als 'quantity' niet is meegegeven)
    risk_pct: float = 1.0
    max_cost_pct: Optional[float] = None   # % van saldo als max marge (bv. 20 = 20%); heeft voorrang op max_cost
    max_cost: float = 500.0                # fallback als max_cost_pct niet is meegegeven
    max_leverage: int = 10
    contract_size: Optional[float] = None
    # Handmatige override (optioneel)
    quantity: Optional[float] = None
    leverage: Optional[int] = None

    def effective_td_mode(self) -> str:
        if self.open_type is not None:
            return "isolated" if int(self.open_type) == 1 else "cross"
        return self.td_mode


# --- Router ---

router = APIRouter(tags=["OKX"])


@router.post("/webhook")
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
            response = okx_close_position(payload.symbol, payload.effective_td_mode(), action)
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

    # 4) Stop-loss verplaatsen naar opgegeven prijs
    if action == "move_sl_be":
        if payload.stop_loss_price is None:
            raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij move_sl_be")
        try:
            response = okx_move_sl_to_breakeven(
                payload.symbol, payload.stop_loss_price, payload.take_profit_price
            )
            logger.info("OKX SL verplaatst: %s", response)
            return {"status": "ok", "action": action, "result": response}
        except Exception as e:
            logger.error("OKX SL verplaatsen mislukt: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    valid = sorted(ORDER_ACTIONS) + ["cancel", "move_sl_be"]
    raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid))


@router.get("/keycheck")
def okx_keycheck(secret: str = "", x_webhook_secret: str = Header(default="")):
    """Controleer of de OKX API key geldig is. Beveiligd met de webhook-secret."""
    provided = x_webhook_secret or secret
    if provided != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = okx_check_key()
    logger.info("OKX keycheck: %s", result)
    return result
