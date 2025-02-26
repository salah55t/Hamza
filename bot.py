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

# ---------------------- إعداد السجلات ----------------------
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

# قيمة الصفقة الثابتة (بالـ USDT)
TRADE_VALUE = 10

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
        logger.info("تهيئة قاعدة البيانات")
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
    except Exception as e:
        logger.error(f"خطأ في فحص الاتصال بقاعدة البيانات: {e}")
        try:
            global db_pool
            db_pool = SimpleConnectionPool(1, 5, dsn=db_url)
        except Exception as ex:
            logger.error(f"فشل إعادة الاتصال: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
client = Client(api_key, api_secret)

# ---------------------- تحديث التيكر عبر WebSocket ----------------------
ticker_data = {}
last_price_update = {}
historical_data_cache = TTLCache(maxsize=100, ttl=300)  # تخزين لمدة 5 دقائق

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
            twm.join()
        except Exception as e:
            logger.error(f"خطأ في WebSocket: {e}")
            time.sleep(5)

# ---------------------- دوال حساب المؤشرات الفنية المحسنة ----------------------
def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    try:
        df = df.copy()
        df['tr1'] = abs(df['high'] - df['low'])
        df['tr2'] = abs(df['high'] - df['close'].shift(1))
        df['tr3'] = abs(df['low'] - df['close'].shift(1))
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['atr'] = df['tr'].rolling(window=period).mean()
        df['plus_dm'] = 0.0
        df['minus_dm'] = 0.0
        df['high_diff'] = df['high'] - df['high'].shift(1)
        df['low_diff'] = df['low'].shift(1) - df['low']
        df.loc[(df['high_diff'] > df['low_diff']) & (df['high_diff'] > 0), 'plus_dm'] = df['high_diff']
        df.loc[(df['low_diff'] > df['high_diff']) & (df['low_diff'] > 0), 'minus_dm'] = df['low_diff']
        df['plus_di'] = 100 * (df['plus_dm'].rolling(window=period).mean() / df['atr'])
        df['minus_di'] = 100 * (df['minus_dm'].rolling(window=period).mean() / df['atr'])
        df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'])
        df['adx'] = df['dx'].rolling(window=period).mean()
        return df['adx']
    except Exception as e:
        logger.error(f"خطأ في حساب ADX: {e}")
        return pd.Series()

def calculate_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    try:
        df = df.copy()
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        raw_money_flow = typical_price * df['volume']
        positive_flow = pd.Series(0.0, index=df.index)
        negative_flow = pd.Series(0.0, index=df.index)
        price_difference = typical_price.diff()
        positive_flow[price_difference > 0] = raw_money_flow[price_difference > 0]
        negative_flow[price_difference < 0] = raw_money_flow[price_difference < 0]
        positive_mf = positive_flow.rolling(window=period).sum()
        negative_mf = negative_flow.rolling(window=period).sum()
        money_flow_ratio = positive_mf / negative_mf
        mfi = 100 - (100 / (1 + money_flow_ratio))
        return mfi
    except Exception as e:
        logger.error(f"خطأ في حساب MFI: {e}")
        return pd.Series()

def calculate_higher_highs(df: pd.DataFrame, period: int = 20) -> pd.Series:
    highs = df['high']
    result = pd.Series(0, index=df.index)
    for i in range(period, len(df)):
        local_highs = []
        for j in range(i - period, i):
            if j > 0 and j < len(df) - 1:
                if highs[j] > highs[j-1] and highs[j] > highs[j+1]:
                    local_highs.append(highs[j])
        if len(local_highs) >= 2 and all(local_highs[k] <= local_highs[k+1] for k in range(len(local_highs)-1)):
            result[i] = 1
    return result

def calculate_higher_lows(df: pd.DataFrame, period: int = 20) -> pd.Series:
    lows = df['low']
    result = pd.Series(0, index=df.index)
    for i in range(period, len(df)):
        local_lows = []
        for j in range(i - period, i):
            if j > 0 and j < len(df) - 1:
                if lows[j] < lows[j-1] and lows[j] < lows[j+1]:
                    local_lows.append(lows[j])
        if len(local_lows) >= 2 and all(local_lows[k] <= local_lows[k+1] for k in range(len(local_lows)-1)):
            result[i] = 1
    return result

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    try:
        return series.ewm(span=period, adjust=False).mean()
    except Exception as e:
        logger.error(f"خطأ في حساب EMA: {e}")
        return series

def calculate_rsi_indicator(df: pd.DataFrame, period: int = 14) -> pd.Series:
    try:
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=period).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=period).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi
    except Exception as e:
        logger.error(f"خطأ في حساب RSI: {e}")
        return pd.Series()

