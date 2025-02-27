#!/usr/bin/env python
import time
import os
import pandas as pd
import numpy as np
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from binance.client import Client
from binance import ThreadedWebsocketManager
from flask import Flask, request
from threading import Thread
import logging
import requests
import json
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from datetime import datetime
from cachetools import TTLCache

# ---------------------- إعدادات التسجيل المحسنة ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    handlers=[logging.FileHandler('crypto_bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------------------- تحميل المتغيرات البيئية ----------------------
api_key = config('BINANCE_API_KEY')
api_secret = config('BINANCE_API_SECRET')
telegram_token = config('TELEGRAM_BOT_TOKEN')
chat_id = config('TELEGRAM_CHAT_ID')
db_url = config('DATABASE_URL')

# تعيين المنطقة الزمنية
timezone = pytz.timezone('Asia/Riyadh')

# قيمة الصفقة الثابتة للتوصيات (بـ USDT)
TRADE_VALUE = 10

# ---------------------- متغيرات التحكم ----------------------
last_price_update = {}

# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
db_pool = SimpleConnectionPool(1, 5, dsn=db_url)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id SERIAL PRIMARY KEY,
                symbol TEXT,
                entry_price DOUBLE PRECISION,
                target DOUBLE PRECISION,
                stop_loss DOUBLE PRECISION,
                dynamic_stop_loss DOUBLE PRECISION,
                r2_score DOUBLE PRECISION,
                volume_15m DOUBLE PRECISION,
                risk_reward_ratio DOUBLE PRECISION,
                achieved_target BOOLEAN DEFAULT FALSE,
                hit_stop_loss BOOLEAN DEFAULT FALSE,
                closed_at TIMESTAMP,
                sent_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_symbol_time UNIQUE (symbol, sent_at)
            )
        """)
        conn.commit()
        logger.info("تم تهيئة قاعدة البيانات بنجاح")
    except Exception as e:
        logger.error(f"فشل تهيئة قاعدة البيانات: {e}")
        raise
    finally:
        release_db_connection(conn)

def check_db_connection():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.commit()
        release_db_connection(conn)
        logger.info("فحص الاتصال بقاعدة البيانات: ناجح")
    except Exception as e:
        logger.warning("إعادة الاتصال بقاعدة البيانات بسبب: %s", e)
        try:
            global db_pool
            db_pool = SimpleConnectionPool(1, 5, dsn=db_url)
        except Exception as ex:
            logger.error(f"فشل إعادة الاتصال: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
client = Client(api_key, api_secret)

# ---------------------- استخدام WebSocket لتحديث بيانات التيكر ----------------------
ticker_data = {}
historical_data_cache = TTLCache(maxsize=100, ttl=300)  # 5 دقائق

def handle_ticker_message(msg):
    try:
        if isinstance(msg, list):
            for m in msg:
                symbol = m.get('s')
                if symbol:
                    ticker_data[symbol] = m
                    last_price_update[symbol] = float(m.get('c', 0))
        else:
            symbol = msg.get('s')
            if symbol:
                ticker_data[symbol] = msg
                last_price_update[symbol] = float(msg.get('c', 0))
    except Exception as e:
        logger.error(f"خطأ في handle_ticker_message: {e}")

def run_ticker_socket_manager():
    while True:
        try:
            twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
            twm.start()
            twm.start_miniticker_socket(callback=handle_ticker_message)
            logger.info("تم تشغيل WebSocket لتحديث التيكر")
            twm.join()
        except Exception as e:
            logger.error(f"خطأ في WebSocket، إعادة المحاولة: {e}")
            time.sleep(5)

# ---------------------- دوال حساب المؤشرات الفنية ----------------------
def calculate_ema(series, span):
    ema = series.ewm(span=span, adjust=False).mean()
    logger.debug(f"حساب EMA بطول {span}: {ema.iloc[-1]:.4f}")
    return ema

def calculate_rsi_indicator(df, period=7):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    avg_loss = avg_loss.replace(0, 1e-10)
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    logger.debug(f"حساب RSI ({period}): {rsi.iloc[-1]:.2f}")
    return rsi

def calculate_atr_indicator(df, period=7):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    logger.debug(f"حساب ATR ({period}): {df['atr'].iloc[-1]:.8f}")
    return df

def calculate_macd_indicator(df, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(df['close'], fast)
    ema_slow = calculate_ema(df['close'], slow)
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    df['macd'] = macd
    df['macd_signal'] = signal_line
    logger.debug(f"حساب MACD: {macd.iloc[-1]:.4f}, Signal: {signal_line.iloc[-1]:.4f}")
    return df

def calculate_stochastic(df, k_period=14, d_period=3):
    lowest_low = df['low'].rolling(window=k_period).min()
    highest_high = df['high'].rolling(window=k_period).max()
    df['%K'] = 100 * ((df['close'] - lowest_low) / (highest_high - lowest_low))
    df['%D'] = df['%K'].rolling(window=d_period).mean()
    logger.debug(f"حساب Stochastic: %K={df['%K'].iloc[-1]:.2f}, %D={df['%D'].iloc[-1]:.2f}")
    return df

# ---------------------- تعريف استراتيجية محسّنة للتداول اليومي ----------------------
class ImprovedDayTradingStrategy:
    stoploss = -0.015
    minimal_roi = {"0": 0.008, "30": 0.005, "60": 0.003}  # ROI متدرج حسب الوقت

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        # إضافة مؤشرات متقدمة
        df['volume_change'] = df['volume'].pct_change().rolling(window=3).mean()
        df['price_momentum'] = df['close'].diff(3).rolling(window=5).mean()
        df['volatility'] = df['high'].div(df['low']).rolling(window=10).mean()
        
        # المؤشرات الحالية مع تحسينات
        df['ema5'] = calculate_ema(df['close'], 5)
        df['ema13'] = calculate_ema(df['close'], 13)
        df['ema21'] = calculate_ema(df['close'], 21)
        df['rsi'] = calculate_rsi_indicator(df, period=7)
        df['rsi_divergence'] = df['rsi'].diff(3)
        
        # حساب Bollinger Bands
        df['ma20'] = df['close'].rolling(window=20).mean()
        std20 = df['close'].rolling(window=20).std()
        df['upper_band'] = df['ma20'] + (std20 * 2)
        df['lower_band'] = df['ma20'] - (std20 * 2)
        
        # باقي المؤشرات
        df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
        df = calculate_atr_indicator(df, period=7)
        df = calculate_macd_indicator(df)
        df = calculate_stochastic(df)
        df['resistance'] = df['high'].rolling(window=20).max()
        df['support'] = df['low'].rolling(window=20).min()
        
        logger.info("تم حساب المؤشرات الفنية بنجاح")
        return df

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema5'] > df['ema13']) &
            (df['rsi'].between(25, 75)) &
            (df['macd'] > df['macd_signal']) &
            (df['%K'] > df['%D'])
        )
        df.loc[conditions, 'buy'] = 1
        logger.debug(f"شروط الشراء الأخيرة: {conditions.iloc[-1]}")
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema5'] < df['ema13']) |
            (df['rsi'] > 80) |
            (df['macd'] < df['macd_signal']) |
            (df['%K'] < df['%D'])
        )
        df.loc[conditions, 'sell'] = 1
        logger.debug(f"شروط البيع الأخيرة: {conditions.iloc[-1]}")
        return df

# ---------------------- دالة توليد الإشارة المحسنة للتداول اليومي ----------------------
def generate_improved_signal(df, symbol):
    if len(df) < 50:
        logger.info(f"{symbol}: تم رفض التوصية - البيانات غير كافية (عدد الصفوف: {len(df)})")
        return None

    strategy = ImprovedDayTradingStrategy()
    df = strategy.populate_indicators(df)
    df = strategy.populate_buy_trend(df)
    last_row = df.iloc[-1]
    
    if last_row.get('buy', 0) != 1:
        logger.info(f"{symbol}: تم رفض التوصية - شروط الشراء غير مستوفاة")
        return None
    
    candle_pattern = analyze_candle_pattern(df)
    if not candle_pattern['bullish']:
        logger.info(f"{symbol}: تم رفض التوصية - نمط الشموع غير صاعد (bullish not detected)")
        return None
    
    market_volatility = df['atr'].iloc[-1] / df['close'].iloc[-1]
    current_price = last_row['close']
    atr = last_row['atr']
    
    resistance = last_row['resistance']
    support = last_row['support']
    price_range = resistance - support
    
    recent_highs = df['high'][-20:].values
    recent_highs.sort()
    fib_levels = [current_price + price_range * level for level in [0.382, 0.618, 0.786]]
    target = select_best_target_level(current_price, fib_levels, recent_highs)
    
    volatility_factor = min(1.5, max(1.0, 1.0 + market_volatility * 10))
    stop_loss = current_price - (atr * volatility_factor)
    stop_loss = max(stop_loss, support * 1.005)
    
    risk = current_price - stop_loss
    reward = target - current_price
    risk_reward_ratio = reward / risk if risk > 0 else 0
    
    confidence_score = calculate_confidence_score(last_row, candle_pattern, risk_reward_ratio, market_volatility)
    
    if risk_reward_ratio < 1.5 or confidence_score < 60:
        logger.info(f"{symbol}: تم رفض التوصية - نسبة المخاطرة/العائد ({risk_reward_ratio:.2f}) أو درجة الثقة ({confidence_score}) غير كافية")
        return None
    if reward / current_price < 0.01:
        logger.info(f"{symbol}: تم رفض التوصية - الربح المتوقع ({reward/current_price:.4f}) أقل من الحد الأدنى")
        return None
    
    dynamic_stop_loss = stop_loss
    signal = {
        'symbol': symbol,
        'price': float(format(current_price, '.8f')),
        'target': float(format(target, '.8f')),
        'stop_loss': float(format(stop_loss, '.8f')),
        'dynamic_stop_loss': float(format(dynamic_stop_loss, '.8f')),
        'strategy': 'improved_day_trading',
        'confidence': confidence_score,
        'market_condition': 'volatile' if market_volatility > 0.02 else 'stable',
        'indicators': {
            'ema5': last_row['ema5'],
            'ema13': last_row['ema13'],
            'rsi': last_row['rsi'],
            'vwap': last_row['vwap'],
            'atr': atr,
            'macd': last_row['macd'],
            'macd_signal': last_row['macd_signal'],
            '%K': last_row['%K'],
            '%D': last_row['%D'],
            'resistance': resistance,
            'support': support
        },
        'trade_value': TRADE_VALUE,
        'risk_reward_ratio': risk_reward_ratio
    }
    
    logger.info(f"{symbol}: تم توليد توصية بنجاح - السعر الحالي: {current_price}, الهدف: {target}, وقف الخسارة: {stop_loss}")
    return signal

def select_best_target_level(current_price, fib_levels, recent_highs):
    for level in fib_levels:
        for high in recent_highs:
            if abs(high - level) / level < 0.01:
                return min(level, high * 0.998)
    return fib_levels[1]

def analyze_candle_pattern(df):
    last_candles = df.iloc[-3:].copy()
    last_candles['body'] = abs(last_candles['close'] - last_candles['open'])
    last_candles['upper_shadow'] = last_candles['high'] - last_candles[['open', 'close']].max(axis=1)
    last_candles['lower_shadow'] = last_candles[['open', 'close']].min(axis=1) - last_candles['low']
    
    doji = last_candles.iloc[-1]['body'] < (last_candles.iloc[-1]['high'] - last_candles.iloc[-1]['low']) * 0.1
    hammer = (last_candles.iloc[-1]['lower_shadow'] > last_candles.iloc[-1]['body'] * 2 and
              last_candles.iloc[-1]['upper_shadow'] < last_candles.iloc[-1]['body'] * 0.5)
    bullish_engulfing = (last_candles.iloc[-2]['close'] < last_candles.iloc[-2]['open'] and
                         last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open'] and
                         last_candles.iloc[-1]['open'] < last_candles.iloc[-2]['close'] and
                         last_candles.iloc[-1]['close'] > last_candles.iloc[-2]['open'])
    
    pattern = {
        'doji': doji,
        'hammer': hammer,
        'bullish_engulfing': bullish_engulfing,
        'bullish': hammer or bullish_engulfing or (not doji and last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open'])
    }
    logger.debug(f"تحليل الشموع: {pattern}")
    return pattern

def calculate_confidence_score(indicators, candle_pattern, risk_reward_ratio, volatility):
    score = 60
    if indicators['rsi'] < 40:
        score += 5
    elif indicators['rsi'] > 65:
        score -= 10
        
    if indicators['macd'] > indicators['macd_signal']:
        score += 7
    
    if indicators['ema5'] > indicators['ema13'] and indicators['ema13'] > indicators.get('ema21', 0):
        score += 8
        
    if candle_pattern['bullish_engulfing']:
        score += 10
    elif candle_pattern['hammer']:
        score += 8
    
    if risk_reward_ratio > 3:
        score += 10
    elif risk_reward_ratio > 2:
        score += 5
    
    if volatility < 0.01:
        score += 5
    elif volatility > 0.03:
        score -= 8
        
    final_score = max(0, min(100, score))
    logger.debug(f"درجة الثقة المحسوبة: {final_score}")
    return final_score

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

def run_flask():
    app.run(host='0.0.0.0', port=5000)

@app.route('/')
def home():
    return "نظام توصيات التداول اليومي يعمل بكفاءة 🚀", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update or "callback_query" not in update:
        logger.warning("تحديث Webhook غير صالح")
        return '', 400
    callback_data = update["callback_query"].get("data", "")
    chat_id_callback = update["callback_query"]["message"]["chat"].get("id", "")
    if callback_data == "get_report" and chat_id_callback:
        send_report(chat_id_callback)
        answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
        requests.post(answer_url, json={"callback_query_id": update["callback_query"]["id"]})
    return '', 200

def send_report(chat_id_callback):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT symbol, entry_price, target, stop_loss, dynamic_stop_loss, sent_at, closed_at 
            FROM signals 
            WHERE achieved_target = TRUE
            ORDER BY sent_at DESC
            LIMIT 10
        """)
        winning_trades = cur.fetchall()
        
        cur.execute("""
            SELECT symbol, entry_price, target, stop_loss, dynamic_stop_loss, sent_at, closed_at 
            FROM signals 
            WHERE hit_stop_loss = TRUE
            ORDER BY sent_at DESC
            LIMIT 10
        """)
        losing_trades = cur.fetchall()
        
        cur.execute("""
            SELECT symbol, entry_price, target, stop_loss, dynamic_stop_loss, sent_at 
            FROM signals 
            WHERE closed_at IS NULL
            ORDER BY sent_at DESC
            LIMIT 10
        """)
        open_trades = cur.fetchall()
        release_db_connection(conn)
        
        report_message = (
            "📊✨ **تقرير أداء التداول الشامل** ✨📊\n"
            f"🕒 محدث بتاريخ: {datetime.now(timezone).strftime('%Y-%m-%d %H:%M')}\n\n"
        )
        
        report_message += "🏆 **الصفقات الرابحة** 🏆\n"
        if winning_trades:
            for trade in winning_trades:
                symbol, entry, target, stop_loss, dyn_stop, sent_at, closed_at = trade
                sent_at_str = sent_at.strftime('%Y-%m-%d %H:%M')
                closed_at_str = closed_at.strftime('%Y-%m-%d %H:%M') if closed_at else "غير محدد"
                profit_percentage = ((target / entry) - 1) * 100
                profit_amount = TRADE_VALUE * ((target / entry) - 1)
                report_message += (
                    f"🌟 **{symbol}**\n"
                    f"  - سعر الدخول: ${entry:.8f}\n"
                    f"  - الهدف: ${target:.8f}\n"
                    f"  - وقف الخسارة: ${stop_loss:.8f}\n"
                    f"  - نسبة الربح: **+{profit_percentage:.2f}%**\n"
                    f"  - الربح المحقق: **${profit_amount:.2f}**\n"
                    f"  - تاريخ الإغلاق: {closed_at_str}\n"
                    f"  - تاريخ الإرسال: {sent_at_str}\n"
                    "----------------------------------------\n"
                )
        else:
            report_message += "🤷‍♂️ لا توجد صفقات رابحة بعد.\n\n"
        
        report_message += "❌ **الصفقات الخاسرة** ❌\n"
        if losing_trades:
            for trade in losing_trades:
                symbol, entry, target, stop_loss, dyn_stop, sent_at, closed_at = trade
                sent_at_str = sent_at.strftime('%Y-%m-%d %H:%M')
                closed_at_str = closed_at.strftime('%Y-%m-%d %H:%M') if closed_at else "غير محدد"
                loss_percentage = abs(((stop_loss / entry) - 1) * 100)
                loss_amount = TRADE_VALUE * abs(((stop_loss / entry) - 1))
                report_message += (
                    f"🔴 **{symbol}**\n"
                    f"  - سعر الدخول: ${entry:.8f}\n"
                    f"  - وقف الخسارة: ${stop_loss:.8f}\n"
                    f"  - نسبة الخسارة: **-{loss_percentage:.2f}%**\n"
                    f"  - الخسارة: **${loss_amount:.2f}**\n"
                    f"  - تاريخ الإغلاق: {closed_at_str}\n"
                    f"  - تاريخ الإرسال: {sent_at_str}\n"
                    "----------------------------------------\n"
                )
        else:
            report_message += "✅ لا توجد صفقات خاسرة بعد.\n\n"
        
        report_message += "⏳ **الصفقات المفتوحة** ⏳\n"
        if open_trades:
            for trade in open_trades:
                symbol, entry, target, stop_loss, dyn_stop, sent_at = trade
                sent_at_str = sent_at.strftime('%Y-%m-%d %H:%M')
                report_message += (
                    f"⏰ **{symbol}**\n"
                    f"  - سعر الدخول: ${entry:.8f}\n"
                    f"  - الهدف: ${target:.8f}\n"
                    f"  - وقف الخسارة: ${stop_loss:.8f}\n"
                    f"  - تاريخ الإرسال: {sent_at_str}\n"
                    "----------------------------------------\n"
                )
        else:
            report_message += "🕒 لا توجد صفقات مفتوحة حالياً.\n\n"
        
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            "chat_id": chat_id_callback,
            "text": report_message,
            "parse_mode": "Markdown"
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("تم إرسال التقرير الشامل بنجاح")
        else:
            logger.error(f"فشل إرسال التقرير: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في إرسال التقرير: {e}")

