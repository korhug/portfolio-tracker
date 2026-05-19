"""
Streamlit Portfolio Tracker

Features
--------
- Manages multiple named portfolios.
- Records BUY, SELL, and DIVIDEND transactions in SQLite.
- Stores all transaction values in original currency and CHF.
- Aggregates multiple transactions per ticker into one portfolio row.
- Fetches latest prices with yfinance.
- Converts non-CHF market values into CHF using latest FX rates.
- Uses historical FX rates for transaction-date CHF conversion.
- Lets you inspect, edit, and delete transactions for a selected ticker.

Install
-------
pip install streamlit pandas yfinance

Run
---
streamlit run app.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
import yfinance as yf
try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None


DB_PATH = Path("portfolio.sqlite3")
BASE_CURRENCY = "CHF"
DEFAULT_PORTFOLIO_NAME = "2026 New"

ACTIONS = ["BUY", "SELL", "DIVIDEND"]


# -----------------------------
# Database layer
# -----------------------------

@st.cache_resource
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """
    Initialize and migrate the SQLite database.

    Migration behavior:
    - Creates a portfolios table if it does not exist.
    - Creates the default portfolio "SmallCaps" if it does not exist.
    - Creates transactions table if it does not exist.
    - If transactions already exists without portfolio_id, adds it.
    - Assigns all existing transactions to "SmallCaps".
    """
    conn = get_connection()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            include_in_grand_total INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    portfolio_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(portfolios)").fetchall()
    }

    if "include_in_grand_total" not in portfolio_columns:
        conn.execute(
            "ALTER TABLE portfolios ADD COLUMN include_in_grand_total INTEGER NOT NULL DEFAULT 1"
        )

    conn.execute(
        "INSERT OR IGNORE INTO portfolios (name, include_in_grand_total) VALUES (?, 1)",
        (DEFAULT_PORTFOLIO_NAME,),
    )

    default_portfolio_id = conn.execute(
        "SELECT id FROM portfolios WHERE name = ?",
        (DEFAULT_PORTFOLIO_NAME,),
    ).fetchone()["id"]

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER,
            action TEXT NOT NULL CHECK(action IN ('BUY', 'SELL', 'DIVIDEND')),
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            number_of_shares REAL NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            rate REAL NOT NULL,
            value REAL NOT NULL,
            value_chf REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE
        )
        """
    )

    transaction_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
    }

    if "portfolio_id" not in transaction_columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN portfolio_id INTEGER")

    conn.execute(
        "UPDATE transactions SET portfolio_id = ? WHERE portfolio_id IS NULL",
        (default_portfolio_id,),
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_ticker ON transactions (portfolio_id, ticker)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date)"
    )

    conn.commit()


def read_portfolios() -> pd.DataFrame:
    conn = get_connection()
    return pd.read_sql_query(
        """
        SELECT
            id,
            name,
            include_in_grand_total,
            created_at,
            updated_at
        FROM portfolios
        ORDER BY name
        """,
        conn,
    )


def get_default_portfolio_id() -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM portfolios WHERE name = ?",
        (DEFAULT_PORTFOLIO_NAME,),
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO portfolios (name) VALUES (?)", (DEFAULT_PORTFOLIO_NAME,))
        conn.commit()
        row = conn.execute(
            "SELECT id FROM portfolios WHERE name = ?",
            (DEFAULT_PORTFOLIO_NAME,),
        ).fetchone()
    return int(row["id"])


def create_portfolio(name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Portfolio name is required.")

    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM portfolios WHERE lower(name) = lower(?)",
        (name,),
    ).fetchone()
    if existing:
        raise ValueError(f"A portfolio named '{name}' already exists.")

    cur = conn.execute(
        "INSERT INTO portfolios (name, include_in_grand_total) VALUES (?, 1)",
        (name,),
    )
    conn.commit()
    return int(cur.lastrowid)


def rename_portfolio(portfolio_id: int, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Portfolio name is required.")

    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM portfolios WHERE lower(name) = lower(?) AND id <> ?",
        (new_name, portfolio_id),
    ).fetchone()
    if existing:
        raise ValueError(f"A portfolio named '{new_name}' already exists.")

    conn.execute(
        """
        UPDATE portfolios
        SET name = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (new_name, portfolio_id),
    )
    conn.commit()


def set_portfolio_include_in_grand_total(portfolio_id: int, include: bool) -> None:
    """Set whether a portfolio is included in grand-total calculations."""
    conn = get_connection()
    conn.execute(
        """
        UPDATE portfolios
        SET include_in_grand_total = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (1 if include else 0, portfolio_id),
    )
    conn.commit()


def delete_portfolio(portfolio_id: int) -> None:
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) AS n FROM portfolios").fetchone()["n"]
    if count <= 1:
        raise ValueError("You cannot delete the only remaining portfolio.")

    # Delete transactions explicitly so this also works for databases migrated
    # from an older schema where the FK constraint may not be enforced.
    conn.execute("DELETE FROM transactions WHERE portfolio_id = ?", (portfolio_id,))
    conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
    conn.commit()


def read_transactions(
    portfolio_id: Optional[int] = None,
    ticker: Optional[str] = None,
    portfolio_ids: Optional[list[int]] = None,
) -> pd.DataFrame:
    conn = get_connection()

    empty_columns = [
        "id", "portfolio_id", "portfolio_name", "action", "date", "ticker",
        "number_of_shares", "price", "currency", "rate", "value",
        "value_chf", "created_at", "updated_at"
    ]

    where_clauses = []
    params: list[object] = []

    if portfolio_id is not None:
        where_clauses.append("t.portfolio_id = ?")
        params.append(int(portfolio_id))

    if portfolio_ids is not None:
        portfolio_ids = [int(pid) for pid in portfolio_ids]
        if len(portfolio_ids) == 0:
            return pd.DataFrame(columns=empty_columns)
        placeholders = ",".join("?" for _ in portfolio_ids)
        where_clauses.append(f"t.portfolio_id IN ({placeholders})")
        params.extend(portfolio_ids)

    if ticker and ticker != "All":
        where_clauses.append("t.ticker = ?")
        params.append(ticker.upper())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
        SELECT
            t.id,
            t.portfolio_id,
            p.name AS portfolio_name,
            t.action,
            t.date,
            t.ticker,
            t.number_of_shares,
            t.price,
            t.currency,
            t.rate,
            t.value,
            t.value_chf,
            t.created_at,
            t.updated_at
        FROM transactions t
        LEFT JOIN portfolios p ON p.id = t.portfolio_id
        {where_sql}
        ORDER BY t.date DESC, t.ticker, t.id DESC
    """

    df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(columns=empty_columns)
    return df


def insert_transaction(
    portfolio_id: int,
    action: str,
    date: str,
    ticker: str,
    number_of_shares: float,
    price: float,
    currency: str,
    rate: float,
) -> None:
    action = action.upper().strip()
    ticker = ticker.upper().strip()
    currency = currency.upper().strip()

    if action == "SELL" and number_of_shares > 0:
        number_of_shares = -number_of_shares

    value = number_of_shares * price
    value_chf = value * rate

    conn = get_connection()
    conn.execute(
        """
        INSERT INTO transactions
            (portfolio_id, action, date, ticker, number_of_shares, price, currency, rate, value, value_chf)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (portfolio_id, action, date, ticker, number_of_shares, price, currency, rate, value, value_chf),
    )
    conn.commit()


def update_transaction(row: pd.Series) -> None:
    action = str(row["action"]).upper().strip()
    ticker = str(row["ticker"]).upper().strip()
    currency = str(row["currency"]).upper().strip()
    number_of_shares = float(row["number_of_shares"])
    price = float(row["price"])
    rate = float(row["rate"])

    if action == "SELL" and number_of_shares > 0:
        number_of_shares = -number_of_shares

    value = number_of_shares * price
    value_chf = value * rate

    conn = get_connection()
    conn.execute(
        """
        UPDATE transactions
        SET action = ?,
            date = ?,
            ticker = ?,
            number_of_shares = ?,
            price = ?,
            currency = ?,
            rate = ?,
            value = ?,
            value_chf = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            action,
            str(row["date"]),
            ticker,
            number_of_shares,
            price,
            currency,
            rate,
            value,
            value_chf,
            int(row["id"]),
        ),
    )
    conn.commit()


def delete_transactions(ids: list[int]) -> None:
    if not ids:
        return
    conn = get_connection()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
    conn.commit()


# -----------------------------
# Market data / FX layer
# -----------------------------

def is_yahoo_pence_currency(currency: Optional[str]) -> bool:
    """
    Yahoo often quotes London-listed shares in pence, not pounds.

    Typical raw Yahoo currency codes for this are GBp or GBX. Do not call
    .upper() before this check, because 'GBp'.upper() becomes 'GBP' and loses
    the distinction between pence and pounds.
    """
    if currency is None:
        return False
    raw = str(currency).strip()
    return raw == "GBp" or raw.upper() == "GBX"


def normalize_yahoo_price_and_currency(
    price: Optional[float],
    currency: Optional[str],
) -> tuple[Optional[float], Optional[str]]:
    """
    Normalize Yahoo prices to major currency units.

    Example:
    - Yahoo price 250 with currency GBp/GBX becomes price 2.50, currency GBP.
    - Yahoo price 150 with currency USD remains price 150, currency USD.
    """
    if currency is None:
        return (float(price) if price is not None else None, None)

    if is_yahoo_pence_currency(currency):
        normalized_price = float(price) / 100.0 if price is not None else None
        return normalized_price, "GBP"

    return (float(price) if price is not None else None, str(currency).upper())


@st.cache_data(ttl=3600, show_spinner=False)
def get_raw_yahoo_currency(ticker: str) -> Optional[str]:
    """Return Yahoo's raw currency code without normalizing case."""
    ticker = ticker.upper().strip()
    try:
        yf_ticker = yf.Ticker(ticker)

        try:
            fast = yf_ticker.fast_info
            currency = fast.get("currency") if hasattr(fast, "get") else getattr(fast, "currency", None)
            if currency:
                return str(currency).strip()
        except Exception:
            pass

        try:
            info = yf_ticker.get_info()
            currency = info.get("currency")
            if currency:
                return str(currency).strip()
        except Exception:
            pass
    except Exception:
        pass

    return None


@st.cache_data(ttl=900, show_spinner=False)
def get_latest_price_and_currency(ticker: str) -> tuple[Optional[float], Optional[str]]:
    """Return latest traded price and normalized currency for a ticker."""
    ticker = ticker.upper().strip()
    try:
        yf_ticker = yf.Ticker(ticker)

        price = None
        currency = None

        # fast_info is quick when available, but can be incomplete for some instruments.
        try:
            fast = yf_ticker.fast_info
            price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
            currency = fast.get("currency") if hasattr(fast, "get") else getattr(fast, "currency", None)
        except Exception:
            pass

        if price is None:
            hist = yf_ticker.history(period="5d", interval="1d", auto_adjust=False)
            if not hist.empty:
                price = float(hist["Close"].dropna().iloc[-1])

        if currency is None:
            currency = get_raw_yahoo_currency(ticker)

        return normalize_yahoo_price_and_currency(price, currency)
    except Exception:
        return None, None


# @st.cache_data(ttl=900, show_spinner=False)
# def get_latest_price_and_currency(ticker: str) -> tuple[Optional[float], Optional[str]]:
#     """Return latest traded price and Yahoo Finance currency for a ticker."""
#     ticker = ticker.upper().strip()
#     try:
#         yf_ticker = yf.Ticker(ticker)

#         price = None
#         currency = None

#         # fast_info is quick when available, but can be incomplete for some instruments.
#         try:
#             fast = yf_ticker.fast_info
#             price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
#             currency = fast.get("currency") if hasattr(fast, "get") else getattr(fast, "currency", None)
#         except Exception:
#             pass

#         if price is None:
#             hist = yf_ticker.history(period="5d", interval="1d", auto_adjust=False)
#             if not hist.empty:
#                 price = float(hist["Close"].dropna().iloc[-1])

#         if currency is None:
#             try:
#                 info = yf_ticker.get_info()
#                 currency = info.get("currency")
#             except Exception:
#                 currency = None

#         return (float(price) if price is not None else None, currency.upper() if currency else None)
#     except Exception:
#         return None, None


def _rate_from_history_on_or_before(hist: pd.DataFrame, target_date: Optional[str] = None) -> Optional[float]:
    """Extract the latest Close rate on or before target_date from a Yahoo history dataframe."""
    if hist.empty or "Close" not in hist.columns:
        return None

    close = hist["Close"].dropna().copy()
    if close.empty:
        return None

    if target_date:
        target = pd.to_datetime(target_date).date()

        # Compare only calendar dates. Yahoo often returns timezone-aware indexes
        # for FX data, while SQLite/date_input values are timezone-naive.
        close_dates = pd.Series(pd.to_datetime(close.index).date, index=close.index)
        close = close.loc[close_dates <= target]

        if close.empty:
            return None

    return float(close.iloc[-1])

# def is_yahoo_pence_currency(currency: Optional[str]) -> bool:
#     if currency is None:
#         return False
#     raw = str(currency).strip()
#     return raw == "GBp" or raw.upper() == "GBX"

# def normalize_yahoo_price_and_currency(
#     price: Optional[float],
#     currency: Optional[str],
# ) -> tuple[Optional[float], Optional[str]]:
#     if currency is None:
#         return (float(price) if price is not None else None, None)

#     if is_yahoo_pence_currency(currency):
#         normalized_price = float(price) / 100.0 if price is not None else None
#         return normalized_price, "GBP"

#     return (float(price) if price is not None else None, str(currency).upper())

@st.cache_data(ttl=900, show_spinner=False)
def get_latest_fx_rate_to_chf(currency: str) -> Optional[float]:
    """Return latest FX rate so that amount_in_currency * rate = amount_in_chf."""
    currency = currency.upper().strip()
    if currency == BASE_CURRENCY:
        return 1.0

    pair = f"{currency}{BASE_CURRENCY}=X"
    inverse_pair = f"{BASE_CURRENCY}{currency}=X"

    try:
        rate = _rate_from_history_on_or_before(
            yf.Ticker(pair).history(period="5d", interval="1d")
        )
        if rate is not None:
            return rate
    except Exception:
        pass

    try:
        inverse_rate = _rate_from_history_on_or_before(
            yf.Ticker(inverse_pair).history(period="5d", interval="1d")
        )
        if inverse_rate is not None and inverse_rate != 0:
            return 1.0 / inverse_rate
    except Exception:
        pass

    return None

@st.cache_data(ttl=900, show_spinner=False)
def get_latest_previous_fx_rates_to_chf(currency: str) -> tuple[Optional[float], Optional[float]]:
    """Return latest and previous FX rates to CHF for daily performance."""
    currency = currency.upper().strip()
    if currency == BASE_CURRENCY:
        return 1.0, 1.0

    pair = f"{currency}{BASE_CURRENCY}=X"
    inverse_pair = f"{BASE_CURRENCY}{currency}=X"

    def _latest_previous_from_symbol(symbol: str) -> tuple[Optional[float], Optional[float]]:
        hist = yf.Ticker(symbol).history(period="10d", interval="1d")
        if hist.empty or "Close" not in hist.columns:
            return None, None
        close = hist["Close"].dropna()
        if close.empty:
            return None, None
        latest = float(close.iloc[-1])
        previous = float(close.iloc[-2]) if len(close) >= 2 else latest
        return latest, previous

    try:
        latest, previous = _latest_previous_from_symbol(pair)
        if latest is not None and previous is not None:
            return latest, previous
    except Exception:
        pass

    try:
        inverse_latest, inverse_previous = _latest_previous_from_symbol(inverse_pair)
        if (
            inverse_latest is not None
            and inverse_previous is not None
            and inverse_latest != 0
            and inverse_previous != 0
        ):
            return 1.0 / inverse_latest, 1.0 / inverse_previous
    except Exception:
        pass

    return None, None

@st.cache_data(ttl=900, show_spinner=False)
def get_historical_fx_rate_to_chf(currency: str, transaction_date: str) -> Optional[float]:
    """
    Return historical FX rate for a transaction date.

    This function is used when SAVING transactions. It deliberately does not
    fall back to today's FX rate. If no historical quote is found, it returns
    None so the transaction is not saved with an incorrect rate.

    If the exact transaction date has no FX quote, for example a weekend or
    holiday, it uses the latest available quote on or before that date.
    """
    currency = currency.upper().strip()
    if currency == BASE_CURRENCY:
        return 1.0

    tx_date = pd.to_datetime(transaction_date).date()
    start = pd.Timestamp(tx_date) - pd.Timedelta(days=14)
    end = pd.Timestamp(tx_date) + pd.Timedelta(days=1)

    pair = f"{currency}{BASE_CURRENCY}=X"
    inverse_pair = f"{BASE_CURRENCY}{currency}=X"

    try:
        hist = yf.Ticker(pair).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
        )
        rate = _rate_from_history_on_or_before(hist, target_date=str(tx_date))
        if rate is not None:
            return rate
    except Exception:
        pass

    # Fallback: try inverse pair and invert it.
    try:
        hist = yf.Ticker(inverse_pair).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
        )
        inverse_rate = _rate_from_history_on_or_before(hist, target_date=str(tx_date))
        if inverse_rate is not None and inverse_rate != 0:
            return 1.0 / inverse_rate
    except Exception:
        pass

    return None


