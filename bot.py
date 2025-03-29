#!/usr/bin/env python
import time
import os
import pandas as pd
import numpy as np
import psycopg2
from binance.client import Client
from binance import ThreadedWebsocketManager
from flask import Flask, request
from threading import Thread
import logging
import requests
import json
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from sklearn.ensemble import GradientBoostingRegressor # For price prediction (optional)

# ---------------------- إعدادات التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('crypto_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------- تحميل المتغيرات البيئية ----------------------
api_key = config('BINANCE_API_KEY')
api_secret = config('BINANCE_API_SECRET')
telegram_token = config('TELEGRAM_BOT_TOKEN')
chat_id = config('TELEGRAM_CHAT_ID')
db_url = config('DATABASE_URL')

logger.info(f" BINANCE_API_KEY: {'Set' if api_key else 'Not Set'}")
logger.info(f" TELEGRAM_BOT_TOKEN: {telegram_token[:10]}...{'*' * (len(telegram_token)-10)}")
logger.info(f" TELEGRAM_CHAT_ID: {chat_id}")
logger.info(f" DATABASE_URL: {'Set' if db_url else 'Not Set'}")


# ---------------------- ثوابت وإعدادات الاستراتيجية ----------------------
TRADE_VALUE = 10  # قيمة الصفقة الثابتة بالدولار
MAX_OPEN_TRADES = 4 # الحد الأقصى للصفقات المفتوحة في نفس الوقت
SIGNAL_GENERATION_TIMEFRAME = '1h' # الفريم الزمني لتوليد توصيات جديدة
SIGNAL_GENERATION_LOOKBACK_DAYS = 4 # عدد أيام البيانات التاريخية لتوليد التوصيات
SIGNAL_TRACKING_TIMEFRAME = '15m' # الفريم الزمني لتتبع التوصيات المفتوحة
SIGNAL_TRACKING_LOOKBACK_DAYS = 2 # عدد أيام البيانات التاريخية لتتبع التوصيات

# إعدادات وقف الخسارة المتحرك (ATR Trailing Stop)
TRAILING_STOP_ACTIVATION_PROFIT_PCT = 0.015 # نسبة الربح المطلوبة لتفعيل الوقف المتحرك (e.g., 1.5%)
TRAILING_STOP_ATR_MULTIPLIER = 2.0 # معامل ATR لتحديد مسافة الوقف المتحرك

# إعدادات إشارة الدخول
ENTRY_ATR_MULTIPLIER = 1.5 # معامل ATR لتحديد الهدف ووقف الخسارة الأولي
MIN_PROFIT_MARGIN_PCT = 1.0 # الحد الأدنى لهامش الربح المطلوب في الإشارة الأولية (%)
MIN_VOLUME_15M_USDT = 500000 # الحد الأدنى لحجم التداول في آخر 15 دقيقة

# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
conn = None
cur = None

