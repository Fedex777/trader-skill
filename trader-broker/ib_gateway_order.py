#!/usr/bin/env python3
"""Eigener Zusatz (nicht Teil des Original-Skill-Pakets, siehe README dort):
nimmt eine von ticket.py bereits APPROVED Order (ticket.py approve --index N
--reply "yes") und sendet sie DIREKT an das lokale IB-Gateway (ib_insync),
statt an Alpaca oder den IBKR-MCP-Connector. Verlangt denselben "approved"-
Status wie ticket.py selbst -- das Yes-Gate (ein Wort, ein Order, Fingerprint-
Match gegen trader-risk) bleibt vollstaendig erhalten, dieses Skript ersetzt
NUR den letzten Schritt (den eigentlichen place_stock_order-Aufruf).

Alle Orders bekommen orderRef="TRADER_SKILL" -- damit sind sie im gemeinsam
genutzten Paper-Account/Journal eindeutig von den Produktiv-Bots und vom
day-trader-skill-Sandbox (orderRef=DAYTRADER_SKILL) unterscheidbar.

Schreibt das Ergebnis danach selbst in trader/state/broker.json zurueck
(per Aufruf von ticket.py result -- keine doppelte State-Logik).

    python3 ib_gateway_order.py place --index 1
"""
import argparse
import json
import os
import random
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket  # noqa: E402  (eigenes Modul im selben Ordner)

IB_HOST = os.environ.get("IB_HOST", "ib-gateway-paper")
IB_PORT = int(os.environ.get("IB_PORT", "4004"))
ORDER_REF = "TRADER_SKILL"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader", "orders_log.jsonl")


def _connect():
    from ib_insync import IB
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=random.randint(80000, 89000), timeout=20)
    return ib