# @st.cache_data(ttl=3600, show_spinner=False)
# def get_historical_price_series(ticker: str, start_date: str, end_date: str) -> pd.Series:
#     """Return a daily historical close-price series for a ticker."""
#     ticker = ticker.upper().strip()
#     start = pd.to_datetime(start_date).normalize() - pd.Timedelta(days=14)
#     end = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1)

#     try:
#         hist = yf.Ticker(ticker).history(
#             start=start.strftime("%Y-%m-%d"),
#             end=end.strftime("%Y-%m-%d"),
#             interval="1d",
#             auto_adjust=False,
#         )
#     except Exception:
#         return pd.Series(dtype="float64")

#     if hist.empty or "Close" not in hist.columns:
#         return pd.Series(dtype="float64")

#     close = hist["Close"].dropna().copy()
#     if close.empty:
#         return pd.Series(dtype="float64")

#     close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
#     close = close.groupby(close.index).last()
#     close.name = ticker
#     return close


@st.cache_data(ttl=3600, show_spinner=False)
def get_historical_price_series(ticker: str, start_date: str, end_date: str) -> pd.Series:
    """Return a daily historical close-price series for a ticker."""
    ticker = ticker.upper().strip()
    start = pd.to_datetime(start_date).normalize() - pd.Timedelta(days=14)
    end = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1)

    try:
        hist = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
        )
    except Exception:
        return pd.Series(dtype="float64")

    if hist.empty or "Close" not in hist.columns:
        return pd.Series(dtype="float64")

    close = hist["Close"].dropna().copy()
    if close.empty:
        return pd.Series(dtype="float64")

    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close.groupby(close.index).last()

    # London-listed shares are often quoted by Yahoo in pence, for example
    # price 250 GBp/GBX means GBP 2.50. Normalize historical prices the same
    # way as latest prices, otherwise historical charts and current valuation
    # would be 100x too high.
    raw_currency = get_raw_yahoo_currency(ticker)
    if is_yahoo_pence_currency(raw_currency):
        close = close / 100.0

    close.name = ticker
    return close

@st.cache_data(ttl=3600, show_spinner=False)
def get_historical_fx_series_to_chf(currency: str, start_date: str, end_date: str) -> pd.Series:
    """Return a historical FX series so that amount_in_currency * rate = amount_in_chf."""
    currency = currency.upper().strip()
    start = pd.to_datetime(start_date).normalize() - pd.Timedelta(days=14)
    end = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1)

    if currency == BASE_CURRENCY:
        idx = pd.date_range(start=start, end=end, freq="D")
        return pd.Series(1.0, index=idx, name=currency)

    pair = f"{currency}{BASE_CURRENCY}=X"
    inverse_pair = f"{BASE_CURRENCY}{currency}=X"

    def _download_fx(symbol: str) -> pd.Series:
        hist = yf.Ticker(symbol).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
        )
        if hist.empty or "Close" not in hist.columns:
            return pd.Series(dtype="float64")
        close = hist["Close"].dropna().copy()
        if close.empty:
            return pd.Series(dtype="float64")
        close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
        close = close.groupby(close.index).last()
        return close

    try:
        direct = _download_fx(pair)
        if not direct.empty:
            direct.name = currency
            return direct
    except Exception:
        pass

    try:
        inverse = _download_fx(inverse_pair)
        if not inverse.empty:
            inverse = 1.0 / inverse
            inverse.name = currency
            return inverse
    except Exception:
        pass

    return pd.Series(dtype="float64")


def align_history_series(series: pd.Series, date_index: pd.DatetimeIndex) -> pd.Series:
    """
    Align historical market data to the chart's daily date index.

    Quotes are forward-filled so weekends and holidays use the latest previous
    market quote. This avoids incorrectly using a future quote for a past day.
    """
    if series.empty:
        return pd.Series(index=date_index, dtype="float64")

    series = series.copy()
    series.index = pd.to_datetime(series.index).tz_localize(None).normalize()
    series = series.sort_index()

    full_start = min(series.index.min(), date_index.min())
    full_index = pd.date_range(start=full_start, end=date_index.max(), freq="D")

    return series.reindex(full_index).ffill().reindex(date_index)

def clear_market_data_caches(clear_transaction_fx: bool = False) -> None:
    """
    Clear cached market data so the next calculations fetch fresh prices/rates.

    Auto-refresh should clear latest valuation/chart data, but it should not
    normally clear historical transaction-date FX rates because those stored
    transaction values should remain stable once saved.
    """
    get_latest_price_and_currency.clear()
    get_raw_yahoo_currency.clear()
    get_latest_fx_rate_to_chf.clear()
    get_historical_price_series.clear()
    get_historical_fx_series_to_chf.clear()

    if clear_transaction_fx:
        get_historical_fx_rate_to_chf.clear()


# -----------------------------
# Portfolio calculations
# -----------------------------

def build_portfolio(transactions: pd.DataFrame) -> pd.DataFrame:
    if transactions.empty:
        return pd.DataFrame()

    rows = []
    for ticker, group in transactions.groupby("ticker"):
        buys = group[group["action"] == "BUY"]
        sells = group[group["action"] == "SELL"]
        dividends = group[group["action"] == "DIVIDEND"]

        shares_held = group[group["action"].isin(["BUY", "SELL"])] ["number_of_shares"].sum()
        buy_cost_chf = buys["value_chf"].sum()
        sale_proceeds_chf = -sells["value_chf"].sum()  # SELL values are negative by design.
        dividends_chf = dividends["value_chf"].sum()

        latest_price, market_currency = get_latest_price_and_currency(ticker)

        # If Yahoo cannot provide a currency, use the most recent transaction currency as fallback.
        fallback_currency = str(group.sort_values("date").iloc[-1]["currency"]).upper()
        market_currency = market_currency or fallback_currency

        latest_rate = get_latest_fx_rate_to_chf(market_currency) if market_currency else None

        if latest_price is not None and latest_rate is not None:
            market_value_chf = shares_held * latest_price * latest_rate
        else:
            market_value_chf = None

        total_return_chf = None
        total_return_pct = None
        if market_value_chf is not None:
            total_return_chf = market_value_chf + sale_proceeds_chf + dividends_chf - buy_cost_chf
            total_return_pct = total_return_chf / buy_cost_chf if buy_cost_chf else None

        # Average buy cost per share is shown in the share's original currency,
        # not CHF. For BUY transactions, Value = Number_of_Shares * Price in
        # the transaction/share currency.
        avg_buy_price = (
            buys["value"].sum() / buys["number_of_shares"].sum()
            if not buys.empty and buys["number_of_shares"].sum()
            else None
        )

        rows.append(
            {
                "Ticker": ticker,
                "Shares": shares_held,
                "Avg Cost / Share": avg_buy_price,
                "Latest Price": latest_price,
                "Currency": market_currency,
                "Rate": latest_rate,
                "Value CHF": market_value_chf,
                "Buy Cost CHF": buy_cost_chf,
                "Sales CHF": sale_proceeds_chf,
                "Dividends CHF": dividends_chf,
                "Total Return CHF": total_return_chf,
                "Total Return %": total_return_pct,
                "Transactions": len(group),
            }
        )

    portfolio = pd.DataFrame(rows)
    if not portfolio.empty:
        portfolio = portfolio.sort_values("Value CHF", ascending=False, na_position="last")
    return portfolio


def summarize_portfolio(portfolio: pd.DataFrame) -> dict[str, Optional[float]]:
    if portfolio.empty:
        return {
            "market_value_chf": 0.0,
            "buy_cost_chf": 0.0,
            "sale_proceeds_chf": 0.0,
            "dividends_chf": 0.0,
            "total_return_chf": 0.0,
            "total_return_pct": None,
        }

    total_market_value = portfolio["Value CHF"].sum(skipna=True)
    total_buy_cost = portfolio["Buy Cost CHF"].sum(skipna=True)
    total_sale_proceeds = portfolio["Sales CHF"].sum(skipna=True)
    total_dividends = portfolio["Dividends CHF"].sum(skipna=True)
    total_return = portfolio["Total Return CHF"].sum(skipna=True)
    total_return_pct = total_return / total_buy_cost if total_buy_cost else None

    return {
        "market_value_chf": total_market_value,
        "buy_cost_chf": total_buy_cost,
        "sale_proceeds_chf": total_sale_proceeds,
        "dividends_chf": total_dividends,
        "total_return_chf": total_return,
        "total_return_pct": total_return_pct,
    }