def init_db():
    global conn, cur
    retries = 5
    delay = 5
    for i in range(retries):
        try:
            logger.info(f"[DB] Attempting to connect (Attempt {i+1}/{retries})...")
            conn = psycopg2.connect(db_url, connect_timeout=10)
            conn.autocommit = False # Important for safe transactions
            cur = conn.cursor()
            # Create table if not exists, with updated columns and constraints
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    initial_target DOUBLE PRECISION NOT NULL, -- Initial target
                    initial_stop_loss DOUBLE PRECISION NOT NULL, -- Initial stop loss
                    current_target DOUBLE PRECISION NOT NULL, -- Current target (can be updated)
                    current_stop_loss DOUBLE PRECISION NOT NULL, -- Current stop loss (can be updated)
                    r2_score DOUBLE PRECISION, -- Kept name, could represent confidence or other metric
                    volume_15m DOUBLE PRECISION,
                    achieved_target BOOLEAN DEFAULT FALSE,
                    hit_stop_loss BOOLEAN DEFAULT FALSE,
                    closing_price DOUBLE PRECISION, -- Price at which the trade was closed
                    closed_at TIMESTAMP,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    profit_percentage DOUBLE PRECISION,
                    profitable_stop_loss BOOLEAN DEFAULT FALSE, -- Was SL hit above entry?
                    is_trailing_active BOOLEAN DEFAULT FALSE -- Is trailing stop active?
                )
            """)
            conn.commit() # Commit table creation immediately

            # --- Add new columns if they don't exist (Idempotent) ---
            new_columns = {
                "initial_target": "DOUBLE PRECISION",
                "initial_stop_loss": "DOUBLE PRECISION",
                "current_target": "DOUBLE PRECISION",
                "current_stop_loss": "DOUBLE PRECISION",
                "is_trailing_active": "BOOLEAN DEFAULT FALSE",
                "closing_price": "DOUBLE PRECISION"
            }
            table_changed = False
            for col_name, col_type in new_columns.items():
                 try:
                     cur.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
                     conn.commit() # Commit each alteration
                     logger.info(f"✅ [DB] Added column '{col_name}' to 'signals' table.")
                     table_changed = True
                 except psycopg2.Error as e:
                     if e.pgcode == '42701': # duplicate_column code
                         # logger.debug(f"ℹ️ [DB] Column '{col_name}' already exists in 'signals'.")
                         conn.rollback() # Rollback the failed ALTER attempt
                     else:
                         logger.error(f"❌ [DB] Failed to add column '{col_name}': {e} (pgcode: {e.pgcode})")
                         conn.rollback()
                         raise # Re-raise other errors

            # --- Add NOT NULL constraints if missing ---
            not_null_columns = [
                "symbol", "entry_price", "initial_target", "initial_stop_loss",
                "current_target", "current_stop_loss"
            ]
            for col_name in not_null_columns:
                try:
                    # Check if constraint already exists (somewhat complex, safer to just try adding)
                    cur.execute(f"ALTER TABLE signals ALTER COLUMN {col_name} SET NOT NULL")
                    conn.commit()
                    logger.info(f"✅ [DB] Ensured column '{col_name}' has NOT NULL constraint.")
                    table_changed = True
                except psycopg2.Error as e:
                     # Errors might occur if already NOT NULL, which is fine. Log other errors.
                     if "is an identity column" in str(e) or "already set" in str(e): # Example error messages
                          conn.rollback()
                     elif e.pgcode == '42704': # object_not_found (should not happen here)
                         conn.rollback()
                     else:
                         # Log potentially significant errors, but allow continuation
                         logger.warning(f"⚠️ [DB] Could not set NOT NULL on '{col_name}' (might be okay): {e}")
                         conn.rollback()


            if table_changed:
                 logger.info("✅ [DB] Database schema updated/verified successfully.")
            else:
                 logger.info("✅ [DB] Database schema is up-to-date.")

            logger.info("✅ [DB] Database connection established successfully.")
            return # Success

        except (psycopg2.OperationalError, psycopg2.DatabaseError) as e:
            logger.error(f"❌ [DB] Connection attempt {i+1} failed: {e}")
            if i < retries - 1:
                logger.info(f"[DB] Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.critical("❌ [DB] All connection attempts failed. Exiting.")
                raise # Raise the last error after all retries failed
        except Exception as e:
            logger.critical(f"❌ [DB] An unexpected error occurred during DB init: {e}")
            raise


def check_db_connection():
    global conn, cur
    try:
        if conn is None or conn.closed != 0:
             logger.warning("⚠️ [DB] Connection is closed or None. Attempting to re-initialize...")
             init_db() # Try to re-establish the connection fully
             return

        # Check if the connection is usable
        cur.execute("SELECT 1")
        # logger.debug("[DB] Connection check successful.")

    except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
        logger.warning(f"⚠️ [DB] Connection lost ({e}). Attempting to re-initialize...")
        try:
            # Close the old potentially broken connection object before re-initializing
            if cur: cur.close()
            if conn: conn.close()
            conn, cur = None, None # Reset globals
            init_db() # Re-initialize fully
        except Exception as ex:
            logger.error(f"❌ [DB] Failed to re-initialize connection after loss: {ex}")
            raise # Re-raise critical failure
    except Exception as e:
        logger.error(f"❌ [DB] Unexpected error during connection check: {e}")
        # Attempt re-initialization as a fallback
        try:
            if cur: cur.close()
            if conn: conn.close()
            conn, cur = None, None
            init_db()
        except Exception as ex:
            logger.error(f"❌ [DB] Failed to re-initialize connection after unexpected error: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
try:
    client = Client(api_key, api_secret)
    client.ping() # Test connection
    logger.info("✅ [Binance] Binance client initialized and connection verified.")
except Exception as e:
    logger.critical(f"❌ [Binance] Failed to initialize Binance client: {e}. Check API keys and connection.")
    raise # Cannot continue without Binance client

# ---------------------- استخدام WebSocket لتحديث بيانات التيكر ----------------------
ticker_data = {} # Dictionary to store latest ticker data for each symbol

def handle_ticker_message(msg):
    """Handles incoming WebSocket messages and updates the ticker_data dictionary."""
    try:
        # Mini-ticker stream sends a list of dictionaries
        if isinstance(msg, list):
            for m in msg:
                symbol = m.get('s')
                if symbol and 'USDT' in symbol: # Process only USDT pairs for now
                    ticker_data[symbol] = {
                        'c': m.get('c'), # Current price
                        'h': m.get('h'), # High price 24h
                        'l': m.get('l'), # Low price 24h
                        'v': m.get('v')  # Total traded base asset volume 24h
                    }
        elif isinstance(msg, dict) and 'stream' not in msg and 'e' in msg and msg['e'] == 'error':
            logger.error(f"❌ [WS] Received error message from WebSocket: {msg.get('m')}")
        # Ignore other message types for now
    except Exception as e:
        logger.error(f"❌ [WS] Error in handle_ticker_message: {e}")
        logger.debug(f"Problematic WS message: {msg}")


def run_ticker_socket_manager():
    """Runs the Binance WebSocket Manager for live price updates."""
    while True: # Keep trying to connect
        try:
            logger.info("ℹ️ [WS] Starting WebSocket Manager...")
            twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
            twm.start()
            # Use start_miniticker_socket for updates on all pairs
            twm.start_miniticker_socket(callback=handle_ticker_message)
            logger.info("✅ [WS] WebSocket connected for mini-ticker updates.")
            twm.join() # Wait until the manager stops (e.g., due to error or manual stop)
            logger.warning("⚠️ [WS] WebSocket Manager stopped. Will attempt to restart...")
        except Exception as e:
            logger.error(f"❌ [WS] Error running WebSocket Manager: {e}. Restarting after delay...")
        time.sleep(15) # Wait before attempting to restart


# ---------------------- دوال حساب المؤشرات الفنية ----------------------
def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi_indicator(df, period=14):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    avg_loss = avg_loss.replace(0, np.nan) # Avoid division by zero
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_atr_indicator(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1, skipna=False)
    atr = tr.rolling(window=period, min_periods=period).mean()
    df['atr'] = atr
    return df

def calculate_macd(df, fast_period=10, slow_period=21, signal_period=8):
    df['ema_fast'] = calculate_ema(df['close'], fast_period)
    df['ema_slow'] = calculate_ema(df['close'], slow_period)
    df['macd'] = df['ema_fast'] - df['ema_slow']
    df['macd_signal'] = calculate_ema(df['macd'], signal_period)
    df['macd_hist'] = df['macd'] - df['macd_signal']
    return df

def calculate_kdj(df, period=14, k_period=3, d_period=3):
    low_min = df['low'].rolling(window=period).min()
    high_max = df['high'].rolling(window=period).max()
    rsv_denominator = (high_max - low_min).replace(0, np.nan)
    rsv = (df['close'] - low_min) / rsv_denominator * 100
    df['kdj_k'] = rsv.ewm(com=(k_period - 1), adjust=False).mean()
    df['kdj_d'] = df['kdj_k'].ewm(com=(d_period - 1), adjust=False).mean()
    df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']
    return df

def calculate_adx(df, period=14):
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    df['+dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['-dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
    tr = pd.concat([
        (df['high'] - df['low']),
        (df['high'] - df['close'].shift(1)).abs(),
        (df['low'] - df['close'].shift(1)).abs()
    ], axis=1).max(axis=1, skipna=False)
    atr = tr.ewm(alpha=1/period, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * (df['+dm'].ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (df['-dm'].ewm(alpha=1/period, adjust=False).mean() / atr)
    dx_denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (abs(plus_di - minus_di) / dx_denominator)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    df['+di'] = plus_di
    df['-di'] = minus_di
    df['adx'] = adx
    return df

# Candlestick pattern detection functions
def is_hammer(row):
    open_price, high, low, close = row['open'], row['high'], row['low'], row['close']
    if None in [open_price, high, low, close]: return 0
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0: return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.3 * body:
        return 100
    return 0

def is_shooting_star(row):
    open_price, high, low, close = row['open'], row['high'], row['low'], row['close']
    if None in [open_price, high, low, close]: return 0
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0: return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and upper_shadow >= 2 * body and lower_shadow <= 0.3 * body:
        return -100
    return 0

def compute_engulfing(df, idx):
    if idx == 0: return 0
    prev = df.iloc[idx - 1]
    curr = df.iloc[idx]
    if None in [prev['close'], prev['open'], curr['close'], curr['open']]: return 0
    if prev['close'] < prev['open'] and curr['close'] > curr['open']:
        if curr['open'] < prev['close'] and curr['close'] > prev['open']:
            return 100
    if prev['close'] > prev['open'] and curr['close'] < curr['open']:
        if curr['open'] > prev['close'] and curr['close'] < prev['open']:
            return -100
    return 0

def detect_candlestick_patterns(df):
    df['Hammer'] = df.apply(is_hammer, axis=1)
    df['ShootingStar'] = df.apply(is_shooting_star, axis=1)
    engulfing_values = [0] * len(df)
    if len(df) > 1:
        engulfing_values = [compute_engulfing(df, i) for i in range(len(df))]
    df['Engulfing'] = engulfing_values
    df['BullishSignal'] = df.apply(lambda row: 100 if (row['Hammer'] == 100 or row['Engulfing'] == 100) else 0, axis=1)
    df['BearishSignal'] = df.apply(lambda row: 100 if (row['ShootingStar'] == -100 or row['Engulfing'] == -100) else 0, axis=1)
    return df

# ---------------------- دوال التنبؤ وتحليل المشاعر ----------------------
def ml_predict_signal(symbol, df):
    """Placeholder ML prediction function."""
    try:
        if df.empty or 'rsi' not in df.columns or 'adx' not in df.columns: return 0.5
        rsi = df['rsi'].iloc[-1]
        adx = df['adx'].iloc[-1]
        if pd.isna(rsi) or pd.isna(adx): return 0.5
        if rsi < 40 and adx > 25: return 0.80
        elif rsi > 65 and adx > 25: return 0.20
        else: return 0.5
    except IndexError: return 0.5
    except Exception as e:
        logger.error(f"❌ [ML] Error in ml_predict_signal for {symbol}: {e}")
        return 0.5

def get_market_sentiment(symbol):
    """Placeholder market sentiment function."""
    return 0.6

def get_fear_greed_index():
    """Fetches Fear & Greed Index from alternative.me"""
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("data"):
            fng_value = float(data["data"][0].get("value", 50))
            fng_classification = data["data"][0].get("value_classification", "Neutral")
            logger.info(f"✅ [FNG] Fear & Greed Index: {fng_value:.0f} - {fng_classification}")
            return fng_value, fng_classification
        else:
            logger.warning("⚠️ [FNG] No data found in F&G Index response.")
            return 50.0, "Neutral"
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [FNG] Error requesting F&G Index: {e}")
        return 50.0, "Error"
    except Exception as e:
        logger.error(f"❌ [FNG] Unexpected error getting F&G Index: {e}")
        return 50.0, "Error"

# ---------------------- استراتيجية Freqtrade المحسّنة (كفئة) ----------------------
class FreqtradeStrategy:
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 50:
            logger.warning(f"⚠️ [Strategy] DataFrame too short ({len(df)} candles) to calculate all indicators.")
            # Return original df only if essential columns exist, otherwise empty
            if all(col in df.columns for col in ['open', 'high', 'low', 'close']):
                 return df
            else:
                 return pd.DataFrame() # Cannot proceed

        try:
            df['ema5'] = calculate_ema(df['close'], 5)
            df['ema8'] = calculate_ema(df['close'], 8)
            df['ema21'] = calculate_ema(df['close'], 21)
            df['ema34'] = calculate_ema(df['close'], 34)
            df['ema50'] = calculate_ema(df['close'], 50)
            df['rsi'] = calculate_rsi_indicator(df)
            df['sma20'] = df['close'].rolling(window=20).mean()
            df['std20'] = df['close'].rolling(window=20).std()
            df['upper_band'] = df['sma20'] + (2.5 * df['std20'])
            df['lower_band'] = df['sma20'] - (2.5 * df['std20'])
            df = calculate_atr_indicator(df)
            df = calculate_macd(df)
            df = calculate_kdj(df)
            df = calculate_adx(df)
            df = detect_candlestick_patterns(df)
            # Drop rows with NaN values generated by indicators
            initial_len = len(df)
            df = df.dropna()
            dropped_count = initial_len - len(df)
            if dropped_count > 0:
                logger.debug(f"ℹ️ [Strategy] Dropped {dropped_count} rows with NaNs after indicator calculation.")

            if df.empty:
                logger.warning("⚠️ [Strategy] DataFrame became empty after calculating indicators and dropping NaNs.")
                return pd.DataFrame()

            logger.info(f"✅ [Strategy] Indicators calculated for DataFrame (final size: {len(df)})")
            return df

        except Exception as e:
            logger.error(f"❌ [Strategy] Error during indicator population: {e}", exc_info=True)
            return pd.DataFrame() # Return empty DataFrame on error


    def composite_buy_score(self, row):
        score = 0
        required_cols = ['ema5', 'ema8', 'ema21', 'ema34', 'ema50', 'rsi', 'close', 'lower_band', 'macd', 'macd_signal', 'macd_hist', 'kdj_j', 'kdj_k', 'kdj_d', 'adx', 'BullishSignal']
        # Check if all required columns exist and are not None
        if any(col not in row or pd.isna(row[col]) for col in required_cols):
            # logger.warning(f"⚠️ [Strategy] Missing or NaN data in row for composite_buy_score. Row keys: {row.keys()}")
            return 0 # Cannot calculate score reliably

        try:
            if row['ema5'] > row['ema8'] > row['ema21'] > row['ema34'] > row['ema50']: score += 1.5
            if row['rsi'] < 40: score += 1
            if row['close'] > row['lower_band'] and ((row['close'] - row['lower_band']) / row['close'] < 0.02): score += 1
            if row['macd'] > row['macd_signal'] and row['macd_hist'] > 0: score += 1
            if row['kdj_j'] > row['kdj_k'] and row['kdj_k'] > row['kdj_d'] and row['kdj_j'] < 80: score += 1
            if row['adx'] > 20: score += 0.5
            if row['BullishSignal'] == 100: score += 1.5
        except TypeError as e:
             logger.error(f"❌ [Strategy] TypeError in composite_buy_score calculation: {e}. Row data: {row.to_dict()}")
             return 0 # Invalid data type in row
        except Exception as e:
             logger.error(f"❌ [Strategy] Unexpected error in composite_buy_score: {e}", exc_info=True)
             return 0
        return score

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        required_score = 4.0
        required_cols = ['ema5', 'rsi', 'lower_band', 'macd', 'kdj_j', 'adx', 'BullishSignal'] # Ensure these are calculated

        if df.empty or not all(col in df.columns for col in required_cols):
             logger.warning("⚠️ [Strategy] DataFrame missing required columns for buy trend calculation.")
             df['buy_score'] = 0
             df['buy'] = 0
             return df

        # Apply score calculation safely
        df['buy_score'] = df.apply(lambda row: self.composite_buy_score(row) if not row.isnull().any() else 0, axis=1)
        df['buy'] = np.where(df['buy_score'] >= required_score, 1, 0)

        buy_signals_count = df['buy'].sum()
        if buy_signals_count > 0:
             logger.info(f"✅ [Strategy] Identified {buy_signals_count} potential buy signal(s) (Score >= {required_score}).")

        return df

# ---------------------- دالة التنبؤ بالسعر المحسّنة (اختياري ومبسط) ----------------------
def improved_predict_future_price(symbol, interval='2h', days=30):
    """Simplified prediction using Gradient Boosting (optional)."""
    try:
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50: return None

        df['ema_fast'] = calculate_ema(df['close'], 10)
        df['ema_slow'] = calculate_ema(df['close'], 30)
        df['rsi'] = calculate_rsi_indicator(df)
        df = df.dropna()
        if len(df) < 2: return None

        features = ['ema_fast', 'ema_slow', 'rsi']
        if not all(f in df.columns for f in features): return None

        X = df[features].iloc[:-1].values
        y = df['close'].iloc[1:].values
        if len(X) == 0: return None

        model = GradientBoostingRegressor(n_estimators=100, max_depth=3, random_state=42, learning_rate=0.1)
        model.fit(X, y)
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]

        if predicted_price <= 0: return None
        logger.info(f"✅ [Price Prediction] Predicted price for {symbol} ({interval}, {days}d): {predicted_price:.8f}")
        return predicted_price
    except ImportError:
         logger.error("❌ [Price Prediction] scikit-learn not installed. Prediction unavailable.")
         return None
    except Exception as e:
        logger.error(f"❌ [Price Prediction] Error predicting price for {symbol}: {e}")
        return None

# ---------------------- دالة توليد الإشارة باستخدام الاستراتيجية المحسنة ----------------------
def generate_signal_using_freqtrade_strategy(df_input, symbol):
    """Generates a buy signal based on the enhanced Freqtrade strategy and checks conditions."""
    if df_input is None or df_input.empty:
        logger.warning(f"⚠️ [Signal Gen] Empty DataFrame provided for {symbol}.")
        return None
    if len(df_input) < 50:
         logger.info(f"ℹ️ [Signal Gen] Insufficient data ({len(df_input)} candles) for {symbol} on {SIGNAL_GENERATION_TIMEFRAME}.")
         return None

    strategy = FreqtradeStrategy()
    df_processed = strategy.populate_indicators(df_input.copy())
    if df_processed.empty:
        logger.warning(f"⚠️ [Signal Gen] DataFrame empty after indicator calculation for {symbol}.")
        return None

    df_with_signals = strategy.populate_buy_trend(df_processed)
    if df_with_signals.empty or df_with_signals['buy'].iloc[-1] != 1:
        # logger.debug(f"ℹ️ [Signal Gen] No buy signal in the last candle for {symbol} on {SIGNAL_GENERATION_TIMEFRAME}.")
        return None

    last_signal_row = df_with_signals.iloc[-1]
    current_price = last_signal_row['close']
    current_atr = last_signal_row['atr']

    # Validate essential data from the signal row
    if pd.isna(current_price) or pd.isna(current_atr) or current_atr <= 0 or current_price <= 0:
        logger.warning(f"⚠️ [Signal Gen] Invalid price ({current_price}) or ATR ({current_atr}) in signal row for {symbol}.")
        return None

    initial_target = current_price + (ENTRY_ATR_MULTIPLIER * current_atr)
    initial_stop_loss = current_price - (ENTRY_ATR_MULTIPLIER * current_atr)

    if initial_stop_loss <= 0:
        min_sl_price = current_price * 0.95 # Fallback to 5% loss
        initial_stop_loss = max(min_sl_price, 1e-9) # Ensure it's positive
        logger.warning(f"⚠️ [Signal Gen] Calculated initial SL for {symbol} was non-positive. Adjusted to fallback: {initial_stop_loss:.8f}")


    profit_margin_pct = ((initial_target / current_price) - 1) * 100 if current_price > 0 else 0
    if profit_margin_pct < MIN_PROFIT_MARGIN_PCT:
        logger.info(f"ℹ️ [Signal Gen] Signal for {symbol} rejected. Profit margin ({profit_margin_pct:.2f}%) below minimum ({MIN_PROFIT_MARGIN_PCT:.1f}%).")
        return None

    # Optional: Price prediction check (currently disabled)
    # predicted_price = improved_predict_future_price(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
    # prediction_threshold = 1.005
    # if predicted_price is None or predicted_price <= current_price * prediction_threshold:
    #     logger.info(f"ℹ️ [Signal Gen] Price prediction ({predicted_price}) for {symbol} does not support buy signal (Current: {current_price}).")
    #     return None

    buy_score = last_signal_row.get('buy_score', 0)

    signal = {
        'symbol': symbol,
        'entry_price': float(f"{current_price:.8f}"),
        'initial_target': float(f"{initial_target:.8f}"),
        'initial_stop_loss': float(f"{initial_stop_loss:.8f}"),
        'current_target': float(f"{initial_target:.8f}"),
        'current_stop_loss': float(f"{initial_stop_loss:.8f}"),
        'strategy': 'freqtrade_improved_atr',
        'indicators': { # Store key indicators at signal time
            'rsi': round(last_signal_row['rsi'], 2) if 'rsi' in last_signal_row and pd.notna(last_signal_row['rsi']) else None,
            'macd_hist': round(last_signal_row['macd_hist'], 5) if 'macd_hist' in last_signal_row and pd.notna(last_signal_row['macd_hist']) else None,
            'adx': round(last_signal_row['adx'], 2) if 'adx' in last_signal_row and pd.notna(last_signal_row['adx']) else None,
            'atr': round(current_atr, 8),
            'buy_score': round(buy_score, 2)
        },
        'r2_score': round(buy_score, 2), # Use buy_score as the r2_score placeholder
        'trade_value': TRADE_VALUE,
    }

    logger.info(f"✅ [Signal Gen] Generated buy signal for {symbol} at {current_price:.8f} (Score: {buy_score:.2f}).")
    # logger.debug(f"Signal Details: {json.dumps(signal, indent=2)}")
    return signal

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    # You can add more info here, like uptime or status
    return f"🚀 Crypto Trading Bot v4.3 (Hazem Mod) - Signal Service Running. {datetime.utcnow().isoformat()}Z", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handles webhook updates from Telegram (especially Callback Queries)."""
    try:
        update = request.get_json()
        if not update:
            logger.warning("⚠️ [Webhook] Received empty update.")
            return '', 200

        logger.debug(f"🔔 [Webhook] Received update: {json.dumps(update, indent=2)}")

        if "callback_query" in update:
            callback_query = update["callback_query"]
            data = callback_query.get("data")
            query_id = callback_query.get("id")
            message = callback_query.get("message")
            chat_info = message.get("chat", {}) if message else {}
            chat_id_callback = chat_info.get("id")
            user_info = callback_query.get("from", {})
            user_id = user_info.get("id")
            username = user_info.get("username", "N/A")

            if not chat_id_callback or not query_id:
                 logger.error(f"❌ [Webhook] Missing chat_id ({chat_id_callback}) or query_id ({query_id}) in callback_query.")
                 return 'Bad Request', 400

            # Answer callback query immediately to prevent timeout in Telegram
            answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
            try:
                 requests.post(answer_url, json={"callback_query_id": query_id}, timeout=5)
            except Exception as ans_err:
                 logger.error(f"❌ [Webhook] Failed to answer callback query {query_id}: {ans_err}")
                 # Continue processing the request anyway

            logger.info(f"ℹ️ [Webhook] Received callback '{data}' from user @{username} (ID: {user_id}) in chat {chat_id_callback}.")

            # Handle different callback data
            if data == "get_report":
                # Run report generation in a separate thread to avoid blocking the webhook response
                Thread(target=send_report, args=(chat_id_callback,), daemon=True).start()
            # Add other callback handlers here
            # elif data.startswith("action_"):
            #     handle_other_action(data, chat_id_callback, user_id)
            else:
                 logger.warning(f"⚠️ [Webhook] Received unknown callback data: {data}")

            return '', 200 # Success response

        # Handle other types of updates if needed (e.g., commands)
        elif "message" in update and "text" in update["message"]:
             message = update["message"]
             chat_info = message.get("chat", {})
             chat_id_msg = chat_info.get("id")
             text = message.get("text", "").strip()
             user_info = message.get("from", {})
             user_id = user_info.get("id")
             username = user_info.get("username", "N/A")

             if text == '/report' or text == '/stats':
                  logger.info(f"ℹ️ [Webhook] Received command '{text}' from user @{username} (ID: {user_id}) in chat {chat_id_msg}.")
                  Thread(target=send_report, args=(chat_id_msg,), daemon=True).start()
                  return '', 200
             elif text == '/status':
                 logger.info(f"ℹ️ [Webhook] Received command '{text}' from user @{username} (ID: {user_id}) in chat {chat_id_msg}.")
                 # Send a simple status message
                 status_msg = f"✅ Bot is running.\n- WebSocket connected: {'Yes' if websocket_thread and websocket_thread.is_alive() else 'No'}\n- Tracker active: {'Yes' if tracker_thread and tracker_thread.is_alive() else 'No'}\n- Scheduler active: {'Yes' if scheduler and scheduler.running else 'No'}"
                 send_telegram_update(status_msg, chat_id_override=chat_id_msg) # Send to the requesting chat
                 return '', 200
             # Handle other commands or messages

    except json.JSONDecodeError:
        logger.error("❌ [Webhook] Received invalid JSON.")
        return 'Invalid JSON', 400
    except Exception as e:
        logger.error(f"❌ [Webhook] Error processing update: {e}", exc_info=True)
        return 'Internal Server Error', 500

    return '', 200 # Default success response

