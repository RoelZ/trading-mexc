import logging
import os

from fastapi import FastAPI, HTTPException
from pymexc import futures
from pydantic import BaseModel
from typing import Optional

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

OPEN_ACTIONS = {"open_long", "open_short"}

# order types
ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 5


class AlertPayload(BaseModel):
    secret: str
    symbol: str                          # bijv. "ETH_USDT"
    action: str                          # "open_long" | "close_long" | "open_short" | "close_short"
    quantity: str                        # aantal contracten
    stop_loss_price: Optional[float] = None   # verplicht bij open_long / open_short
    take_profit_price: Optional[float] = None
    entry_price: Optional[float] = None  # ingevuld = limit order, leeg = market order
    open_type: int = 1                   # 1 = isolated, 2 = cross
    leverage: int = 10


@app.post("/webhook")
async def receive_alert(payload: AlertPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    action = payload.action.lower()
    if action not in SIDE_MAP:
        valid_actions = list(SIDE_MAP.keys())
        raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid_actions))

    if action in OPEN_ACTIONS and payload.stop_loss_price is None:
        raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij " + action)

    side = SIDE_MAP[action]

    if payload.entry_price is not None:
        order_type = ORDER_TYPE_LIMIT
        price = payload.entry_price
    else:
        order_type = ORDER_TYPE_MARKET
        price = 0

    logger.info("Alert: %s %s qty=%s price=%s sl=%s tp=%s type=%s",
                payload.symbol, action, payload.quantity,
                price, payload.stop_loss_price, payload.take_profit_price,
                "limit" if order_type == ORDER_TYPE_LIMIT else "market")

    try:
        response = client.order(
            symbol=payload.symbol.upper(),
            price=price,
            vol=float(payload.quantity),
            side=side,
            type=order_type,
            open_type=payload.open_type,
            leverage=payload.leverage,
            stop_loss_price=payload.stop_loss_price,
            take_profit_price=payload.take_profit_price,
            loss_trend=1,
            profit_trend=1,
        )
        logger.info("Order geplaatst: %s", response)
        return {"status": "ok", "order": response}

    except Exception as e:
        logger.error("Order mislukt: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}