def calculate_atr_indicator(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    try:
        df = df.copy()
        high = df['high']
        low = df['low']
        close = df['close'].shift(1)
        tr1 = high - low
        tr2 = abs(high - close)
        tr3 = abs(low - close)
        df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr'] = df['tr'].rolling(window=period).mean()
        return df
    except Exception as e:
        logger.error(f"خطأ في حساب ATR: {e}")
        return df

def calculate_macd_indicator(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    try:
        df = df.copy()
        ema_fast = calculate_ema(df['close'], fast)
        ema_slow = calculate_ema(df['close'], slow)
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = calculate_ema(df['macd'], signal)
        df['macd_hist'] = df['macd'] - df['macd_signal']
        return df
    except Exception as e:
        logger.error(f"خطأ في حساب MACD: {e}")
        return df

def calculate_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    try:
        df = df.copy()
        low_min = df['low'].rolling(window=k_period).min()
        high_max = df['high'].rolling(window=k_period).max()
        df['%K'] = 100 * ((df['close'] - low_min) / (high_max - low_min))
        df['%D'] = df['%K'].rolling(window=d_period).mean()
        return df
    except Exception as e:
        logger.error(f"خطأ في حساب Stochastic: {e}")
        return df

def analyze_advanced_candle_patterns(df: pd.DataFrame) -> dict:
    try:
        last_candles = df.iloc[-5:].copy()
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
        
        morning_star = False
        if len(last_candles) >= 3:
            first_candle_bearish = last_candles.iloc[-3]['close'] < last_candles.iloc[-3]['open']
            second_candle_small = last_candles.iloc[-2]['body'] < last_candles.iloc[-3]['body'] * 0.5
            third_candle_bullish = last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open']
            third_candle_mid = (last_candles.iloc[-3]['open'] + last_candles.iloc[-3]['close']) / 2
            third_candle_closes_above_midpoint = last_candles.iloc[-1]['close'] > third_candle_mid
            morning_star = (first_candle_bearish and second_candle_small and third_candle_bullish and third_candle_closes_above_midpoint)
        
        three_white_soldiers = False
        if len(last_candles) >= 3:
            all_bullish = all(last_candles.iloc[-i]['close'] > last_candles.iloc[-i]['open'] for i in range(1, 4))
            progressively_higher = all(last_candles.iloc[-i]['close'] > last_candles.iloc[-i-1]['close'] for i in range(1, 3))
            small_upper_shadows = all(last_candles.iloc[-i]['upper_shadow'] < last_candles.iloc[-i]['body'] * 0.3 for i in range(1, 4))
            three_white_soldiers = all_bullish and progressively_higher and small_upper_shadows
        
        pattern = {
            'doji': doji,
            'hammer': hammer,
            'bullish_engulfing': bullish_engulfing,
            'morning_star': morning_star,
            'three_white_soldiers': three_white_soldiers,
            'bullish': hammer or bullish_engulfing or morning_star or three_white_soldiers or (not doji and last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open'])
        }
        return pattern
    except Exception as e:
        logger.error(f"خطأ في تحليل أنماط الشموع: {e}")
        return {}

def determine_market_condition(df: pd.DataFrame) -> str:
    try:
        if len(df) < 200:
            return "neutral"
        df = df.copy()
        df['ma50'] = df['close'].rolling(window=50).mean()
        df['ma200'] = df['close'].rolling(window=200).mean()
        last_row = df.iloc[-1]
        if (last_row['ma50'] > last_row['ma200'] and last_row['close'] > last_row['ma50']):
            return "bullish"
        elif (last_row['ma50'] < last_row['ma200'] and last_row['close'] < last_row['ma50']):
            return "bearish"
        else:
            df['atr_pct'] = df['atr'] / df['close'] * 100
            avg_atr_pct = df['atr_pct'].iloc[-14:].mean()
            return "volatile" if avg_atr_pct > 3.0 else "neutral"
    except Exception as e:
        logger.error(f"خطأ في تحديد حالة السوق: {e}")
        return "neutral"

def select_best_target_level(current_price: float, fib_levels: list, recent_highs: np.ndarray) -> float:
    targets = []
    for level in fib_levels:
        if level > current_price:
            targets.append((level, 1.0))
    for high in np.sort(recent_highs):
        if high > current_price:
            found_close_fib = False
            for level in fib_levels:
                if abs(high - level) / level < 0.01:
                    found_close_fib = True
                    targets = [(t, w * 1.5 if abs(t - level) / level < 0.01 else w) for t, w in targets]
                    break
            if not found_close_fib:
                targets.append((high, 0.8))
    if not targets:
        return fib_levels[1] if len(fib_levels) > 1 else current_price * 1.02
    target_scores = []
    for target, importance in targets:
        distance_factor = current_price / target
        score = importance * distance_factor
        target_scores.append((target, score))
    best_target = max(target_scores, key=lambda x: x[1])[0]
    return best_target

def calculate_enhanced_confidence_score(indicators: dict, candle_pattern: dict, 
                                        risk_reward_ratio: float, volatility: float, 
                                        market_condition: str, proximity_to_support: float) -> int:
    try:
        score = 65 if market_condition == 'bullish' else 55 if market_condition == 'bearish' else 60
        if indicators['rsi'] < 40:
            score += 5
        elif indicators['rsi'] > 65:
            score -= 10
        if indicators['macd'] > indicators['macd_signal']:
            score += 7
        if indicators.get('adx', 0) > 25:
            score += 8
        elif indicators.get('adx', 0) < 20:
            score -= 5
        if indicators.get('mfi', 50) > 50:
            score += 5
        if indicators['ema5'] > indicators['ema13'] and indicators['ema13'] > indicators.get('ema21', 0):
            score += 8
        if candle_pattern.get('morning_star', False):
            score += 15
        elif candle_pattern.get('three_white_soldiers', False):
            score += 12
        elif candle_pattern.get('bullish_engulfing', False):
            score += 10
        elif candle_pattern.get('hammer', False):
            score += 8
        if risk_reward_ratio > 3:
            score += 10
        elif risk_reward_ratio > 2:
            score += 5
        if volatility < 0.01:
            score += 5
        elif volatility > 0.03:
            score -= 8
        final_score = max(0, min(100, int(score)))
        return final_score
    except Exception as e:
        logger.error(f"خطأ في حساب درجة الثقة: {e}")
        return 0

def calculate_dynamic_stop_loss(df: pd.DataFrame, current_price: float, atr: float, 
                                support_level: float, volatility: float) -> float:
    try:
        volatility_factor = min(2.0, max(1.0, 1.0 + volatility * 10))
        atr_stop_loss = current_price - (atr * volatility_factor)
        support_stop_loss = support_level * 0.995
        stop_loss = max(atr_stop_loss, support_stop_loss)
        max_stop_distance = current_price * 0.05
        min_stop_distance = current_price * 0.005
        stop_loss = max(current_price - max_stop_distance, min(current_price - min_stop_distance, stop_loss))
        return stop_loss
    except Exception as e:
        logger.error(f"خطأ في حساب وقف الخسارة: {e}")
        return current_price

# ---------------------- فئة الاستراتيجية المحسنة ----------------------
class EnhancedTradingStrategy:
    stoploss = -0.015
    minimal_roi = {"0": 0.008, "30": 0.005, "60": 0.003}

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            df['volume_change'] = df['volume'].pct_change().rolling(window=3).mean()
            df['price_momentum'] = df['close'].diff(3).rolling(window=5).mean()
            df['volatility'] = df['high'].div(df['low']).rolling(window=10).mean()
            df['ema5'] = calculate_ema(df['close'], 5)
            df['ema13'] = calculate_ema(df['close'], 13)
            df['ema21'] = calculate_ema(df['close'], 21)
            df['rsi'] = calculate_rsi_indicator(df, period=7)
            df['rsi_divergence'] = df['rsi'].diff(3)
            df['ma20'] = df['close'].rolling(window=20).mean()
            std20 = df['close'].rolling(window=20).std()
            df['upper_band'] = df['ma20'] + (std20 * 2)
            df['lower_band'] = df['ma20'] - (std20 * 2)
            df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
            
            df['adx'] = calculate_adx(df, period=14)
            df['mfi'] = calculate_mfi(df, period=14)
            df['higher_highs'] = calculate_higher_highs(df, period=20)
            df['higher_lows'] = calculate_higher_lows(df, period=20)
            
            df = calculate_atr_indicator(df, period=7)
            df = calculate_macd_indicator(df)
            df = calculate_stochastic(df)
            
            df['resistance'] = df['high'].rolling(window=20).max()
            df['support'] = df['low'].rolling(window=20).min()
            
            df['price_distance_from_vwap'] = (df['close'] - df['vwap']) / df['vwap']
            df['volume_trend'] = df['volume'].diff(5).rolling(window=10).mean()
            df['bollinger_bandwidth'] = (df['upper_band'] - df['lower_band']) / df['ma20']
            return df
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return df

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        basic_conditions = (
            (df['ema5'] > df['ema13']) &
            (df['rsi'].between(30, 70)) &
            (df['macd'] > df['macd_signal']) &
            (df['%K'] > df['%D'])
        )
        enhanced_conditions = (
            (df['adx'] > 20) &
            (df['mfi'] > 40) &
            (df['close'] > df['vwap'])
        )
        risk_management = (
            (df['close'] > df['lower_band']) &
            (df['bollinger_bandwidth'] > 0.03)
        )
        market_position = (
            (df['higher_lows'] > 0) |
            ((df['close'] > df['ma20']) & (df['price_distance_from_vwap'] < 0.02))
        )
        conditions = basic_conditions & enhanced_conditions & risk_management & market_position
        df.loc[conditions, 'buy'] = 1
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema5'] < df['ema13']) |
            (df['rsi'] > 80) |
            (df['macd'] < df['macd_signal']) |
            (df['%K'] < df['%D']) |
            (df['close'] > df['upper_band']) |
            ((df['close'] > df['resistance'] * 0.99) & (df['rsi'] > 70))
        )
        df.loc[conditions, 'sell'] = 1
        return df

# ---------------------- دالة توليد إشارة التداول المحسنة ----------------------
def generate_enhanced_signal(df: pd.DataFrame, symbol: str) -> dict:
    if len(df) < 50:
        logger.info(f"{symbol}: رفض التوصية - البيانات غير كافية (عدد الصفوف: {len(df)})")
        return None

    strategy = EnhancedTradingStrategy()
    df = strategy.populate_indicators(df)
    df = strategy.populate_buy_trend(df)
    last_row = df.iloc[-1]
    
    if last_row.get('buy', 0) != 1:
        logger.info(f"{symbol}: رفض التوصية - شروط الشراء غير مستوفاة")
        return None

    candle_pattern = analyze_advanced_candle_patterns(df)
    if not candle_pattern.get('bullish', False):
        logger.info(f"{symbol}: رفض التوصية - نمط الشموع غير صاعد")
        return None

    market_condition = determine_market_condition(df)
    market_volatility = df['atr'].iloc[-1] / df['close'].iloc[-1]
    current_price = last_row['close']
    atr = last_row['atr']
    resistance = last_row['resistance']
    support = last_row['support']
    price_range = resistance - support
    proximity_to_support = (current_price - support) / current_price

    recent_highs = df['high'][-30:].values
    fib_levels = [current_price + price_range * level for level in [0.382, 0.618, 0.786, 1.0]]
    target = select_best_target_level(current_price, fib_levels, recent_highs)

    risk = current_price - support
    reward = target - current_price
    risk_reward_ratio = reward / risk if risk > 0 else 0

    confidence_score = calculate_enhanced_confidence_score(
        indicators=last_row,
        candle_pattern=candle_pattern,
        risk_reward_ratio=risk_reward_ratio,
        volatility=market_volatility,
        market_condition=market_condition,
        proximity_to_support=proximity_to_support
    )
    if confidence_score < 60 or risk_reward_ratio < 1.5:
        logger.info(f"{symbol}: رفض التوصية - نسبة المخاطرة/العائد أو درجة الثقة غير كافية")
        return None

    stop_loss = calculate_dynamic_stop_loss(df, current_price, atr, support, market_volatility)

    signal = {
        'symbol': symbol,
        'price': float(format(current_price, '.8f')),
        'target': float(format(target, '.8f')),
        'stop_loss': float(format(stop_loss, '.8f')),
        'dynamic_stop_loss': float(format(stop_loss, '.8f')),
        'strategy': 'enhanced_trading',
        'confidence': int(confidence_score),
        'market_condition': market_condition,
        'indicators': {
            'ema5': float(last_row['ema5']),
            'ema13': float(last_row['ema13']),
            'rsi': float(last_row['rsi']),
            'vwap': float(last_row['vwap']),
            'atr': float(atr),
            'macd': float(last_row['macd']),
            'macd_signal': float(last_row['macd_signal']),
            '%K': float(last_row['%K']),
            '%D': float(last_row['%D']),
            'resistance': float(resistance),
            'support': float(support)
        },
        'trade_value': TRADE_VALUE,
        'risk_reward_ratio': float(risk_reward_ratio)
    }
    logger.info(f"{symbol}: تم توليد التوصية - السعر: {current_price}, الهدف: {target}, وقف الخسارة: {stop_loss}")
    return signal

# ---------------------- دوال إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        rtl_mark = "\u200F"
        message = (
            f"{rtl_mark}🌟 **توصية تداول - {signal['symbol']}** 🌟\n"
            "----------------------------------------\n"
            f"💰 الدخول: ${signal['price']}\n"
            f"🎯 الهدف: ${signal['target']} (**+{profit}%**)\n"
            f"🛑 وقف الخسارة: ${signal['stop_loss']} (**{loss}%**)\n"
            f"🔄 وقف الخسارة المتحرك: ${signal['dynamic_stop_loss']}\n"
            f"⚖️ نسبة المخاطرة/العائد: **{signal['risk_reward_ratio']:.2f}**\n"
            "----------------------------------------\n"
            f"📈 المؤشرات:\n"
            f"   • RSI: **{signal['indicators']['rsi']:.2f}**\n"
            f"   • VWAP: **${signal['indicators']['vwap']:.4f}**\n"
            f"   • ATR: **{signal['indicators']['atr']:.8f}**\n"
            f"   • Stochastic %K: **{signal['indicators']['%K']:.2f}**\n"
            f"   • Stochastic %D: **{signal['indicators']['%D']:.2f}**\n"
            "----------------------------------------\n"
            f"💧 السيولة (15 دقيقة): **{volume:,.2f} USDT**\n"
            f"💵 قيمة الصفقة: **${TRADE_VALUE}**\n"
            f"📊 سيطرة السوق:\n"
            f"   • BTC: **{btc_dominance:.2f}%**\n"
            f"   • ETH: **{eth_dominance:.2f}%**\n"
            f"⏰ وقت التوصية: {datetime.now(timezone).strftime('%Y-%m-%d %H:%M')}"
        )
        reply_markup = {"inline_keyboard": [[{"text": "📊 عرض التقرير", "callback_data": "get_report"}]]}
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "reply_markup": json.dumps(reply_markup)}
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"{signal['symbol']}: فشل إرسال التوصية: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في send_telegram_alert: {e}")

