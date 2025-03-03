#!/usr/bin/env python
"""
📈 نظام تداول ذكي متعدد الاستراتيجيات

يوفر هذا البوت استراتيجيتين:
1. سكالبينڨ: يعتمد على مؤشرات EMA (3 و7) وRSI (5) وMACD سريع وBollinger Bands مع شروط دخول بنسبة مخاطرة/عائد ≥ 2.
2. Hummingbot: يعتمد على مؤشرات EMA (5 و13) وRSI (7) وMACD وStochastic ونموذج الشموع (Bullish Engulfing) مع شروط بنسبة مخاطرة/عائد ≥ 2.5.

يتم اختيار الاستراتيجية عبر متغير البيئة STRATEGY_MODE (افتراضي "scalping").
يُرسل البوت تنبيهات عبر Telegram مع تقرير أداء ويتم تحديث الإشارات (Trailing Stop) تلقائيًا.
"""

import time, os, json, logging
from threading import Thread
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import psycopg2
import requests
from flask import Flask, request
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance import ThreadedWebsocketManager

# ---------------------- إعدادات المتغيرات البيئية ----------------------
api_key = config('BINANCE_API_KEY')
api_secret = config('BINANCE_API_SECRET')
telegram_token = config('TELEGRAM_BOT_TOKEN')
chat_id = config('TELEGRAM_CHAT_ID')
db_url = config('DATABASE_URL')
STRATEGY_MODE = config('STRATEGY_MODE', default="scalping")  # "scalping" أو "hummingbot"

# قيمة الصفقة الثابتة
TRADE_VALUE = 10

# ---------------------- إعداد التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('trading_bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------------------- دالة الحصول على الوقت بتوقيت GMT+1 ----------------------
def get_gmt_plus1_time():
    return datetime.utcnow() + timedelta(hours=1)

# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
conn = None
cur = None

