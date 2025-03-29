#!/usr/bin/env python
# -*- coding: utf-8 -*-  # لتحديد الترميز

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
from sklearn.ensemble import GradientBoostingRegressor  # للتنبؤ الاختياري
from sklearn.linear_model import LinearRegression       # لاستيراد نموذج الانحدار الخطي
from sklearn.metrics import r2_score                    # لاحتساب معامل التحديد

# ---------------------- إعدادات التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('crypto_bot_ar.log', encoding='utf-8'),
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

logger.info(f" مفتاح Binance API: {'موجود' if api_key else 'غير موجود'}")
logger.info(f" توكن تليجرام: {telegram_token[:10]}...{'*' * (len(telegram_token)-10)}")
logger.info(f" معرف دردشة تليجرام: {chat_id}")
logger.info(f" رابط قاعدة البيانات: {'موجود' if db_url else 'غير موجود'}")

# ---------------------- ثوابت وإعدادات الاستراتيجية ----------------------
TRADE_VALUE = 10               # قيمة الصفقة الثابتة بالدولار
MAX_OPEN_TRADES = 4            # الحد الأقصى للصفقات المفتوحة في نفس الوقت
SIGNAL_GENERATION_TIMEFRAME = '1h'   # الفريم الزمني لتوليد توصيات جديدة
SIGNAL_GENERATION_LOOKBACK_DAYS = 4   # عدد أيام البيانات التاريخية لتوليد التوصيات
SIGNAL_TRACKING_TIMEFRAME = '15m'      # الفريم الزمني لتتبع التوصيات المفتوحة
SIGNAL_TRACKING_LOOKBACK_DAYS = 2      # عدد أيام البيانات التاريخية لتتبع التوصيات

# إعدادات وقف الخسارة المتحرك (ATR Trailing Stop)
TRAILING_STOP_ACTIVATION_PROFIT_PCT = 0.015  # نسبة الربح المطلوبة لتفعيل الوقف المتحرك (مثلاً 1.5%)
TRAILING_STOP_ATR_MULTIPLIER = 2.0           # معامل ATR لتحديد مسافة الوقف المتحرك

# إعدادات إشارة الدخول
ENTRY_ATR_MULTIPLIER = 1.5     # معامل ATR لتحديد الهدف ووقف الخسارة الأولي
MIN_PROFIT_MARGIN_PCT = 1.0    # الحد الأدنى لهامش الربح المطلوب (%)
MIN_VOLUME_15M_USDT = 500000   # الحد الأدنى لحجم التداول في آخر 15 دقيقة

# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
conn = None
cur = None