def build_portfolio_summary_by_name(portfolios: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in portfolios.iterrows():
        portfolio_id = int(row["id"])
        portfolio_name = str(row["name"])
        tx = read_transactions(portfolio_id=portfolio_id)
        pf = build_portfolio(tx)
        summary = summarize_portfolio(pf)
        rows.append(
            {
                "Portfolio": portfolio_name,
                "Included in Grand Total": "Yes" if int(row.get("include_in_grand_total", 1)) == 1 else "No",
                "Market Value CHF": summary["market_value_chf"],
                "Buy Cost CHF": summary["buy_cost_chf"],
                "Sales CHF": summary["sale_proceeds_chf"],
                "Dividends CHF": summary["dividends_chf"],
                "Total Return CHF": summary["total_return_chf"],
                "Total Return %": summary["total_return_pct"],
            }
        )
    return pd.DataFrame(rows)


def build_portfolio_history(transactions: pd.DataFrame) -> pd.DataFrame:
    """
    Build a daily CHF portfolio history from transactions and historical prices.

    The result supports these charts:
    - Market Value CHF vs Net Invested CHF
    - Total Return CHF
    - Daily cash flows

    Total Return CHF follows the same method as the current portfolio table:
    Market Value CHF + cumulative sales + cumulative dividends - cumulative buys.
    """
    if transactions.empty:
        return pd.DataFrame()

    tx = transactions.copy()
    tx["date"] = pd.to_datetime(tx["date"], errors="coerce").dt.normalize()
    tx = tx.dropna(subset=["date"])

    if tx.empty:
        return pd.DataFrame()

    for col in ["number_of_shares", "value_chf"]:
        tx[col] = pd.to_numeric(tx[col], errors="coerce").fillna(0.0)

    start_date = tx["date"].min()
    end_date = pd.Timestamp.today().normalize()

    if start_date > end_date:
        end_date = start_date

    date_index = pd.date_range(start=start_date, end=end_date, freq="D")

    tx["Buy CHF"] = tx["value_chf"].where(tx["action"] == "BUY", 0.0)
    tx["Sales CHF"] = (-tx["value_chf"]).where(tx["action"] == "SELL", 0.0)
    tx["Dividends CHF"] = tx["value_chf"].where(tx["action"] == "DIVIDEND", 0.0)
    tx["Daily Cash Flow CHF"] = tx["Sales CHF"] + tx["Dividends CHF"] - tx["Buy CHF"]

    daily_cash = (
        tx.groupby("date")[["Buy CHF", "Sales CHF", "Dividends CHF", "Daily Cash Flow CHF"]]
        .sum()
        .reindex(date_index, fill_value=0.0)
    )

    market_value = pd.Series(0.0, index=date_index, name="Value CHF")

    for ticker, group in tx.groupby("ticker"):
        ticker = str(ticker).upper().strip()
        if not ticker:
            continue

        share_changes = (
            group[group["action"].isin(["BUY", "SELL"])]
            .groupby("date")["number_of_shares"]
            .sum()
            .reindex(date_index, fill_value=0.0)
        )
        shares_held = share_changes.cumsum()

        if shares_held.abs().sum() == 0:
            continue

        # Use the most recent stored transaction currency for this ticker.
        # This is the same currency basis used in the transaction ledger.
        currency_values = group.sort_values("date")["currency"].dropna().astype(str).str.upper()
        currency = currency_values.iloc[-1] if not currency_values.empty else BASE_CURRENCY

        price_series = align_history_series(
            get_historical_price_series(ticker, str(start_date.date()), str(end_date.date())),
            date_index,
        )
        fx_series = align_history_series(
            get_historical_fx_series_to_chf(currency, str(start_date.date()), str(end_date.date())),
            date_index,
        )

        position_value = shares_held * price_series * fx_series
        market_value = market_value.add(position_value.fillna(0.0), fill_value=0.0)

    history = pd.DataFrame(index=date_index)
    history["Value CHF"] = market_value
    history["Buy Cost CHF"] = daily_cash["Buy CHF"].cumsum()
    history["Sales CHF"] = daily_cash["Sales CHF"].cumsum()
    history["Dividends CHF"] = daily_cash["Dividends CHF"].cumsum()
    history["Net Invested CHF"] = history["Buy Cost CHF"] - history["Sales CHF"]
    history["Total Return CHF"] = (
        history["Value CHF"]
        + history["Sales CHF"]
        + history["Dividends CHF"]
        - history["Buy Cost CHF"]
    )
    history["Return %"] = history["Total Return CHF"] / history["Buy Cost CHF"].replace(0, pd.NA)
    history["Daily Buy CHF"] = daily_cash["Buy CHF"]
    history["Daily Sales CHF"] = daily_cash["Sales CHF"]
    history["Daily Dividends CHF"] = daily_cash["Dividends CHF"]
    history["Daily Cash Flow CHF"] = daily_cash["Daily Cash Flow CHF"]

    history = history.reset_index().rename(columns={"index": "Date"})
    return history

# Daily Performance

# @st.cache_data(ttl=900, show_spinner=False)
# def get_latest_previous_price_and_currency(ticker: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
#     """
#     Return latest price, previous close, and normalized currency for daily performance.

#     For London-listed shares quoted by Yahoo in GBp/GBX, both prices are
#     normalized to GBP by normalize_yahoo_price_and_currency().
#     """
#     ticker = ticker.upper().strip()
#     try:
#         yf_ticker = yf.Ticker(ticker)

#         latest_price = None
#         previous_close = None
#         currency = None

#         try:
#             fast = yf_ticker.fast_info
#             if hasattr(fast, "get"):
#                 latest_price = fast.get("last_price")
#                 previous_close = fast.get("previous_close")
#                 currency = fast.get("currency")
#             else:
#                 latest_price = getattr(fast, "last_price", None)
#                 previous_close = getattr(fast, "previous_close", None)
#                 currency = getattr(fast, "currency", None)
#         except Exception:
#             pass

#         hist = yf_ticker.history(period="10d", interval="1d", auto_adjust=False)
#         if not hist.empty and "Close" in hist.columns:
#             close = hist["Close"].dropna()
#             if not close.empty:
#                 if latest_price is None:
#                     latest_price = float(close.iloc[-1])
#                 if previous_close is None:
#                     previous_close = float(close.iloc[-2]) if len(close) >= 2 else float(close.iloc[-1])

#         if currency is None:
#             currency = get_raw_yahoo_currency(ticker)

#         latest_price, normalized_currency = normalize_yahoo_price_and_currency(latest_price, currency)
#         previous_close, _ = normalize_yahoo_price_and_currency(previous_close, currency)

#         return latest_price, previous_close, normalized_currency
#     except Exception:
#         return None, None, None

@st.cache_data(ttl=900, show_spinner=False)
def get_latest_previous_price_and_currency(
    ticker: str,
) -> tuple[Optional[float], Optional[float], Optional[str], bool]:
    """
    Return latest price, comparison price, normalized currency, and whether
    the latest Yahoo daily quote is dated today.

    If Yahoo's latest quote is not from today, return the same price for
    latest_price and previous_close. That makes today's price change zero.

    For London-listed shares quoted by Yahoo in GBp/GBX, both prices are
    normalized to GBP by normalize_yahoo_price_and_currency().
    """
    ticker = ticker.upper().strip()

    try:
        yf_ticker = yf.Ticker(ticker)

        latest_price = None
        previous_close = None
        currency = None
        latest_quote_is_today = False

        # Get currency first. Do not rely only on history for this.
        try:
            fast = yf_ticker.fast_info
            if hasattr(fast, "get"):
                currency = fast.get("currency")
            else:
                currency = getattr(fast, "currency", None)
        except Exception:
            pass

        hist = yf_ticker.history(period="10d", interval="1d", auto_adjust=False)
        # st.write(ticker, hist)
        if not hist.empty and "Close" in hist.columns:
            close = hist["Close"].dropna()

            if not close.empty:
                close_dates = pd.to_datetime(close.index).tz_localize(None).normalize()
                latest_quote_date = close_dates[-1].date()
                today = pd.Timestamp.today().normalize().date()

                latest_quote_is_today = latest_quote_date == today
                # st.write("latest_quote_is_today", latest_quote_is_today)
                if latest_quote_is_today:
                    latest_price = float(close.iloc[-1])
                    previous_close = (
                        float(close.iloc[-2])
                        if len(close) >= 2
                        else float(close.iloc[-1])
                    )
                else:
                    # Stale quote: show zero daily price movement.
                    latest_price = float(close.iloc[-1])
                    previous_close = float(close.iloc[-1])

        # st.write("latest_price", latest_price)
        if currency is None:
            currency = get_raw_yahoo_currency(ticker)

        latest_price, normalized_currency = normalize_yahoo_price_and_currency(
            latest_price,
            currency,
        )
        previous_close, _ = normalize_yahoo_price_and_currency(
            previous_close,
            currency,
        )
        # st.write(latest_price, normalized_currency)
        return latest_price, previous_close, normalized_currency, latest_quote_is_today

    except Exception:
        return None, None, None, False
    

def build_daily_performance(transactions: pd.DataFrame) -> pd.DataFrame:
    """Build today's per-ticker performance for currently held positions."""
    if transactions.empty:
        return pd.DataFrame()

    rows = []

    for ticker, group in transactions.groupby("ticker"):
        ticker = str(ticker).upper().strip()
        if not ticker:
            continue

        position_tx = group[group["action"].isin(["BUY", "SELL"])]
        shares_held = position_tx["number_of_shares"].sum()

        # Daily performance is only meaningful for currently open positions.
        if abs(shares_held) < 1e-9:
            continue

        # latest_price, previous_close, market_currency = get_latest_previous_price_and_currency(ticker)
        latest_price, previous_close, market_currency, latest_quote_is_today = (
            get_latest_previous_price_and_currency(ticker)
        )
        fallback_currency = str(group.sort_values("date").iloc[-1]["currency"]).upper()
        market_currency = market_currency or fallback_currency

        latest_fx, previous_fx = get_latest_previous_fx_rates_to_chf(market_currency) if market_currency else (None, None)
        if not latest_quote_is_today:
            # If the stock quote is stale, do not show a false CHF movement caused only by FX.
            # The row should show zero daily performance until Yahoo has a quote dated today.
            previous_fx = latest_fx

        current_value_chf = None
        previous_value_chf = None
        day_change_chf = None
        day_change_pct = None

        if (
            latest_price is not None
            and previous_close is not None
            and latest_fx is not None
            and previous_fx is not None
        ):
            current_value_chf = shares_held * latest_price * latest_fx
            previous_value_chf = shares_held * previous_close * previous_fx
            day_change_chf = current_value_chf - previous_value_chf
            day_change_pct = day_change_chf / previous_value_chf if previous_value_chf else None

        rows.append(
            {
                "Ticker": ticker,
                "Shares": shares_held,
                # "Previous Close": previous_close,
                "Latest Price": latest_price,
                "Currency": market_currency,
                "Previous Value CHF": previous_value_chf,
                "Value CHF": current_value_chf,
                "Today CHF": day_change_chf,
                "Today %": day_change_pct,
            }
        )

    daily = pd.DataFrame(rows)
    if not daily.empty:
        daily = daily.sort_values("Today CHF", ascending=False, na_position="last")
    return daily


def summarize_daily_performance(daily: pd.DataFrame) -> dict[str, Optional[float]]:
    if daily.empty:
        return {
            "previous_value_chf": 0.0,
            "current_value_chf": 0.0,
            "today_chf": 0.0,
            "today_pct": None,
        }

    previous_value = daily["Previous Value CHF"].sum(skipna=True)
    current_value = daily["Value CHF"].sum(skipna=True)
    today_chf = daily["Today CHF"].sum(skipna=True)
    today_pct = today_chf / previous_value if previous_value else None

    return {
        "previous_value_chf": previous_value,
        "current_value_chf": current_value,
        "today_chf": today_chf,
        "today_pct": today_pct,
    }



# -----------------------------
# Formatting helpers
# -----------------------------

def format_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.2f}".replace(",", "'")


def format_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:.2%}"


def format_swiss_number(value: float | None, decimals: int = 2, prefix: str = "") -> str:
    """Format numbers as 1'234'567.89 for display."""
    if value is None or pd.isna(value):
        return "—"
    formatted = f"{float(value):,.{decimals}f}".replace(",", "'")
    return f"{prefix}{formatted}"


def format_swiss_percent(value: float | None) -> str:
    """Format a decimal return value as 12.34%, using apostrophe thousands if needed."""
    if value is None or pd.isna(value):
        return "—"
    formatted = f"{float(value) * 100:,.2f}".replace(",", "'")
    return f"{formatted}%"


