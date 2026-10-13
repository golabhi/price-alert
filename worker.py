"""Background checker. Run separately from Streamlit: python worker.py"""
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()
DB_PATH = os.getenv("DB_PATH", "stock_alerts.db")
CHECK_INTERVAL_SECONDS = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
TZ = ZoneInfo("Asia/Kolkata")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_price(symbol):
    ticker = yf.Ticker(symbol)
    try:
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if not hist.empty:
            close = hist["Close"].dropna()
            if not close.empty:
                return float(close.iloc[-1])
    except Exception:
        pass
    try:
        price = ticker.fast_info.get("last_price")
        if price is not None:
            return float(price)
    except Exception:
        pass
    hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
    close = hist["Close"].dropna()
    if close.empty:
        raise RuntimeError("No price data returned")
    return float(close.iloc[-1])


async def telegram_send(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram token/chat ID missing. Configure .env.")
    bot = Bot(token=token)
    await bot.send_message(chat_id=chat_id, text=message)


def send_message(message):
    import asyncio
    asyncio.run(telegram_send(message))


def currency_text(amount, currency):
    return f"{currency} {amount:,.4f}".rstrip("0").rstrip(".")


def check_once():
    with connect_db() as conn:
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE enabled=1 AND triggered=0 ORDER BY id"
        ).fetchall()

    for alert in alerts:
        try:
            price = get_price(alert["symbol"])
            now = datetime.now(TZ).isoformat()
            condition_met = (
                price >= float(alert["target"]) if alert["condition"] == "above"
                else price <= float(alert["target"])
            )
            with connect_db() as conn:
                conn.execute(
                    "UPDATE alerts SET last_price=?, last_checked=? WHERE id=?",
                    (price, now, alert["id"])
                )
            if condition_met:
                message = (
                    f"🔔 STOCK PRICE ALERT\n"
                    f"Stock: {alert['label']} ({alert['symbol']})\n"
                    f"Condition: price {alert['condition']} {currency_text(alert['target'], alert['currency'])}\n"
                    f"Available price: {currency_text(price, alert['currency'])}\n"
                    f"Market: {alert['market']}\n"
                    f"Checked: {now}\n\n"
                    f"Note: Yahoo Finance data may be delayed or stale."
                )
                # Send first; mark triggered only after Telegram confirms send.
                send_message(message)
                with connect_db() as conn:
                    conn.execute("""
                        UPDATE alerts SET triggered=1, enabled=0, last_message=?
                        WHERE id=? AND triggered=0
                    """, (message, alert["id"]))
                    conn.execute("""
                        INSERT INTO alert_events
                        (alert_id, symbol, price, target, condition, message, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        alert["id"], alert["symbol"], price, alert["target"],
                        alert["condition"], message, now
                    ))
                print(f"[TRIGGERED] #{alert['id']} {alert['symbol']} price={price}")
            else:
                print(f"[OK] #{alert['id']} {alert['symbol']} price={price}")
        except Exception as exc:
            print(f"[ERROR] #{alert['id']} {alert['symbol']}: {exc}")


if __name__ == "__main__":
    print(f"Stock alert worker started. Poll interval: {CHECK_INTERVAL_SECONDS}s")
    print("Press Ctrl+C to stop.")
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SECONDS)