def send_telegram_alert_special(message):
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"فشل إرسال التنبيه الخاص: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في send_telegram_alert_special: {e}")

# ---------------------- وظائف الحصول على البيانات ----------------------
def get_crypto_symbols():
    try:
        exchange_info = client.get_exchange_info()
        symbols = [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        filtered_symbols = []
        for symbol in symbols:
            volume = fetch_recent_volume(symbol)
            if volume > 50000:
                filtered_symbols.append(symbol)
        return filtered_symbols
    except Exception as e:
        logger.error(f"خطأ في جلب الأزواج: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=3):
    cache_key = f"{symbol}_{interval}_{days}"
    if cache_key in historical_data_cache:
        return historical_data_cache[cache_key]
    for attempt in range(3):
        try:
            klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume',
                                                 'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                                                 'taker_buy_quote', 'ignore'])
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].astype(float)
            historical_data_cache[cache_key] = df
            return df
        except Exception as e:
            logger.error(f"{symbol}: خطأ في جلب البيانات (محاولة {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    logger.error(f"{symbol}: فشل جلب البيانات بعد 3 محاولات")
    return None

def fetch_recent_volume(symbol):
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        return volume
    except Exception as e:
        logger.error(f"{symbol}: خطأ في جلب حجم السيولة: {e}")
        return 0

def get_market_dominance():
    try:
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

# ---------------------- تتبع التوصيات المفتوحة ----------------------
def improved_track_signals():
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
                current_price = last_price_update.get(symbol)
                if current_price is None:
                    continue

                df = fetch_historical_data(symbol, interval='5m', days=1)
                if df is None or len(df) < 20:
                    logger.info(f"{symbol}: تجاهل التوصية - البيانات التاريخية غير كافية")
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
                    candidate_stops = [dynamic_stop_loss, pct_based_stop, atr_based_stop, time_based_stop, fib_based_stop, stop_loss]
                    new_dynamic_stop_loss = max(candidate_stops)
                    if new_dynamic_stop_loss > dynamic_stop_loss * 1.005:
                        cur.execute("UPDATE signals SET dynamic_stop_loss = %s WHERE id = %s", 
                                    (float(new_dynamic_stop_loss), int(signal_id)))
                        conn.commit()
                        # تنبيه عند تغيير وقف الخسارة
                        if new_dynamic_stop_loss > dynamic_stop_loss * 1.05:
                            msg = (
                                f"📊 **تحديث وقف الخسارة - {symbol}**\n"
                                "----------------------------------------\n"
                                f"💰 الدخول: ${entry:.8f}\n"
                                f"💹 السعر الحالي: ${current_price:.8f}\n"
                                f"🛡️ وقف الخسارة الجديد: ${new_dynamic_stop_loss:.8f}\n"
                                f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                            )
                            send_telegram_alert_special(msg)
                else:
                    new_dynamic_stop_loss = stop_loss

                try:
                    new_resistance = df['high'].rolling(window=20).max().iloc[-1]
                    new_support = df['low'].rolling(window=20).min().iloc[-1]
                    new_price_range = new_resistance - new_support
                    new_fib_levels = [current_price + new_price_range * level for level in [0.382, 0.618, 0.786]]
                    recent_highs = np.sort(df['high'].tail(20).values)
                    new_target = select_best_target_level(current_price, new_fib_levels, recent_highs)
                    if new_target and abs(new_target - target) / target > 0.01:
                        cur.execute("UPDATE signals SET target = %s WHERE id = %s", (float(new_target), int(signal_id)))
                        conn.commit()
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
                        f"🎉 **نجاح! تحقيق الهدف - {symbol}**\n"
                        "----------------------------------------\n"
                        f"💰 الدخول: ${entry:.8f}\n"
                        f"✅ الخروج: ${current_price:.8f}\n"
                        f"📈 الربح: +{profit:.2f}%\n"
                        f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                    )
                    send_telegram_alert_special(msg)
                    cur.execute("UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s", (int(signal_id),))
                    conn.commit()
                elif current_price <= new_dynamic_stop_loss:
                    loss = ((current_price - entry) / entry) * 100
                    msg = (
                        f"⚠️ **تنبيه: تفعيل وقف الخسارة - {symbol}**\n"
                        "----------------------------------------\n"
                        f"💰 الدخول: ${entry:.8f}\n"
                        f"❌ الخروج: ${current_price:.8f}\n"
                        f"📉 التغيير: {loss:.2f}%\n"
                        f"⏰ الوقت: {datetime.now(timezone).strftime('%H:%M:%S')}"
                    )
                    send_telegram_alert_special(msg)
                    cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (int(signal_id),))
                    conn.commit()
        except Exception as e:
            logger.error(f"خطأ في تتبع الإشارات: {e}")
            conn.rollback()
        finally:
            release_db_connection(conn)
        time.sleep(30)

