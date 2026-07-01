"""IBKR (Interactive Brokers via IB Gateway) exchange-module.

TIJDELIJK UITGESCHAKELD — zie main_backup_20260630.py voor de originele werkende versie.

Om in te schakelen:
  1. Verwijder alle '#  ' commentaar-prefixen in dit bestand.
  2. Zorg dat ib_insync geïnstalleerd is:  pip install ib_insync
  3. Verwijder het commentaar van de twee ibkr-regels in main.py:
       from exchanges.ibkr import router as ibkr_router
       app.include_router(ibkr_router, prefix="/ibkr")
"""

# import asyncio
# import logging
# import os
# from typing import Optional
#
# from fastapi import APIRouter, Header, HTTPException
# from pydantic import BaseModel
#
# from utils import OPEN_ACTIONS, CLOSE_ACTIONS, ORDER_ACTIONS
#
# logger = logging.getLogger(__name__)
#
# WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
# IB_GATEWAY_HOST = os.environ.get("IB_GATEWAY_HOST", "172.17.0.1")
# IB_GATEWAY_PORT = int(os.environ.get("IB_GATEWAY_PORT", "4002"))  # 4002=paper, 4001=live
# IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "10"))
#
# try:
#     from ib_insync import IB, Contract, Stock, Future, Forex
#     from ib_insync import MarketOrder, LimitOrder, StopOrder
#     IB_INSYNC_AVAILABLE = True
# except ImportError:
#     IB_INSYNC_AVAILABLE = False
#     logger.warning("ib_insync niet geïnstalleerd — /ibkr endpoints niet beschikbaar.")
#
# _ib: "IB | None" = None
#
#
# async def _get_ib() -> "IB":
#     """Geeft een actieve IB-verbinding terug; verbindt opnieuw als de connectie weg is."""
#     global _ib
#     if not IB_INSYNC_AVAILABLE:
#         raise Exception("ib_insync is niet geïnstalleerd.")
#     if _ib is None:
#         _ib = IB()
#     if not _ib.isConnected():
#         await _ib.connectAsync(IB_GATEWAY_HOST, IB_GATEWAY_PORT, clientId=IB_CLIENT_ID)
#         logger.info("Verbonden met IB Gateway op %s:%s", IB_GATEWAY_HOST, IB_GATEWAY_PORT)
#     return _ib
#
#
# def _ibkr_make_contract(symbol: str, sec_type: str, exchange: str,
#                          currency: str, expiry: str) -> "Contract":
#     """Maak het juiste ib_insync Contract-object op basis van sec_type."""
#     sec_type = sec_type.upper()
#     if sec_type == "STK":
#         return Stock(symbol.upper(), exchange or "SMART", currency or "USD")
#     if sec_type == "FUT":
#         if not expiry:
#             raise Exception("expiry (bijv. '202509') is verplicht voor futures (FUT).")
#         return Future(symbol.upper(), expiry, exchange or "CME", currency=currency or "USD")
#     if sec_type == "CASH":
#         sym, ccy = (symbol[:3], symbol[3:]) if len(symbol) == 6 else (symbol, currency)
#         return Forex(sym.upper(), currency=ccy.upper() or "USD")
#     raise Exception(f"Onbekend sec_type '{sec_type}'. Gebruik STK, FUT of CASH.")
#
#
# async def ibkr_place_entry(payload: "IbkrAlertPayload") -> dict:
#     """Plaatst een entry order (market of limit) met optionele SL via aparte stop-order."""
#     ib = await _get_ib()
#     action = payload.action.lower()
#     ib_side = "BUY" if action == "open_long" else "SELL"
#     contract = _ibkr_make_contract(
#         payload.symbol, payload.sec_type, payload.exchange,
#         payload.currency, payload.expiry
#     )
#     qualified = await ib.qualifyContractsAsync(contract)
#     if not qualified:
#         raise Exception(f"Kon contract niet kwalificeren: {payload.symbol} ({payload.sec_type})")
#     contract = qualified[0]
#     quantity = float(payload.quantity) if payload.quantity is not None else 1.0
#     if payload.stop_loss_price is not None:
#         sl_action = "SELL" if ib_side == "BUY" else "BUY"
#         if payload.entry_price is not None:
#             parent = LimitOrder(ib_side, quantity, payload.entry_price,
#                                 outsideRth=payload.outside_rth, tif="GTC")
#         else:
#             parent = MarketOrder(ib_side, quantity,
#                                  outsideRth=payload.outside_rth, tif="GTC")
#         sl_order = StopOrder(sl_action, quantity, payload.stop_loss_price, tif="GTC")
#         parent.transmit = False
#         sl_order.parentId = 0
#         parent_trade = ib.placeOrder(contract, parent)
#         await asyncio.sleep(0.5)
#         sl_order.parentId = parent_trade.order.orderId
#         sl_order.transmit = True
#         if payload.take_profit_price is not None:
#             tp_action = sl_action
#             tp_order = LimitOrder(tp_action, quantity, payload.take_profit_price, tif="GTC")
#             tp_order.parentId = parent_trade.order.orderId
#             tp_order.transmit = False
#             ib.placeOrder(contract, tp_order)
#         sl_trade = ib.placeOrder(contract, sl_order)
#         await asyncio.sleep(0.3)
#         return {
#             "entry_orderId": parent_trade.order.orderId,
#             "sl_orderId": sl_trade.order.orderId,
#             "contract": contract.localSymbol or contract.symbol,
#             "side": ib_side,
#             "quantity": quantity,
#         }
#     else:
#         if payload.entry_price is not None:
#             order = LimitOrder(ib_side, quantity, payload.entry_price,
#                                outsideRth=payload.outside_rth, tif="GTC")
#         else:
#             order = MarketOrder(ib_side, quantity, outsideRth=payload.outside_rth)
#         trade = ib.placeOrder(contract, order)
#         await asyncio.sleep(0.3)
#         return {
#             "orderId": trade.order.orderId,
#             "contract": contract.localSymbol or contract.symbol,
#             "side": ib_side,
#             "quantity": quantity,
#         }
#
#
# async def ibkr_close_position(payload: "IbkrAlertPayload") -> dict:
#     """Sluit de volledige bestaande positie via een market order."""
#     ib = await _get_ib()
#     action = payload.action.lower()
#     close_side = "SELL" if action == "close_long" else "BUY"
#     contract = _ibkr_make_contract(
#         payload.symbol, payload.sec_type, payload.exchange,
#         payload.currency, payload.expiry
#     )
#     qualified = await ib.qualifyContractsAsync(contract)
#     if not qualified:
#         raise Exception(f"Kon contract niet kwalificeren: {payload.symbol}")
#     contract = qualified[0]
#     await ib.reqPositionsAsync()
#     positions = [p for p in ib.positions()
#                  if p.contract.conId == contract.conId and p.position != 0]
#     if positions:
#         qty = abs(positions[0].position)
#     elif payload.quantity is not None:
#         qty = float(payload.quantity)
#     else:
#         raise Exception(f"Geen open positie gevonden voor {payload.symbol} en geen quantity opgegeven.")
#     order = MarketOrder(close_side, qty, outsideRth=payload.outside_rth)
#     trade = ib.placeOrder(contract, order)
#     await asyncio.sleep(0.3)
#     return {
#         "orderId": trade.order.orderId,
#         "contract": contract.localSymbol or contract.symbol,
#         "side": close_side,
#         "quantity": qty,
#     }
#
#
# async def ibkr_cancel_all(payload: "IbkrAlertPayload") -> dict:
#     """Annuleer alle openstaande orders voor dit contract."""
#     ib = await _get_ib()
#     contract = _ibkr_make_contract(
#         payload.symbol, payload.sec_type, payload.exchange,
#         payload.currency, payload.expiry
#     )
#     qualified = await ib.qualifyContractsAsync(contract)
#     con_id = qualified[0].conId if qualified else None
#     open_trades = ib.openTrades()
#     cancelled = 0
#     for trade in open_trades:
#         if con_id and trade.contract.conId != con_id:
#             continue
#         ib.cancelOrder(trade.order)
#         cancelled += 1
#     return {"cancelled": cancelled, "symbol": payload.symbol}
#
#
# async def ibkr_move_sl_to_breakeven(payload: "IbkrAlertPayload") -> dict:
#     """Verplaats de bestaande stop-order naar de opgegeven stop_loss_price."""
#     ib = await _get_ib()
#     contract = _ibkr_make_contract(
#         payload.symbol, payload.sec_type, payload.exchange,
#         payload.currency, payload.expiry
#     )
#     qualified = await ib.qualifyContractsAsync(contract)
#     con_id = qualified[0].conId if qualified else None
#     open_trades = ib.openTrades()
#     stop_trades = [
#         t for t in open_trades
#         if t.order.orderType in ("STP", "STOP")
#         and (con_id is None or t.contract.conId == con_id)
#     ]
#     if not stop_trades:
#         raise Exception(f"Geen open stop-order gevonden voor {payload.symbol}.")
#     modified = []
#     for trade in stop_trades:
#         trade.order.auxPrice = payload.stop_loss_price
#         ib.placeOrder(trade.contract, trade.order)
#         modified.append(trade.order.orderId)
#     await asyncio.sleep(0.3)
#     return {"modified_orders": modified, "new_sl": payload.stop_loss_price}
#
#
# async def ibkr_check_connection() -> dict:
#     """Test de verbinding met IB Gateway."""
#     if not IB_INSYNC_AVAILABLE:
#         return {"valid": False, "status": "not_installed",
#                 "reason": "ib_insync niet geïnstalleerd"}
#     try:
#         ib = await _get_ib()
#         accounts = ib.managedAccounts()
#         return {"valid": True, "status": "ok", "accounts": accounts}
#     except Exception as e:
#         return {"valid": False, "status": "unreachable", "reason": str(e)}
#
#
# class IbkrAlertPayload(BaseModel):
#     secret: str
#     action: str
#     symbol: str
#     sec_type: str = "STK"
#     exchange: str = "SMART"
#     currency: str = "USD"
#     expiry: str = ""
#     entry_price: Optional[float] = None
#     stop_loss_price: Optional[float] = None
#     take_profit_price: Optional[float] = None
#     quantity: Optional[float] = None
#     outside_rth: bool = False
#
#
# router = APIRouter(tags=["IBKR"])
#
#
# @router.post("/webhook")
# async def receive_ibkr_alert(payload: IbkrAlertPayload):
#     if payload.secret != WEBHOOK_SECRET:
#         raise HTTPException(status_code=401, detail="Unauthorized")
#     action = payload.action.lower()
#     if action in OPEN_ACTIONS:
#         try:
#             response = await ibkr_place_entry(payload)
#             logger.info("IBKR order geplaatst: %s", response)
#             return {"status": "ok", "action": action, **response}
#         except Exception as e:
#             logger.error("IBKR order mislukt: %s", e)
#             raise HTTPException(status_code=500, detail=str(e))
#     if action in CLOSE_ACTIONS:
#         try:
#             response = await ibkr_close_position(payload)
#             logger.info("IBKR positie gesloten: %s", response)
#             return {"status": "ok", "action": action, **response}
#         except Exception as e:
#             logger.error("IBKR sluiten mislukt: %s", e)
#             raise HTTPException(status_code=500, detail=str(e))
#     if action == "cancel":
#         try:
#             response = await ibkr_cancel_all(payload)
#             logger.info("IBKR orders geannuleerd: %s", response)
#             return {"status": "ok", "action": action, **response}
#         except Exception as e:
#             logger.error("IBKR annuleren mislukt: %s", e)
#             raise HTTPException(status_code=500, detail=str(e))
#     if action == "move_sl_be":
#         if payload.stop_loss_price is None:
#             raise HTTPException(status_code=400, detail="stop_loss_price is verplicht bij move_sl_be")
#         try:
#             response = await ibkr_move_sl_to_breakeven(payload)
#             logger.info("IBKR break-even gezet: %s", response)
#             return {"status": "ok", "action": action, **response}
#         except Exception as e:
#             logger.error("IBKR break-even mislukt: %s", e)
#             raise HTTPException(status_code=500, detail=str(e))
#     valid = sorted(ORDER_ACTIONS) + ["cancel", "move_sl_be"]
#     raise HTTPException(status_code=400, detail="Ongeldig action. Gebruik: " + str(valid))
#
#
# @router.get("/keycheck")
# async def ibkr_keycheck(secret: str = "", x_webhook_secret: str = Header(default="")):
#     """Test verbinding met IB Gateway."""
#     provided = x_webhook_secret or secret
#     if provided != WEBHOOK_SECRET:
#         raise HTTPException(status_code=401, detail="Unauthorized")
#     try:
#         import importlib
#         importlib.import_module("ib_insync")
#         return {"valid": True, "status": "module_ok"}
#     except ImportError:
#         return {"valid": False, "status": "ib_insync_not_installed"}