def _find_existing_order(ib, signature):
    """Duplicate-Guard, identisch im Prinzip zum day-trader-skill-Sandbox
    (Vorfall 2026-11-22): orderId ist pro IB-Verbindung neu vergeben, nur
    permId ist wirklich eindeutig. Schaut in orders_log.jsonl nach der
    letzten Order mit demselben Fingerprint und prueft bei IB, ob sie noch
    aktiv/gefuellt ist."""
    last_match = None
    try:
        with open(LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("signature") == signature:
                    last_match = entry
    except FileNotFoundError:
        return None
    if not last_match:
        return None

    perm_ids = {leg.get("permId") for leg in last_match["legs"] if leg.get("permId")}
    if not perm_ids:
        return None

    ib.reqAllOpenOrders()
    ib.sleep(2)
    by_perm = {t.order.permId: t for t in ib.trades()}
    for pid in perm_ids:
        t = by_perm.get(pid)
        if t and t.orderStatus.status not in ("Cancelled", "ApiCancelled", "Inactive"):
            return last_match
    for t in ib.reqCompletedOrders(apiOnly=False):
        if t.order.permId in perm_ids and t.orderStatus.status == "Filled":
            return last_match
    return None


def _log_activity(symbol, side, qty, is_bracket, legs, signature):
    """Append-only Log fuer die Django-Monitor-Seite (range_trader_django,
    liest dieselbe Datei ueber einen geteilten Einzel-Datei-Mount)."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "symbol": symbol, "side": side, "qty": qty, "bracket": is_bracket,
        "legs": legs, "signature": signature,
    }
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[Log] Fehler: {e}", file=sys.stderr)


def _notify(text):
    try:
        import notify
        notify.send_telegram(text)
    except Exception as e:
        print(f"[Telegram] Fehler: {e}", file=sys.stderr)


def cmd_place(a):
    orders, cfg, risk, state, today = ticket._load(a)
    order = ticket._pick(orders, a.index)
    fp = ticket.fingerprint(order)
    rec = ticket.latest(state, fp, today)
    if not (rec and rec.get("status") == "approved"):
        ready, label, _ = ticket.status_of(order, risk, state, today)
        print("FEHLER: Order %d hat heute kein 'approved' von ticket.py (Status: %s) -- "
              "erst 'ticket.py approve --index %d --reply \"yes\"' ausfuehren."
              % (a.index, label, a.index), file=sys.stderr)
        sys.exit(1)

    tool_input = ticket.tool_input(order)
    symbol = order["symbol"]
    side = str(order["side"]).upper()

    if tool_input.get("qty") is None and tool_input.get("notional") is not None:
        print("FEHLER: notional-Order (%s $) -- dieser IB-Adapter unterstuetzt nur Stueckzahl "
              "(qty), kein notional (IB bildet das fuer Aktien nicht ab). Nicht gesendet."
              % tool_input["notional"], file=sys.stderr)
        sys.exit(1)
    qty = int(float(tool_input["qty"]))
    tp = tool_input.get("take_profit_limit_price")
    sl = tool_input.get("stop_loss_stop_price")
    is_bracket = tool_input.get("order_class") == "bracket" or tp is not None or sl is not None
    tif = str(tool_input.get("time_in_force", "day")).upper()

    ib = _connect()
    try:
        existing = _find_existing_order(ib, fp)
        if existing is not None:
            print("Bereits platzierte Order mit identischer Absicht gefunden -- "
                  "keine neue Order gesendet:")
            print(json.dumps(existing, indent=2))
            return

        from ib_insync import Stock, MarketOrder, LimitOrder
        stock = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(stock)

        if is_bracket:
            if tp is None or sl is None:
                print("FEHLER: Bracket braucht sowohl take_profit_limit_price als auch "
                      "stop_loss_stop_price", file=sys.stderr)
                sys.exit(1)
            entry_price = (float(tool_input["limit_price"])
                            if tool_input.get("type") == "limit" and tool_input.get("limit_price") else None)
            parent, take_profit, stop_loss = ib.bracketOrder(
                side, qty, limitPrice=entry_price, takeProfitPrice=float(tp), stopLossPrice=float(sl)
            )
            if entry_price is None:
                parent = MarketOrder(side, qty)
            for leg in (parent, take_profit, stop_loss):
                leg.orderRef = ORDER_REF
                leg.tif = tif
            trades = [ib.placeOrder(stock, parent)]
            ib.sleep(1)
            take_profit.parentId = parent.orderId
            stop_loss.parentId = parent.orderId
            trades.append(ib.placeOrder(stock, take_profit))
            trades.append(ib.placeOrder(stock, stop_loss))
        else:
            if tool_input.get("type") == "limit" and tool_input.get("limit_price"):
                o = LimitOrder(side, qty, float(tool_input["limit_price"]))
            else:
                o = MarketOrder(side, qty)
            o.orderRef = ORDER_REF
            o.tif = tif
            trades = [ib.placeOrder(stock, o)]

        for _ in range(10):
            ib.sleep(3)
            if all(t.orderStatus.status in ("Filled", "Submitted", "PreSubmitted") for t in trades):
                break

        result = [{"orderId": t.order.orderId, "permId": t.order.permId, "status": t.orderStatus.status,
                    "avgFillPrice": t.orderStatus.avgFillPrice} for t in trades]
        print(json.dumps({"orderRef": ORDER_REF, "legs": result}, indent=2))
        _log_activity(symbol, side, qty, is_bracket, result, fp)
        _notify("🧪 [trader-skill] %s %s %s @ IB (%s)" % (side, qty, symbol, result[0]["status"]))

        parent_trade = trades[0]
        status_args = ["result", "--index", str(a.index), "--status", "placed",
                        "--broker-id", str(parent_trade.order.permId),
                        "--state", a.state, "--config", a.config, "--orders", a.orders]
        if parent_trade.orderStatus.status == "Filled":
            status_args += ["--filled-qty", str(qty), "--filled-price", str(parent_trade.orderStatus.avgFillPrice),
                             "--portfolio", a.portfolio]
        subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket.py")]
                        + status_args, check=False)
    finally:
        ib.disconnect()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("place")
    pl.add_argument("--index", type=int, required=True)
    pl.add_argument("--orders", default="trader/orders.json")
    pl.add_argument("--config", default="trader/config.json")
    pl.add_argument("--risk", default="trader/state/risk.json")
    pl.add_argument("--state", default="trader/state/broker.json")
    pl.add_argument("--portfolio", default="trader/portfolio.json")
    pl.add_argument("--today")
    a = p.parse_args()
    {"place": cmd_place}[a.cmd](a)


if __name__ == "__main__":
    main()