def set_telegram_webhook():
    """Registers the Flask app URL as a webhook with Telegram."""
    render_service_name = os.environ.get("RENDER_SERVICE_NAME") # e.g., from Render env vars
    if not render_service_name:
        # Attempt to guess based on common Render patterns or use a default/local setup
        # For Render: https://<service-name>.onrender.com
        # Fallback or local testing URL (requires ngrok or similar for local testing)
        webhook_base_url = config('WEBHOOK_BASE_URL', default=None) # Define in .env if needed
        if not webhook_base_url:
             logger.warning("⚠️ [Webhook] RENDER_SERVICE_NAME or WEBHOOK_BASE_URL not set. Cannot automatically set webhook.")
             # Check if webhook is already set
             try:
                 get_wh_url = f"https://api.telegram.org/bot{telegram_token}/getWebhookInfo"
                 resp = requests.get(get_wh_url, timeout=10)
                 wh_info = resp.json()
                 if wh_info.get("ok") and wh_info.get("result",{}).get("url"):
                     logger.info(f"ℹ️ [Webhook] Webhook seems to be already set to: {wh_info['result']['url']}")
                     return
                 else:
                     logger.warning("⚠️ [Webhook] Could not confirm existing webhook setting.")
             except Exception as e:
                 logger.error(f"❌ [Webhook] Error checking existing webhook: {e}")
             return # Exit if URL cannot be determined

        webhook_url = f"{webhook_base_url.rstrip('/')}/webhook"
    else:
         webhook_url = f"https://{render_service_name}.onrender.com/webhook"


    set_url = f"https://api.telegram.org/bot{telegram_token}/setWebhook"
    params = {'url': webhook_url}
    try:
        response = requests.get(set_url, params=params, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"✅ [Webhook] Successfully set Telegram webhook to: {webhook_url}")
            logger.info(f"ℹ️ [Webhook] Telegram response: {res_json.get('description')}")
        else:
            logger.error(f"❌ [Webhook] Failed to set webhook: {res_json}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Webhook] Exception during webhook setup request: {e}")
    except Exception as e:
        logger.error(f"❌ [Webhook] Unexpected error during webhook setup: {e}")