def init_db():
    global conn, cur
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id SERIAL PRIMARY KEY,
                symbol TEXT,
                entry_price DOUBLE PRECISION,
                target DOUBLE PRECISION,
                stop_loss DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                volume_15m DOUBLE PRECISION,
                achieved_target BOOLEAN DEFAULT FALSE,
                hit_stop_loss BOOLEAN DEFAULT FALSE,
                closed_at TIMESTAMP,
                sent_at TIMESTAMP DEFAULT NOW(),
                stage INTEGER DEFAULT 1,
                target_multiplier DOUBLE PRECISION DEFAULT 1.5,
                stop_loss_multiplier DOUBLE PRECISION DEFAULT 0.75,
                CONSTRAINT unique_symbol_time UNIQUE (symbol, sent_at)
            )
        """)
        conn.commit()
        logger.info("تم تهيئة قاعدة البيانات بنجاح")
    except Exception as e:
        logger.error(f"فشل تهيئة قاعدة البيانات: {e}")
        raise

def check_db_connection():
    global conn, cur
    try:
        cur.execute("SELECT 1")
        conn.commit()
    except Exception:
        logger.warning("إعادة الاتصال بقاعدة البيانات...")
        try:
            if conn:
                conn.close()
            init_db()
        except Exception as ex:
            logger.error(f"فشل إعادة الاتصال: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
client = Client(api_key, api_secret)

# ---------------------- استخدام WebSocket لتحديث بيانات التيكر ----------------------
ticker_data = {}

def handle_ticker_message(msg):
    try:
        if isinstance(msg, list):
            for m in msg:
                symbol = m.get('s')
                if symbol:
                    ticker_data[symbol] = m
        else:
            symbol = msg.get('s')
            if symbol:
                ticker_data[symbol] = msg
    except Exception as e:
        logger.error(f"خطأ في handle_ticker_message: {e}")

def run_ticker_socket_manager():
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("تم تشغيل WebSocket لتحديث التيكر لجميع الأزواج")
    except Exception as e:
        logger.error(f"خطأ في تشغيل WebSocket: {e}")

# ---------------------- دوال حساب المؤشرات الفنية ----------------------
def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

# بالنسبة لاستراتيجية السكالبينڨ
def calculate_ema_values_scalping(df):
    df['ema3'] = calculate_ema(df['close'], span=3)
    df['ema7'] = calculate_ema(df['close'], span=7)
    return df

def calculate_rsi_scalping(df, period=5):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd_scalping(df, fast=8, slow=16, signal=4):
    df['ema_fast'] = calculate_ema(df['close'], span=fast)
    df['ema_slow'] = calculate_ema(df['close'], span=slow)
    df['macd'] = df['ema_fast'] - df['ema_slow']
    df['macd_signal'] = calculate_ema(df['macd'], span=signal)
    return df

def calculate_bollinger_bands(df, period=10, std_dev=2.0):
    df['bb_middle'] = df['close'].rolling(window=period).mean()
    rolling_std = df['close'].rolling(window=period).std()
    df['bb_upper'] = df['bb_middle'] + (rolling_std * std_dev)
    df['bb_lower'] = df['bb_middle'] - (rolling_std * std_dev)
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle']
    df['bb_squeeze'] = rolling_std.rolling(window=20).std()
    return df

def calculate_atr(df, period=7):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df

# بالنسبة لاستراتيجية Hummingbot
def calculate_ema_values_hummingbot(df):
    df['ema5'] = calculate_ema(df['close'], span=5)
    df['ema13'] = calculate_ema(df['close'], span=13)
    return df

def calculate_rsi_hummingbot(df, period=7):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd_hummingbot(df, fast=12, slow=26, signal=9):
    df['ema_fast'] = calculate_ema(df['close'], span=fast)
    df['ema_slow'] = calculate_ema(df['close'], span=slow)
    df['macd'] = df['ema_fast'] - df['ema_slow']
    df['macd_signal'] = calculate_ema(df['macd'], span=signal)
    return df

def calculate_stochastic(df, period=14, smooth_k=3):
    df['lowest_low'] = df['low'].rolling(window=period).min()
    df['highest_high'] = df['high'].rolling(window=period).max()
    df['stochastic_k'] = ((df['close'] - df['lowest_low']) / (df['highest_high'] - df['lowest_low'])) * 100
    df['stochastic_d'] = df['stochastic_k'].rolling(window=smooth_k).mean()
    return df

# ---------------------- توليد الإشارات ----------------------
def generate_scalping_signal(df, symbol):
    # إذا كانت البيانات غير كافية
    if df.empty or len(df) < 20:
        logger.info(f"{symbol}: بيانات غير كافية لاستراتيجية السكالبينڨ")
        return None

    df = calculate_ema_values_scalping(df)
    df['rsi'] = calculate_rsi_scalping(df)
    df = calculate_macd_scalping(df)
    df = calculate_bollinger_bands(df)
    
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    
    conditions = []
    # 1. تقاطع EMA
    ema_cross = (prev_candle['ema3'] <= prev_candle['ema7']) and (last_candle['ema3'] > last_candle['ema7'])
    conditions.append(ema_cross)
    # 2. RSI في منطقة ذروة البيع مع زيادة
    rsi_condition = last_candle['rsi'] < 40 and last_candle['rsi'] > prev_candle['rsi']
    conditions.append(rsi_condition)
    # 3. MACD: تقاطع إيجابي
    macd_cross = (prev_candle['macd'] <= prev_candle['macd_signal']) and (last_candle['macd'] > last_candle['macd_signal'])
    conditions.append(macd_cross)
    # 4. Bollinger: ارتداد من الحد السفلي
    bb_bounce = last_candle['close'] <= last_candle['bb_lower'] * 1.01 and last_candle['close'] > prev_candle['close']
    conditions.append(bb_bounce)
    # 5. حجم التداول: زيادة النشاط
    volume_increase = df['volume'].iloc[-1] > df['volume'].rolling(window=5).mean().iloc[-1]
    conditions.append(volume_increase)
    
    required = 3
    passed = sum(conditions)
    confidence = (passed / len(conditions)) * 100
    
    if passed < required:
        logger.info(f"{symbol}: فشل تحقيق الشروط ({passed}/{len(conditions)})")
        return None

    df = calculate_atr(df)
    atr = df['atr'].iloc[-1]
    current_price = df['close'].iloc[-1]
    spread = 0.0005
    buy_price = current_price * (1 + spread)
    target_multiplier = 1.5
    stop_loss_multiplier = 0.75
    target = buy_price + target_multiplier * atr
    stop_loss = buy_price - stop_loss_multiplier * atr

    risk = buy_price - stop_loss
    reward = target - buy_price
    rr_ratio = reward / risk if risk > 0 else 0
    if rr_ratio < 2.0:
        logger.info(f"{symbol}: نسبة مخاطرة/عائد غير كافية: {rr_ratio:.2f}")
        return None

    signal = {
        'symbol': symbol,
        'price': float(format(buy_price, '.8f')),
        'target': float(format(target, '.8f')),
        'stop_loss': float(format(stop_loss, '.8f')),
        'strategy': 'scalping',
        'confidence': confidence,
        'indicators': {
            'target_multiplier': target_multiplier,
            'stop_loss_multiplier': stop_loss_multiplier
        },
        'trade_value': TRADE_VALUE,
        'stage': 1
    }
    logger.info(f"تم توليد إشارة سكالبينڨ للزوج {symbol} بثقة {confidence:.1f}%")
    return signal

def check_trade_conditions(df, buy_price, target, stop_loss):
    risk = buy_price - stop_loss
    reward = target - buy_price
    rr_ratio = reward / risk if risk != 0 else 0
    if rr_ratio < 2.5:
        logger.info(f"نسبة مخاطرة/عائد {rr_ratio:.2f} أقل من المطلوب")
        return False
    df = calculate_ema_values_hummingbot(df)
    if df.iloc[-1]['ema5'] <= df.iloc[-1]['ema13']:
        logger.info("EMA5 لم تتجاوز EMA13")
        return False
    rsi = calculate_rsi_hummingbot(df, period=7)
    if rsi.iloc[-1] >= 70:
        logger.info(f"RSI مرتفع ({rsi.iloc[-1]:.2f}) مما يشير لتشبع شرائي")
        return False
    df = calculate_macd_hummingbot(df)
    if df.iloc[-1]['macd'] <= df.iloc[-1]['macd_signal']:
        logger.info("MACD لم يتجاوز خط الإشارة")
        return False
    df = calculate_stochastic(df)
    if df.iloc[-1]['stochastic_k'] <= df.iloc[-1]['stochastic_d'] or df.iloc[-1]['stochastic_k'] > 80:
        logger.info("شروط Stochastic لم تتحقق")
        return False
    return True

def check_candlestick_pattern_and_support_resistance(df):
    if len(df) < 2:
        return False
    last_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    bullish_engulfing = (prev_candle['close'] < prev_candle['open']) and \
                        (last_candle['close'] > last_candle['open']) and \
                        (last_candle['open'] < prev_candle['close']) and \
                        (last_candle['close'] > prev_candle['open'])
    window = 20
    support = df['low'].rolling(window=window).min().iloc[-1]
    near_support = (last_candle['close'] - support) / support <= 0.02
    return bullish_engulfing and near_support

def generate_hummingbot_signal(df, symbol):
    df = df.dropna().reset_index(drop=True)
    if df.empty:
        return None
    current_price = df.iloc[-1]['close']
    df = calculate_atr(df, period=14)
    atr = df.iloc[-1]['atr']
    target_multiplier = 2
    stop_loss_multiplier = 1
    spread = 0.005
    buy_price = current_price * (1 - spread)
    target = buy_price + target_multiplier * atr
    stop_loss = buy_price - stop_loss_multiplier * atr

    # تحقق من الشروط الأساسية
    if not check_candlestick_pattern_and_support_resistance(df):
        logger.info(f"{symbol}: لا يستوفي نموذج الشموع أو الدعم/المقاومة")
        return None
    if (buy_price - stop_loss) <= 0:
        logger.info(f"{symbol}: معطيات وقف الخسارة غير منطقية")
        return None
    if not check_trade_conditions(df, buy_price, target, stop_loss):
        logger.info(f"{symbol}: شروط المؤشرات لم تتحقق")
        return None

    signal = {
        'symbol': symbol,
        'price': float(format(buy_price, '.8f')),
        'target': float(format(target, '.8f')),
        'stop_loss': float(format(stop_loss, '.8f')),
        'strategy': 'hummingbot',
        'confidence': 100,  # يمكن تعديلها حسب الحاجة
        'indicators': {
            'spread': spread,
            'reference_price': current_price,
            'atr': atr,
            'target_multiplier': target_multiplier,
            'stop_loss_multiplier': stop_loss_multiplier
        },
        'trade_value': TRADE_VALUE,
        'stage': 1
    }
    logger.info(f"تم توليد إشارة Hummingbot للزوج {symbol}")
    return signal

# ---------------------- وظائف جلب البيانات ----------------------
def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip()]
            logger.info(f"تم الحصول على {len(symbols)} زوج من العملات")
            return symbols
    except Exception as e:
        logger.error(f"خطأ في قراءة الملف: {e}")
        return []

def fetch_historical_data(symbol, interval='1m', lookback='3 hours'):
    try:
        klines = client.get_historical_klines(symbol, interval, lookback)
        if not klines:
            return None
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
        return None

def fetch_recent_volume(symbol):
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"حجم السيولة للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT")
        return volume
    except Exception as e:
        logger.error(f"خطأ في جلب حجم {symbol}: {e}")
        return 0

def get_market_dominance():
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json().get("data", {})
            market_cap_percentage = data.get("market_cap_percentage", {})
            btc = market_cap_percentage.get("btc", 0)
            eth = market_cap_percentage.get("eth", 0)
            logger.info(f"BTC Dominance: {btc}%, ETH Dominance: {eth}%")
            return btc, eth
        else:
            logger.error(f"خطأ في جلب نسب السيطرة: {response.status_code}")
            return 0.0, 0.0
    except Exception as e:
        logger.error(f"خطأ في get_market_dominance: {e}")
        return 0.0, 0.0

# ---------------------- إرسال التنبيهات ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        rtl_mark = "\u200F"
        message = (
            f"{rtl_mark}🚨 **إشارة تداول - {signal['symbol']} ({signal['strategy']})**\n\n"
            f"▫️ سعر الدخول: ${signal['price']}\n"
            f"🎯 الهدف: ${signal['target']} (+{profit}%)\n"
            f"🛑 وقف الخسارة: ${signal['stop_loss']} ({loss}%)\n"
            f"💧 السيولة (15 دقيقة): {volume:,.2f} USDT\n"
            f"💵 قيمة الصفقة: ${TRADE_VALUE}\n\n"
            f"📈 **نسب السيطرة (4H):**\n"
            f"   - BTC: {btc_dominance:.2f}%\n"
            f"   - ETH: {eth_dominance:.2f}%\n\n"
            f"⏰ {get_gmt_plus1_time().strftime('%Y-%m-%d %H:%M')}"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "عرض التقرير", "callback_data": "get_report"}]
            ]
        }
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown',
            'reply_markup': json.dumps(reply_markup)
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"فشل إرسال التنبيه للزوج {signal['symbol']}: {response.status_code}")
        else:
            logger.info(f"تم إرسال التنبيه للزوج {signal['symbol']} بنجاح")
    except Exception as e:
        logger.error(f"فشل إرسال التنبيه للزوج {signal['symbol']}: {e}")

def send_telegram_alert_special(message):
    try:
        ltr_mark = "\u200E"
        full_message = f"{ltr_mark}{message}"
        reply_markup = {
            "inline_keyboard": [
                [{"text": "عرض التقرير", "callback_data": "get_report"}]
            ]
        }
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': full_message,
            'parse_mode': 'Markdown',
            'reply_markup': json.dumps(reply_markup)
        }
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"رد Telegram: {response.status_code}")
    except Exception as e:
        logger.error(f"فشل إرسال التنبيه الخاص: {e}")

def send_report(target_chat_id):
    try:
        check_db_connection()
        cur.execute("SELECT achieved_target, entry_price, target, stop_loss FROM signals WHERE closed_at IS NOT NULL")
        closed_signals = cur.fetchall()
        success_count = 0
        stop_loss_count = 0
        profit_percentages = []
        loss_percentages = []
        total_profit = 0.0
        total_loss = 0.0
        for row in closed_signals:
            achieved, entry, target_val, stop_loss_val = row
            if achieved:
                profit_pct = (target_val / entry - 1) * 100
                profit_dollar = TRADE_VALUE * (target_val / entry - 1)
                success_count += 1
                profit_percentages.append(profit_pct)
                total_profit += profit_dollar
            else:
                loss_pct = (stop_loss_val / entry - 1) * 100
                loss_dollar = TRADE_VALUE * (stop_loss_val / entry - 1)
                stop_loss_count += 1
                loss_percentages.append(loss_pct)
                total_loss += loss_dollar
        avg_profit = sum(profit_percentages)/len(profit_percentages) if profit_percentages else 0
        avg_loss = sum(loss_percentages)/len(loss_percentages) if loss_percentages else 0
        net_profit = total_profit + total_loss

        report_message = (
            f"📊 **تقرير الأداء الشامل**\n\n"
            f"✅ توصيات ناجحة: {success_count}\n"
            f"❌ توصيات بوقف الخسارة: {stop_loss_count}\n"
            f"💹 متوسط الربح: {avg_profit:.2f}%\n"
            f"📉 متوسط الخسارة: {avg_loss:.2f}%\n"
            f"💵 صافي الربح/الخسارة: ${net_profit:.2f}"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': target_chat_id, 'text': report_message, 'parse_mode': 'Markdown'}
        requests.post(url, json=payload, timeout=10)
        logger.info("تم إرسال تقرير الأداء")
    except Exception as e:
        logger.error(f"فشل إرسال تقرير الأداء: {e}")

# ---------------------- تتبع الإشارات وتحديث وقف الخسارة ----------------------
def track_signals():
    logger.info("بدء خدمة تتبع الإشارات...")
    while True:
        try:
            check_db_connection()
            cur.execute("""
                SELECT id, symbol, entry_price, target, stop_loss, stage, target_multiplier, stop_loss_multiplier
                FROM signals 
                WHERE achieved_target = FALSE AND hit_stop_loss = FALSE AND closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            logger.info(f"تم العثور على {len(active_signals)} إشارة نشطة للتتبع")
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss, stage, tgt_mult, sl_mult = signal
                try:
                    if symbol in ticker_data:
                        current_price = float(ticker_data[symbol].get('c', 0))
                    else:
                        logger.warning(f"لا يوجد تحديث أسعار لـ {symbol} من WebSocket")
                        continue
                    logger.info(f"فحص {symbol}: السعر الحالي {current_price}, الدخول {entry}, الهدف {target}, وقف الخسارة {stop_loss}, المرحلة {stage}")
                    
                    if current_price >= target:
                        df = fetch_historical_data(symbol, interval='5m', lookback='3 day ago UTC')
                        if df is None or len(df) < 50:
                            logger.warning(f"بيانات غير كافية لتحديث {symbol}")
                            continue
                        df = calculate_atr(df, period=14)
                        atr = df['atr'].iloc[-1]
                        old_target = target
                        if stage == 1:
                            new_stop_loss = entry
                        else:
                            new_stop_loss = target
                        new_target = target + tgt_mult * atr
                        new_stage = stage + 1
                        msg = (
                            f"🎯 **تحديث {symbol}**\n"
                            f"• الهدف السابق: ${old_target:.8f}\n"
                            f"• وقف الخسارة الجديد: ${new_stop_loss:.8f}\n"
                            f"• الهدف الجديد: ${new_target:.8f}\n"
                            f"• المرحلة: {new_stage}\n"
                            f"⏱ {get_gmt_plus1_time().strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        try:
                            cur.execute("""
                                UPDATE signals 
                                SET target = %s, stop_loss = %s, stage = %s
                                WHERE id = %s
                            """, (new_target, new_stop_loss, new_stage, signal_id))
                            conn.commit()
                            logger.info(f"تم تحديث {symbol}: الهدف {new_target}, وقف الخسارة {new_stop_loss}, المرحلة {new_stage}")
                        except Exception as e:
                            logger.error(f"فشل تحديث {symbol}: {e}")
                            conn.rollback()
                    
                    elif current_price <= stop_loss:
                        loss_pct = ((current_price - entry) / entry) * 100
                        msg = (
                            f"🛑 **تفعيل وقف الخسارة - {symbol}**\n"
                            f"• الدخول: ${entry:.8f}\n"
                            f"• الخروج: ${current_price:.8f}\n"
                            f"• الخسارة: {loss_pct:.2f}%\n"
                            f"⏱ {get_gmt_plus1_time().strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        try:
                            cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                            conn.commit()
                            logger.info(f"تم إغلاق {symbol} بتفعيل وقف الخسارة")
                        except Exception as e:
                            logger.error(f"فشل تحديث {symbol} بعد وقف الخسارة: {e}")
                            conn.rollback()
                except Exception as e:
                    logger.error(f"خطأ في تتبع {symbol}: {e}")
                    conn.rollback()
                    continue
            time.sleep(60)
        except Exception as e:
            logger.error(f"خطأ في خدمة تتبع الإشارات: {e}")
            conn.rollback()
            time.sleep(60)

# ---------------------- تحليل السوق وإرسال التوصيات ----------------------
def analyze_market():
    logger.info("بدء تحليل السوق...")
    check_db_connection()
    cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
    active_count = cur.fetchone()[0]
    if active_count >= 4:
        logger.info("عدد التوصيات النشطة وصل للحد الأقصى (4). لن يتم إرسال توصيات جديدة الآن.")
        return
    btc_dom, eth_dom = get_market_dominance()
    symbols = get_crypto_symbols()
    if not symbols:
        logger.warning("لا توجد أزواج في الملف!")
        return
    for symbol in symbols:
        logger.info(f"فحص {symbol}...")
        try:
            df = None
            # استخدام معايير زمنية مختلفة حسب الاستراتيجية
            if STRATEGY_MODE.lower() == "scalping":
                df = fetch_historical_data(symbol, interval='1m', lookback='3 hours')
            else:
                df = fetch_historical_data(symbol, interval='5m', lookback='3 day ago UTC')
            if df is None or len(df) < (20 if STRATEGY_MODE.lower() == "scalping" else 100):
                logger.warning(f"تجاهل {symbol} - بيانات تاريخية غير كافية")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 100000:
                logger.info(f"تجاهل {symbol} - سيولة منخفضة: {volume_15m:,.2f} USDT")
                continue
            signal = None
            if STRATEGY_MODE.lower() == "scalping":
                signal = generate_scalping_signal(df, symbol)
            else:
                if not check_candlestick_pattern_and_support_resistance(df):
                    logger.info(f"تجاهل {symbol} - لا يستوفي شروط نموذج الشموع أو الدعم/المقاومة")
                    continue
                signal = generate_hummingbot_signal(df, symbol)
            if not signal:
                continue
            logger.info(f"الشروط مستوفاة؛ سيتم إرسال تنبيه للزوج {symbol}")
            send_telegram_alert(signal, volume_15m, btc_dom, eth_dom)
            try:
                cur.execute("""
                    INSERT INTO signals 
                    (symbol, entry_price, target, stop_loss, confidence, volume_15m, stage, target_multiplier, stop_loss_multiplier)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    signal['symbol'], signal['price'], signal['target'], signal['stop_loss'],
                    signal.get('confidence', 100), volume_15m, signal['stage'],
                    signal['indicators'].get('target_multiplier', 1.5),
                    signal['indicators'].get('stop_loss_multiplier', 0.75)
                ))
                conn.commit()
                logger.info(f"تم تسجيل إشارة {symbol} بنجاح")
            except Exception as e:
                logger.error(f"فشل تسجيل إشارة {symbol}: {e}")
                conn.rollback()
            time.sleep(1)
        except Exception as e:
            logger.error(f"خطأ في معالجة {symbol}: {e}")
            conn.rollback()
            continue
    logger.info("انتهى تحليل جميع الأزواج")

# ---------------------- إعداد تطبيق Flask وخدمة Webhook ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "نظام توصيات التداول متعدد الاستراتيجيات يعمل بكفاءة 🚀", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logger.info("استقبال تحديث: " + str(update))
    if "callback_query" in update:
        callback_data = update["callback_query"]["data"]
        chat_id_callback = update["callback_query"]["message"]["chat"]["id"]
        if callback_data == "get_report":
            send_report(chat_id_callback)
            answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": update["callback_query"]["id"]})
    return '', 200

def set_telegram_webhook():
    webhook_url = "https://hamza-drs4.onrender.com/webhook"
    url = f"https://api.telegram.org/bot{telegram_token}/setWebhook?url={webhook_url}"
    try:
        response = requests.get(url, timeout=10)
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"تم تسجيل webhook بنجاح")
        else:
            logger.error(f"فشل تسجيل webhook: {res_json}")
    except Exception as e:
        logger.error(f"استثناء أثناء تسجيل webhook: {e}")

def test_telegram():
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': chat_id, 'text': 'رسالة اختبار من البوت', 'parse_mode': 'Markdown'}
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"رد اختبار Telegram: {response.status_code}")
    except Exception as e:
        logger.error(f"فشل إرسال رسالة الاختبار: {e}")

# ---------------------- الجزء الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    set_telegram_webhook()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    Thread(target=track_signals, daemon=True).start()
    Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000))), daemon=True).start()
    test_telegram()
    logger.info(f"✅ تم بدء التشغيل بنجاح باستخدام الاستراتيجية: {STRATEGY_MODE}")
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
