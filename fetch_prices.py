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

import requests
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

    # Yahoo Finance a volte restituisce Close = NaN per l'ultimo giorno di
    # alcuni titoli (in particolare su alcune borse europee). NaN non è un
    # valore JSON valido (romperebbe il file per l'intera app), e comunque
    # vogliamo il prezzo di OGGI, non un giorno precedente spacciato per
    # quello odierno: se il giorno più recente è NaN trattiamo il dato come
    # assente, così il chiamante può provare una fonte di riserva (Stooq)
    # prima di arrendersi.
    if close != close:  # NaN è l'unico valore che non è uguale a se stesso
        return None
    if previous_close is not None and previous_close != previous_close:
        previous_close = None

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
        if isinstance(pe, (int, float)) and pe == pe:  # esclude anche qui i NaN
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


# Fonte di riserva quando Yahoo Finance non ha una chiusura fresca per un
# titolo (capita più spesso per alcune borse europee). Stooq è un fornitore
# di dati di mercato pubblico e gratuito (non è il sito ufficiale della
# borsa, ma pubblica dati di chiusura sourced dai mercati reali), con un
# semplice export CSV. La mappatura dei simboli qui sotto è un primo
# tentativo plausibile: va confermata/corretta al primo test reale su
# GitHub Actions, verificando che la data restituita sia davvero quella
# odierna (o comunque abbastanza fresca) e non un errore silenzioso.
STOOQ_SYMBOL_MAP = {
    "IFX.DE": "ifx.de",
    "SY1.DE": "sy1.de",
    "LR.PA": "lr.fr",
}


def fetch_quote_stooq(symbol: str):
    stooq_symbol = STOOQ_SYMBOL_MAP.get(symbol)
    if not stooq_symbol:
        return None
    try:
        resp = requests.get(
            "https://stooq.com/q/d/l/",
            params={"s": stooq_symbol, "i": "d"},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        lines = [ln for ln in resp.text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return None  # nessun dato: simbolo sconosciuto a Stooq o non disponibile
        header = lines[0].split(",")
        last_row = dict(zip(header, lines[-1].split(",")))
        date_str = last_row.get("Date")
        close_str = last_row.get("Close")
        if not date_str or not close_str:
            return None
        quote_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        close = float(close_str)
        if close != close:
            return None

        previous_close = None
        if len(lines) >= 3:
            prev_row = dict(zip(header, lines[-2].split(",")))
            try:
                pc = float(prev_row.get("Close"))
                previous_close = pc if pc == pc else None
            except (TypeError, ValueError):
                previous_close = None

        return {
            "close": round(close, 4),
            "previousClose": round(previous_close, 4) if previous_close is not None else None,
            "date": quote_date,
            "currency": None,  # Stooq non indica la valuta nel CSV: la deduciamo da stocks.json
            "peRatio": None,  # Stooq non fornisce il P/E
        }
    except Exception as exc:
        print(f"Stooq: errore nello scaricare {symbol} ({stooq_symbol}): {exc}")
        return None


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
            print(f"Errore nello scaricare {symbol} da Yahoo Finance: {exc}")

        # Vogliamo assolutamente il prezzo di OGGI: se Yahoo Finance non ce
        # l'ha (dato mancante/NaN o troppo vecchio), proviamo Stooq come
        # fonte di riserva prima di arrenderci e segnare "non disponibile".
        if not (quote and is_fresh_enough(quote["date"], today)):
            stooq_quote = None
            try:
                stooq_quote = fetch_quote_stooq(symbol)
            except Exception as exc:
                print(f"Errore nello scaricare {symbol} da Stooq: {exc}")
            if stooq_quote and is_fresh_enough(stooq_quote["date"], today):
                # Stooq non fornisce valuta/P-E: li recuperiamo da Yahoo se
                # disponibili (anche se il suo prezzo era scartato), altrimenti
                # lasciamo che il fallback su stock.get("currency") più sotto
                # se ne occupi.
                stooq_quote["currency"] = stooq_quote["currency"] or (quote or {}).get("currency")
                stooq_quote["peRatio"] = stooq_quote["peRatio"] or (quote or {}).get("peRatio")
                quote = stooq_quote
                print(f"{symbol}: usato Stooq come fonte di riserva (Yahoo non aveva il dato di oggi).")

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