# ---------------------- وظائف تحليل البيانات المساعدة ----------------------
def get_crypto_symbols(filename='crypto_list.txt'):
    """Reads the list of crypto symbols from a text file."""
    symbols = []
    try:
        # Determine the absolute path to the file relative to the script's location
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)

        if not os.path.exists(file_path):
            logger.error(f"❌ [Data] Symbol list file '{filename}' not found at path: {file_path}")
            # Attempt fallback to current working directory (less reliable)
            alt_path = os.path.abspath(filename)
            if os.path.exists(alt_path):
                logger.warning(f"⚠️ [Data] Using symbol list file found in current directory: {alt_path}")
                file_path = alt_path
            else:
                return [] # Return empty list if not found anywhere

        with open(file_path, 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip() and not line.startswith('#')]
        logger.info(f"✅ [Data] Loaded {len(symbols)} symbols from '{os.path.basename(file_path)}'.")
        return symbols
    except Exception as e:
        logger.error(f"❌ [Data] Error reading symbol file '{filename}': {e}")
        return [] # Return empty list on error

def fetch_historical_data(symbol, interval='1h', days=10):
    """Fetches historical K-line data for a specific symbol from Binance."""
    try:
        # logger.debug(f"⏳ [Data] Fetching historical data: {symbol} - {interval} - {days}d")
        start_str = f"{days} day ago UTC"
        klines = client.get_historical_klines(symbol, interval, start_str)

        if not klines:
            logger.warning(f"⚠️ [Data] No historical data found for {symbol} ({interval}, {days}d).")
            return None

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])

        # Convert essential columns to numeric, coercing errors to NaN
        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # Convert timestamp to datetime objects (optional, but good practice)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # Drop rows with NaN in essential price columns after conversion
        initial_len = len(df)
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        if len(df) < initial_len:
             logger.debug(f"ℹ️ [Data] Dropped {initial_len - len(df)} rows with NaN prices for {symbol}.")

        if df.empty:
             logger.warning(f"⚠️ [Data] DataFrame for {symbol} became empty after processing NaNs.")
             return None

        # logger.info(f"✅ [Data] Fetched {len(df)} candles for {symbol} ({interval}, {days}d).")
        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

    except Exception as e: # Consider catching specific Binance exceptions
        logger.error(f"❌ [Data] Error fetching data for {symbol} ({interval}, {days}d): {e}")
        return None