def style_swiss_numbers(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    swiss_formatters = {
        "Shares": lambda x: format_swiss_number(x, decimals=2),
        "Avg Cost / Share": lambda x: format_swiss_number(x, decimals=2),
        "Latest Price": lambda x: format_swiss_number(x, decimals=2),
        "Rate": lambda x: format_swiss_number(x, decimals=3),
        "Value CHF": lambda x: format_swiss_number(x, decimals=2),
        "Value CHF": lambda x: format_swiss_number(x, decimals=2),
        "Buy Cost CHF": lambda x: format_swiss_number(x, decimals=2),
        "Sales CHF": lambda x: format_swiss_number(x, decimals=2),
        "Dividends CHF": lambda x: format_swiss_number(x, decimals=2),
        "Total Return CHF": lambda x: format_swiss_number(x, decimals=2, prefix=""),
        "Total Return %": format_swiss_percent,

        "Value CHF": lambda x: format_swiss_number(x, decimals=2, prefix=""),
        "Today CHF": lambda x: format_swiss_number(x, decimals=2, prefix=""),
        "Today %": format_swiss_percent,
        
    }

    swiss_formatters = {
        col: formatter
        for col, formatter in swiss_formatters.items()
        if col in df.columns
    }

    return (
        df.style
        .format(swiss_formatters)
        .set_properties(
            subset=list(swiss_formatters.keys()),
            **{"text-align": "right"},
        )
    )

def dataframe_height(
    df: pd.DataFrame,
    row_height: int = 35,
    header_height: int = 38,
    max_height: int = 1700,
    min_height: int = 120,
) -> int:
    height = header_height + (len(df) * row_height)
    return max(min_height, min(height, max_height))

# -----------------------------
# Streamlit UI
# -----------------------------

st.set_page_config(page_title="Portfolio Tracker", layout="wide")
init_db()

portfolios = read_portfolios()

if "selected_portfolio_id" not in st.session_state:
    st.session_state["selected_portfolio_id"] = get_default_portfolio_id()

if int(st.session_state["selected_portfolio_id"]) not in set(portfolios["id"].astype(int)):
    st.session_state["selected_portfolio_id"] = int(portfolios.iloc[0]["id"])

portfolio_names = portfolios["name"].tolist()
portfolio_ids = portfolios["id"].astype(int).tolist()
portfolio_id_by_name = dict(zip(portfolio_names, portfolio_ids))
portfolio_name_by_id = dict(zip(portfolio_ids, portfolio_names))

selected_portfolio_id = int(st.session_state["selected_portfolio_id"])
selected_portfolio_name = portfolio_name_by_id[selected_portfolio_id]
selected_index = portfolio_names.index(selected_portfolio_name)
selected_portfolio_row = portfolios.loc[portfolios["id"].astype(int) == selected_portfolio_id].iloc[0]
selected_include_in_grand_total = bool(int(selected_portfolio_row.get("include_in_grand_total", 1)))

st.title("Portfolio Tracker")
st.caption("Transactions are stored locally in SQLite. Portfolio performance is calculated in CHF.")


with st.sidebar:
    st.header("Portfolio")

    selected_portfolio_name = st.selectbox(
        "Current portfolio",
        portfolio_names,
        index=selected_index,
    )
    selected_portfolio_id = int(portfolio_id_by_name[selected_portfolio_name])
    st.session_state["selected_portfolio_id"] = selected_portfolio_id

    with st.expander("Manage portfolios"):
        with st.form("add_portfolio_form", clear_on_submit=True):
            new_portfolio_name = st.text_input("New portfolio name")
            add_portfolio = st.form_submit_button("Add portfolio")
            if add_portfolio:
                try:
                    new_id = create_portfolio(new_portfolio_name)
                    st.session_state["selected_portfolio_id"] = new_id
                    st.success("Portfolio added.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        with st.form("rename_portfolio_form"):
            renamed_portfolio_name = st.text_input(
                "Rename selected portfolio to",
                value=selected_portfolio_name,
            )
            rename_selected = st.form_submit_button("Rename selected portfolio")
            if rename_selected:
                try:
                    rename_portfolio(selected_portfolio_id, renamed_portfolio_name)
                    st.success("Portfolio renamed.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        include_selected = st.checkbox(
            "Include selected portfolio in grand total",
            value=selected_include_in_grand_total,
            key=f"include_in_grand_total_{selected_portfolio_id}",
        )
        if include_selected != selected_include_in_grand_total:
            set_portfolio_include_in_grand_total(selected_portfolio_id, include_selected)
            st.success("Grand-total setting updated.")
            st.rerun()

        st.warning("Deleting a portfolio also deletes all transactions in that portfolio.")
        confirm_delete = st.checkbox(
            f"I understand: delete '{selected_portfolio_name}' and its transactions",
            key=f"confirm_delete_portfolio_{selected_portfolio_id}",
        )
        if st.button("Delete selected portfolio", disabled=not confirm_delete):
            try:
                delete_portfolio(selected_portfolio_id)
                refreshed = read_portfolios()
                st.session_state["selected_portfolio_id"] = int(refreshed.iloc[0]["id"])
                st.success("Portfolio deleted.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    # st.divider()
    st.header("Data")
    st.write(f"Database: `{DB_PATH}`")

    st.subheader("Price refresh")
    auto_refresh_enabled = st.toggle(
        "Auto-refresh latest prices",
        value=False,
        key="auto_refresh_enabled",
    )
    auto_refresh_minutes = st.number_input(
        "Refresh interval (minutes)",
        min_value=1,
        max_value=240,
        value=15,
        step=1,
        disabled=not auto_refresh_enabled,
        key="auto_refresh_minutes",
    )

    if auto_refresh_enabled:
        if st_autorefresh is None:
            st.warning(
                "Auto-refresh requires the package `streamlit-autorefresh`. "
                "Install it with: pip install streamlit-autorefresh"
            )
        else:
            refresh_count = st_autorefresh(
                interval=int(auto_refresh_minutes * 60 * 1000),
                key="market_price_autorefresh",
            )
            last_refresh_count = st.session_state.get("last_market_price_autorefresh_count")

            if last_refresh_count is None:
                st.session_state["last_market_price_autorefresh_count"] = refresh_count
            elif refresh_count != last_refresh_count:
                clear_market_data_caches(clear_transaction_fx=False)
                st.session_state["last_market_price_autorefresh_count"] = refresh_count
                st.session_state["last_market_price_refresh_time"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

            st.caption(f"Automatic refresh every {int(auto_refresh_minutes)} minute(s).")

    if "last_market_price_refresh_time" in st.session_state:
        st.caption(f"Last automatic market-data refresh: {st.session_state['last_market_price_refresh_time']}")

    if st.button("Refresh market prices now"):
        clear_market_data_caches(clear_transaction_fx=True)
        st.rerun()

    # st.divider()
    st.header("Performance method")
    st.caption(
        "Cash-flow return = current market value + sale proceeds + dividends − buy cost. "
        "This is not FIFO/LIFO tax-lot accounting."
    )


transactions = read_transactions(portfolio_id=selected_portfolio_id)
portfolio = build_portfolio(transactions)
selected_summary = summarize_portfolio(portfolio)

included_portfolio_ids = (
    portfolios.loc[portfolios["include_in_grand_total"].astype(int) == 1, "id"]
    .astype(int)
    .tolist()
)

all_transactions = read_transactions()
grand_total_transactions = read_transactions(portfolio_ids=included_portfolio_ids)

all_portfolio = build_portfolio(all_transactions)
grand_total_portfolio = build_portfolio(grand_total_transactions)

grand_summary = summarize_portfolio(grand_total_portfolio)
portfolio_summary = build_portfolio_summary_by_name(portfolios)

st.markdown(
    """
    <style>
    /* Main tab container */
    div[data-testid="stTabs"] > div[role="tablist"] {
        display: flex;
        gap: 0.35rem;
        border-bottom: 1px solid #3A3A3A;
        margin-bottom: 1rem;
    }

    /* Every tab */
    div[data-testid="stTabs"] button[role="tab"] {
        flex: 1;
        justify-content: center;
        background-color: #1E293B;
        color: #CBD5E1;
        border: 1px solid #334155;
        border-bottom: none;
        border-radius: 10px 10px 0 0;
        padding: 0.75rem 1rem;
        font-weight: 700;
        font-size: 1rem;
    }

    /* Hovered tab */
    div[data-testid="stTabs"] button[role="tab"]:hover {
        background-color: #334155;
        color: #FFFFFF;
    }

    /* Active tab */
    div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
        background-color: #0F172A;
        color: #FFFFFF;
        border-top: 3px solid #38BDF8;
        border-left: 1px solid #38BDF8;
        border-right: 1px solid #38BDF8;
    }

    /* Tab label text */
    div[data-testid="stTabs"] button[role="tab"] p {
        font-size: 1rem;
        font-weight: 700;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
tab_overview, tab_daily, tab_add, tab_transactions = st.tabs(
    ["Portfolio", "Daily performance", "Add transaction", "Transactions / edit"]
)


with tab_overview:
    st.subheader("All portfolios grand total")

    g1, g2, g3, g4, g5 = st.columns(5)
    g1.metric("Value CHF", format_money(grand_summary["market_value_chf"]))
    g2.metric("Buy Cost CHF", format_money(grand_summary["buy_cost_chf"]))
    g3.metric("Sales CHF", format_money(grand_summary["sale_proceeds_chf"]))
    g4.metric("Dividends CHF", format_money(grand_summary["dividends_chf"]))
    g5.metric(
        "Total Return",
        f"CHF {format_money(grand_summary['total_return_chf'])}",
        format_pct(grand_summary["total_return_pct"]),
    )

    if not portfolio_summary.empty:
        with st.expander("Summary by portfolio", expanded=False):
            st.dataframe(
                style_swiss_numbers(portfolio_summary),
                use_container_width=True,
                hide_index=True,
            )

    st.divider()
    st.subheader(f"Portfolio overview: {selected_portfolio_name}")

    if portfolio.empty:
        st.info("No transactions yet. Add a BUY transaction to start.")
    else:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Value CHF", format_money(selected_summary["market_value_chf"]))
        c2.metric("Buy Cost CHF", format_money(selected_summary["buy_cost_chf"]))
        c3.metric("Sales CHF", format_money(selected_summary["sale_proceeds_chf"]))
        c4.metric("Dividends CHF", format_money(selected_summary["dividends_chf"]))
        c5.metric(
            "Total Return",
            f"CHF {format_money(selected_summary['total_return_chf'])}",
            format_pct(selected_summary["total_return_pct"]),
        )

        st.dataframe(
            style_swiss_numbers(portfolio),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Portfolio growth")

    chart_col1, chart_col2 = st.columns([1, 1])

    with chart_col1:
        chart_scope = st.radio(
            "Chart scope",
            ["Selected portfolio", "All portfolios"],
            horizontal=True,
            key="portfolio_growth_scope",
        )

    with chart_col2:
        chart_timeframe = st.selectbox(
            "Time frame",
            ["All", "1 week", "1 month", "6 months", "1 year", "3 years", "5 years", "10 years"],
            index=0,
            key="portfolio_growth_timeframe",
        )

    chart_transactions = transactions if chart_scope == "Selected portfolio" else all_transactions
    chart_history = build_portfolio_history(chart_transactions)

    if chart_history.empty:
        st.info("No chart data available yet. Add at least one transaction to build portfolio history.")
    else:
        chart_history = chart_history.copy()
        chart_history["Date"] = pd.to_datetime(chart_history["Date"]).dt.normalize()

        timeframe_offsets = {
            "1 week": pd.DateOffset(days=7),
            "1 month": pd.DateOffset(months=1),
            "6 months": pd.DateOffset(months=6),
            "1 year": pd.DateOffset(years=1),
            "3 years": pd.DateOffset(years=3),
            "5 years": pd.DateOffset(years=5),
            "10 years": pd.DateOffset(years=10),
        }

        if chart_timeframe != "All":
            last_chart_date = chart_history["Date"].max()
            first_visible_date = last_chart_date - timeframe_offsets[chart_timeframe]
            chart_history_visible = chart_history[chart_history["Date"] >= first_visible_date].copy()
        else:
            chart_history_visible = chart_history.copy()

        chart_data = chart_history_visible.set_index("Date")

        with st.container(border=True):
            visible_start = chart_history_visible["Date"].min().date()
            visible_end = chart_history_visible["Date"].max().date()

            st.caption(
                f"Showing {chart_scope.lower()} from {visible_start} to {visible_end}. "
                "Historical prices and FX rates are forward-filled over weekends and holidays. "
                "Net invested capital = cumulative buys − cumulative sales. "
                "Total return = market value + sales + dividends − buys."
            )

            st.markdown("**Portfolio value vs net invested capital**")
            st.line_chart(
                chart_data[["Value CHF", "Net Invested CHF"]],
                use_container_width=True,
            )

            st.markdown("**Total return in CHF**")
            st.line_chart(
                chart_data[["Total Return CHF"]],
                use_container_width=True,
            )

            with st.expander("Daily cash-flow bars", expanded=False):
                st.caption("BUY transactions are negative cash flows; SELL and DIVIDEND transactions are positive cash flows.")
                st.bar_chart(
                    chart_data[["Daily Cash Flow CHF"]],
                    use_container_width=True,
                )

            export_label = chart_timeframe.lower().replace(" ", "_")
            scope_label = chart_scope.lower().replace(" ", "_")
            st.download_button(
                "Download visible portfolio history CSV",
                data=chart_history_visible.to_csv(index=False).encode("utf-8"),
                file_name=f"{scope_label}_{export_label}_portfolio_history.csv",
                mime="text/csv",
            )

            with st.expander("Download full history instead", expanded=False):
                st.download_button(
                    "Download full portfolio history CSV",
                    data=chart_history.to_csv(index=False).encode("utf-8"),
                    file_name=f"{scope_label}_all_portfolio_history.csv",
                    mime="text/csv",
                )

with tab_daily:
    st.subheader(f"Daily performance: {selected_portfolio_name}")
    st.caption(
        "Shows currently held positions only. Today is calculated as  todays latest "
        "available price/value versus previous close, converted to CHF."
    )

    daily_scope = st.radio(
        "Performance scope",
        ["Selected portfolio", "Grand total portfolios", "All portfolios"],
        horizontal=True,
        key="daily_performance_scope",
    )

    if daily_scope == "Selected portfolio":
        daily_transactions = transactions
    elif daily_scope == "Grand total portfolios":
        daily_transactions = grand_total_transactions
    else:
        daily_transactions = all_transactions

    daily_performance = build_daily_performance(daily_transactions)
    daily_summary = summarize_daily_performance(daily_performance)

    if daily_performance.empty:
        st.info("No open positions available for daily performance.")
    else:
        with st.container(border=True):
            d1, d2, d3, d4 = st.columns(4)

            d1.metric(
                "Previous Value CHF",
                format_money(daily_summary["previous_value_chf"]),
            )
            d2.metric(
                "Value CHF",
                format_money(daily_summary["current_value_chf"]),
            )
            d3.metric(
                "Today CHF",
                format_money(daily_summary["today_chf"]),
            )
            d4.metric(
                "Today %",
                format_pct(daily_summary["today_pct"]),
            )

            daily_performance = daily_performance.drop(columns=["Previous Value CHF"],errors="ignore")
            st.dataframe(
                style_swiss_numbers(daily_performance),
                use_container_width=False,
                height=dataframe_height(daily_performance),
                hide_index=True,
            )

with tab_add:
    st.subheader("Add transaction")
    st.caption(f"Transaction will be saved to portfolio: {selected_portfolio_name}")

    # Transaction entry is intentionally simplified:
    # the user enters ticker, shares, and price; the app looks up currency and CHF FX rate.
    with st.form("add_transaction_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        action = c1.selectbox("Action", ACTIONS)
        date = c2.date_input("Date")
        ticker = c3.text_input("Ticker", placeholder="AAPL, NESN.SW, MSFT, ...")

        c4, c5 = st.columns(2)
        number_of_shares = c4.number_input("Number of Shares", min_value=1.0, step=10.0, format="%.1f")
        price = c5.number_input("Price / Dividend per Share", min_value=0.01, step=0.01, format="%.4f")

        ticker_clean = ticker.upper().strip()
        detected_currency = None
        detected_rate = None

        if ticker_clean:
            _, detected_currency = get_latest_price_and_currency(ticker_clean)
            if detected_currency:
                detected_rate = get_historical_fx_rate_to_chf(detected_currency, transaction_date=str(date))

        if ticker_clean and detected_currency and detected_rate:
            value_preview = number_of_shares * price
            if action == "SELL":
                value_preview = -abs(value_preview)
            value_chf_preview = value_preview * detected_rate

            st.info(
                f"Detected currency: {detected_currency} | "
                f"CHF rate on {date}: {detected_rate:.6f} | "
                f"Value: {format_swiss_number(value_preview, decimals=2)} {detected_currency} | "
                f"Value_CHF: {format_swiss_number(value_chf_preview, decimals=2)} CHF"
            )
        elif ticker_clean:
            st.warning(
                "Could not automatically detect the ticker currency or historical CHF exchange rate. "
                "Check that the ticker symbol is valid for Yahoo Finance, for example AAPL, NESN.SW, MSFT."
            )
        else:
            st.caption("Enter a ticker to automatically detect currency and CHF exchange rate.")

        submitted = st.form_submit_button("Save transaction")

    if submitted:
        if not ticker_clean:
            st.error("Ticker is required.")
        elif number_of_shares <= 0:
            st.error("Number of shares must be greater than zero.")
        elif price <= 0:
            st.error("Price must be greater than zero.")
        elif not detected_currency:
            st.error("Could not detect the ticker currency. Please check the ticker symbol.")
        elif not detected_rate or detected_rate <= 0:
            st.error("Could not fetch a valid historical CHF exchange rate for this ticker currency and date.")
        else:
            insert_transaction(
                portfolio_id=selected_portfolio_id,
                action=action,
                date=str(date),
                ticker=ticker_clean,
                number_of_shares=number_of_shares,
                price=price,
                currency=detected_currency,
                rate=detected_rate,
            )
            st.success("Transaction saved.")
            st.rerun()

with tab_transactions:
    st.subheader("Transactions / edit")
    st.caption(f"Showing transactions for portfolio: {selected_portfolio_name}")

    if transactions.empty:
        st.info("No transactions to edit yet.")
    else:
        tickers = ["All"] + sorted(transactions["ticker"].dropna().unique().tolist())
        selected_ticker = st.selectbox("Select ticker", tickers)
        tx = read_transactions(portfolio_id=selected_portfolio_id, ticker=selected_ticker)

        st.caption(
            "Edit the fields below and click Save changes. "
            "Value and Value_CHF are recalculated automatically from shares, price, and rate."
        )

        editable = tx.copy()

        # SQLite stores dates as text. Streamlit's DateColumn requires an actual
        # date/datetime-like value, so convert before passing the dataframe to
        # st.data_editor. update_transaction() converts it back to text for SQLite.
        editable["date"] = pd.to_datetime(editable["date"], errors="coerce").dt.date

        editable["delete"] = False

        edited = st.data_editor(
            editable,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            disabled=[
                "id", "portfolio_id", "portfolio_name", "value", "value_chf",
                "created_at", "updated_at"
            ],
            column_config={
                "delete": st.column_config.CheckboxColumn("Delete"),
                "portfolio_name": st.column_config.TextColumn("Portfolio"),
                "action": st.column_config.SelectboxColumn("Action", options=ACTIONS, required=True),
                "date": st.column_config.DateColumn("Date", required=True),
                "ticker": st.column_config.TextColumn("Ticker", required=True),
                "number_of_shares": st.column_config.NumberColumn("Number_of_Shares", format="%.6f", required=True),
                "price": st.column_config.NumberColumn("Price", format="%.6f", required=True),
                "currency": st.column_config.TextColumn("Currency", max_chars=3, required=True),
                "rate": st.column_config.NumberColumn("Rate", format="%.6f", required=True),
                "value": st.column_config.NumberColumn("Value", format="%.2f"),
                "value_chf": st.column_config.NumberColumn("Value_CHF", format="%.2f"),
            },
        )

        c1, c2, c3 = st.columns([1, 2, 2])
        save = c1.button("Save changes", type="primary")
        c2.download_button(
            "Download selected portfolio CSV",
            data=transactions.to_csv(index=False).encode("utf-8"),
            file_name=f"{selected_portfolio_name}_transactions.csv",
            mime="text/csv",
        )
        c3.download_button(
            "Download all portfolios CSV",
            data=all_transactions.to_csv(index=False).encode("utf-8"),
            file_name="all_portfolio_transactions.csv",
            mime="text/csv",
        )

        if save:
            delete_ids = edited.loc[edited["delete"] == True, "id"].astype(int).tolist()
            delete_transactions(delete_ids)

            rows_to_update = edited.loc[edited["delete"] != True].drop(columns=["delete"])
            for _, row in rows_to_update.iterrows():
                update_transaction(row)

            st.success("Changes saved.")
            st.rerun()


# # """
# # Streamlit Portfolio Tracker

# # Features
# # --------
# # - Manages multiple named portfolios.
# # - Records BUY, SELL, and DIVIDEND transactions in SQLite.
# # - Stores all transaction values in original currency and CHF.
# # - Aggregates multiple transactions per ticker into one portfolio row.
# # - Fetches latest prices with yfinance.
# # - Converts non-CHF market values into CHF using latest FX rates.
# # - Uses historical FX rates for transaction-date CHF conversion.
# # - Lets you inspect, edit, and delete transactions for a selected ticker.

# # Install
# # -------
# # pip install streamlit pandas yfinance

# # Run
# # ---
# # streamlit run app.py
# # """

# # from __future__ import annotations

# # import sqlite3
# # from pathlib import Path
# # from typing import Optional

# # import pandas as pd
# # import streamlit as st
# # import yfinance as yf


# # DB_PATH = Path("portfolio.sqlite3")
# # BASE_CURRENCY = "CHF"
# # DEFAULT_PORTFOLIO_NAME = "SmallCaps"

# # ACTIONS = ["BUY", "SELL", "DIVIDEND"]


# # # -----------------------------
# # # Database layer
# # # -----------------------------

# # @st.cache_resource
# # def get_connection() -> sqlite3.Connection:
# #     conn = sqlite3.connect(DB_PATH, check_same_thread=False)
# #     conn.row_factory = sqlite3.Row
# #     conn.execute("PRAGMA foreign_keys = ON")
# #     return conn


# # def init_db() -> None:
# #     """
# #     Initialize and migrate the SQLite database.

# #     Migration behavior:
# #     - Creates a portfolios table if it does not exist.
# #     - Creates the default portfolio "SmallCaps" if it does not exist.
# #     - Creates transactions table if it does not exist.
# #     - If transactions already exists without portfolio_id, adds it.
# #     - Assigns all existing transactions to "SmallCaps".
# #     """
# #     conn = get_connection()

# #     conn.execute(
# #         """
# #         CREATE TABLE IF NOT EXISTS portfolios (
# #             id INTEGER PRIMARY KEY AUTOINCREMENT,
# #             name TEXT NOT NULL UNIQUE COLLATE NOCASE,
# #             created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
# #             updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
# #         )
# #         """
# #     )

# #     conn.execute(
# #         "INSERT OR IGNORE INTO portfolios (name) VALUES (?)",
# #         (DEFAULT_PORTFOLIO_NAME,),
# #     )

# #     default_portfolio_id = conn.execute(
# #         "SELECT id FROM portfolios WHERE name = ?",
# #         (DEFAULT_PORTFOLIO_NAME,),
# #     ).fetchone()["id"]

# #     conn.execute(
# #         """
# #         CREATE TABLE IF NOT EXISTS transactions (
# #             id INTEGER PRIMARY KEY AUTOINCREMENT,
# #             portfolio_id INTEGER,
# #             action TEXT NOT NULL CHECK(action IN ('BUY', 'SELL', 'DIVIDEND')),
# #             date TEXT NOT NULL,
# #             ticker TEXT NOT NULL,
# #             number_of_shares REAL NOT NULL,
# #             price REAL NOT NULL,
# #             currency TEXT NOT NULL,
# #             rate REAL NOT NULL,
# #             value REAL NOT NULL,
# #             value_chf REAL NOT NULL,
# #             created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
# #             updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
# #             FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE
# #         )
# #         """
# #     )

# #     transaction_columns = {
# #         row["name"]
# #         for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
# #     }

# #     if "portfolio_id" not in transaction_columns:
# #         conn.execute("ALTER TABLE transactions ADD COLUMN portfolio_id INTEGER")

# #     conn.execute(
# #         "UPDATE transactions SET portfolio_id = ? WHERE portfolio_id IS NULL",
# #         (default_portfolio_id,),
# #     )

# #     conn.execute(
# #         "CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_ticker ON transactions (portfolio_id, ticker)"
# #     )
# #     conn.execute(
# #         "CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date)"
# #     )

# #     conn.commit()


# # def read_portfolios() -> pd.DataFrame:
# #     conn = get_connection()
# #     return pd.read_sql_query(
# #         "SELECT id, name, created_at, updated_at FROM portfolios ORDER BY name",
# #         conn,
# #     )


# # def get_default_portfolio_id() -> int:
# #     conn = get_connection()
# #     row = conn.execute(
# #         "SELECT id FROM portfolios WHERE name = ?",
# #         (DEFAULT_PORTFOLIO_NAME,),
# #     ).fetchone()
# #     if row is None:
# #         conn.execute("INSERT INTO portfolios (name) VALUES (?)", (DEFAULT_PORTFOLIO_NAME,))
# #         conn.commit()
# #         row = conn.execute(
# #             "SELECT id FROM portfolios WHERE name = ?",
# #             (DEFAULT_PORTFOLIO_NAME,),
# #         ).fetchone()
# #     return int(row["id"])


# # def create_portfolio(name: str) -> int:
# #     name = name.strip()
# #     if not name:
# #         raise ValueError("Portfolio name is required.")

# #     conn = get_connection()
# #     existing = conn.execute(
# #         "SELECT id FROM portfolios WHERE lower(name) = lower(?)",
# #         (name,),
# #     ).fetchone()
# #     if existing:
# #         raise ValueError(f"A portfolio named '{name}' already exists.")

# #     cur = conn.execute("INSERT INTO portfolios (name) VALUES (?)", (name,))
# #     conn.commit()
# #     return int(cur.lastrowid)


# # def rename_portfolio(portfolio_id: int, new_name: str) -> None:
# #     new_name = new_name.strip()
# #     if not new_name:
# #         raise ValueError("Portfolio name is required.")

# #     conn = get_connection()
# #     existing = conn.execute(
# #         "SELECT id FROM portfolios WHERE lower(name) = lower(?) AND id <> ?",
# #         (new_name, portfolio_id),
# #     ).fetchone()
# #     if existing:
# #         raise ValueError(f"A portfolio named '{new_name}' already exists.")

# #     conn.execute(
# #         """
# #         UPDATE portfolios
# #         SET name = ?, updated_at = CURRENT_TIMESTAMP
# #         WHERE id = ?
# #         """,
# #         (new_name, portfolio_id),
# #     )
# #     conn.commit()


# # def delete_portfolio(portfolio_id: int) -> None:
# #     conn = get_connection()
# #     count = conn.execute("SELECT COUNT(*) AS n FROM portfolios").fetchone()["n"]
# #     if count <= 1:
# #         raise ValueError("You cannot delete the only remaining portfolio.")

# #     # Delete transactions explicitly so this also works for databases migrated
# #     # from an older schema where the FK constraint may not be enforced.
# #     conn.execute("DELETE FROM transactions WHERE portfolio_id = ?", (portfolio_id,))
# #     conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
# #     conn.commit()


# # def read_transactions(
# #     portfolio_id: Optional[int] = None,
# #     ticker: Optional[str] = None,
# # ) -> pd.DataFrame:
# #     conn = get_connection()

# #     where_clauses = []
# #     params: list[object] = []

# #     if portfolio_id is not None:
# #         where_clauses.append("t.portfolio_id = ?")
# #         params.append(int(portfolio_id))

# #     if ticker and ticker != "All":
# #         where_clauses.append("t.ticker = ?")
# #         params.append(ticker.upper())

# #     where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

# #     query = f"""
# #         SELECT
# #             t.id,
# #             t.portfolio_id,
# #             p.name AS portfolio_name,
# #             t.action,
# #             t.date,
# #             t.ticker,
# #             t.number_of_shares,
# #             t.price,
# #             t.currency,
# #             t.rate,
# #             t.value,
# #             t.value_chf,
# #             t.created_at,
# #             t.updated_at
# #         FROM transactions t
# #         LEFT JOIN portfolios p ON p.id = t.portfolio_id
# #         {where_sql}
# #         ORDER BY t.date DESC, t.ticker, t.id DESC
# #     """

# #     df = pd.read_sql_query(query, conn, params=params)

# #     if df.empty:
# #         return pd.DataFrame(
# #             columns=[
# #                 "id", "portfolio_id", "portfolio_name", "action", "date", "ticker",
# #                 "number_of_shares", "price", "currency", "rate", "value",
# #                 "value_chf", "created_at", "updated_at"
# #             ]
# #         )
# #     return df


# # def insert_transaction(
# #     portfolio_id: int,
# #     action: str,
# #     date: str,
# #     ticker: str,
# #     number_of_shares: float,
# #     price: float,
# #     currency: str,
# #     rate: float,
# # ) -> None:
# #     action = action.upper().strip()
# #     ticker = ticker.upper().strip()
# #     currency = currency.upper().strip()

# #     if action == "SELL" and number_of_shares > 0:
# #         number_of_shares = -number_of_shares

# #     value = number_of_shares * price
# #     value_chf = value * rate

# #     conn = get_connection()
# #     conn.execute(
# #         """
# #         INSERT INTO transactions
# #             (portfolio_id, action, date, ticker, number_of_shares, price, currency, rate, value, value_chf)
# #         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
# #         """,
# #         (portfolio_id, action, date, ticker, number_of_shares, price, currency, rate, value, value_chf),
# #     )
# #     conn.commit()


# # def update_transaction(row: pd.Series) -> None:
# #     action = str(row["action"]).upper().strip()
# #     ticker = str(row["ticker"]).upper().strip()
# #     currency = str(row["currency"]).upper().strip()
# #     number_of_shares = float(row["number_of_shares"])
# #     price = float(row["price"])
# #     rate = float(row["rate"])

# #     if action == "SELL" and number_of_shares > 0:
# #         number_of_shares = -number_of_shares

# #     value = number_of_shares * price
# #     value_chf = value * rate

# #     conn = get_connection()
# #     conn.execute(
# #         """
# #         UPDATE transactions
# #         SET action = ?,
# #             date = ?,
# #             ticker = ?,
# #             number_of_shares = ?,
# #             price = ?,
# #             currency = ?,
# #             rate = ?,
# #             value = ?,
# #             value_chf = ?,
# #             updated_at = CURRENT_TIMESTAMP
# #         WHERE id = ?
# #         """,
# #         (
# #             action,
# #             str(row["date"]),
# #             ticker,
# #             number_of_shares,
# #             price,
# #             currency,
# #             rate,
# #             value,
# #             value_chf,
# #             int(row["id"]),
# #         ),
# #     )
# #     conn.commit()


# # def delete_transactions(ids: list[int]) -> None:
# #     if not ids:
# #         return
# #     conn = get_connection()
# #     placeholders = ",".join("?" for _ in ids)
# #     conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
# #     conn.commit()


# # # -----------------------------
# # # Market data / FX layer
# # # -----------------------------

# # @st.cache_data(ttl=900, show_spinner=False)
# # def get_latest_price_and_currency(ticker: str) -> tuple[Optional[float], Optional[str]]:
# #     """Return latest traded price and Yahoo Finance currency for a ticker."""
# #     ticker = ticker.upper().strip()
# #     try:
# #         yf_ticker = yf.Ticker(ticker)

# #         price = None
# #         currency = None

# #         # fast_info is quick when available, but can be incomplete for some instruments.
# #         try:
# #             fast = yf_ticker.fast_info
# #             price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
# #             currency = fast.get("currency") if hasattr(fast, "get") else getattr(fast, "currency", None)
# #         except Exception:
# #             pass

# #         if price is None:
# #             hist = yf_ticker.history(period="5d", interval="1d", auto_adjust=False)
# #             if not hist.empty:
# #                 price = float(hist["Close"].dropna().iloc[-1])

# #         if currency is None:
# #             try:
# #                 info = yf_ticker.get_info()
# #                 currency = info.get("currency")
# #             except Exception:
# #                 currency = None

# #         return (float(price) if price is not None else None, currency.upper() if currency else None)
# #     except Exception:
# #         return None, None


# # def _rate_from_history_on_or_before(hist: pd.DataFrame, target_date: Optional[str] = None) -> Optional[float]:
# #     """Extract the latest Close rate on or before target_date from a Yahoo history dataframe."""
# #     if hist.empty or "Close" not in hist.columns:
# #         return None

# #     close = hist["Close"].dropna().copy()
# #     if close.empty:
# #         return None

# #     if target_date:
# #         target = pd.to_datetime(target_date).date()

# #         # Compare only calendar dates. Yahoo often returns timezone-aware indexes
# #         # for FX data, while SQLite/date_input values are timezone-naive.
# #         close_dates = pd.Series(pd.to_datetime(close.index).date, index=close.index)
# #         close = close.loc[close_dates <= target]

# #         if close.empty:
# #             return None

# #     return float(close.iloc[-1])


# # @st.cache_data(ttl=900, show_spinner=False)
# # def get_latest_fx_rate_to_chf(currency: str) -> Optional[float]:
# #     """Return latest FX rate so that amount_in_currency * rate = amount_in_chf."""
# #     currency = currency.upper().strip()
# #     if currency == BASE_CURRENCY:
# #         return 1.0

# #     pair = f"{currency}{BASE_CURRENCY}=X"
# #     inverse_pair = f"{BASE_CURRENCY}{currency}=X"

# #     try:
# #         rate = _rate_from_history_on_or_before(
# #             yf.Ticker(pair).history(period="5d", interval="1d")
# #         )
# #         if rate is not None:
# #             return rate
# #     except Exception:
# #         pass

# #     try:
# #         inverse_rate = _rate_from_history_on_or_before(
# #             yf.Ticker(inverse_pair).history(period="5d", interval="1d")
# #         )
# #         if inverse_rate is not None and inverse_rate != 0:
# #             return 1.0 / inverse_rate
# #     except Exception:
# #         pass

# #     return None


# # @st.cache_data(ttl=900, show_spinner=False)
# # def get_historical_fx_rate_to_chf(currency: str, transaction_date: str) -> Optional[float]:
# #     """
# #     Return historical FX rate for a transaction date.

# #     This function is used when SAVING transactions. It deliberately does not
# #     fall back to today's FX rate. If no historical quote is found, it returns
# #     None so the transaction is not saved with an incorrect rate.

# #     If the exact transaction date has no FX quote, for example a weekend or
# #     holiday, it uses the latest available quote on or before that date.
# #     """
# #     currency = currency.upper().strip()
# #     if currency == BASE_CURRENCY:
# #         return 1.0

# #     tx_date = pd.to_datetime(transaction_date).date()
# #     start = pd.Timestamp(tx_date) - pd.Timedelta(days=14)
# #     end = pd.Timestamp(tx_date) + pd.Timedelta(days=1)

# #     pair = f"{currency}{BASE_CURRENCY}=X"
# #     inverse_pair = f"{BASE_CURRENCY}{currency}=X"

# #     try:
# #         hist = yf.Ticker(pair).history(
# #             start=start.strftime("%Y-%m-%d"),
# #             end=end.strftime("%Y-%m-%d"),
# #             interval="1d",
# #         )
# #         rate = _rate_from_history_on_or_before(hist, target_date=str(tx_date))
# #         if rate is not None:
# #             return rate
# #     except Exception:
# #         pass

# #     # Fallback: try inverse pair and invert it.
# #     try:
# #         hist = yf.Ticker(inverse_pair).history(
# #             start=start.strftime("%Y-%m-%d"),
# #             end=end.strftime("%Y-%m-%d"),
# #             interval="1d",
# #         )
# #         inverse_rate = _rate_from_history_on_or_before(hist, target_date=str(tx_date))
# #         if inverse_rate is not None and inverse_rate != 0:
# #             return 1.0 / inverse_rate
# #     except Exception:
# #         pass

# #     return None


# # # -----------------------------
# # # Portfolio calculations
# # # -----------------------------

# # def build_portfolio(transactions: pd.DataFrame) -> pd.DataFrame:
# #     if transactions.empty:
# #         return pd.DataFrame()

# #     rows = []
# #     for ticker, group in transactions.groupby("ticker"):
# #         buys = group[group["action"] == "BUY"]
# #         sells = group[group["action"] == "SELL"]
# #         dividends = group[group["action"] == "DIVIDEND"]

# #         shares_held = group[group["action"].isin(["BUY", "SELL"])] ["number_of_shares"].sum()
# #         buy_cost_chf = buys["value_chf"].sum()
# #         sale_proceeds_chf = -sells["value_chf"].sum()  # SELL values are negative by design.
# #         dividends_chf = dividends["value_chf"].sum()

# #         latest_price, market_currency = get_latest_price_and_currency(ticker)

# #         # If Yahoo cannot provide a currency, use the most recent transaction currency as fallback.
# #         fallback_currency = str(group.sort_values("date").iloc[-1]["currency"]).upper()
# #         market_currency = market_currency or fallback_currency

# #         latest_rate = get_latest_fx_rate_to_chf(market_currency) if market_currency else None

# #         if latest_price is not None and latest_rate is not None:
# #             market_value_chf = shares_held * latest_price * latest_rate
# #         else:
# #             market_value_chf = None

# #         total_return_chf = None
# #         total_return_pct = None
# #         if market_value_chf is not None:
# #             total_return_chf = market_value_chf + sale_proceeds_chf + dividends_chf - buy_cost_chf
# #             total_return_pct = total_return_chf / buy_cost_chf if buy_cost_chf else None

# #         # Average buy cost per share is shown in the share's original currency,
# #         # not CHF. For BUY transactions, Value = Number_of_Shares * Price in
# #         # the transaction/share currency.
# #         avg_buy_price = (
# #             buys["value"].sum() / buys["number_of_shares"].sum()
# #             if not buys.empty and buys["number_of_shares"].sum()
# #             else None
# #         )

# #         rows.append(
# #             {
# #                 "Ticker": ticker,
# #                 "Shares Held": shares_held,
# #                 "Avg Cost / Share": avg_buy_price,
# #                 "Latest Price": latest_price,
# #                 "Currency": market_currency,
# #                 "Latest Rate": latest_rate,
# #                 "Market Value CHF": market_value_chf,
# #                 "Buy Cost CHF": buy_cost_chf,
# #                 "Sales CHF": sale_proceeds_chf,
# #                 "Dividends CHF": dividends_chf,
# #                 "Total Return CHF": total_return_chf,
# #                 "Return %": total_return_pct,
# #                 "Transactions": len(group),
# #             }
# #         )

# #     portfolio = pd.DataFrame(rows)
# #     if not portfolio.empty:
# #         portfolio = portfolio.sort_values("Market Value CHF", ascending=False, na_position="last")
# #     return portfolio


# # def summarize_portfolio(portfolio: pd.DataFrame) -> dict[str, Optional[float]]:
# #     if portfolio.empty:
# #         return {
# #             "market_value_chf": 0.0,
# #             "buy_cost_chf": 0.0,
# #             "sale_proceeds_chf": 0.0,
# #             "dividends_chf": 0.0,
# #             "total_return_chf": 0.0,
# #             "total_return_pct": None,
# #         }

# #     total_market_value = portfolio["Market Value CHF"].sum(skipna=True)
# #     total_buy_cost = portfolio["Buy Cost CHF"].sum(skipna=True)
# #     total_sale_proceeds = portfolio["Sales CHF"].sum(skipna=True)
# #     total_dividends = portfolio["Dividends CHF"].sum(skipna=True)
# #     total_return = portfolio["Total Return CHF"].sum(skipna=True)
# #     total_return_pct = total_return / total_buy_cost if total_buy_cost else None
    
# #     return {
# #         "market_value_chf": total_market_value,
# #         "buy_cost_chf": total_buy_cost,
# #         "sale_proceeds_chf": total_sale_proceeds,
# #         "dividends_chf": total_dividends,
# #         "total_return_chf": total_return,
# #         "total_return_pct": total_return_pct,
# #     }


# # def build_portfolio_summary_by_name(portfolios: pd.DataFrame) -> pd.DataFrame:
# #     rows = []
# #     for _, row in portfolios.iterrows():
# #         portfolio_id = int(row["id"])
# #         portfolio_name = str(row["name"])
# #         tx = read_transactions(portfolio_id=portfolio_id)
# #         pf = build_portfolio(tx)
# #         summary = summarize_portfolio(pf)
# #         rows.append(
# #             {
# #                 "Portfolio": portfolio_name,
# #                 "Market Value CHF": summary["market_value_chf"],
# #                 "Buy Cost CHF": summary["buy_cost_chf"],
# #                 "Sales CHF": summary["sale_proceeds_chf"],
# #                 "Dividends CHF": summary["dividends_chf"],
# #                 "Total Return CHF": summary["total_return_chf"],
# #                 "Total Return %": summary["total_return_pct"],
# #             }
# #         )
# #     return pd.DataFrame(rows)


# # # -----------------------------
# # # Formatting helpers
# # # -----------------------------

# # def format_money(value: float | None) -> str:
# #     if value is None or pd.isna(value):
# #         return "—"
# #     return f"{float(value):,.2f}".replace(",", "'")


# # def format_pct(value: float | None) -> str:
# #     if value is None or pd.isna(value):
# #         return "—"
# #     return f"{value:.2%}"


# # def format_swiss_number(value: float | None, decimals: int = 2, prefix: str = "") -> str:
# #     """Format numbers as 1'234'567.89 for display."""
# #     if value is None or pd.isna(value):
# #         return "—"
# #     formatted = f"{float(value):,.{decimals}f}".replace(",", "'")
# #     return f"{prefix}{formatted}"


# # def format_swiss_percent(value: float | None) -> str:
# #     """Format a decimal return value as 12.34%, using apostrophe thousands if needed."""
# #     if value is None or pd.isna(value):
# #         return "—"
# #     formatted = f"{float(value) * 100:,.2f}".replace(",", "'")
# #     return f"{formatted}%"


# # def style_swiss_numbers(df: pd.DataFrame) -> pd.io.formats.style.Styler:
# #     swiss_formatters = {
# #         "Shares Held": lambda x: format_swiss_number(x, decimals=2),
# #         "Avg Cost / Share": lambda x: format_swiss_number(x, decimals=2),
# #         "Latest Price": lambda x: format_swiss_number(x, decimals=2),
# #         "Latest Rate": lambda x: format_swiss_number(x, decimals=3),
# #         "Market Value CHF": lambda x: format_swiss_number(x, decimals=2),
# #         "Value CHF": lambda x: format_swiss_number(x, decimals=2),
# #         "Buy Cost CHF": lambda x: format_swiss_number(x, decimals=2),
# #         "Sales CHF": lambda x: format_swiss_number(x, decimals=2),
# #         "Dividends CHF": lambda x: format_swiss_number(x, decimals=2),
# #         "Total Return CHF": lambda x: format_swiss_number(x, decimals=2, prefix=""),
# #         "Return %": format_swiss_percent,
# #     }

# #     swiss_formatters = {
# #         col: formatter
# #         for col, formatter in swiss_formatters.items()
# #         if col in df.columns
# #     }

# #     return (
# #         df.style
# #         .format(swiss_formatters)
# #         .set_properties(
# #             subset=list(swiss_formatters.keys()),
# #             **{"text-align": "right"},
# #         )
# #     )


# # # -----------------------------
# # # Streamlit UI
# # # -----------------------------

# # st.set_page_config(page_title="Portfolio Tracker", layout="wide")
# # init_db()

# # portfolios = read_portfolios()

# # if "selected_portfolio_id" not in st.session_state:
# #     st.session_state["selected_portfolio_id"] = get_default_portfolio_id()

# # if int(st.session_state["selected_portfolio_id"]) not in set(portfolios["id"].astype(int)):
# #     st.session_state["selected_portfolio_id"] = int(portfolios.iloc[0]["id"])

# # portfolio_names = portfolios["name"].tolist()
# # portfolio_ids = portfolios["id"].astype(int).tolist()
# # portfolio_id_by_name = dict(zip(portfolio_names, portfolio_ids))
# # portfolio_name_by_id = dict(zip(portfolio_ids, portfolio_names))

# # selected_portfolio_id = int(st.session_state["selected_portfolio_id"])
# # selected_portfolio_name = portfolio_name_by_id[selected_portfolio_id]
# # selected_index = portfolio_names.index(selected_portfolio_name)

# # st.title("Portfolio Tracker")
# # st.caption("Portfolio performance calculated in CHF.")

# # with st.sidebar:
# #     st.header("Portfolio")

# #     selected_portfolio_name = st.selectbox(
# #         "Current portfolio",
# #         portfolio_names,
# #         index=selected_index,
# #     )
# #     selected_portfolio_id = int(portfolio_id_by_name[selected_portfolio_name])
# #     st.session_state["selected_portfolio_id"] = selected_portfolio_id

# #     with st.expander("Manage portfolios"):
# #         with st.form("add_portfolio_form", clear_on_submit=True):
# #             new_portfolio_name = st.text_input("New portfolio name")
# #             add_portfolio = st.form_submit_button("Add portfolio")
# #             if add_portfolio:
# #                 try:
# #                     new_id = create_portfolio(new_portfolio_name)
# #                     st.session_state["selected_portfolio_id"] = new_id
# #                     st.success("Portfolio added.")
# #                     st.rerun()
# #                 except ValueError as exc:
# #                     st.error(str(exc))

# #         with st.form("rename_portfolio_form"):
# #             renamed_portfolio_name = st.text_input(
# #                 "Rename selected portfolio to",
# #                 value=selected_portfolio_name,
# #             )
# #             rename_selected = st.form_submit_button("Rename selected portfolio")
# #             if rename_selected:
# #                 try:
# #                     rename_portfolio(selected_portfolio_id, renamed_portfolio_name)
# #                     st.success("Portfolio renamed.")
# #                     st.rerun()
# #                 except ValueError as exc:
# #                     st.error(str(exc))

# #         st.warning("Deleting a portfolio also deletes all transactions in that portfolio.")
# #         confirm_delete = st.checkbox(
# #             f"I understand: delete '{selected_portfolio_name}' and its transactions",
# #             key=f"confirm_delete_portfolio_{selected_portfolio_id}",
# #         )
# #         if st.button("Delete selected portfolio", disabled=not confirm_delete):
# #             try:
# #                 delete_portfolio(selected_portfolio_id)
# #                 refreshed = read_portfolios()
# #                 st.session_state["selected_portfolio_id"] = int(refreshed.iloc[0]["id"])
# #                 st.success("Portfolio deleted.")
# #                 st.rerun()
# #             except ValueError as exc:
# #                 st.error(str(exc))

# #     st.divider()
# #     st.header("Data")
# #     st.write(f"Database: `{DB_PATH}`")
# #     if st.button("Refresh market prices"):
# #         get_latest_price_and_currency.clear()
# #         get_latest_fx_rate_to_chf.clear()
# #         get_historical_fx_rate_to_chf.clear()
# #         st.rerun()

# #     st.divider()
# #     st.write("Performance method")
# #     st.caption(
# #         "Cash-flow return = current market value + sale proceeds + dividends − buy cost. "
# #         "This is not FIFO/LIFO tax-lot accounting."
# #     )


# # transactions = read_transactions(portfolio_id=selected_portfolio_id)
# # portfolio = build_portfolio(transactions)
# # selected_summary = summarize_portfolio(portfolio)

# # all_transactions = read_transactions()
# # all_portfolio = build_portfolio(all_transactions)
# # grand_summary = summarize_portfolio(all_portfolio)
# # portfolio_summary = build_portfolio_summary_by_name(portfolios)

# # st.markdown("""
# # <style>

# # /* Container for tabs */
# # div[data-testid="stTabs"] > div[role="tablist"] {
# #     display: flex;
# # }

# # /* Each tab */
# # div[data-testid="stTabs"] button[role="tab"] {
# #     flex: 1;                      /* equal width */
# #     background-color: #e6f2ff;    /* light blue */
# #     color: #003366;               /* dark blue text */
# #     border: 1px solid #cce0ff;
# #     border-bottom: none;
# #     padding: 10px 0;
# #     font-weight: 600;
# # }

# # /* Hover effect */
# # div[data-testid="stTabs"] button[role="tab"]:hover {
# #     background-color: #d6e9ff;
# #     color: #002244;
# # }

# # /* Active tab */
# # div[data-testid="stTabs"] button[aria-selected="true"] {
# #     background-color: #ffffff;    /* active = white */
# #     color: #001933;
# #     border-bottom: 2px solid white;
# # }

# # /* Optional: spacing under tabs */
# # div[data-testid="stTabs"] {
# #     margin-bottom: 1rem;
# # }

# # </style>
# # """, unsafe_allow_html=True)


# # tab_overview, tab_add, tab_transactions = st.tabs(
# #     ["Portfolio", "Add transaction", "Transactions / edit"]
# # )

# # with tab_overview:
    
# #     # Create a container with a border
# #     with st.container(border=True, ):
# #         st.subheader("Summary for all portfolios")

# #         g1, g2, g3, g4, g5 = st.columns(5)

# #         g1.metric("Market Value CHF", format_money(grand_summary["market_value_chf"]))
# #         g2.metric("Buy Cost CHF", format_money(grand_summary["buy_cost_chf"]))
# #         g3.metric("Sales CHF", format_money(grand_summary["sale_proceeds_chf"]))
# #         g4.metric("Dividends CHF", format_money(grand_summary["dividends_chf"]))
# #         g5.metric(
# #             "Total Return",
# #             f"CHF {format_money(grand_summary['total_return_chf'])}",
# #             format_pct(grand_summary["total_return_pct"]),
# #         )
 
# #         if not portfolio_summary.empty:
# #             with st.expander("Summary by portfolio", expanded=False):
# #                 st.dataframe(
# #                     style_swiss_numbers(portfolio_summary),
# #                     use_container_width=True,
# #                     hide_index=True,
# #                 )

# #     # st.divider()
# #     st.subheader(f"Portfolio overview: {selected_portfolio_name}")

# #     if portfolio.empty:
# #         st.info("No transactions yet. Add a BUY transaction to start.")
# #     else:
# #         with st.container(border=True, ):
# #             c1, c2, c3, c4, c5 = st.columns(5)
# #             c1.metric("Market Value CHF", format_money(selected_summary["market_value_chf"]))
# #             c2.metric("Buy Cost CHF", format_money(selected_summary["buy_cost_chf"]))
# #             c3.metric("Sales CHF", format_money(selected_summary["sale_proceeds_chf"]))
# #             c4.metric("Dividends CHF", format_money(selected_summary["dividends_chf"]))
# #             c5.metric(
# #                 "Total Return",
# #                 f"CHF {format_money(selected_summary['total_return_chf'])}",
# #                 format_pct(selected_summary["total_return_pct"]),
# #             )

# #             st.dataframe(
# #                 style_swiss_numbers(portfolio),
# #                 use_container_width=True,
# #                 hide_index=True,
# #             )

# # with tab_add:
# #     st.subheader(f'Add transaction to  "{selected_portfolio_name}" ')
# #     # st.caption(f"Transaction will be saved to portfolio: {selected_portfolio_name}")

# #     # Transaction entry is intentionally simplified:
# #     # the user enters ticker, shares, and price; the app looks up currency and CHF FX rate.
# #     with st.form("add_transaction_form", clear_on_submit=True):
# #         c1, c2, c3 = st.columns(3)
# #         action = c1.selectbox("Action", ACTIONS)
# #         date = c2.date_input("Date")
# #         ticker = c3.text_input("Ticker", placeholder="Yahoo-style")

# #         c4, c5 = st.columns(2)
# #         number_of_shares = c4.number_input("Number of Shares", min_value=1.0, step=10.0, format="%.1f")
# #         price = c5.number_input("Price / Dividend", min_value=0.0, step=0.01, format="%.4f")

# #         ticker_clean = ticker.upper().strip()
# #         detected_currency = None
# #         detected_rate = None

# #         if ticker_clean:
# #             _, detected_currency = get_latest_price_and_currency(ticker_clean)
# #             if detected_currency:
# #                 detected_rate = get_historical_fx_rate_to_chf(detected_currency, transaction_date=str(date))

# #         if ticker_clean and detected_currency and detected_rate:
# #             value_preview = number_of_shares * price
# #             if action == "SELL":
# #                 value_preview = -abs(value_preview)
# #             value_chf_preview = value_preview * detected_rate

# #             st.info(
# #                 f"Detected currency: {detected_currency} | "
# #                 f"CHF rate on {date}: {detected_rate:.6f} | "
# #                 f"Value: {format_swiss_number(value_preview, decimals=2)} {detected_currency} | "
# #                 f"Value_CHF: {format_swiss_number(value_chf_preview, decimals=2)} CHF"
# #             )
# #         elif ticker_clean:
# #             st.warning(
# #                 "Could not automatically detect the ticker currency or historical CHF exchange rate. "
# #                 "Check that the ticker symbol is valid for Yahoo Finance, for example AAPL, NESN.SW, MSFT."
# #             )
# #         else:
# #             st.caption("Enter a ticker to automatically detect currency and CHF exchange rate.")

# #         submitted = st.form_submit_button("Save transaction")

# #     if submitted:
# #         if not ticker_clean:
# #             st.error("Ticker is required.")
# #         elif number_of_shares <= 0:
# #             st.error("Number of shares must be greater than zero.")
# #         elif price <= 0:
# #             st.error("Price must be greater than zero.")
# #         elif not detected_currency:
# #             st.error("Could not detect the ticker currency. Please check the ticker symbol.")
# #         elif not detected_rate or detected_rate <= 0:
# #             st.error("Could not fetch a valid historical CHF exchange rate for this ticker currency and date.")
# #         else:
# #             insert_transaction(
# #                 portfolio_id=selected_portfolio_id,
# #                 action=action,
# #                 date=str(date),
# #                 ticker=ticker_clean,
# #                 number_of_shares=number_of_shares,
# #                 price=price,
# #                 currency=detected_currency,
# #                 rate=detected_rate,
# #             )
# #             st.success("Transaction saved.")
# #             st.rerun()

# # with tab_transactions:
# #     st.subheader(f'Edit Transactions in "{selected_portfolio_name}" ')
# #     # st.caption(f"Showing transactions for portfolio: {selected_portfolio_name}")

# #     if transactions.empty:
# #         st.info("No transactions to edit yet.")
# #     else:
# #         tickers = ["All"] + sorted(transactions["ticker"].dropna().unique().tolist())
# #         selected_ticker = st.selectbox("Select ticker", tickers)
# #         tx = read_transactions(portfolio_id=selected_portfolio_id, ticker=selected_ticker)

# #         st.caption(
# #             "Edit the fields below and click Save changes. "
# #             "Value and Value_CHF are recalculated automatically from shares, price, and rate."
# #         )

# #         editable = tx.copy()

# #         # SQLite stores dates as text. Streamlit's DateColumn requires an actual
# #         # date/datetime-like value, so convert before passing the dataframe to
# #         # st.data_editor. update_transaction() converts it back to text for SQLite.
# #         editable["date"] = pd.to_datetime(editable["date"], errors="coerce").dt.date

# #         editable["delete"] = False

# #         edited = st.data_editor(
# #             editable,
# #             use_container_width=True,
# #             hide_index=True,
# #             num_rows="fixed",
# #             disabled=[
# #                 "id", "portfolio_id", "portfolio_name", "value", "value_chf",
# #                 "created_at", "updated_at"
# #             ],
# #             column_config={
# #                 "delete": st.column_config.CheckboxColumn("Delete"),
# #                 "portfolio_name": st.column_config.TextColumn("Portfolio"),
# #                 "action": st.column_config.SelectboxColumn("Action", options=ACTIONS, required=True),
# #                 "date": st.column_config.DateColumn("Date", required=True),
# #                 "ticker": st.column_config.TextColumn("Ticker", required=True),
# #                 "number_of_shares": st.column_config.NumberColumn("Number_of_Shares", format="%.1f", required=True),
# #                 "price": st.column_config.NumberColumn("Price", format="%.4f", required=True),
# #                 "currency": st.column_config.TextColumn("Currency", max_chars=3, required=True),
# #                 "rate": st.column_config.NumberColumn("Rate", format="%.4f", required=True),
# #                 "value": st.column_config.NumberColumn("Value", format="%.2f"),
# #                 "value_chf": st.column_config.NumberColumn("Value_CHF", format="%.2f"),
# #             },
# #         )

# #         c1, c2, c3 = st.columns([1, 2, 2])
# #         save = c1.button("Save changes", type="primary")
# #         c2.download_button(
# #             "Download selected portfolio CSV",
# #             data=transactions.to_csv(index=False).encode("utf-8"),
# #             file_name=f"{selected_portfolio_name}_transactions.csv",
# #             mime="text/csv",
# #         )
# #         c3.download_button(
# #             "Download all portfolios CSV",
# #             data=all_transactions.to_csv(index=False).encode("utf-8"),
# #             file_name="all_portfolio_transactions.csv",
# #             mime="text/csv",
# #         )

# #         if save:
# #             delete_ids = edited.loc[edited["delete"] == True, "id"].astype(int).tolist()
# #             delete_transactions(delete_ids)

# #             rows_to_update = edited.loc[edited["delete"] != True].drop(columns=["delete"])
# #             for _, row in rows_to_update.iterrows():
# #                 update_transaction(row)

# #             st.success("Changes saved.")
# #             st.rerun()



# """
# Streamlit Portfolio Tracker

# Features
# --------
# - Records BUY, SELL, and DIVIDEND transactions in SQLite.
# - Stores all transaction values in original currency and CHF.
# - Aggregates multiple transactions per ticker into one portfolio row.
# - Fetches latest prices with yfinance.
# - Converts non-CHF market values into CHF using latest FX rates.
# - Lets you inspect, edit, and delete transactions for a selected ticker.

# Install
# -------
# pip install streamlit pandas yfinance

# Run
# ---
# streamlit run app.py
# """

# from __future__ import annotations

# import sqlite3
# from pathlib import Path
# from typing import Optional

# import pandas as pd
# import streamlit as st
# import yfinance as yf


# DB_PATH = Path("portfolio.sqlite3")
# BASE_CURRENCY = "CHF"

# ACTIONS = ["BUY", "SELL", "DIVIDEND"]


# # -----------------------------
# # Database layer
# # -----------------------------

# @st.cache_resource
# def get_connection() -> sqlite3.Connection:
#     conn = sqlite3.connect(DB_PATH, check_same_thread=False)
#     conn.row_factory = sqlite3.Row
#     return conn


# def init_db() -> None:
#     conn = get_connection()
#     conn.execute(
#         """
#         CREATE TABLE IF NOT EXISTS transactions (
#             id INTEGER PRIMARY KEY AUTOINCREMENT,
#             action TEXT NOT NULL CHECK(action IN ('BUY', 'SELL', 'DIVIDEND')),
#             date TEXT NOT NULL,
#             ticker TEXT NOT NULL,
#             number_of_shares REAL NOT NULL,
#             price REAL NOT NULL,
#             currency TEXT NOT NULL,
#             rate REAL NOT NULL,
#             value REAL NOT NULL,
#             value_chf REAL NOT NULL,
#             created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
#             updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
#         )
#         """
#     )
#     conn.commit()


# def read_transactions(ticker: Optional[str] = None) -> pd.DataFrame:
#     conn = get_connection()
#     if ticker and ticker != "All":
#         query = "SELECT * FROM transactions WHERE ticker = ? ORDER BY date DESC, id DESC"
#         df = pd.read_sql_query(query, conn, params=(ticker.upper(),))
#     else:
#         query = "SELECT * FROM transactions ORDER BY date DESC, ticker, id DESC"
#         df = pd.read_sql_query(query, conn)

#     if df.empty:
#         return pd.DataFrame(
#             columns=[
#                 "id", "action", "date", "ticker", "number_of_shares", "price",
#                 "currency", "rate", "value", "value_chf", "created_at", "updated_at"
#             ]
#         )
#     return df


# def insert_transaction(
#     action: str,
#     date: str,
#     ticker: str,
#     number_of_shares: float,
#     price: float,
#     currency: str,
#     rate: float,
# ) -> None:
#     action = action.upper().strip()
#     ticker = ticker.upper().strip()
#     currency = currency.upper().strip()

#     if action == "SELL" and number_of_shares > 0:
#         number_of_shares = -number_of_shares

#     value = number_of_shares * price
#     value_chf = value * rate

#     conn = get_connection()
#     conn.execute(
#         """
#         INSERT INTO transactions
#             (action, date, ticker, number_of_shares, price, currency, rate, value, value_chf)
#         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
#         """,
#         (action, date, ticker, number_of_shares, price, currency, rate, value, value_chf),
#     )
#     conn.commit()


# def update_transaction(row: pd.Series) -> None:
#     action = str(row["action"]).upper().strip()
#     ticker = str(row["ticker"]).upper().strip()
#     currency = str(row["currency"]).upper().strip()
#     number_of_shares = float(row["number_of_shares"])
#     price = float(row["price"])
#     rate = float(row["rate"])

#     if action == "SELL" and number_of_shares > 0:
#         number_of_shares = -number_of_shares

#     value = number_of_shares * price
#     value_chf = value * rate

#     conn = get_connection()
#     conn.execute(
#         """
#         UPDATE transactions
#         SET action = ?,
#             date = ?,
#             ticker = ?,
#             number_of_shares = ?,
#             price = ?,
#             currency = ?,
#             rate = ?,
#             value = ?,
#             value_chf = ?,
#             updated_at = CURRENT_TIMESTAMP
#         WHERE id = ?
#         """,
#         (
#             action,
#             str(row["date"]),
#             ticker,
#             number_of_shares,
#             price,
#             currency,
#             rate,
#             value,
#             value_chf,
#             int(row["id"]),
#         ),
#     )
#     conn.commit()


# def delete_transactions(ids: list[int]) -> None:
#     if not ids:
#         return
#     conn = get_connection()
#     placeholders = ",".join("?" for _ in ids)
#     conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
#     conn.commit()


# # -----------------------------
# # Market data / FX layer
# # -----------------------------

# @st.cache_data(ttl=900, show_spinner=False)
# def get_latest_price_and_currency(ticker: str) -> tuple[Optional[float], Optional[str]]:
#     """Return latest traded price and Yahoo Finance currency for a ticker."""
#     ticker = ticker.upper().strip()
#     try:
#         yf_ticker = yf.Ticker(ticker)

#         price = None
#         currency = None

#         # fast_info is quick when available, but can be incomplete for some instruments.
#         try:
#             fast = yf_ticker.fast_info
#             price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
#             currency = fast.get("currency") if hasattr(fast, "get") else getattr(fast, "currency", None)
#         except Exception:
#             pass

#         if price is None:
#             hist = yf_ticker.history(period="5d", interval="1d", auto_adjust=False)
#             if not hist.empty:
#                 price = float(hist["Close"].dropna().iloc[-1])

#         if currency is None:
#             try:
#                 info = yf_ticker.get_info()
#                 currency = info.get("currency")
#             except Exception:
#                 currency = None

#         return (float(price) if price is not None else None, currency.upper() if currency else None)
#     except Exception:
#         return None, None


# def _rate_from_history_on_or_before(hist: pd.DataFrame, target_date: Optional[str] = None) -> Optional[float]:
#     """Extract the latest Close rate on or before target_date from a Yahoo history dataframe."""
#     if hist.empty or "Close" not in hist.columns:
#         return None

#     close = hist["Close"].dropna().copy()
#     if close.empty:
#         return None

#     if target_date:
#         target = pd.to_datetime(target_date).date()

#         # Compare only calendar dates. Yahoo often returns timezone-aware indexes
#         # for FX data, while SQLite/date_input values are timezone-naive.
#         close_dates = pd.Series(pd.to_datetime(close.index).date, index=close.index)
#         close = close.loc[close_dates <= target]

#         if close.empty:
#             return None

#     return float(close.iloc[-1])


# @st.cache_data(ttl=900, show_spinner=False)
# def get_latest_fx_rate_to_chf(currency: str) -> Optional[float]:
#     """Return latest FX rate so that amount_in_currency * rate = amount_in_chf."""
#     currency = currency.upper().strip()
#     if currency == BASE_CURRENCY:
#         return 1.0

#     pair = f"{currency}{BASE_CURRENCY}=X"
#     inverse_pair = f"{BASE_CURRENCY}{currency}=X"

#     try:
#         rate = _rate_from_history_on_or_before(
#             yf.Ticker(pair).history(period="5d", interval="1d")
#         )
#         if rate is not None:
#             return rate
#     except Exception:
#         pass

#     try:
#         inverse_rate = _rate_from_history_on_or_before(
#             yf.Ticker(inverse_pair).history(period="5d", interval="1d")
#         )
#         if inverse_rate is not None and inverse_rate != 0:
#             return 1.0 / inverse_rate
#     except Exception:
#         pass

#     return None


# @st.cache_data(ttl=900, show_spinner=False)
# def get_historical_fx_rate_to_chf(currency: str, transaction_date: str) -> Optional[float]:
#     """
#     Return historical FX rate for a transaction date.

#     This function is used when SAVING transactions. It deliberately does not
#     fall back to today's FX rate. If no historical quote is found, it returns
#     None so the transaction is not saved with an incorrect rate.

#     If the exact transaction date has no FX quote, for example a weekend or
#     holiday, it uses the latest available quote on or before that date.
#     """
#     currency = currency.upper().strip()
#     if currency == BASE_CURRENCY:
#         return 1.0

#     tx_date = pd.to_datetime(transaction_date).date()
#     start = pd.Timestamp(tx_date) - pd.Timedelta(days=14)
#     end = pd.Timestamp(tx_date) + pd.Timedelta(days=1)

#     pair = f"{currency}{BASE_CURRENCY}=X"
#     inverse_pair = f"{BASE_CURRENCY}{currency}=X"

#     try:
#         hist = yf.Ticker(pair).history(
#             start=start.strftime("%Y-%m-%d"),
#             end=end.strftime("%Y-%m-%d"),
#             interval="1d",
#         )
#         rate = _rate_from_history_on_or_before(hist, target_date=str(tx_date))
#         if rate is not None:
#             return rate
#     except Exception:
#         pass

#     # Fallback: try inverse pair and invert it.
#     try:
#         hist = yf.Ticker(inverse_pair).history(
#             start=start.strftime("%Y-%m-%d"),
#             end=end.strftime("%Y-%m-%d"),
#             interval="1d",
#         )
#         inverse_rate = _rate_from_history_on_or_before(hist, target_date=str(tx_date))
#         if inverse_rate is not None and inverse_rate != 0:
#             return 1.0 / inverse_rate
#     except Exception:
#         pass

#     return None


# # -----------------------------
# # Portfolio calculations
# # -----------------------------

# def build_portfolio(transactions: pd.DataFrame) -> pd.DataFrame:
#     if transactions.empty:
#         return pd.DataFrame()

#     rows = []
#     for ticker, group in transactions.groupby("ticker"):
#         buys = group[group["action"] == "BUY"]
#         sells = group[group["action"] == "SELL"]
#         dividends = group[group["action"] == "DIVIDEND"]

#         shares_held = group[group["action"].isin(["BUY", "SELL"])] ["number_of_shares"].sum()
#         buy_cost_chf = buys["value_chf"].sum()
#         sale_proceeds_chf = -sells["value_chf"].sum()  # SELL values are negative by design.
#         dividends_chf = dividends["value_chf"].sum()

#         latest_price, market_currency = get_latest_price_and_currency(ticker)

#         # If Yahoo cannot provide a currency, use the most recent transaction currency as fallback.
#         fallback_currency = str(group.sort_values("date").iloc[-1]["currency"]).upper()
#         market_currency = market_currency or fallback_currency

#         latest_rate = get_latest_fx_rate_to_chf(market_currency) if market_currency else None

#         if latest_price is not None and latest_rate is not None:
#             market_value_chf = shares_held * latest_price * latest_rate
#         else:
#             market_value_chf = None

#         total_return_chf = None
#         total_return_pct = None
#         if market_value_chf is not None:
#             total_return_chf = market_value_chf + sale_proceeds_chf + dividends_chf - buy_cost_chf
#             total_return_pct = total_return_chf / buy_cost_chf if buy_cost_chf else None

#         # avg_buy_price_chf = buy_cost_chf / buys["number_of_shares"].sum() if not buys.empty and buys["number_of_shares"].sum() else None
#         avg_buy_price = (
#             buys["value"].sum() / buys["number_of_shares"].sum()
#             if not buys.empty and buys["number_of_shares"].sum()
#             else None
#         )
#         rows.append(
#             {
#                 "Ticker": ticker,
#                 "Shares Held": shares_held,
#                 "Avg Cost / Share": avg_buy_price,
#                 "Latest Price": latest_price,
#                 "Market Currency": market_currency,
#                 "Latest FX to CHF": latest_rate,
#                 "Market Value CHF": market_value_chf,
#                 "Buy Cost CHF": buy_cost_chf,
#                 "Sales CHF": sale_proceeds_chf,
#                 "Dividends CHF": dividends_chf,
#                 "Total Return CHF": total_return_chf,
#                 "Total Return %": total_return_pct,
#                 "Transactions": len(group),
#             }
#         )

#     portfolio = pd.DataFrame(rows)
#     if not portfolio.empty:
#         portfolio = portfolio.sort_values("Market Value CHF", ascending=False, na_position="last")
#     return portfolio


# def format_money(value: float | None) -> str:
#     if value is None or pd.isna(value):
#         return "—"
#     return f"{value:,.2f}"


# def format_pct(value: float | None) -> str:
#     if value is None or pd.isna(value):
#         return "—"
#     return f"{value:.2%}"


# def format_swiss_number(value: float | None, decimals: int = 2, prefix: str = "") -> str:
#     """Format numbers as 1'234'567.89 for display."""
#     if value is None or pd.isna(value):
#         return "—"
#     formatted = f"{float(value):,.{decimals}f}".replace(",", "'")
#     return f"{prefix}{formatted}"


# def format_swiss_percent(value: float | None) -> str:
#     """Format a decimal return value as 12.34%, using apostrophe thousands if needed."""
#     if value is None or pd.isna(value):
#         return "—"
#     formatted = f"{float(value) * 100:,.2f}".replace(",", "'")
#     return f"{formatted}%"


# # -----------------------------
# # Streamlit UI
# # -----------------------------

# st.set_page_config(page_title="Portfolio Tracker", layout="wide")
# init_db()

# st.title("Portfolio Tracker")
# st.caption("Transactions are stored locally in SQLite. Portfolio performance is calculated in CHF.")

# transactions = read_transactions()
# portfolio = build_portfolio(transactions)

# with st.sidebar:
#     st.header("Data")
#     st.write(f"Database: `{DB_PATH}`")
#     if st.button("Refresh market prices"):
#         get_latest_price_and_currency.clear()
#         get_latest_fx_rate_to_chf.clear()
#         get_historical_fx_rate_to_chf.clear()
#         st.rerun()

#     st.divider()
#     st.write("Performance method")
#     st.caption(
#         "Cash-flow return = current market value + sale proceeds + dividends − buy cost. "
#         "This is not FIFO/LIFO tax-lot accounting."
#     )


# tab_overview, tab_add, tab_transactions = st.tabs(
#     ["Portfolio", "Add transaction", "Transactions / edit"]
# )

# with tab_overview:
#     st.subheader("Portfolio overview")

#     if portfolio.empty:
#         st.info("No transactions yet. Add a BUY transaction to start.")
#     else:
#         total_market_value = portfolio["Market Value CHF"].sum(skipna=True)
#         total_buy_cost = portfolio["Buy Cost CHF"].sum(skipna=True)
#         total_sale_proceeds = portfolio["Sales CHF"].sum(skipna=True)
#         total_dividends = portfolio["Dividends CHF"].sum(skipna=True)
#         total_return = portfolio["Total Return CHF"].sum(skipna=True)
#         total_return_pct = total_return / total_buy_cost if total_buy_cost else None

#         c1, c2, c3, c4, c5 = st.columns(5)
#         c1.metric("Market Value CHF", format_money(total_market_value))
#         c2.metric("Buy Cost CHF", format_money(total_buy_cost))
#         c3.metric("Sales CHF", format_money(total_sale_proceeds))
#         c4.metric("Dividends CHF", format_money(total_dividends))
#         c5.metric("Total Return", f"CHF {format_money(total_return)}", format_pct(total_return_pct))

#         portfolio_display = portfolio.copy()

#         # Streamlit's NumberColumn supports comma grouping, for example 1,234.56,
#         # but not Swiss-style apostrophe grouping, for example 1'234.56.
#         # Instead of converting the dataframe values to text, use a pandas Styler.
#         # This keeps the original dataframe numeric and allows right alignment.
#         swiss_formatters = {
#             "Shares Held": lambda x: format_swiss_number(x, decimals=2),
#             "Avg Cost / Share": lambda x: format_swiss_number(x, decimals=2),
#             "Latest Price": lambda x: format_swiss_number(x, decimals=2),
#             "Latest FX to CHF": lambda x: format_swiss_number(x, decimals=3),
#             "Market Value CHF": lambda x: format_swiss_number(x, decimals=2),
#             "Value CHF": lambda x: format_swiss_number(x, decimals=2),
#             "Buy Cost CHF": lambda x: format_swiss_number(x, decimals=2),
#             "Sales CHF": lambda x: format_swiss_number(x, decimals=2),
#             "Dividends CHF": lambda x: format_swiss_number(x, decimals=2),
#             "Total Return CHF": lambda x: format_swiss_number(x, decimals=2, prefix=""),
#             "Total Return %": format_swiss_percent,
#         }

#         # Only include columns that actually exist in the current dataframe.
#         swiss_formatters = {
#             col: formatter
#             for col, formatter in swiss_formatters.items()
#             if col in portfolio_display.columns
#         }

#         portfolio_styler = (
#             portfolio_display.style
#             .format(swiss_formatters)
#             .set_properties(
#                 subset=list(swiss_formatters.keys()),
#                 **{"text-align": "right"},
#             )
#         )

#         st.dataframe(
#             portfolio_styler,
#             use_container_width=True,
#             hide_index=True,
#         )

# with tab_add:
#     st.subheader("Add transaction")

#     # Transaction entry is intentionally simplified:
#     # the user enters ticker, shares, and price; the app looks up currency and CHF FX rate.
#     with st.form("add_transaction_form", clear_on_submit=True):
#         c1, c2, c3 = st.columns(3)
#         action = c1.selectbox("Action", ACTIONS)
#         date = c2.date_input("Date")
#         ticker = c3.text_input("Ticker", placeholder="AAPL, NESN.SW, MSFT, ...")

#         c4, c5 = st.columns(2)
#         number_of_shares = c4.number_input("Number of Shares", min_value=1.0, step=10.0, format="%.1f")
#         price = c5.number_input("Price / Dividend per Share", min_value=0.01, step=0.01, format="%.4f")

#         ticker_clean = ticker.upper().strip()
#         detected_currency = None
#         detected_rate = None

#         if ticker_clean:
#             _, detected_currency = get_latest_price_and_currency(ticker_clean)
#             if detected_currency:
#                 detected_rate = get_historical_fx_rate_to_chf(detected_currency, transaction_date=str(date))

#         if ticker_clean and detected_currency and detected_rate:
#             value_preview = number_of_shares * price
#             if action == "SELL":
#                 value_preview = -abs(value_preview)
#             value_chf_preview = value_preview * detected_rate

#             st.info(
#                 f"Detected currency: {detected_currency} | "
#                 f"CHF rate on {date}: {detected_rate:.6f} | "
#                 f"Value: {value_preview:,.2f} {detected_currency} | "
#                 f"Value_CHF: {value_chf_preview:,.2f} CHF"
#             )
#         elif ticker_clean:
#             st.warning(
#                 "Could not automatically detect the ticker currency or historical CHF exchange rate. "
#                 "Check that the ticker symbol is valid for Yahoo Finance, for example AAPL, NESN.SW, MSFT."
#             )
#         else:
#             st.caption("Enter a ticker to automatically detect currency and CHF exchange rate.")

#         submitted = st.form_submit_button("Save transaction")

#     if submitted:
#         if not ticker_clean:
#             st.error("Ticker is required.")
#         elif number_of_shares <= 0:
#             st.error("Number of shares must be greater than zero.")
#         elif price <= 0:
#             st.error("Price must be greater than zero.")
#         elif not detected_currency:
#             st.error("Could not detect the ticker currency. Please check the ticker symbol.")
#         elif not detected_rate or detected_rate <= 0:
#             st.error("Could not fetch a valid historical CHF exchange rate for this ticker currency and date.")
#         else:
#             insert_transaction(
#                 action=action,
#                 date=str(date),
#                 ticker=ticker_clean,
#                 number_of_shares=number_of_shares,
#                 price=price,
#                 currency=detected_currency,
#                 rate=detected_rate,
#             )
#             st.success("Transaction saved.")
#             st.rerun()

# with tab_transactions:
#     st.subheader("Transactions / edit")

#     if transactions.empty:
#         st.info("No transactions to edit yet.")
#     else:
#         tickers = ["All"] + sorted(transactions["ticker"].dropna().unique().tolist())
#         selected_ticker = st.selectbox("Select ticker", tickers)
#         tx = read_transactions(selected_ticker)

#         st.caption(
#             "Edit the fields below and click Save changes. "
#             "Value and Value_CHF are recalculated automatically from shares, price, and rate."
#         )

#         editable = tx.copy()

#         # SQLite stores dates as text. Streamlit's DateColumn requires an actual
#         # date/datetime-like value, so convert before passing the dataframe to
#         # st.data_editor. update_transaction() converts it back to text for SQLite.
#         editable["date"] = pd.to_datetime(editable["date"], errors="coerce").dt.date

#         editable["delete"] = False

#         edited = st.data_editor(
#             editable,
#             use_container_width=True,
#             hide_index=True,
#             num_rows="fixed",
#             disabled=["id", "value", "value_chf", "created_at", "updated_at"],
#             column_config={
#                 "delete": st.column_config.CheckboxColumn("Delete"),
#                 "action": st.column_config.SelectboxColumn("Action", options=ACTIONS, required=True),
#                 "date": st.column_config.DateColumn("Date", required=True),
#                 "ticker": st.column_config.TextColumn("Ticker", required=True),
#                 "number_of_shares": st.column_config.NumberColumn("Number_of_Shares", format="%.6f", required=True),
#                 "price": st.column_config.NumberColumn("Price", format="%.6f", required=True),
#                 "currency": st.column_config.TextColumn("Currency", max_chars=3, required=True),
#                 "rate": st.column_config.NumberColumn("Rate", format="%.6f", required=True),
#                 "value": st.column_config.NumberColumn("Value", format="%.2f"),
#                 "value_chf": st.column_config.NumberColumn("Value_CHF", format="%.2f"),
#             },
#         )

#         c1, c2 = st.columns([1, 4])
#         save = c1.button("Save changes", type="primary")
#         export = c2.download_button(
#             "Download all transactions as CSV",
#             data=transactions.to_csv(index=False).encode("utf-8"),
#             file_name="portfolio_transactions.csv",
#             mime="text/csv",
#         )

#         if save:
#             delete_ids = edited.loc[edited["delete"] == True, "id"].astype(int).tolist()
#             delete_transactions(delete_ids)

#             rows_to_update = edited.loc[edited["delete"] != True].drop(columns=["delete"])
#             for _, row in rows_to_update.iterrows():
#                 update_transaction(row)

#             st.success("Changes saved.")
#             st.rerun()