# ---------------------- وظائف تحليل البيانات ----------------------
def get_crypto_symbols():
    try:
        exchange_info = client.get_exchange_info()
        symbols = [
            s['symbol'] 
            for s in exchange_info['symbols'] 
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'
        ]
        filtered_symbols = []
        for symbol in symbols:
            volume = fetch_recent_volume(symbol)
            if volume > 50000:
                filtered_symbols.append(symbol)
        logger.info(f"تم جلب {len(filtered_symbols)} زوج USDT بعد الفلترة")
        return filtered_symbols
    except Exception as e:
        logger.error(f"خطأ في جلب الأزواج من Binance: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=3):
    cache_key = f"{symbol}_{interval}_{days}"
    if cache_key in historical_data_cache:
        logger.info(f"{symbol}: جلب بيانات تاريخية من الكاش")
        return historical_data_cache[cache_key]
    for attempt in range(3):
        try:
            klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                                                 'close_time', 'quote_volume', 'trades', 'taker_buy_base', 
                                                 'taker_buy_quote', 'ignore'])
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].astype(float)
            historical_data_cache[cache_key] = df
            logger.info(f"{symbol}: تم جلب {len(df)} صف من البيانات التاريخية")
            return df
        except Exception as e:
            logger.error(f"{symbol}: خطأ في جلب البيانات (محاولة {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    logger.error(f"{symbol}: فشل جلب البيانات بعد 3 محاولات")
    return None

def fetch_recent_volume(symbol):
    try:
        logger.info(f"{symbol}: جلب حجم السيولة في آخر 15 دقيقة")
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        return volume
    except Exception as e:
        logger.error(f"{symbol}: خطأ في جلب حجم السيولة: {e}")
        return 0

def get_market_dominance():
    try:
        logger.info("جلب نسب السيطرة على السوق من CoinGecko")
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        data = response.json().get("data", {})
        market_cap_percentage = data.get("market_cap_percentage", {})
        btc_dominance = market_cap_percentage.get("btc")
        eth_dominance = market_cap_percentage.get("eth")
        return btc_dominance, eth_dominance
    except Exception as e:
        logger.error(f"خطأ في get_market_dominance: {e}")
        return None, None

def send_telegram_alert(signal, volume, btc_dominance, eth_dominance):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        rtl_mark = "\u200F"
        message = (
            f"{rtl_mark}🌟 **توصية تداول ذهبية - {signal['symbol']}** 🌟\n"
            "----------------------------------------\n"
            f"💰 **سعر الدخول**: ${signal['price']}\n"
            f"🎯 **الهدف المتوقع**: ${signal['target']} (**+{profit}%**)\n"
            f"🛑 **وقف الخسارة**: ${signal['stop_loss']} (**{loss}%**)\n"
            f"🔄 **وقف الخسارة المتحرك**: ${signal['dynamic_stop_loss']}\n"
            f"⚖️ **نسبة المخاطرة/العائد**: **{signal['risk_reward_ratio']:.2f}**\n"
            "----------------------------------------\n"
            f"📈 **تحليل المؤشرات**:\n"
            f"   • RSI: **{signal['indicators']['rsi']:.2f}**\n"
            f"   • VWAP: **${signal['indicators']['vwap']:.4f}**\n"
            f"   • ATR: **{signal['indicators']['atr']:.8f}**\n"
            f"   • Stochastic %K: **{signal['indicators']['%K']:.2f}**\n"
            f"   • Stochastic %D: **{signal['indicators']['%D']:.2f}**\n"
            "----------------------------------------\n"
            f"💧 **السيولة (15 دق)**: **{volume:,.2f} USDT**\n"
            f"💵 **قيمة الصفقة**: **${TRADE_VALUE}**\n"
            f"📊 **نسب السيطرة على السوق**:\n"
            f"   • BTC: **{btc_dominance:.2f}%**\n"
            f"   • ETH: **{eth_dominance:.2f}%**\n"
            f"⏰ **وقت التوصية**: {datetime.now(timezone).strftime('%Y-%m-%d %H:%M')}"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض التقرير الشامل", "callback_data": "get_report"}]
            ]
        }
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown',
            'reply_markup': json.dumps(reply_markup)
        }
        for attempt in range(3):
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    logger.info(f"{signal['symbol']}: تم إرسال التوصية بنجاح")
                    return
                else:
                    logger.error(f"{signal['symbol']}: فشل إرسال التوصية: {response.text}")
            except Exception as e:
                logger.error(f"{signal['symbol']}: فشل إرسال التوصية (محاولة {attempt+1}): {e}")
                time.sleep(2 ** attempt)
        logger.error(f"{signal['symbol']}: فشل إرسال التوصية بعد 3 محاولات")
    except Exception as e:
        logger.error(f"خطأ في send_telegram_alert: {e}")