# ---------------------- إصدار توصيات جديدة (تحليل السوق) ----------------------
def analyze_market():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_signals_count = cur.fetchone()[0]
        if active_signals_count >= 4:
            return

        btc_dominance, eth_dominance = get_market_dominance() or (0.0, 0.0)
        symbols = get_crypto_symbols()
        for symbol in symbols:
            df = fetch_historical_data(symbol)
            if df is None or len(df) < 100:
                logger.info(f"{symbol}: رفض التوصية - البيانات التاريخية غير كافية")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 50000:
                logger.info(f"{symbol}: رفض التوصية - السيولة أقل من المطلوب")
                continue
            signal = generate_enhanced_signal(df, symbol)
            if signal:
                send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance)
                cur.execute("""
                    INSERT INTO signals 
                    (symbol, entry_price, target, stop_loss, dynamic_stop_loss, r2_score, volume_15m, risk_reward_ratio)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    signal['symbol'],
                    float(signal['price']),
                    float(signal['target']),
                    float(signal['stop_loss']),
                    float(signal['dynamic_stop_loss']),
                    int(signal.get('confidence', 100)),
                    float(volume_15m),
                    float(signal['risk_reward_ratio'])
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

# ---------------------- تطبيق Flask ----------------------
app = Flask(__name__)

def run_flask():
    app.run(host='0.0.0.0', port=5000)

@app.route('/')
def home():
    return "نظام توصيات التداول اليومي يعمل بكفاءة", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update or "callback_query" not in update:
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
        
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        open_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NOT NULL")
        closed_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM signals WHERE achieved_target = TRUE")
        win_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM signals WHERE hit_stop_loss = TRUE")
        lose_count = cur.fetchone()[0]
        cur.execute("SELECT AVG((target/entry_price - 1)*100) FROM signals WHERE achieved_target = TRUE")
        avg_win = cur.fetchone()[0] or 0
        cur.execute("SELECT AVG(ABS((stop_loss/entry_price - 1)*100)) FROM signals WHERE hit_stop_loss = TRUE")
        avg_loss = cur.fetchone()[0] or 0
        
        release_db_connection(conn)
        
        report_message = (
            "📊 **تقرير أداء التداول الشامل** 📊\n"
            f"🕒 محدث: {datetime.now(timezone).strftime('%Y-%m-%d %H:%M')}\n\n"
            f"🔹 التوصيات المفتوحة: {open_count}\n"
            f"🔹 التوصيات المغلقة: {closed_count}\n"
            f"🏆 التوصيات الناجحة: {win_count} (متوسط ربح: +{avg_win:.2f}%)\n"
            f"❌ التوصيات الخاسرة: {lose_count} (متوسط خسارة: -{avg_loss:.2f}%)\n\n"
        )
        
        report_message += "🏆 **الصفقات الرابحة (آخر 10)**\n"
        if winning_trades:
            for trade in winning_trades:
                symbol, entry, target, stop_loss, dyn_stop, sent_at, closed_at = trade
                sent_at_str = sent_at.strftime('%Y-%m-%d %H:%M')
                closed_at_str = closed_at.strftime('%Y-%m-%d %H:%M') if closed_at else "غير محدد"
                profit_percentage = ((target / entry) - 1) * 100
                profit_amount = TRADE_VALUE * ((target / entry) - 1)
                report_message += (
                    f"🌟 {symbol}\n"
                    f"  - الدخول: ${entry:.8f}\n"
                    f"  - الهدف: ${target:.8f}\n"
                    f"  - وقف الخسارة: ${stop_loss:.8f}\n"
                    f"  - الربح: +{profit_percentage:.2f}% (${profit_amount:.2f})\n"
                    f"  - الإرسال: {sent_at_str}\n"
                    f"  - الإغلاق: {closed_at_str}\n"
                    "----------------------------------------\n"
                )
        else:
            report_message += "لا توجد صفقات رابحة بعد.\n\n"
        
        report_message += "❌ **الصفقات الخاسرة (آخر 10)**\n"
        if losing_trades:
            for trade in losing_trades:
                symbol, entry, target, stop_loss, dyn_stop, sent_at, closed_at = trade
                sent_at_str = sent_at.strftime('%Y-%m-%d %H:%M')
                closed_at_str = closed_at.strftime('%Y-%m-%d %H:%M') if closed_at else "غير محدد"
                loss_percentage = abs(((stop_loss / entry) - 1) * 100)
                loss_amount = TRADE_VALUE * abs(((stop_loss / entry) - 1))
                report_message += (
                    f"🔴 {symbol}\n"
                    f"  - الدخول: ${entry:.8f}\n"
                    f"  - وقف الخسارة: ${stop_loss:.8f}\n"
                    f"  - الخسارة: -{loss_percentage:.2f}% (${loss_amount:.2f})\n"
                    f"  - الإرسال: {sent_at_str}\n"
                    f"  - الإغلاق: {closed_at_str}\n"
                    "----------------------------------------\n"
                )
        else:
            report_message += "لا توجد صفقات خاسرة بعد.\n\n"
        
        report_message += "⏳ **الصفقات المفتوحة (آخر 10)**\n"
        if open_trades:
            for trade in open_trades:
                symbol, entry, target, stop_loss, dyn_stop, sent_at = trade
                sent_at_str = sent_at.strftime('%Y-%m-%d %H:%M')
                report_message += (
                    f"⏰ {symbol}\n"
                    f"  - الدخول: ${entry:.8f}\n"
                    f"  - الهدف: ${target:.8f}\n"
                    f"  - وقف الخسارة: ${stop_loss:.8f}\n"
                    f"  - الإرسال: {sent_at_str}\n"
                    "----------------------------------------\n"
                )
        else:
            report_message += "لا توجد صفقات مفتوحة حالياً.\n\n"
        
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": report_message, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"فشل إرسال التقرير: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في إرسال التقرير: {e}")

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    set_telegram_webhook_url = f"https://api.telegram.org/bot{telegram_token}/setWebhook?url=https://hamza-drs4.onrender.com/webhook"
    try:
        response = requests.get(set_telegram_webhook_url, timeout=10)
        res_json = response.json()
        if not res_json.get("ok"):
            logger.error(f"فشل تسجيل webhook: {res_json}")
    except Exception as e:
        logger.error(f"استثناء أثناء تسجيل webhook: {e}")
    
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    Thread(target=improved_track_signals, daemon=True).start()
    Thread(target=run_flask, daemon=True).start()
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=10)
    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("إيقاف النظام")