def init_db():
    global conn, cur
    retries = 5
    delay = 5
    for i in range(retries):
        try:
            logger.info(f"[DB] محاولة الاتصال بقاعدة البيانات (محاولة {i+1}/{retries})...")
            conn = psycopg2.connect(db_url, connect_timeout=10)
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    initial_target DOUBLE PRECISION NOT NULL,
                    initial_stop_loss DOUBLE PRECISION NOT NULL,
                    current_target DOUBLE PRECISION NOT NULL,
                    current_stop_loss DOUBLE PRECISION NOT NULL,
                    r2_score DOUBLE PRECISION,
                    volume_15m DOUBLE PRECISION,
                    achieved_target BOOLEAN DEFAULT FALSE,
                    hit_stop_loss BOOLEAN DEFAULT FALSE,
                    closing_price DOUBLE PRECISION,
                    closed_at TIMESTAMP,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    profit_percentage DOUBLE PRECISION,
                    profitable_stop_loss BOOLEAN DEFAULT FALSE,
                    is_trailing_active BOOLEAN DEFAULT FALSE
                )
            """)
            conn.commit()
            logger.info("✅ [DB] تم تأسيس الاتصال بقاعدة البيانات بنجاح.")
            return
        except Exception as e:
            logger.error(f"❌ [DB] فشل الاتصال: {e}")
            time.sleep(delay)
    raise Exception("❌ [DB] فشل جميع محاولات الاتصال.")

def check_db_connection():
    global conn, cur
    try:
        if conn is None or conn.closed != 0:
            init_db()
            return
        cur.execute("SELECT 1")
    except Exception as e:
        init_db()

# ---------------------- إعداد عميل Binance ----------------------
try:
    client = Client(api_key, api_secret)
    client.ping()
    logger.info("✅ [Binance] تم تهيئة عميل Binance والتحقق من الاتصال.")
except Exception as e:
    logger.critical(f"❌ [Binance] فشل تهيئة عميل Binance: {e}")
    raise

# ---------------------- خدمات WebSocket ----------------------
ticker_data = {}

def handle_ticker_message(msg):
    try:
        if isinstance(msg, list):
            for m in msg:
                symbol = m.get('s')
                if symbol and 'USDT' in symbol:
                    ticker_data[symbol] = {
                        'c': m.get('c'),
                        'h': m.get('h'),
                        'l': m.get('l'),
                        'v': m.get('v')
                    }
        elif isinstance(msg, dict) and 'stream' not in msg and 'e' in msg and msg['e'] == 'error':
            logger.error(f"❌ [WS] رسالة خطأ من WebSocket: {msg.get('m')}")
    except Exception as e:
        logger.error(f"❌ [WS] خطأ في handle_ticker_message: {e}")

def run_ticker_socket_manager():
    while True:
        try:
            logger.info("ℹ️ [WS] بدء تشغيل مدير WebSocket...")
            twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
            twm.start()
            twm.start_miniticker_socket(callback=handle_ticker_message)
            logger.info("✅ [WS] تم توصيل WebSocket لتحديثات الميني-تيكر.")
            twm.join()
            logger.warning("⚠️ [WS] توقف WebSocket. إعادة التشغيل بعد تأخير...")
        except Exception as e:
            logger.error(f"❌ [WS] خطأ في تشغيل WebSocket: {e}")
        time.sleep(15)

# ---------------------- دوال حساب المؤشرات الفنية ----------------------
def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi_indicator(df, period=14):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean().replace(0, np.nan)
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

def is_hammer(row):
    open_price, high, low, close = row['open'], row['high'], row['low'], row['close']
    if None in [open_price, high, low, close]:
        return 0
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0:
        return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.3 * body:
        return 100
    return 0

def is_shooting_star(row):
    open_price, high, low, close = row['open'], row['high'], row['low'], row['close']
    if None in [open_price, high, low, close]:
        return 0
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0:
        return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and upper_shadow >= 2 * body and lower_shadow <= 0.3 * body:
        return -100
    return 0

def compute_engulfing(df, idx):
    if idx == 0:
        return 0
    prev = df.iloc[idx - 1]
    curr = df.iloc[idx]
    if None in [prev['close'], prev['open'], curr['close'], curr['open']]:
        return 0
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
    try:
        if df.empty or 'rsi' not in df.columns or 'adx' not in df.columns:
            return 0.5
        rsi = df['rsi'].iloc[-1]
        adx = df['adx'].iloc[-1]
        if pd.isna(rsi) or pd.isna(adx):
            return 0.5
        if rsi < 40 and adx > 25:
            return 0.80
        elif rsi > 65 and adx > 25:
            return 0.20
        else:
            return 0.5
    except Exception as e:
        logger.error(f"❌ [ML] خطأ في ml_predict_signal للزوج {symbol}: {e}")
        return 0.5

def get_market_sentiment(symbol):
    return 0.6

def get_fear_greed_index():
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("data"):
            fng_value = float(data["data"][0].get("value", 50))
            fng_classification_en = data["data"][0].get("value_classification", "Neutral")
            fng_translations = {
                "Extreme Fear": "خوف شديد",
                "Fear": "خوف",
                "Neutral": "محايد",
                "Greed": "جشع",
                "Extreme Greed": "جشع شديد"
            }
            fng_classification_ar = fng_translations.get(fng_classification_en, fng_classification_en)
            logger.info(f"✅ [FNG] مؤشر الخوف والجشع: {fng_value:.0f} - {fng_classification_ar}")
            return fng_value, fng_classification_ar
        else:
            logger.warning("⚠️ [FNG] لم يتم العثور على بيانات مؤشر الخوف والجشع.")
            return 50.0, "محايد"
    except Exception as e:
        logger.error(f"❌ [FNG] خطأ في جلب مؤشر الخوف والجشع: {e}")
        return 50.0, "خطأ"

# ---------------------- دالة التنبؤ بالسعر باستخدام Linear Regression ----------------------
def predict_price_linear_regression(symbol, interval='2h', days=30):
    try:
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50:
            return None, None
        df['ema_fast'] = calculate_ema(df['close'], 10)
        df['ema_slow'] = calculate_ema(df['close'], 30)
        df['rsi'] = calculate_rsi_indicator(df)
        df = df.dropna()
        features = ['ema_fast', 'ema_slow', 'rsi']
        if not all(f in df.columns for f in features):
            return None, None
        X = df[features].iloc[:-1].values
        y = df['close'].iloc[1:].values
        if len(X) == 0:
            return None, None
        model = LinearRegression()
        model.fit(X, y)
        y_pred = model.predict(X)
        score = r2_score(y, y_pred)
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]
        logger.info(f"✅ [Linear Regression] للزوج {symbol} السعر المتوقع: {predicted_price:.8f} بمعامل R²: {score:.4f}")
        return predicted_price, score
    except Exception as e:
        logger.error(f"❌ [Linear Regression] خطأ في التنبؤ للسعر للزوج {symbol}: {e}")
        return None, None

# ---------------------- استراتيجية Freqtrade ----------------------
class FreqtradeStrategy:
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 50:
            logger.warning(f"⚠️ [Strategy] DataFrame قصير جدًا ({len(df)} شمعة).")
            return df if all(col in df.columns for col in ['open', 'high', 'low', 'close']) else pd.DataFrame()
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
            initial_len = len(df)
            df = df.dropna()
            logger.info(f"✅ [Strategy] المؤشرات محسوبة (الحجم: {len(df)}).")
            return df
        except Exception as e:
            logger.error(f"❌ [Strategy] خطأ في حساب المؤشرات: {e}", exc_info=True)
            return pd.DataFrame()

    def composite_buy_score(self, row):
        score = 0
        required_cols = ['ema5', 'ema8', 'ema21', 'ema34', 'ema50', 'rsi', 'close', 'lower_band', 'macd', 'macd_signal', 'macd_hist', 'kdj_j', 'kdj_k', 'kdj_d', 'adx', 'BullishSignal']
        if any(col not in row or pd.isna(row[col]) for col in required_cols):
            return 0
        try:
            if row['ema5'] > row['ema8'] > row['ema21'] > row['ema34'] > row['ema50']:
                score += 1.5
            if row['rsi'] < 40:
                score += 1
            if row['close'] > row['lower_band'] and ((row['close'] - row['lower_band']) / row['close'] < 0.02):
                score += 1
            if row['macd'] > row['macd_signal'] and row['macd_hist'] > 0:
                score += 1
            if row['kdj_j'] > row['kdj_k'] and row['kdj_k'] > row['kdj_d'] and row['kdj_j'] < 80:
                score += 1
            if row['adx'] > 20:
                score += 0.5
            if row['BullishSignal'] == 100:
                score += 1.5
        except Exception as e:
            logger.error(f"❌ [Strategy] خطأ في composite_buy_score: {e}")
            return 0
        return score

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        required_score = 4.0
        required_cols = ['ema5', 'rsi', 'lower_band', 'macd', 'kdj_j', 'adx', 'BullishSignal']
        if df.empty or not all(col in df.columns for col in required_cols):
            logger.warning("⚠️ [Strategy] DataFrame يفتقد للأعمدة المطلوبة.")
            df['buy_score'] = 0
            df['buy'] = 0
            return df
        df['buy_score'] = df.apply(lambda row: self.composite_buy_score(row) if not row.isnull().any() else 0, axis=1)
        df['buy'] = np.where(df['buy_score'] >= required_score, 1, 0)
        if df['buy'].sum() > 0:
            logger.info(f"✅ [Strategy] تحديد {df['buy'].sum()} إشارة شراء محتملة.")
        return df

# ---------------------- دالة التنبؤ بالسعر المحسنة ----------------------
def improved_predict_future_price(symbol, interval='2h', days=30):
    try:
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50:
            return None
        df['ema_fast'] = calculate_ema(df['close'], 10)
        df['ema_slow'] = calculate_ema(df['close'], 30)
        df['rsi'] = calculate_rsi_indicator(df)
        df = df.dropna()
        features = ['ema_fast', 'ema_slow', 'rsi']
        if not all(f in df.columns for f in features):
            return None
        X = df[features].iloc[:-1].values
        y = df['close'].iloc[1:].values
        if len(X) == 0:
            return None
        model = GradientBoostingRegressor(n_estimators=100, max_depth=3, random_state=42, learning_rate=0.1)
        model.fit(X, y)
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]
        logger.info(f"✅ [Price Prediction] للزوج {symbol} السعر المتوقع: {predicted_price:.8f}")
        return predicted_price
    except Exception as e:
        logger.error(f"❌ [Price Prediction] خطأ في التنبؤ للسعر للزوج {symbol}: {e}")
        return None

# ---------------------- دالة توليد الإشارة ----------------------
def generate_signal_using_freqtrade_strategy(df_input, symbol):
    if df_input is None or df_input.empty:
        logger.warning(f"⚠️ [Signal Gen] DataFrame فارغ للزوج {symbol}.")
        return None
    if len(df_input) < 50:
        logger.info(f"ℹ️ [Signal Gen] بيانات غير كافية للزوج {symbol}.")
        return None
    strategy = FreqtradeStrategy()
    df_processed = strategy.populate_indicators(df_input.copy())
    if df_processed.empty:
        logger.warning(f"⚠️ [Signal Gen] DataFrame فارغ بعد حساب المؤشرات للزوج {symbol}.")
        return None
    df_with_signals = strategy.populate_buy_trend(df_processed)
    if df_with_signals.empty or df_with_signals['buy'].iloc[-1] != 1:
        return None
    last_signal_row = df_with_signals.iloc[-1]
    current_price = last_signal_row['close']
    current_atr = last_signal_row['atr']
    if pd.isna(current_price) or pd.isna(current_atr) or current_atr <= 0 or current_price <= 0:
        logger.warning(f"⚠️ [Signal Gen] بيانات سعر أو ATR غير صالحة للزوج {symbol}.")
        return None

    predicted_price, lr_score = predict_price_linear_regression(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
    if predicted_price is not None and lr_score is not None:
        profit_margin = ((predicted_price / current_price) - 1) * 100
        if profit_margin >= 4 and lr_score > 0.97:
            target_price = predicted_price
            logger.info(f"✅ [Signal Gen] استخدام السعر المتوقع (predicted: {predicted_price:.8f}, R²: {lr_score:.4f}) كهدف للزوج {symbol}.")
        else:
            target_price = current_price + (ENTRY_ATR_MULTIPLIER * current_atr)
    else:
        target_price = current_price + (ENTRY_ATR_MULTIPLIER * current_atr)

    profit_margin_pct = ((target_price / current_price) - 1) * 100 if current_price > 0 else 0
    if profit_margin_pct < MIN_PROFIT_MARGIN_PCT:
        logger.info(f"ℹ️ [Signal Gen] هامش الربح ({profit_margin_pct:.2f}%) أقل من المطلوب للزوج {symbol}.")
        return None
    buy_score = last_signal_row.get('buy_score', 0)
    signal = {
        'symbol': symbol,
        'entry_price': float(f"{current_price:.8f}"),
        'initial_target': float(f"{target_price:.8f}"),
        'initial_stop_loss': float(f"{current_price - (ENTRY_ATR_MULTIPLIER * current_atr):.8f}"),
        'current_target': float(f"{target_price:.8f}"),
        'current_stop_loss': float(f"{current_price - (ENTRY_ATR_MULTIPLIER * current_atr):.8f}"),
        'strategy': 'freqtrade_improved_atr',
        'indicators': {
            'rsi': round(last_signal_row['rsi'], 2) if 'rsi' in last_signal_row and pd.notna(last_signal_row['rsi']) else None,
            'macd_hist': round(last_signal_row['macd_hist'], 5) if 'macd_hist' in last_signal_row and pd.notna(last_signal_row['macd_hist']) else None,
            'adx': round(last_signal_row['adx'], 2) if 'adx' in last_signal_row and pd.notna(last_signal_row['adx']) else None,
            'atr': round(current_atr, 8),
            'buy_score': round(buy_score, 2)
        },
        'r2_score': round(buy_score, 2),
        'trade_value': TRADE_VALUE,
    }
    logger.info(f"✅ [Signal Gen] تم توليد إشارة شراء للزوج {symbol} عند سعر {current_price:.8f}.")
    return signal

# ---------------------- دوال جلب البيانات ----------------------
def get_crypto_symbols(filename='crypto_list.txt'):
    symbols = []
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)
        if not os.path.exists(file_path):
            logger.error(f"❌ [Data] ملف {filename} غير موجود.")
            return []
        with open(file_path, 'r', encoding='utf-8') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip() and not line.startswith('#')]
        logger.info(f"✅ [Data] تم تحميل {len(symbols)} رمز من {filename}.")
        return symbols
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في قراءة ملف الرموز: {e}")
        return []

def fetch_historical_data(symbol, interval='1h', days=10):
    try:
        start_str = f"{days} day ago UTC"
        klines = client.get_historical_klines(symbol, interval, start_str)
        if not klines:
            logger.warning(f"⚠️ [Data] لا توجد بيانات تاريخية للزوج {symbol}.")
            return None
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        initial_len = len(df)
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        if len(df) < initial_len:
            logger.debug(f"ℹ️ [Data] حذف {initial_len - len(df)} صف للزوج {symbol} بسبب NaN.")
        if df.empty:
            logger.warning(f"⚠️ [Data] DataFrame للزوج {symbol} فارغ بعد معالجة NaN.")
            return None
        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب بيانات الزوج {symbol}: {e}")
        return None

def fetch_recent_volume(symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=15)
        if not klines:
            logger.warning(f"⚠️ [Data] لا توجد شموع 1m للزوج {symbol}.")
            return 0.0
        volume = sum(float(k[7]) for k in klines if len(k) > 7 and k[7])
        return volume
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب الحجم للزوج {symbol}: {e}")
        return 0.0

# ---------------------- دمج Gemini API ----------------------
def get_gemini_volume(pair):
    try:
        url = f"https://api.gemini.com/v1/pubticker/{pair}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        volume = float(data.get("volume", 0.0))
        logger.info(f"✅ [Gemini] حجم التداول للزوج {pair}: {volume:.2f}")
        return volume
    except Exception as e:
        logger.error(f"❌ [Gemini] خطأ في جلب بيانات {pair}: {e}")
        return 0.0

def calculate_market_dominance():
    btc_volume = get_gemini_volume("BTCUSD")
    eth_volume = get_gemini_volume("ETHUSD")
    total_volume = btc_volume + eth_volume
    if total_volume == 0:
        logger.warning("⚠️ [Gemini] إجمالي حجم التداول صفر.")
        return 0.0, 0.0
    btc_dominance = (btc_volume / total_volume) * 100
    eth_dominance = (eth_volume / total_volume) * 100
    logger.info(f"✅ [Gemini] سيطرة BTC: {btc_dominance:.2f}%, ETH: {eth_dominance:.2f}%")
    return btc_dominance, eth_dominance

# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_message(chat_id_target, text, reply_markup=None, parse_mode='Markdown', disable_web_page_preview=True, timeout=15):
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
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"❌ [Telegram] خطأ في إرسال الرسالة إلى {chat_id_target}: {e}")
        raise

def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    try:
        entry_price = signal['entry_price']
        target_price = signal['initial_target']
        stop_loss_price = signal['initial_stop_loss']
        if entry_price <= 0:
            logger.error(f"❌ [Telegram] سعر الدخول غير صالح للزوج {signal['symbol']}.")
            return
        profit_pct = ((target_price / entry_price) - 1) * 100
        loss_pct = ((stop_loss_price / entry_price) - 1) * 100
        profit_usdt = TRADE_VALUE * (profit_pct / 100)
        loss_usdt = TRADE_VALUE * (loss_pct / 100)
        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
        fng_value, fng_label = get_fear_greed_index()
        safe_symbol = signal['symbol'].replace('_', '\\_').replace('*', '\\*')
        message = (
            f"🚀 **hamza توصية تداول جديدة** 🚀\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **سعر الدخول:** `${entry_price:.8f}`\n"
            f"🎯 **الهدف:** `${target_price:.8f}` ({profit_pct:+.2f}% / {profit_usdt:+.2f} USDT)\n"
            f"🛑 **وقف الخسارة:** `${stop_loss_price:.8f}` ({loss_pct:.2f}% / {loss_usdt:.2f} USDT)\n"
            f"⏱ **الفريم:** {timeframe}\n"
            f"💧 **السيولة (آخر 15د):** {volume:,.0f} USDT\n"
            f"💰 **قيمة الصفقة:** ${TRADE_VALUE}\n"
            f"——————————————\n"
            f"🌍 **ظروف السوق:**\n"
            f"   - سيطرة BTC: {btc_dominance:.2f}%\n"
            f"   - سيطرة ETH: {eth_dominance:.2f}%\n"
            f"   - مؤشر الخوف/الجشع: {fng_value:.0f} ({fng_label})\n"
            f"——————————————\n"
            f"⏰ {timestamp} (توقيت +3)"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }
        send_telegram_message(chat_id, message, reply_markup=reply_markup)
        logger.info(f"✅ [Telegram] تم إرسال تنبيه التوصية للزوج {signal['symbol']}.")
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال تنبيه التوصية للزوج {signal['symbol']}: {e}")

def send_telegram_update(message, chat_id_override=None):
    target_chat = chat_id_override if chat_id_override else chat_id
    try:
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }
        send_telegram_message(target_chat, message, reply_markup=reply_markup)
        logger.info(f"✅ [Telegram] تم إرسال رسالة التحديث إلى {target_chat}.")
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال رسالة التحديث إلى {target_chat}: {e}")

# ---------------------- إرسال تقرير الأداء ----------------------
def send_report(target_chat_id):
    logger.info(f"⏳ [Report] إنشاء تقرير الأداء للدردشة: {target_chat_id}")
    report_message = "⚠️ فشل إنشاء تقرير الأداء."
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]
        cur.execute("""
            SELECT achieved_target, hit_stop_loss, profitable_stop_loss, profit_percentage
            FROM signals WHERE closed_at IS NOT NULL
        """)
        closed_signals = cur.fetchall()
        total_closed_trades = len(closed_signals)
        if total_closed_trades == 0:
            report_message = f"📊 **تقرير الأداء**\n\nلا توجد صفقات مغلقة.\n⏳ التوصيات النشطة: {active_count}"
            send_telegram_update(report_message, chat_id_override=target_chat_id)
            return
        successful_target_hits = sum(1 for s in closed_signals if s[0])
        profitable_sl_hits = sum(1 for s in closed_signals if s[1] and s[2])
        losing_sl_hits = sum(1 for s in closed_signals if s[1] and not s[2])
        total_profit_usd = 0
        total_loss_usd = 0
        profit_percentages = []
        loss_percentages = []
        for signal in closed_signals:
            profit_pct = signal[3]
            if profit_pct is not None:
                trade_result_usd = TRADE_VALUE * (profit_pct / 100)
                if trade_result_usd > 0:
                    total_profit_usd += trade_result_usd
                    profit_percentages.append(profit_pct)
                else:
                    total_loss_usd += trade_result_usd
                    loss_percentages.append(profit_pct)
        net_profit_usd = total_profit_usd + total_loss_usd
        win_rate = (successful_target_hits + profitable_sl_hits) / total_closed_trades * 100 if total_closed_trades > 0 else 0
        avg_profit_pct = np.mean(profit_percentages) if profit_percentages else 0
        avg_loss_pct = np.mean(loss_percentages) if loss_percentages else 0
        profit_factor = abs(total_profit_usd / total_loss_usd) if total_loss_usd != 0 else float('inf')
        timestamp = (datetime.utcnow() + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
        report_message = (
            f"📊 **Hamza تقرير أداء البوت** ({timestamp} توقيت +3)\n"
            f"——————————————\n"
            f"ملخص الصفقات المغلقة ({total_closed_trades}):\n"
            f"  ✅ تحقيق الهدف: {successful_target_hits}\n"
            f"  📈 وقف خسارة رابح: {profitable_sl_hits}\n"
            f"  📉 وقف خسارة خاسر: {losing_sl_hits}\n"
            f"  📊 معدل الربح: {win_rate:.2f}%\n"
            f"——————————————\n"
            f"الأداء المالي:\n"
            f"  💰 إجمالي الربح: +{total_profit_usd:.2f} USDT\n"
            f"  💸 إجمالي الخسارة: {total_loss_usd:.2f} USDT\n"
            f"  💵 صافي الربح/الخسارة: {net_profit_usd:+.2f} USDT\n"
            f"  🎯 متوسط ربح الصفقة: {avg_profit_pct:+.2f}%\n"
            f"  🛑 متوسط خسارة الصفقة: {avg_loss_pct:.2f}%\n"
            f"  ⚖️ معامل الربح: {profit_factor:.2f}\n"
            f"——————————————\n"
            f"التوصيات النشطة: {active_count}"
        )
        send_telegram_update(report_message, chat_id_override=target_chat_id)
        logger.info(f"✅ [Report] تم إرسال تقرير الأداء إلى {target_chat_id}.")
    except Exception as e:
        logger.error(f"❌ [Report] خطأ أثناء إنشاء التقرير: {e}", exc_info=True)

# ---------------------- تتبع التوصيات ----------------------
def track_signals():
    logger.info("🔄 [Tracker] بدء تتبع التوصيات...")
    while True:
        try:
            check_db_connection()
            cur.execute("""
                SELECT id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active
                FROM signals
                WHERE closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            if not active_signals:
                time.sleep(20)
                continue
            logger.info(f"🔍 [Tracker] تتبع {len(active_signals)} توصية نشطة...")
            for signal_data in active_signals:
                signal_id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active = signal_data
                current_price = None
                if symbol not in ticker_data or ticker_data[symbol].get('c') is None:
                    logger.warning(f"⚠️ [Tracker] لا توجد بيانات سعر للزوج {symbol}.")
                    continue
                try:
                    current_price = float(ticker_data[symbol]['c'])
                    if current_price <= 0:
                        logger.warning(f"⚠️ [Tracker] السعر غير صالح للزوج {symbol}.")
                        continue
                except Exception as e:
                    logger.warning(f"⚠️ [Tracker] خطأ في قراءة السعر للزوج {symbol}: {e}")
                    continue
                if current_price >= current_target:
                    profit_pct = ((current_target / entry_price) - 1) * 100
                    profit_usdt = TRADE_VALUE * (profit_pct / 100)
                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*')
                    msg = (f"✅ **تحقيق الهدف!**\n"
                           f"📈 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                           f"💰 أغلق عند: ${current_price:.8f}\n"
                           f"📊 الربح: +{profit_pct:.2f}% ({profit_usdt:+.2f} USDT)")
                    try:
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET achieved_target = TRUE, closed_at = NOW(), profit_percentage = %s
                            WHERE id = %s AND closed_at IS NULL
                        """, (profit_pct, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Tracker] إغلاق توصية {symbol} (ID: {signal_id}) بتحقيق الهدف.")
                    except Exception as update_err:
                        logger.error(f"❌ [Tracker] خطأ في تحديث توصية {signal_id}: {update_err}")
                        if conn and not conn.closed:
                            conn.rollback()
                    continue
                elif current_price <= current_stop_loss:
                    loss_pct = ((current_stop_loss / entry_price) - 1) * 100
                    loss_usdt = TRADE_VALUE * (loss_pct / 100)
                    stop_type_msg = "وقف خسارة رابح" if current_stop_loss > entry_price else "وقف خسارة"
                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*')
                    msg = (f"🛑 **{stop_type_msg}**\n"
                           f"📉 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                           f"💰 أغلق عند: ${current_price:.8f}\n"
                           f"📊 النتيجة: {loss_pct:.2f}% ({loss_usdt:.2f} USDT)")
                    try:
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s
                            WHERE id = %s AND closed_at IS NULL
                        """, (loss_pct, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Tracker] إغلاق توصية {symbol} (ID: {signal_id}) بواسطة {stop_type_msg}.")
                    except Exception as update_err:
                        logger.error(f"❌ [Tracker] خطأ في تحديث توصية {signal_id}: {update_err}")
                        if conn and not conn.closed:
                            conn.rollback()
                    continue
                # تحديث وقف الخسارة المتحرك إذا توفر بيانات كافية
                df_track = fetch_historical_data(symbol, interval=SIGNAL_TRACKING_TIMEFRAME, days=SIGNAL_TRACKING_LOOKBACK_DAYS)
                if df_track is None or df_track.empty or len(df_track) < 20:
                    logger.warning(f"⚠️ [Tracker] بيانات {SIGNAL_TRACKING_TIMEFRAME} غير كافية للزوج {symbol}.")
                else:
                    df_track = calculate_atr_indicator(df_track, period=14)
                    if 'atr' not in df_track.columns or pd.isna(df_track['atr'].iloc[-1]):
                        logger.warning(f"⚠️ [Tracker] فشل حساب ATR للزوج {symbol}.")
                    else:
                        current_atr = df_track['atr'].iloc[-1]
                        if current_atr > 0:
                            current_gain_pct = (current_price - entry_price) / entry_price
                            if current_gain_pct >= TRAILING_STOP_ACTIVATION_PROFIT_PCT:
                                potential_new_stop_loss = current_price - (TRAILING_STOP_ATR_MULTIPLIER * current_atr)
                                if potential_new_stop_loss > current_stop_loss:
                                    new_stop_loss = potential_new_stop_loss
                                    logger.info(f"  => [Tracker] تحديث وقف خسارة متحرك للزوج {symbol} (ID: {signal_id}).")
                                    update_msg = (
                                        f"🔄 **تحديث وقف الخسارة (متحرك)**\n"
                                        f"📈 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                                        f"   - الوقف القديم: ${current_stop_loss:.8f}\n"
                                        f"   - الوقف الجديد: ${new_stop_loss:.8f}"
                                    )
                                    try:
                                        send_telegram_update(update_msg)
                                        cur.execute("""
                                            UPDATE signals
                                            SET current_stop_loss = %s, is_trailing_active = TRUE
                                            WHERE id = %s AND closed_at IS NULL
                                        """, (new_stop_loss, signal_id))
                                        conn.commit()
                                        logger.info(f"✅ [Tracker] وقف خسارة محدث للزوج {symbol} (ID: {signal_id}).")
                                    except Exception as update_err:
                                        logger.error(f"❌ [Tracker] خطأ في تحديث وقف الخسارة لتوصية {signal_id}: {update_err}")
                                        if conn and not conn.closed:
                                            conn.rollback()
            time.sleep(30)
        except Exception as e:
            logger.error(f"❌ [Tracker] خطأ أثناء تتبع التوصيات: {e}", exc_info=True)
            time.sleep(60)

