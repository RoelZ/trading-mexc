import logging
import os

from fastapi import FastAPI, HTTPException
from pymexc import futures
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TradingView MEXC Futures Webhook")

API_KEY = os.environ["MEXC_API_KEY"]
API_SECRET = os.environ["MEXC_API_SECRET"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

client = futures.HTTP(api_key=API_KEY, api_secret=API_SECRET)

# MEXC Futures side codes:
# 1 = Open Long  | 2 = Close Short | 3 = Open Short | 4 = Close Long
SIDE_MAP = {
    "open_long": 1,
    "close_short": 2,
    "open_short": 3,
    "close_long": 4,
}

# Trigger type:
# 1 = prijs >= stop_price (short stop-loss)
# 2 = prijs <= stop_price (long stop-loss)
TRIGGER_MAP = {
    "open_long": 2,
    "close_short": 1,
    "open_short": 1,
    "close_long": 2,
}


class AlertPayload(BaseModel):
    secret: str
    symbol: str        # bijv. "BTC_USDT"
    action: str        # "open_long" | "close_long" | "open_short" | "close_short"
    quantity: str      # aantal contracten
    stop_price: str    # trigger prijs
    open_type: int = 1  # 1 = isolated, 2 = cross
    leverage: int = 10


@app.post("/webhook")
async def receive_alert(payload: AlertPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    action = payload.action.lower()
    if action not in SIDE_MAP:
        valid_actions = list(SIDE_MAP.keys())
        raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid_actions))

    side = SIDE_MAP[action]
    trigger_type = TRIGGER_MAP[action]

    logger.info("Alert: %s %s qty=%s stop=%s side=%s trigger=%s",
                payload.symbol, action, payload.quantity,
                payload.stop_price, side, trigger_type)

    try:
        response = client.place_plan_order(
            symbol=payload.symbol.upper(),
            side=side,
            vol=payload.quantity,
            trigger_price=payload.stop_price,
            trigger_type=trigger_type,
            execute_cycle=1,
            order_type=2,
            open_type=payload.open_type,
            lever_rate=payload.leverage,
        )
        logger.info("Order geplaatst: %s", response)
        return {"status": "ok", "order": response}

    except Exception as e:
        logger.error("Order mislukt: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}
