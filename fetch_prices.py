#!/usr/bin/env python3
"""
Aggiorna prices.json leggendo l'elenco titoli/soglie da stocks.json.

Regola di freschezza: un prezzo viene accettato SOLO se la sua data di
chiusura (quella riportata dalla fonte dati) non è più vecchia di 3 giorni
di calendario rispetto ad oggi (margine per weekend/festivi). Se il prezzo
più recente disponibile è più vecchio, il titolo viene scritto come "non
disponibile" (close: null) invece di mostrare un dato superato spacciato
per attuale, ed eventuali vecchi allarmi vengono azzerati.

Se il file non trova una nuova soglia superata rispetto all'ultimo
prices.json, non invia nulla. Se trova una transizione fresca verso
"upper"/"lower" e sono configurate le variabili d'ambiente SMTP_USER,
SMTP_PASS (e opzionalmente SMTP_TO, SMTP_HOST, SMTP_PORT) come GitHub
Secrets, invia un'email di allarme.
"""

import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText

import yfinance as yf

STOCKS_FILE = "stocks.json"
PRICES_FILE = "prices.json"
MAX_STALE_DAYS = 3


def load_stocks():
    with open(STOCKS_FILE, encoding="utf-8") as f:
        return json.load(f).get("stocks", [])


def load_previous_prices():
    try:
        with open(PRICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"generatedAt": None, "quotes": {}}


def fetch_quote(symbol: str):
    t = yf.Ticker(symbol)
    hist = t.history(period="10d", auto_adjust=False)
    if hist.empty:
        return None

    last_date = hist.index[-1].to_pydatetime().date()
    close = float(hist.iloc[-1]["Close"])
    previous_close = float(hist.iloc[-2]["Close"]) if len(hist) >= 2 else None

    currency = None
    pe_ratio = None
    try:
        fast = t.fast_info
        currency = fast.get("currency") if isinstance(fast, dict) else getattr(fast, "currency", None)
    except Exception:
        pass
    try:
        info = t.info or {}
        if not currency:
            currency = info.get("currency")
        pe = info.get("trailingPE")
        if isinstance(pe, (int, float)):
            pe_ratio = round(pe, 2)
    except Exception:
        pass

    return {
        "close": round(close, 4),
        "previousClose": round(previous_close, 4) if previous_close is not None else None,
        "date": last_date,
        "currency": currency,
        "peRatio": pe_ratio,
    }


def is_fresh_enough(quote_date, today) -> bool:
    if quote_date > today:
        return False
    return (today - quote_date).days <= MAX_STALE_DAYS


def compute_alert_status(stock: dict, price):
    if price is None:
        return "none"
    ref = stock.get("referencePrice")
    upper = None
    lower = None
    if stock.get("upperType") == "price" and stock.get("upperValue") is not None:
        upper = stock["upperValue"]
    elif stock.get("upperType") == "pct" and stock.get("upperValue") is not None and ref:
        upper = ref * (1 + stock["upperValue"] / 100)
    if stock.get("lowerType") == "price" and stock.get("lowerValue") is not None:
        lower = stock["lowerValue"]
    elif stock.get("lowerType") == "pct" and stock.get("lowerValue") is not None and ref:
        lower = ref * (1 - stock["lowerValue"] / 100)
    if upper is not None and price >= upper:
        return "upper"
    if lower is not None and price <= lower:
        return "lower"
    return "none"


def send_alert_email(alerts):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("SMTP_TO") or user
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    if not (user and password and to_addr):
        print("Variabili SMTP_USER/SMTP_PASS non configurate: salto l'invio dell'email (i dati restano comunque aggiornati in prices.json).")
        return

    lines = ["Allarme Watchlist Titoli", ""]
    for a in alerts:
        verb = "ha superato la soglia di rialzo" if a["status"] == "upper" else "ha superato la soglia di ribasso"
        currency = (a.get("currency") or "").strip()
        lines.append(
            f"- {a['name']} ({a['ticker']}) {verb}: prezzo {a['close']} {currency} "
            f"(prezzo di riferimento {a['referencePrice']}, dato del {a['asOfDate']})"
        )
    lines.append("")
    lines.append("Aggiornamento generato automaticamente dalla GitHub Action Watchlist Titoli.")
    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "🔔 Allarme Watchlist Titoli"
    msg["From"] = user
    msg["To"] = to_addr

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx) as server:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print("Email di allarme inviata a", to_addr)


def main():
    stocks = load_stocks()
    previous = load_previous_prices()
    prev_quotes = previous.get("quotes", {})
    today = datetime.now(timezone.utc).date()

    quotes = {}
    new_alerts = []

    for stock in stocks:
        symbol = stock.get("symbol")
        if not symbol:
            continue
        prev_alert = (prev_quotes.get(symbol) or {}).get("alertStatus", "none")

        quote = None
        try:
            quote = fetch_quote(symbol)
        except Exception as exc:
            print(f"Errore nello scaricare {symbol}: {exc}")

        if quote and is_fresh_enough(quote["date"], today):
            status = compute_alert_status(stock, quote["close"])
            quotes[symbol] = {
                "close": quote["close"],
                "previousClose": quote["previousClose"],
                "asOfDate": quote["date"].isoformat(),
                "currency": quote["currency"] or stock.get("currency"),
                "peRatio": quote["peRatio"],
                "alertStatus": status,
            }
            if status in ("upper", "lower") and status != prev_alert:
                new_alerts.append({
                    "ticker": stock.get("ticker"),
                    "name": stock.get("name"),
                    "status": status,
                    "close": quote["close"],
                    "currency": quote["currency"] or stock.get("currency"),
                    "referencePrice": stock.get("referencePrice"),
                    "asOfDate": quote["date"].isoformat(),
                })
        else:
            # Nessun prezzo con una chiusura abbastanza recente: "non disponibile",
            # mai un dato vecchio spacciato per attuale. Azzera anche un eventuale
            # vecchio allarme, perché non è più verificabile con dati freschi.
            pe_fallback = quote["peRatio"] if quote else (prev_quotes.get(symbol) or {}).get("peRatio")
            quotes[symbol] = {
                "close": None,
                "previousClose": None,
                "asOfDate": None,
                "currency": stock.get("currency"),
                "peRatio": pe_fallback,
                "alertStatus": "none",
            }
            if quote:
                print(f"{symbol}: scarto il prezzo trovato, troppo vecchio ({quote['date'].isoformat()}).")

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "quotes": quotes,
    }
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Scritto {PRICES_FILE} con {len(quotes)} titoli, {len(new_alerts)} nuovi allarmi.")
    if new_alerts:
        send_alert_email(new_alerts)


if __name__ == "__main__":
    main()