# ---------------------- تحليل السوق ----------------------
def analyze_market():
    logger.info("==========================================")
    logger.info(" H [Market Analysis] بدء تحليل السوق...")
    if not can_generate_new_recommendation():
        logger.info(" H [Market Analysis] تم الوصول للحد الأقصى للتوصيات المفتوحة.")
        return
    btc_dominance, eth_dominance = calculate_market_dominance()
    if btc_dominance is None or eth_dominance is None:
        btc_dominance, eth_dominance = 0.0, 0.0
    symbols_to_analyze = get_crypto_symbols()
    if not symbols_to_analyze:
        logger.warning("⚠️ [Market Analysis] قائمة الرموز فارغة.")
        return
    logger.info(f" H [Market Analysis] تحليل {len(symbols_to_analyze)} زوج عملات...")
    generated_signals_count = 0
    processed_symbols_count = 0
    for symbol in symbols_to_analyze:
        processed_symbols_count += 1
        if not can_generate_new_recommendation():
            logger.info(" H [Market Analysis] تم الوصول للحد الأقصى للتوصيات المفتوحة.")
            break
        try:
            check_db_connection()
            cur.execute("SELECT COUNT(*) FROM signals WHERE symbol = %s AND closed_at IS NULL", (symbol,))
            if cur.fetchone()[0] > 0:
                continue
        except Exception as e:
            logger.error(f"❌ [Market Analysis] خطأ DB للزوج {symbol}: {e}")
            continue
        df_signal_gen = fetch_historical_data(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
        if df_signal_gen is None or df_signal_gen.empty:
            continue
        signal = generate_signal_using_freqtrade_strategy(df_signal_gen, symbol)
        if signal:
            try:
                cur.execute("""
                    INSERT INTO signals (symbol, entry_price, initial_target, initial_stop_loss, current_target, current_stop_loss, r2_score, volume_15m)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (signal['symbol'], signal['entry_price'], signal['initial_target'], signal['initial_stop_loss'],
                      signal['current_target'], signal['current_stop_loss'], signal['r2_score'], fetch_recent_volume(symbol)))
                conn.commit()
                generated_signals_count += 1
                volume = fetch_recent_volume(symbol)
                send_telegram_alert(signal, volume, btc_dominance, eth_dominance, SIGNAL_GENERATION_TIMEFRAME)
            except Exception as insert_err:
                logger.error(f"❌ [Market Analysis] خطأ في حفظ إشارة الزوج {symbol}: {insert_err}")
                if conn and not conn.closed:
                    conn.rollback()
    logger.info(f"✅ [Market Analysis] توليد {generated_signals_count} إشارة من {processed_symbols_count} زوج.")

def can_generate_new_recommendation():
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]
        return active_count < MAX_OPEN_TRADES
    except Exception as e:
        logger.error(f"❌ [Gate] خطأ في التحقق من عدد التوصيات: {e}")
        return False

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return f"🚀 بوت توصيات التداول الإصدار 4.3 (Hazem Mod) - الخدمة تعمل. {datetime.utcnow().isoformat()}Z", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json()
        if not update:
            logger.warning("⚠️ [Webhook] تحديث فارغ.")
            return '', 200
        logger.info("ℹ️ [Webhook] تحديث مستلم.")
        # يمكن تضمين منطق المعالجة هنا حسب الحاجة
        return '', 200
    except Exception as e:
        logger.error(f"❌ [Webhook] خطأ في معالجة التحديث: {e}", exc_info=True)
        return 'Internal Server Error', 500

def set_telegram_webhook():
    # يمكن تضمين كود تعيين webhook لتليجرام هنا إذا لزم الأمر
    pass

# ---------------------- تشغيل الخدمات الخلفية ----------------------
def start_background_services():
    # بدء WebSocket
    ws_thread = Thread(target=run_ticker_socket_manager, name="WebSocketThread", daemon=True)
    ws_thread.start()
    logger.info("✅ [Main] تم بدء خيط WebSocket.")

    # بدء تتبع التوصيات
    tracker_thread = Thread(target=track_signals, name="TrackerThread", daemon=True)
    tracker_thread.start()
    logger.info("✅ [Main] تم بدء خيط تتبع التوصيات.")

    # بدء جدولة تحليل السوق
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(analyze_market, 'interval', minutes=5, id='market_analyzer', replace_existing=True, misfire_grace_time=60)
    scheduler.start()
    logger.info("✅ [Main] تم بدء APScheduler.")

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    try:
        init_db()
    except Exception as e:
        logger.critical(f"❌ [Main] فشل تهيئة قاعدة البيانات: {e}")
        exit(1)
    set_telegram_webhook()
    start_background_services()
    logger.info("✅ النظام متصل ويعمل الآن.")

    # تشغيل Flask في خيط منفصل مع الربط بالمنفذ المحدد
    port = int(os.environ.get("PORT", 5000))
    flask_thread = Thread(target=lambda: app.run(host="0.0.0.0", port=port), name="FlaskThread")
    flask_thread.start()

    # الانتظار في الخيط الرئيسي حتى انتهاء خيط Flask لضمان بقاء الخدمة live
    flask_thread.join()
