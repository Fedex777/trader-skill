#!/usr/bin/env python3
"""Eigener Zusatz (nicht Teil des Original-Skill-Pakets): macht aus dem
"yes"-Gate von ticket.py einen Telegram-Workflow, damit Freigaben nicht
mehr per docker exec/Claude-Chat erfolgen muessen. Laeuft dauerhaft im
Container (siehe Dockerfile CMD).

NUR Paper-Account: der IB-Adapter (ib_gateway_order.py) verbindet fest mit
ib-gateway-paper:4004 -- es gibt in diesem Skript und im ganzen trader-
skill-test-Container KEINEN Pfad zu einem Live-Konto. Ein Telegram-Tap kann
also nie mehr als eine Paper-Order auslosen.

Ablauf je Zyklus (Default alle 10s, fuer schnelles Feedback nach einem Tap):
  1. trader-risk/check.py laufen lassen (aktualisiert state/risk.json fuer
     alle aktuellen orders.json-Eintraege -- Alpaca-Fingerprint-Logik,
     siehe ticket.py).
  2. Jedes READY Ticket, das noch nicht gepostet wurde (Dedup-Schluessel
     Datum+Fingerprint), als Telegram-Nachricht mit Inline-Buttons posten.
  3. Telegram-Updates abholen (getUpdates, Offset-Datei), auf Button-Taps
     reagieren: approve -> ticket.py approve + ib_gateway_order.py place;
     decline -> ticket.py decline. Danach die Original-Nachricht editieren
     (Buttons entfernen, Ergebnis anzeigen) + answerCallbackQuery.

    python3 telegram_approval_bot.py run              # Dauerbetrieb
    python3 telegram_approval_bot.py run --once        # ein Zyklus, zum Testen
"""
import argparse
import json
import os
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket  # noqa: E402  (eigenes Modul im selben Ordner)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.join(HERE, "..")
POSTED_PATH = os.path.join(SKILL_ROOT, "trader", "state", "telegram_posted.json")
OFFSET_PATH = os.path.join(SKILL_ROOT, "trader", "state", "telegram_offset.json")
RISK_SCRIPT = os.path.join(SKILL_ROOT, "trader-risk", "check.py")
IB_ADAPTER = os.path.join(HERE, "ib_gateway_order.py")


def _api(method, **params):
    if not TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[telegram_approval_bot] API-Fehler ({method}): {e}", file=sys.stderr)
        return None


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _default_args():
    return argparse.Namespace(
        orders="trader/orders.json", config="trader/config.json",
        risk="trader/state/risk.json", state="trader/state/broker.json",
        portfolio="trader/portfolio.json", today=None,
    )


def run_risk_check():
    subprocess.run([sys.executable, RISK_SCRIPT], cwd=SKILL_ROOT, capture_output=True)


def post_ready_tickets():
    a = _default_args()
    try:
        orders, cfg, risk, state, today = ticket._load(a)
    except ticket.TicketError:
        return
    posted = _load_json(POSTED_PATH, {})
    changed = False
    for i, order in enumerate(orders, 1):
        fp = ticket.fingerprint(order)
        ready, _, _ = ticket.status_of(order, risk, state, today)
        key = "%s:%s" % (today, fp)
        if not ready or key in posted:
            continue
        text, _ = ticket.render(order, i, len(orders), cfg, risk, state, today)
        resp = _api("sendMessage", chat_id=CHAT_ID,
                     text="\U0001f9ea trader-skill (PAPER) -- Order wartet auf Freigabe:\n\n" + text,
                     reply_markup={"inline_keyboard": [[
                         {"text": "✅ Freigeben", "callback_data": "approve:%d:%s" % (i, fp)},
                         {"text": "❌ Ablehnen", "callback_data": "decline:%d:%s" % (i, fp)},
                     ]]})
        if resp and resp.get("ok"):
            posted[key] = resp["result"]["message_id"]
            changed = True
    if changed:
        _save_json(POSTED_PATH, posted)


def _clear_buttons_and_set_text(chat_id, message_id, text):
    _api("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
         reply_markup={"inline_keyboard": []})


def _is_authorized(cq):
    """Whitelist: nur der eine konfigurierte Chat (TELEGRAM_CHAT_ID) darf
    Freigaben erteilen -- sowohl der tippende User als auch der Chat, in dem
    die Nachricht steht, muessen damit uebereinstimmen (schuetzt auch, falls
    der Bot je in eine Gruppe eingeladen wird, in der jemand anderes tippt)."""
    from_id = str(cq.get("from", {}).get("id", ""))
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    return bool(CHAT_ID) and from_id == str(CHAT_ID) and chat_id == str(CHAT_ID)