def fetch_recent_volume(symbol):
    """Fetches the quote asset volume for the last 15 minutes."""
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=15)
        if not klines:
             logger.warning(f"⚠️ [Data] No 1m klines found for {symbol} to calculate recent volume.")
             return 0.0

        # klines index 7 is Quote asset volume
        volume = sum(float(k[7]) for k in klines if len(k) > 7 and k[7])
        # logger.info(f"✅ [Data] Quote volume for {symbol} (last 15m): {volume:,.2f} USDT.")
        return volume
    except Exception as e:
        logger.error(f"❌ [Data] Error fetching recent volume for {symbol}: {e}")
        return 0.0

def get_market_dominance():
    """Fetches BTC and ETH dominance from CoinGecko."""
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json().get("data", {})
        market_cap_percentage = data.get("market_cap_percentage", {})
        btc_dominance = market_cap_percentage.get("btc", 0.0)
        eth_dominance = market_cap_percentage.get("eth", 0.0)
        logger.info(f"✅ [Data] Market Dominance - BTC: {btc_dominance:.2f}%, ETH: {eth_dominance:.2f}%")
        return btc_dominance, eth_dominance
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Data] Error requesting dominance from CoinGecko: {e}")
        return None, None # Return None to indicate failure
    except Exception as e:
        logger.error(f"❌ [Data] Unexpected error in get_market_dominance: {e}")
        return None, None

# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    """Sends a new signal alert to Telegram."""
    try:
        entry_price = signal['entry_price']
        target_price = signal['initial_target']
        stop_loss_price = signal['initial_stop_loss']

        if entry_price <= 0:
             logger.error(f"❌ [Telegram] Invalid entry price ({entry_price}) for {signal['symbol']}. Cannot send alert.")
             return

        profit_pct = ((target_price / entry_price) - 1) * 100
        loss_pct = ((stop_loss_price / entry_price) - 1) * 100
        profit_usdt = TRADE_VALUE * (profit_pct / 100)
        loss_usdt = TRADE_VALUE * (loss_pct / 100)

        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
        fng_value, fng_label = get_fear_greed_index()

        # Escape markdown sensitive characters in symbol name
        safe_symbol = signal['symbol'].replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')

        message = (
            f"🚀 **إشارة تداول جديدة** 🚀\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **سعر الدخول المقترح:** `${entry_price:.8f}`\n"
            f"🎯 **الهدف الأولي:** `${target_price:.8f}` (+{profit_pct:.2f}% / +{profit_usdt:.2f} USDT)\n"
            f"🛑 **وقف الخسارة الأولي:** `${stop_loss_price:.8f}` ({loss_pct:.2f}% / {loss_usdt:.2f} USDT)\n"
            f"⏱ **الفريم الزمني للإشارة:** {timeframe}\n"
            f"💧 **السيولة (آخر 15د):** {volume:,.0f} USDT\n"
            f"💰 **قيمة الصفقة المقترحة:** ${TRADE_VALUE}\n"
            f"——————————————\n"
            f"🌍 **ظروف السوق:**\n"
            f"   - سيطرة BTC: {btc_dominance:.2f}%\n"
            f"   - سيطرة ETH: {eth_dominance:.2f}%\n"
            f"   - الخوف/الجشع: {fng_value:.0f} ({fng_label})\n"
            f"——————————————\n"
            f"⏰ {timestamp} (UTC+1)"
        )

        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }

        send_telegram_message(chat_id, message, reply_markup=reply_markup) # Use helper function
        logger.info(f"✅ [Telegram] New signal alert sent for {signal['symbol']}.")

    except Exception as e:
        logger.error(f"❌ [Telegram] Failed to build or send signal alert for {signal['symbol']}: {e}", exc_info=True)


def send_telegram_update(message, chat_id_override=None):
    """Sends a general update message (like SL/TP update, closure) to Telegram."""
    target_chat = chat_id_override if chat_id_override else chat_id
    try:
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }
        send_telegram_message(target_chat, message, reply_markup=reply_markup) # Use helper function
        logger.info(f"✅ [Telegram] Update message sent successfully to chat {target_chat}.")

    except Exception as e:
        logger.error(f"❌ [Telegram] Failed to send update message to chat {target_chat}: {e}")


def send_telegram_message(chat_id_target, text, reply_markup=None, parse_mode='Markdown', disable_web_page_preview=True, timeout=15):
    """Helper function to send messages to Telegram with error handling."""
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {
        'chat_id': chat_id_target,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': disable_web_page_preview
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        # logger.debug(f"Telegram send successful. Response: {response.text}")
        return response.json()
    except requests.exceptions.Timeout:
         logger.error(f"❌ [Telegram] Request timed out sending message to chat {chat_id_target}.")
         raise # Re-raise timeout
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Telegram] Network/API error sending message to chat {chat_id_target}: {e}")
        # Log Telegram's error response if available
        if e.response is not None:
            try:
                error_info = e.response.json()
                logger.error(f"❌ [Telegram] API Error Details: {error_info}")
            except json.JSONDecodeError:
                logger.error(f"❌ [Telegram] Non-JSON API Error Response: {e.response.text}")
        raise # Re-raise the exception
    except Exception as e:
        logger.error(f"❌ [Telegram] Unexpected error sending message to chat {chat_id_target}: {e}")
        raise # Re-raise unexpected errors

# ---------------------- إرسال تقرير الأداء الشامل ----------------------
def send_report(target_chat_id):
    """Calculates and sends a comprehensive performance report to Telegram."""
    logger.info(f"⏳ [Report] Generating performance report for chat_id: {target_chat_id}")
    report_message = "⚠️ فشل إنشاء تقرير الأداء." # Default error message
    try:
        check_db_connection()

        # 1. Active trades count
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]

        # 2. Statistics for closed trades
        cur.execute("""
            SELECT achieved_target, hit_stop_loss, profitable_stop_loss, profit_percentage
            FROM signals WHERE closed_at IS NOT NULL
        """)
        closed_signals = cur.fetchall()

        total_closed_trades = len(closed_signals)
        if total_closed_trades == 0:
            report_message = f"📊 **تقرير الأداء**\n\nلا توجد صفقات مغلقة حتى الآن.\n⏳ **التوصيات النشطة حالياً:** {active_count}"
            send_telegram_update(report_message, chat_id_override=target_chat_id)
            logger.info("✅ [Report] Sent report (no closed trades).")
            return

        # Calculate stats
        successful_target_hits = sum(1 for s in closed_signals if s[0])
        profitable_sl_hits = sum(1 for s in closed_signals if s[1] and s[2])
        losing_sl_hits = sum(1 for s in closed_signals if s[1] and not s[2])

        # Calculate profits/losses based on stored profit_percentage
        total_profit_usd = 0
        total_loss_usd = 0
        profit_percentages = []
        loss_percentages = []

        for signal in closed_signals:
            profit_pct = signal[3] # Stored profit_percentage
            if profit_pct is not None:
                trade_result_usd = TRADE_VALUE * (profit_pct / 100)
                if trade_result_usd > 0:
                    total_profit_usd += trade_result_usd
                    profit_percentages.append(profit_pct)
                else:
                    total_loss_usd += trade_result_usd # Loss is negative
                    loss_percentages.append(profit_pct)
            else:
                 logger.warning(f"⚠️ [Report] Found closed signal without profit_percentage (ID might be missing).") # Need signal ID here if possible

        net_profit_usd = total_profit_usd + total_loss_usd
        win_rate = (successful_target_hits + profitable_sl_hits) / total_closed_trades * 100 if total_closed_trades > 0 else 0
        avg_profit_pct = np.mean(profit_percentages) if profit_percentages else 0
        avg_loss_pct = np.mean(loss_percentages) if loss_percentages else 0
        profit_factor = abs(total_profit_usd / total_loss_usd) if total_loss_usd != 0 else float('inf')

        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')

        # Build the report message
        report_message = (
            f"📊 **تقرير أداء البوت** ({timestamp} UTC+1)\n"
            f"——————————————\n"
            f"**ملخص الصفقات المغلقة ({total_closed_trades}):**\n"
            f"  ✅ تحقيق الهدف: {successful_target_hits}\n"
            f"  📈 وقف خسارة رابح: {profitable_sl_hits}\n"
            f"  📉 وقف خسارة خاسر: {losing_sl_hits}\n"
            f"  📊 معدل الربح (Win Rate): {win_rate:.2f}%\n"
            f"——————————————\n"
            f"**الأداء المالي:**\n"
            f"  💰 إجمالي الربح: +{total_profit_usd:.2f} USDT\n"
            f"  💸 إجمالي الخسارة: {total_loss_usd:.2f} USDT\n"
            f"  💵 **صافي الربح/الخسارة:** {net_profit_usd:+.2f} USDT\n"
            f"  🎯 متوسط ربح الصفقة: +{avg_profit_pct:.2f}%\n"
            f"  🛑 متوسط خسارة الصفقة: {avg_loss_pct:.2f}%\n"
            f"  ⚖️ معامل الربح (Profit Factor): {profit_factor:.2f}\n"
            f"——————————————\n"
            f"⏳ **التوصيات النشطة حالياً:** {active_count}"
        )
        send_telegram_update(report_message, chat_id_override=target_chat_id) # Send the final report
        logger.info(f"✅ [Report] Performance report sent successfully to chat {target_chat_id}.")

    except psycopg2.Error as db_err:
        logger.error(f"❌ [Report] Database error during report generation: {db_err}")
        if conn and not conn.closed: conn.rollback()
        report_message = f"⚠️ حدث خطأ في قاعدة البيانات أثناء إنشاء التقرير.\n`{db_err}`"
        try:
            send_telegram_update(report_message, chat_id_override=target_chat_id)
        except Exception as send_err:
             logger.error(f"❌ [Report] Failed to send DB error message: {send_err}")
    except Exception as e:
        logger.error(f"❌ [Report] Failed to generate or send performance report: {e}", exc_info=True)
        report_message = f"⚠️ حدث خطأ غير متوقع أثناء إنشاء التقرير.\n`{e}`"
        try:
            send_telegram_update(report_message, chat_id_override=target_chat_id)
        except Exception as send_err:
             logger.error(f"❌ [Report] Failed to send general error message: {send_err}")


