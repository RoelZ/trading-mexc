"""Gedeelde utilities voor alle exchange-modules."""
import math

# Mogelijke acties — gedeeld door alle exchanges
OPEN_ACTIONS: set[str] = {"open_long", "open_short"}
CLOSE_ACTIONS: set[str] = {"close_long", "close_short"}
ORDER_ACTIONS: set[str] = OPEN_ACTIONS | CLOSE_ACTIONS


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