def handle_callback(cq):
    if not _is_authorized(cq):
        from_id = cq.get("from", {}).get("id")
        from_name = cq.get("from", {}).get("username") or cq.get("from", {}).get("first_name") or "?"
        print(f"[telegram_approval_bot] UNAUTORISIERTER Tap von user_id={from_id} ({from_name}) -- ignoriert.",
              file=sys.stderr)
        _api("answerCallbackQuery", callback_query_id=cq["id"],
             text="Nicht autorisiert.", show_alert=True)
        _api("sendMessage", chat_id=CHAT_ID,
             text="⚠️ Unautorisierter Freigabe-Versuch von user_id=%s (%s) blockiert." % (from_id, from_name))
        return

    data = cq.get("data", "")
    parts = data.split(":")
    if len(parts) != 3:
        return
    action, idx_s, fp = parts
    idx = int(idx_s)
    message_id = cq["message"]["message_id"]
    chat_id = cq["message"]["chat"]["id"]

    a = _default_args()
    try:
        orders, cfg, risk, state, today = ticket._load(a)
    except ticket.TicketError:
        _api("answerCallbackQuery", callback_query_id=cq["id"], text="Keine Orders mehr vorhanden.")
        _clear_buttons_and_set_text(chat_id, message_id, "⚠️ Veraltet (keine Orders mehr): ignoriert.")
        return
    if idx < 1 or idx > len(orders) or ticket.fingerprint(orders[idx - 1]) != fp:
        _api("answerCallbackQuery", callback_query_id=cq["id"], text="Ticket veraltet -- ignoriert.")
        _clear_buttons_and_set_text(chat_id, message_id, "⚠️ Veraltet (orders.json geaendert): ignoriert.")
        return
    order = orders[idx - 1]
    plain = ticket.plain(order)

    if action == "decline":
        a.index, a.reply = idx, "no"
        ticket.cmd_decline(a)
        _api("answerCallbackQuery", callback_query_id=cq["id"], text="Abgelehnt.")
        _clear_buttons_and_set_text(chat_id, message_id, "❌ Abgelehnt: " + plain)
        return

    # approve
    ready, label, _ = ticket.status_of(order, risk, state, today)
    if not ready:
        _api("answerCallbackQuery", callback_query_id=cq["id"], text="Nicht mehr bereit: " + label)
        _clear_buttons_and_set_text(chat_id, message_id, "⚠️ Nicht mehr bereit (%s): %s" % (label, plain))
        return
    a.index, a.reply = idx, "yes"
    ticket.cmd_approve(a)
    _api("answerCallbackQuery", callback_query_id=cq["id"], text="✅ Bestätigt")
    _clear_buttons_and_set_text(chat_id, message_id, "✅ Bestätigt -- wird an IB (Paper) gesendet: " + plain)

    result = subprocess.run([sys.executable, IB_ADAPTER, "place", "--index", str(idx)],
                             cwd=SKILL_ROOT, capture_output=True, text=True)
    ok = result.returncode == 0
    suffix = "✅ Platziert (Paper)" if ok else "⚠️ Fehler beim Senden"
    detail = (result.stdout or result.stderr or "").strip()[-600:]
    _clear_buttons_and_set_text(chat_id, message_id, "%s: %s\n\n%s" % (suffix, plain, detail))


def poll_updates():
    offset = _load_json(OFFSET_PATH, {}).get("offset", 0)
    resp = _api("getUpdates", offset=offset, timeout=5)
    if not resp or not resp.get("ok"):
        print(f"[telegram_approval_bot] getUpdates nicht ok: {resp}", file=sys.stderr)
        return
    if resp["result"]:
        print(f"[telegram_approval_bot] {len(resp['result'])} Update(s) erhalten: "
              f"{[list(u.keys()) for u in resp['result']]}")
    new_offset = offset
    for upd in resp["result"]:
        new_offset = upd["update_id"] + 1
        if "callback_query" in upd:
            print(f"[telegram_approval_bot] Callback: {upd['callback_query'].get('data')}")
            try:
                handle_callback(upd["callback_query"])
            except Exception as e:
                print(f"[telegram_approval_bot] Fehler bei Callback: {e}", file=sys.stderr)
    if new_offset != offset:
        _save_json(OFFSET_PATH, {"offset": new_offset})


def run(once=False, interval=10):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nicht gesetzt -- Bot bleibt inaktiv "
              "(trader-skill-test/.env ausfuellen).", file=sys.stderr)
        if once:
            return
        while True:
            time.sleep(interval)
    while True:
        try:
            run_risk_check()
            post_ready_tickets()
            poll_updates()
        except Exception as e:
            print(f"[telegram_approval_bot] Fehler im Zyklus: {e}", file=sys.stderr)
        if once:
            return
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true")
    r.add_argument("--interval", type=int, default=10)
    a = p.parse_args()
    run(once=a.once, interval=a.interval)


if __name__ == "__main__":
    main()