# ---------------------- خدمة تتبع الإشارات وتحديثها (مع الحل للمشكلة) ----------------------
def track_signals():
    """
    Tracks open signals, checks for target/stop-loss hits, and applies ATR Trailing Stop.
    Includes fix for NoneType formatting error.
    """
    logger.info(f"🔄 [Tracker] Starting signal tracking service (Interval: {SIGNAL_TRACKING_TIMEFRAME}, Lookback: {SIGNAL_TRACKING_LOOKBACK_DAYS} days)...")

    while True:
        try:
            check_db_connection() # Ensure connection is healthy

            # Fetch all active signals (not yet closed)
            cur.execute("""
                SELECT id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active
                FROM signals
                WHERE closed_at IS NULL
            """)
            active_signals = cur.fetchall()

            if not active_signals:
                # logger.info("ℹ️ [Tracker] No active signals to track currently.")
                time.sleep(20) # Sleep longer if no active signals
                continue # Go to the start of the while loop

            logger.info("==========================================")
            logger.info(f"🔍 [Tracker] Tracking {len(active_signals)} active signal(s)...")

            for signal_data in active_signals:
                signal_id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active = signal_data
                # logger.info(f"--- [Tracker] Tracking {symbol} (ID: {signal_id}) ---") # Log less verbosely per cycle

                # 1. Get Current Price
                current_price = None # Initialize current_price to None for safety
                if symbol not in ticker_data or ticker_data[symbol].get('c') is None:
                    logger.warning(f"⚠️ [Tracker] No current price data from WebSocket for {symbol}. Skipping this cycle.")
                    continue
                try:
                    price_str = ticker_data[symbol]['c']
                    if price_str is not None:
                         current_price = float(price_str)
                         if current_price <= 0:
                              logger.warning(f"⚠️ [Tracker] Invalid current price received ({current_price}) for {symbol}. Skipping.")
                              current_price = None # Ensure it's None if invalid
                    else:
                         logger.warning(f"⚠️ [Tracker] Received None price value ('c') for {symbol}. Skipping.")
                except (ValueError, TypeError) as e:
                     logger.warning(f"⚠️ [Tracker] Received non-numeric price value ({ticker_data[symbol].get('c')}) for {symbol}: {e}. Skipping.")
                     current_price = None # Ensure it's None on conversion error

                # ** CRITICAL CHECK 1 **: Ensure we have a valid current price before proceeding
                if current_price is None:
                     # logger.warning(f"⚠️ [Tracker] Cannot proceed without a valid current price for {symbol}. Skipping.")
                     continue # Skip this signal for this cycle

                # ** CRITICAL CHECK 2 **: Ensure essential data from DB is not None
                if entry_price is None or current_target is None or current_stop_loss is None:
                    logger.error(f"❌ [Tracker] Critical data missing (None) from DB for Signal ID {signal_id} ({symbol}): "
                                 f"Entry={entry_price}, Target={current_target}, Stop={current_stop_loss}. Cannot process this signal.")
                    # Consider marking this signal as invalid in the DB?
                    continue # Skip this signal entirely for this cycle

                # Log current status (safe now after None checks)
                logger.info(f"  [Tracker] {symbol} (ID:{signal_id}) | P: {current_price:.8f} | E: {entry_price:.8f} | T: {current_target:.8f} | SL: {current_stop_loss:.8f} | Trail: {is_trailing_active}")

                # ** CRITICAL CHECK 3 **: Validate entry price for calculations
                if abs(entry_price) < 1e-9: # Use a small threshold instead of zero
                    logger.error(f"❌ [Tracker] Entry price ({entry_price}) is too close to zero for Signal ID {signal_id} ({symbol}). Cannot calculate percentages.")
                    continue

                # 2. Check for Trade Closure (Target or Stop Loss)
                # --- Target Check ---
                if current_price >= current_target:
                    profit_pct = ((current_target / entry_price) - 1) * 100
                    profit_usdt = TRADE_VALUE * (profit_pct / 100)
                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                    msg = (f"✅ **هدف محقق!** ✅\n"
                           f"📈 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                           f"💰 أغلق عند: ${current_price:.8f} (الهدف: ${current_target:.8f})\n"
                           f"📊 الربح: +{profit_pct:.2f}% (+{profit_usdt:.2f} USDT)")
                    try:
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET achieved_target = TRUE, closed_at = NOW(), profit_percentage = %s, closing_price = %s
                            WHERE id = %s AND closed_at IS NULL
                        """, (profit_pct, current_price, signal_id)) # Add closed_at IS NULL condition
                        conn.commit()
                        logger.info(f"✅ [Tracker] Closed signal {symbol} (ID: {signal_id}) - TARGET HIT.")
                    except Exception as update_err:
                        logger.error(f"❌ [Tracker] Error updating/sending Target Hit for {signal_id}: {update_err}")
                        if conn and not conn.closed: conn.rollback()
                    continue # Move to the next signal

                # --- Stop Loss Check ---
                elif current_price <= current_stop_loss:
                    loss_pct = ((current_stop_loss / entry_price) - 1) * 100
                    loss_usdt = TRADE_VALUE * (loss_pct / 100)
                    profitable_stop = current_stop_loss > entry_price
                    stop_type_msg = "وقف خسارة رابح" if profitable_stop else "وقف خسارة"
                    emoji = "📈" if profitable_stop else "🛑"
                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                    msg = (f"{emoji} **{stop_type_msg}** {emoji}\n"
                           f"📉 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                           f"💰 أغلق عند: ${current_price:.8f} (الوقف: ${current_stop_loss:.8f})\n"
                           f"📊 النتيجة: {loss_pct:.2f}% ({loss_usdt:.2f} USDT)")
                    try:
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s, profitable_stop_loss = %s, closing_price = %s
                            WHERE id = %s AND closed_at IS NULL
                        """, (loss_pct, profitable_stop, current_price, signal_id)) # Add closed_at IS NULL condition
                        conn.commit()
                        logger.info(f"✅ [Tracker] Closed signal {symbol} (ID: {signal_id}) - STOP LOSS HIT ({stop_type_msg}).")
                    except Exception as update_err:
                        logger.error(f"❌ [Tracker] Error updating/sending Stop Loss Hit for {signal_id}: {update_err}")
                        if conn and not conn.closed: conn.rollback()
                    continue # Move to the next signal

                # 3. Apply/Update ATR Trailing Stop Loss
                # --- Fetch short-term data (e.g., 15m) for current ATR ---
                df_track = fetch_historical_data(symbol, interval=SIGNAL_TRACKING_TIMEFRAME, days=SIGNAL_TRACKING_LOOKBACK_DAYS)

                if df_track is None or df_track.empty or len(df_track) < 20: # Need enough data for ATR
                    logger.warning(f"⚠️ [Tracker] Insufficient {SIGNAL_TRACKING_TIMEFRAME} data to calculate ATR for {symbol}. Skipping trailing stop update.")
                else:
                    # Calculate ATR on tracking data
                    df_track = calculate_atr_indicator(df_track, period=14) # Use standard ATR period
                    if 'atr' not in df_track.columns or df_track['atr'].iloc[-1] is None or pd.isna(df_track['atr'].iloc[-1]):
                         logger.warning(f"⚠️ [Tracker] Failed to calculate ATR for {symbol} on {SIGNAL_TRACKING_TIMEFRAME}.")
                    else:
                        current_atr = df_track['atr'].iloc[-1]
                        if current_atr > 0:
                            # logger.debug(f"  - ATR ({SIGNAL_TRACKING_TIMEFRAME}) for {symbol}: {current_atr:.8f}")

                            # --- Calculate current profit percentage ---
                            current_gain_pct = (current_price - entry_price) / entry_price

                            # --- Check condition to activate/update trailing stop ---
                            if current_gain_pct >= TRAILING_STOP_ACTIVATION_PROFIT_PCT:
                                # Calculate potential new trailing stop loss
                                potential_new_stop_loss = current_price - (TRAILING_STOP_ATR_MULTIPLIER * current_atr)

                                # --- Check if the proposed stop is higher than the current one ---
                                # Trailing stop should only move up, never down
                                if potential_new_stop_loss > current_stop_loss:
                                    new_stop_loss = potential_new_stop_loss
                                    logger.info(f"  => [Tracker] Updating Trailing Stop for {symbol} (ID: {signal_id})!")
                                    # logger.info(f"     Current Price: {current_price:.8f} (> Activation: {entry_price * (1 + TRAILING_STOP_ACTIVATION_PROFIT_PCT):.8f})")
                                    # logger.info(f"     Old Stop Loss: {current_stop_loss:.8f}")
                                    logger.info(f"     New Stop Loss: {new_stop_loss:.8f} (Current - {TRAILING_STOP_ATR_MULTIPLIER} * ATR)")

                                    # --- Send update notification and update the database ---
                                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                                    update_msg = (
                                        f"🔄 **تحديث وقف الخسارة (Trailing Stop)** 🔄\n"
                                        f"📈 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                                        f"   - سعر الدخول: ${entry_price:.8f}\n"
                                        f"   - السعر الحالي: ${current_price:.8f} ({current_gain_pct:+.2%})\n"
                                        f"   - الوقف القديم: ${current_stop_loss:.8f}\n"
                                        f"   - **الوقف الجديد:** `${new_stop_loss:.8f}` ✅"
                                    )
                                    try:
                                        send_telegram_update(update_msg)
                                        cur.execute("""
                                            UPDATE signals
                                            SET current_stop_loss = %s, is_trailing_active = TRUE
                                            WHERE id = %s AND closed_at IS NULL
                                        """, (new_stop_loss, signal_id)) # Add closed_at IS NULL condition
                                        conn.commit()
                                        logger.info(f"✅ [Tracker] Trailing Stop Loss updated for {symbol} (ID: {signal_id}) to {new_stop_loss:.8f}")
                                    except Exception as update_err:
                                         logger.error(f"❌ [Tracker] Error updating/sending Trailing SL update for {signal_id}: {update_err}")
                                         if conn and not conn.closed: conn.rollback()
                                    # No need to update local variable `current_stop_loss` as it's fetched fresh next cycle
                                # else:
                                #     logger.debug(f"  - Proposed trailing stop ({potential_new_stop_loss:.8f}) not higher than current ({current_stop_loss:.8f}) for {symbol}. No change.")

                            # else:
                            #      logger.debug(f"  - Current gain ({current_gain_pct:.2%}) for {symbol} has not reached trailing stop activation threshold ({TRAILING_STOP_ACTIVATION_PROFIT_PCT:.2%}).")
                        # else:
                        #      logger.warning(f"⚠️ [Tracker] Calculated ATR is not positive ({current_atr}) for {symbol}. Cannot use for trailing stop.")

                # Short sleep between checking each signal to avoid hammering APIs/DB
                time.sleep(0.2) # 200 milliseconds

            # logger.debug("[Tracker] Completed tracking cycle.")

        except psycopg2.Error as db_err:
            logger.error(f"❌ [Tracker] Database error during signal tracking loop: {db_err}", exc_info=True)
            if conn and not conn.closed: conn.rollback() # Rollback any partial changes
            logger.info("[Tracker] Waiting longer after DB error...")
            time.sleep(60) # Wait longer after a DB error
        except Exception as e:
            logger.error(f"❌ [Tracker] Unexpected general error in tracking service: {e}", exc_info=True)
            # Rollback just in case
            try:
                 if conn and not conn.closed: conn.rollback()
            except Exception as rb_err:
                 logger.error(f"❌ [Tracker] Error during rollback attempt after general error: {rb_err}")
            logger.info("[Tracker] Waiting longer after unexpected error...")
            time.sleep(60) # Wait longer after an unexpected error

        # Sleep interval between full tracking cycles
        time.sleep(30) # Check all signals every 30 seconds (adjust as needed)

