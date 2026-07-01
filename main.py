"""TradingView Futures Webhook — entry point.

Structuur:
  main.py              — FastAPI app, /health, router-registratie
  utils.py             — gedeelde logica (compute_position, acties)
  exchanges/okx.py     — OKX EU (eea.okx.com, X-Perps)
  exchanges/ibkr.py    — IBKR via IB Gateway (tijdelijk uitgeschakeld)

Nieuwe exchange toevoegen:
  1. Maak exchanges/<naam>.py met een APIRouter genaamd 'router'.
  2. Voeg hieronder toe:
       from exchanges.<naam> import router as <naam>_router
       app.include_router(<naam>_router, prefix="/<naam>")
"""

import logging
import os

from fastapi import FastAPI

from exchanges.okx import router as okx_router
# from exchanges.ibkr import router as ibkr_router  # TIJDELIJK UITGESCHAKELD

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="TradingView Futures Webhook")

app.include_router(okx_router, prefix="/okx")
# app.include_router(ibkr_router, prefix="/ibkr")  # TIJDELIJK UITGESCHAKELD


@app.get("/health")
def health():
    return {"status": "ok"}
