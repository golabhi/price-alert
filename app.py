import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()
DB_PATH = os.getenv("DB_PATH", "stock_alerts.db")
TZ = ZoneInfo("Asia/Kolkata")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                label TEXT NOT NULL,
                market TEXT NOT NULL,
                condition TEXT NOT NULL CHECK(condition IN ('above', 'below')),
                target REAL NOT NULL,
                currency TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                triggered INTEGER NOT NULL DEFAULT 0,
                last_price REAL,
                last_checked TEXT,
                last_message TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                target REAL NOT NULL,
                condition TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def get_price(symbol):
    """Best-effort Yahoo Finance quote; not a guaranteed real-time feed."""
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
    # Daily close fallback; may be stale/out of market hours.
    try:
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        close = hist["Close"].dropna()
        if not close.empty:
            return float(close.iloc[-1])
    except Exception:
        pass
    raise RuntimeError("Could not retrieve a price. Check the symbol or try again later.")


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.")
    import asyncio
    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=message)
    try:
        asyncio.run(_send())
    except RuntimeError:
        # If called from an environment with an active event loop.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send())
        finally:
            loop.close()


def money(value, currency):
    return f"{currency} {value:,.4f}".rstrip("0").rstrip(".")


init_db()
st.set_page_config(page_title="Stock Price Alerts", page_icon="🔔", layout="wide")
st.title("🔔 Stock Price Alert Dashboard")
st.caption("Personal-use demo • Python + Streamlit + SQLite + Yahoo Finance + Telegram")
st.warning(
    "Yahoo Finance data is unofficial and may be delayed, stale, incomplete, or unavailable. "
    "Do not use this app as the sole source for trading decisions."
)

with st.sidebar:
    st.header("Telegram setup")
    token_set = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    chat_set = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())
    st.write("Bot token:", "✅ configured" if token_set else "❌ missing in .env")
    st.write("Chat ID:", "✅ configured" if chat_set else "❌ missing in .env")
    if st.button("Send test Telegram message", use_container_width=True):
        try:
            send_telegram("✅ Stock Alert app test message. Telegram notifications are working.")
            st.success("Test message sent. Check Telegram.")
        except Exception as exc:
            st.error(str(exc))
    st.divider()
    st.markdown("**Ticker examples**")
    st.code("India: RELIANCE.NS\nIndia: TCS.NS\nBSE: RELIANCE.BO\nUS: AAPL\nUS: MSFT\nUS: NVDA")

tab_add, tab_alerts, tab_history, tab_quote = st.tabs(
    ["➕ Create alert", "📋 My alerts", "🧾 Trigger history", "💹 Check a price"]
)

with tab_add:
    st.subheader("Create a price alert")
    with st.form("new_alert", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            symbol = st.text_input("Yahoo Finance ticker", placeholder="e.g. RELIANCE.NS").strip().upper()
            label = st.text_input("Display name (optional)", placeholder="e.g. Reliance Industries")
            market = st.selectbox("Market", ["India (NSE)", "India (BSE)", "United States", "Other"])
        with col2:
            condition = st.selectbox("Alert when price is", ["above", "below"])
            target = st.number_input("Target price", min_value=0.0001, value=100.0, step=1.0, format="%.4f")
            currency = st.selectbox("Currency", ["INR", "USD", "Other"])
        submitted = st.form_submit_button("Create alert", type="primary")
        if submitted:
            if not symbol:
                st.error("Enter a ticker symbol.")
            else:
                try:
                    current = get_price(symbol)
                    display_name = label or symbol
                    with connect_db() as conn:
                        cur = conn.execute("""
                            INSERT INTO alerts
                            (symbol, label, market, condition, target, currency, last_price,
                             last_checked, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            symbol, display_name, market, condition, float(target), currency,
                            current, datetime.now(TZ).isoformat(), datetime.now(TZ).isoformat()
                        ))
                        alert_id = cur.lastrowid
                    st.success(f"Alert #{alert_id} created. Latest available price: {money(current, currency)}")
                    st.info("The background worker must be running for automatic alerts.")
                except Exception as exc:
                    st.error(f"Could not create alert: {exc}")

with tab_alerts:
    st.subheader("Your alerts")
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM alerts ORDER BY id DESC").fetchall()
    if not rows:
        st.info("No alerts yet. Create one in the first tab.")
    else:
        df = pd.DataFrame([dict(r) for r in rows])
        view = df[["id", "symbol", "label", "market", "condition", "target", "currency",
                   "enabled", "triggered", "last_price", "last_checked"]].copy()
        view["enabled"] = view["enabled"].map({1: "Yes", 0: "No"})
        view["triggered"] = view["triggered"].map({1: "Yes", 0: "No"})
        st.dataframe(view, use_container_width=True, hide_index=True)
        st.caption("Alerts are one-shot: once triggered, they are marked triggered and won't send again unless re-armed.")
        alert_ids = [int(r["id"]) for r in rows]
        selected_id = st.selectbox("Select alert to manage", alert_ids)
        selected = next(r for r in rows if int(r["id"]) == selected_id)
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Re-arm / enable", use_container_width=True):
                with connect_db() as conn:
                    conn.execute("UPDATE alerts SET enabled=1, triggered=0 WHERE id=?", (selected_id,))
                st.rerun()
        with c2:
            if st.button("Disable", use_container_width=True):
                with connect_db() as conn:
                    conn.execute("UPDATE alerts SET enabled=0 WHERE id=?", (selected_id,))
                st.rerun()
        with c3:
            if st.button("Delete alert", type="secondary", use_container_width=True):
                with connect_db() as conn:
                    conn.execute("DELETE FROM alerts WHERE id=?", (selected_id,))
                    conn.execute("DELETE FROM alert_events WHERE alert_id=?", (selected_id,))
                st.rerun()

with tab_history:
    st.subheader("Triggered alerts")
    with connect_db() as conn:
        events = conn.execute("SELECT * FROM alert_events ORDER BY id DESC LIMIT 500").fetchall()
    if events:
        hist_df = pd.DataFrame([dict(r) for r in events])
        st.dataframe(hist_df[["created_at", "symbol", "condition", "target", "price", "message"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No alerts have triggered yet.")

with tab_quote:
    st.subheader("Check a stock price manually")
    with st.form("quote_form"):
        quote_symbol = st.text_input("Ticker symbol", placeholder="e.g. AAPL or TCS.NS").strip().upper()
        quote_currency = st.selectbox("Display currency", ["INR", "USD", "Other"], key="quote_currency")
        quote_submit = st.form_submit_button("Fetch price")
    if quote_submit:
        try:
            price = get_price(quote_symbol)
            st.metric(quote_symbol, money(price, quote_currency))
            st.caption("This is the latest price Yahoo Finance made available, not a guaranteed live quote.")
        except Exception as exc:
            st.error(str(exc))

st.divider()
st.caption("Keep this dashboard open to use it. For automatic monitoring, run worker.py in a separate terminal.")