# ---------------------- دالة التحقق من عدد التوصيات المفتوحة ----------------------
def can_generate_new_recommendation():
    """Checks if a new recommendation can be generated based on MAX_OPEN_TRADES limit."""
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]
        if active_count < MAX_OPEN_TRADES:
            logger.info(f"✅ [Gate] Open trades ({active_count}) < limit ({MAX_OPEN_TRADES}). New recommendations allowed.")
            return True
        else:
            logger.info(f"⚠️ [Gate] Max open trades limit ({MAX_OPEN_TRADES}) reached. New recommendations paused.")
            return False
    except Exception as e:
        logger.error(f"❌ [Gate] Error checking open recommendations count: {e}")
        # Default to False (don't generate) in case of error for safety
        return False

# ---------------------- تحليل السوق (البحث عن صفقات جديدة) ----------------------
def analyze_market():
    """Analyzes the market for new trading opportunities based on the defined strategy."""
    logger.info("==========================================")
    logger.info(f" H [Market Analysis] Starting market analysis cycle (Interval: {SIGNAL_GENERATION_TIMEFRAME}, Lookback: {SIGNAL_GENERATION_LOOKBACK_DAYS} days)...")

    # --- Gate Check: Can we open a new trade? ---
    if not can_generate_new_recommendation():
        logger.info(" H [Market Analysis] Cycle skipped: Max open trades limit reached.")
        return # Exit if limit reached

    # --- Fetch Market Context ---
    btc_dominance, eth_dominance = get_market_dominance()
    if btc_dominance is None or eth_dominance is None:
        logger.warning("⚠️ [Market Analysis] Failed to fetch market dominance. Proceeding with defaults (0.0).")
        btc_dominance, eth_dominance = 0.0, 0.0

    # --- Get Symbols to Analyze ---
    symbols_to_analyze = get_crypto_symbols()
    if not symbols_to_analyze:
        logger.warning("⚠️ [Market Analysis] Symbol list is empty. Analysis cannot proceed.")
        return

    logger.info(f" H [Market Analysis] Analyzing {len(symbols_to_analyze)} symbols...")
    generated_signals_count = 0
    processed_symbols_count = 0

    # --- Iterate Through Symbols ---
    for symbol in symbols_to_analyze:
        processed_symbols_count += 1
        # Re-check gate before processing each symbol (important if analysis takes time)
        if not can_generate_new_recommendation():
             logger.info(f" H [Market Analysis] Max trades limit reached during analysis. Stopping further symbol checks.")
             break # Exit loop if limit reached mid-analysis

        # logger.info(f"--- [Market Analysis] Analyzing Symbol: {symbol} ({processed_symbols_count}/{len(symbols_to_analyze)}) ---")

        # 1. Check for existing open signal for this symbol
        try:
            check_db_connection()
            cur.execute("SELECT COUNT(*) FROM signals WHERE symbol = %s AND closed_at IS NULL", (symbol,))
            if cur.fetchone()[0] > 0:
                # logger.info(f"ℹ️ [Market Analysis] Skipping {symbol}: Active signal already exists.")
                continue # Move to the next symbol
        except Exception as e:
             logger.error(f"❌ [Market Analysis] DB error checking existing signal for {symbol}: {e}")
             continue # Skip this symbol on error

        # 2. Fetch historical data for signal generation timeframe
        df_signal_gen = fetch_historical_data(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
        if df_signal_gen is None or df_signal_gen.empty:
            # logger.warning(f"⚠️ [Market Analysis] Insufficient or error fetching {SIGNAL_GENERATION_TIMEFRAME} data for {symbol}. Skipping.")
            continue # Skip if data is bad

        # 3. Generate signal using the strategy
        signal = generate_signal_using_freqtrade_strategy(df_signal_gen, symbol)

        if signal:
            logger.info(f"✅ [Market Analysis] Potential BUY signal found for {symbol}!")

            # 4. Check Volume/Liquidity
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < MIN_VOLUME_15M_USDT:
                logger.info(f"⚠️ [Market Analysis] Signal for {symbol} REJECTED due to low volume ({volume_15m:,.0f} USDT < {MIN_VOLUME_15M_USDT:,.0f} USDT).")
                continue # Skip this signal

            logger.info(f"✅ [Market Analysis] Sufficient volume for {symbol} ({volume_15m:,.0f} USDT). Proceeding with signal.")

            # 5. Send Alert and Save Signal to DB
            try:
                # Send alert first (more immediate)
                send_telegram_alert(signal, volume_15m, btc_dominance if btc_dominance is not None else 0.0, eth_dominance if eth_dominance is not None else 0.0, SIGNAL_GENERATION_TIMEFRAME)

                # Save signal to database
                check_db_connection()
                cur.execute("""
                    INSERT INTO signals
                    (symbol, entry_price, initial_target, initial_stop_loss, current_target, current_stop_loss,
                     r2_score, volume_15m, sent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    signal['symbol'],
                    signal['entry_price'],
                    signal['initial_target'],
                    signal['initial_stop_loss'],
                    signal['current_target'], # Initial current = initial
                    signal['current_stop_loss'],# Initial current = initial
                    signal.get('r2_score'), # Use the score from the signal dict
                    volume_15m
                ))
                conn.commit()
                logger.info(f"✅ [Market Analysis] Signal for {symbol} saved successfully to database.")
                generated_signals_count += 1

                # Optional: Short delay after generating a signal
                time.sleep(1)

            except psycopg2.Error as db_err:
                logger.error(f"❌ [Market Analysis] Failed to save signal for {symbol} to DB: {db_err}")
                if conn and not conn.closed: conn.rollback() # Rollback failed insert
                # Consider if alert should still be sent if DB save fails? Currently sends first.
            except Exception as e:
                logger.error(f"❌ [Market Analysis] Unexpected error processing signal for {symbol}: {e}", exc_info=True)
                if conn and not conn.closed: conn.rollback() # Rollback as precaution

        # else:
             # logger.debug(f"ℹ️ [Market Analysis] No qualifying signal found for {symbol} in this cycle.")
             # pass # No signal found

        # Small delay between analyzing each symbol to be nice to APIs
        time.sleep(0.5)

    logger.info(f" H [Market Analysis] Cycle finished. Processed {processed_symbols_count} symbols. Generated {generated_signals_count} new signal(s).")
    logger.info("==========================================")

# ---------------------- اختبار Telegram ----------------------
def test_telegram():
    """Sends a simple test message to Telegram to verify setup."""
    logger.info("🧪 [Test] Attempting to send test message to Telegram...")
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        test_message = f'🚀 **Bot Startup Test** 🚀\nService started successfully.\nTime: {timestamp}'
        send_telegram_message(chat_id, test_message) # Use helper function
        logger.info(f"✅ [Test] Telegram test message sent successfully to chat ID {chat_id}.")
    except Exception as e:
        logger.error(f"❌ [Test] Failed to send Telegram test message: {e}")

# ---------------------- تشغيل Flask (في خيط منفصل) ----------------------
def run_flask():
    """Runs the Flask web server to handle incoming webhooks."""
    port = int(os.environ.get("PORT", 10000)) # Default port for Render or local dev
    logger.info(f"🌍 [Flask] Starting Flask server on host 0.0.0.0, port {port}...")
    try:
        # Use 'waitress' for production instead of Flask's development server
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=4) # Adjust threads as needed
        # app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False) # Development server
    except ImportError:
         logger.warning("⚠️ [Flask] 'waitress' not installed. Falling back to Flask development server (not recommended for production).")
         app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
         logger.critical(f"❌ [Flask] Flask server failed to start: {e}", exc_info=True)


# ---------------------- التشغيل الرئيسي والتزامن ----------------------
if __name__ == '__main__':
    logger.info("==========================================")
    logger.info("🚀 BOOTING UP Crypto Trading Bot v4.3 (Hazem Mod)...")
    logger.info("==========================================")

    # Global references to threads for status checking
    flask_thread = None
    websocket_thread = None
    tracker_thread = None
    scheduler = None

    try:
        # 1. Initialize Database
        init_db() # This will raise exceptions if connection fails after retries

        # 2. Set Telegram Webhook (Best effort)
        # Run this early, relies on environment vars or .env for URL determination
        set_telegram_webhook()

        # 3. Start Flask Server Thread
        flask_thread = Thread(target=run_flask, name="FlaskThread", daemon=True)
        flask_thread.start()
        logger.info("✅ [Main] Flask server thread started.")
        time.sleep(2) # Give flask a moment to potentially start listening

        # 4. Start WebSocket Manager Thread
        websocket_thread = Thread(target=run_ticker_socket_manager, name="WebSocketThread", daemon=True)
        websocket_thread.start()
        logger.info("✅ [Main] WebSocket manager thread started.")
        logger.info("ℹ️ [Main] Allowing 15 seconds for WebSocket to connect and receive initial data...")
        time.sleep(15) # Allow time for WS connection and initial data

        # 5. Start Signal Tracker Thread
        tracker_thread = Thread(target=track_signals, name="TrackerThread", daemon=True)
        tracker_thread.start()
        logger.info("✅ [Main] Signal tracker thread started.")

        # 6. Send Startup Test Message
        test_telegram()

        # 7. Setup and Start Scheduled Tasks (APScheduler)
        scheduler = BackgroundScheduler(timezone="UTC")
        # Schedule market analysis every 5 minutes
        scheduler.add_job(analyze_market, 'interval', minutes=5, id='market_analyzer', replace_existing=True, misfire_grace_time=60) # Allow 60s delay if missed
        logger.info("✅ [Main] Market analysis task scheduled (every 5 minutes).")

        # Add other scheduled tasks here (e.g., daily report)
        # scheduler.add_job(send_report, 'cron', hour=8, minute=0, args=[chat_id], id='daily_report', replace_existing=True)
        # logger.info("✅ [Main] Daily report task scheduled (08:00 UTC).")

        scheduler.start()
        logger.info("✅ [Main] APScheduler started.")
        logger.info("==========================================")
        logger.info("✅ SYSTEM ONLINE AND OPERATIONAL")
        logger.info("==========================================")

        # Keep the main thread alive and monitor other threads
        while True:
            if flask_thread and not flask_thread.is_alive():
                 logger.critical("❌ [Main] Flask thread has died! Restarting is not implemented. Exiting.")
                 # In a real deployment, a process manager (like systemd, supervisor) should handle restarts.
                 break
            if websocket_thread and not websocket_thread.is_alive():
                 logger.critical("❌ [Main] WebSocket thread has died! Exiting to allow external restart.")
                 # Attempting restart within the script is complex; rely on external manager.
                 break
            if tracker_thread and not tracker_thread.is_alive():
                 logger.critical("❌ [Main] Signal Tracker thread has died! Exiting to allow external restart.")
                 break
            if scheduler and not scheduler.running:
                logger.critical("❌ [Main] APScheduler is not running! Exiting.")
                break

            time.sleep(30) # Check thread health periodically

    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 [Main] Shutdown signal received (KeyboardInterrupt/SystemExit)...")
    except Exception as e:
        logger.critical(f"❌ [Main] CRITICAL UNHANDLED EXCEPTION in main execution block: {e}", exc_info=True)
    finally:
        logger.info("------------------------------------------")
        logger.info("ℹ️ [Main] Initiating graceful shutdown...")
        if scheduler and scheduler.running:
            logger.info("   - Shutting down APScheduler...")
            scheduler.shutdown(wait=False) # Don't wait for jobs to complete
            logger.info("   - APScheduler shut down.")
        # Threads are daemonized, they will exit when the main thread exits.
        # Add any other cleanup needed here (e.g., close DB connection explicitly?)
        if conn and not conn.closed:
            logger.info("   - Closing database connection...")
            conn.close()
            logger.info("   - Database connection closed.")
        logger.info("✅ [Main] Shutdown complete. Exiting.")
        logger.info("==========================================")