def send_telegram_alert_special(message):
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("تم إرسال تنبيه خاص بنجاح")
        else:
            logger.error(f"فشل إرسال التنبيه الخاص: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في send_telegram_alert_special: {e}")

# ---------------------- خدمة تتبع الإشارات مع وقف خسارة متحرك وتحديث الهدف ----------------------
def improved_track_signals():
    logger.info("بدء خدمة تتبع الإشارات المحسنة")
    
    while True:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, symbol, entry_price, target, stop_loss, dynamic_stop_loss, sent_at  
                FROM signals
                WHERE achieved_target = FALSE AND hit_stop_loss = FALSE AND closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss, dynamic_stop_loss, sent_at = signal
                current_price = last_price_update.get(symbol, None)
                if not current_price:
                    logger.debug(f"{symbol}: لم يتم تحديث السعر الحالي بعد")
                    continue
                
                df = fetch_historical_data(symbol, interval='5m', days=1)
                if df is None or len(df) < 20:
                    logger.info(f"{symbol}: تم تجاهل التوصية - البيانات التاريخية غير كافية")
                    continue
                    
                df = calculate_atr_indicator(df)
                current_atr = df['atr'].iloc[-1]
                
                time_in_trade = (datetime.now(timezone) - sent_at).total_seconds() / 3600
                price_change_pct = (current_price - entry) / entry * 100
                
                if current_price > entry:
                    pct_based_stop = entry + (current_price - entry) * 0.5
                    atr_based_stop = current_price - current_atr * 1.5
                    time_factor = min(0.8, time_in_trade / 24)
                    time_based_stop = entry + (current_price - entry) * time_factor
                    
                    if price_change_pct > 3:
                        fib_based_stop = entry + (current_price - entry) * 0.382
                    elif price_change_pct > 1:
                        fib_based_stop = entry + (current_price - entry) * 0.236
                    else:
                        fib_based_stop = stop_loss
                    
                    candidate_stops = [
                        dynamic_stop_loss,
                        pct_based_stop,
                        atr_based_stop,
                        time_based_stop,
                        fib_based_stop,
                        stop_loss
                    ]
                    
                    new_dynamic_stop_loss = max(candidate_stops)
                    
                    if new_dynamic_stop_loss > dynamic_stop_loss * 1.005:
                        cur.execute("UPDATE signals SET dynamic_stop_loss = %s WHERE id = %s", 
                                   (new_dynamic_stop_loss, signal_id))
                        conn.commit()
                        logger.info(f"{symbol}: تحديث وقف الخسارة المتحرك من {dynamic_stop_loss:.8f} إلى {new_dynamic_stop_loss:.8f}")
                        if new_dynamic_stop_loss > dynamic_stop_loss * 1.05:
                            msg = (
                                f"📊 **تحديث وقف الخسارة - {symbol}** 📊\n"
                                "----------------------------------------\n"
                                f"💰 سعر الدخول: **${entry:.8f}**\n"
                                f"💹 السعر الحالي: **${current_price:.8f}**\n"
                                f"🛡️ وقف الخسارة الجديد: **${new_dynamic_stop_loss:.8f}**\n"
                                f"📈 الربح المضمون: **+{((new_dynamic_stop_loss - entry) / entry * 100):.2f}%**\n"
                                f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                            )
                            send_telegram_alert_special(msg)
                else:
                    new_dynamic_stop_loss = stop_loss
                
                # تحديث الهدف بناءً على البيانات الحديثة وإرسال تنبيه عند تغييره
                try:
                    new_resistance = df['high'].rolling(window=20).max().iloc[-1]
                    new_support = df['low'].rolling(window=20).min().iloc[-1]
                    new_price_range = new_resistance - new_support
                    new_fib_levels = [current_price + new_price_range * level for level in [0.382, 0.618, 0.786]]
                    recent_highs = np.sort(df['high'].tail(20).values)
                    new_target = select_best_target_level(current_price, new_fib_levels, recent_highs)
                    if new_target and abs(new_target - target) / target > 0.01:
                        cur.execute("UPDATE signals SET target = %s WHERE id = %s", (new_target, signal_id))
                        conn.commit()
                        logger.info(f"{symbol}: تغيير الهدف من {target:.8f} إلى {new_target:.8f}")
                        msg = (
                            f"🔄 **تغيير الهدف - {symbol}**\n"
                            "----------------------------------------\n"
                            f"🎯 الهدف القديم: ${target:.8f}\n"
                            f"🎯 الهدف الجديد: ${new_target:.8f}\n"
                            f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        target = new_target
                except Exception as e:
                    logger.error(f"{symbol}: خطأ في إعادة حساب الهدف: {e}")
                
                if current_price >= target:
                    profit = ((current_price - entry) / entry) * 100
                    msg = (
                        f"🎉✨ **نجاح كبير! تحقيق الهدف - {symbol}** ✨🎉\n"
                        "----------------------------------------\n"
                        f"💰 الدخول: **${entry:.8f}**\n"
                        f"✅ الخروج: **${current_price:.8f}**\n"
                        f"📈 الربح المحقق: **+{profit:.2f}%**\n"
                        f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                    )
                    send_telegram_alert_special(msg)
                    cur.execute("UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                    conn.commit()
                elif current_price <= new_dynamic_stop_loss:
                    loss = ((current_price - entry) / entry) * 100
                    msg = (
                        f"⚠️ **تنبيه: تفعيل وقف الخسارة - {symbol}** ⚠️\n"
                        "----------------------------------------\n"
                        f"💰 الدخول: **${entry:.8f}**\n"
                        f"❌ الخروج: **${current_price:.8f}**\n"
                        f"📉 التغيير: **{loss:.2f}%**\n"
                        f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                    )
                    send_telegram_alert_special(msg)
                    cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                    conn.commit()
        except Exception as e:
            logger.error(f"خطأ في تتبع الإشارات: {e}")
            conn.rollback()
        finally:
            release_db_connection(conn)
        
        time.sleep(90)

# ---------------------- فحص الأزواج بشكل دوري ----------------------
def analyze_market():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_signals_count = cur.fetchone()[0]
        if active_signals_count >= 4:
            logger.info("الحد الأقصى للتوصيات النشطة (4) تم الوصول إليه")
            return
        btc_dominance, eth_dominance = get_market_dominance() or (0.0, 0.0)
        symbols = get_crypto_symbols()
        for symbol in symbols:
            df = fetch_historical_data(symbol)
            if df is None:
                logger.info(f"{symbol}: تم رفض التوصية - عدم توفر البيانات التاريخية")
                continue
            if len(df) < 100:
                logger.info(f"{symbol}: تم رفض التوصية - البيانات التاريخية غير كافية (عدد الصفوف: {len(df)})")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 50000:
                logger.info(f"{symbol}: تم رفض التوصية - السيولة ({volume_15m:,.2f} USDT) أقل من 50000")
                continue
            signal = generate_improved_signal(df, symbol)
            if signal:
                send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance)
                cur.execute("""
                    INSERT INTO signals 
                    (symbol, entry_price, target, stop_loss, dynamic_stop_loss, r2_score, volume_15m, risk_reward_ratio)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    signal['symbol'], signal['price'], signal['target'], signal['stop_loss'],
                    signal['dynamic_stop_loss'], signal.get('confidence', 100), volume_15m,
                    signal['risk_reward_ratio']
                ))
                conn.commit()
            else:
                logger.info(f"{symbol}: لم يتم توليد توصية")
            time.sleep(1)
    except Exception as e:
        logger.error(f"خطأ في analyze_market: {e}")
        conn.rollback()
    finally:
        release_db_connection(conn)

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    set_telegram_webhook_url = f"https://api.telegram.org/bot{telegram_token}/setWebhook?url=https://hamza-drs4.onrender.com/webhook"
    try:
        response = requests.get(set_telegram_webhook_url, timeout=10)
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"تم تسجيل webhook بنجاح: {res_json}")
        else:
            logger.error(f"فشل تسجيل webhook: {res_json}")
    except Exception as e:
        logger.error(f"استثناء أثناء تسجيل webhook: {e}")
    
    Thread(target=run_flask, daemon=True).start()
    Thread(target=improved_track_signals, daemon=True).start()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=10)
    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("إيقاف النظام")
