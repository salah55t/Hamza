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

logger.info(f"TELEGRAM_BOT_TOKEN: {telegram_token[:10]}...")
logger.info(f"TELEGRAM_CHAT_ID: {chat_id}")

TRADE_VALUE = 10

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
                volume_15m DOUBLE PRECISION,
                achieved_target BOOLEAN DEFAULT FALSE,
                hit_stop_loss BOOLEAN DEFAULT FALSE,
                closed_at TIMESTAMP,
                sent_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_symbol_time UNIQUE (symbol, sent_at)
            )
        """)
        conn.commit()
        logger.info("✅ تم تهيئة قاعدة البيانات بنجاح")
    except Exception as e:
        logger.error(f"❌ فشل تهيئة قاعدة البيانات: {e}")
        raise

def check_db_connection():
    global conn, cur
    try:
        cur.execute("SELECT 1")
        conn.commit()
    except Exception as e:
        logger.warning("⚠️ إعادة الاتصال بقاعدة البيانات...")
        try:
            if conn:
                conn.close()
            init_db()
        except Exception as ex:
            logger.error(f"❌ فشل إعادة الاتصال: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
client = Client(api_key, api_secret)

# ---------------------- WebSocket لتحديث الأسعار ----------------------
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
        logger.error(f"❌ خطأ في معالجة الرسالة: {e}")

def run_ticker_socket_manager():
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("✅ تم تشغيل WebSocket لاستقبال تحديثات الأسعار")
    except Exception as e:
        logger.error(f"❌ خطأ في تشغيل WebSocket: {e}")

# ---------------------- المؤشرات الفنية ----------------------
def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi_indicator(df, period=14):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_atr_indicator(df, period=14):
    df['tr'] = df[['high', 'low']].diff(axis=1).abs().max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df

def detect_candlestick_patterns(df):
    patterns = {
        'CDLHAMMER': ta.candle.cdl_hammer,
        'CDLENGULFING': ta.candle.cdl_engulfing,
        'CDLMORNINGSTAR': ta.candle.cdl_morning_star,
        'CDLEVENINGSTAR': ta.candle.cdl_evening_star,
        'CDLSHOOTINGSTAR': ta.candle.cdl_shooting_star,
    }
    for name, func in patterns.items():
        df[name] = func(df['open'], df['high'], df['low'], df['close'])
    return df

# ---------------------- استراتيجية التداول ----------------------
class FreqtradeStrategy:
    stoploss = -0.02
    minimal_roi = {"0": 0.01}

    def populate_indicators(self, df):
        df['ema8'] = calculate_ema(df['close'], 8)
        df['ema21'] = calculate_ema(df['close'], 21)
        df['rsi'] = calculate_rsi_indicator(df)
        df['upper_band'] = df['close'].rolling(20).mean() + 2 * df['close'].rolling(20).std()
        df = calculate_atr_indicator(df)
        df = detect_candlestick_patterns(df)
        return df

    def populate_buy_trend(self, df):
        conditions = (
            (df['ema8'] > df['ema21']) &
            (df['rsi'] >= 50) & (df['rsi'] <= 70) &
            (df['close'] > df['upper_band']) &
            (df['CDLHAMMER'] == 100)
        )
        df.loc[conditions, 'buy'] = 1
        return df

# ---------------------- توليد الإشارات ----------------------
def generate_signal_using_freqtrade_strategy(df, symbol):
    df = df.dropna().reset_index(drop=True)
    if len(df) < 50:
        return None

    strategy = FreqtradeStrategy()
    df = strategy.populate_indicators(df)
    df = strategy.populate_buy_trend(df)
    last_row = df.iloc[-1]

    if last_row.get('buy', 0) == 1:
        current_price = last_row['close']
        current_atr = last_row['atr']
        atr_multiplier = 1.5
        target = current_price + atr_multiplier * current_atr
        stop_loss = current_price - (current_price * 0.015)

        signal = {
            'symbol': symbol,
            'price': round(current_price, 8),
            'target': round(target, 8),
            'stop_loss': round(stop_loss, 8),
            'indicators': {
                'ema8': round(last_row['ema8'], 2),
                'ema21': round(last_row['ema21'], 2),
                'rsi': round(last_row['rsi'], 2),
                'atr': round(current_atr, 8)
            },
            'trade_value': TRADE_VALUE
        }
        logger.info(f"✅ تم توليد إشارة شراء لـ {symbol}")
        return signal
    return None

# ---------------------- وظائف التحليل ----------------------
def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [line.strip().upper() + 'USDT' for line in f]
            logger.info(f"✅ تم استيراد {len(symbols)} زوج تداول")
            return symbols
    except Exception as e:
        logger.error(f"❌ خطأ في قراءة الرموز: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=3):
    try:
        logger.info(f"⏳ جاري استرجاع بيانات {symbol} على فريم {interval}")
        klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.astype({'open': 'float', 'high': 'float', 'low': 'float', 'close': 'float'})
        return df[['timestamp', 'open', 'high', 'low', 'close']]
    except Exception as e:
        logger.error(f"❌ خطأ في استرجاع بيانات {symbol}: {e}")
        return None

def fetch_volume(symbol):
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15m ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"📊 حجم السيولة لـ {symbol}: {volume:,.2f} USDT")
        return volume
    except Exception as e:
        logger.error(f"❌ خطأ في حساب الحجم لـ {symbol}: {e}")
        return 0

# ---------------------- إرسال التنبيهات ----------------------
def send_telegram_alert(signal, volume):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        message = f"""
🚨 **إشارـة تجديـد**  
✨ **الزوج**: {signal['symbol']}  
💰 **السعر الحالي**: ${signal['price']:.8f}  
🎯 **الهدف**: ${signal['target']:.8f} (+{profit}%)  
⚠️ **وقف الخسارة**: ${signal['stop_loss']:.8f}  
📊 **المؤشرات**:  
   • EMA8: {signal['indicators']['ema8']}  
   • EMA21: {signal['indicators']['ema21']}  
   • RSI: {signal['indicators']['rsi']}  
   • ATR: {signal['indicators']['atr']}  
💸 **القيمة الموصى بها**: ${TRADE_VALUE}  
⏱ **الوقت**: {time.strftime('%Y-%m-%d %H:%M')}
        """.strip()
        
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"Telegram Response: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ فشل إرسال الإشعار: {e}")

# ---------------------- تحليل السوق ----------------------
def analyze_market():
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active = cur.fetchone()[0]
        if active >= 4:
            logger.info("⚠️ تجاوز الحد الأقصى لعدد الإشارات النشطة (4)")
            return

        symbols = get_crypto_symbols()
        for symbol in symbols:
            df_5m = fetch_historical_data(symbol, interval='5m', days=2)
            if df_5m is None or len(df_5m) < 50:
                logger.warning(f"⚠️ بيانات {symbol} غير كافية")
                continue

            signal_5m = generate_signal_using_freqtrade_strategy(df_5m, symbol)

            if signal_5m:
                volume = fetch_volume(symbol)
                if volume < 40000:
                    logger.warning(f"⚠️ حجم {symbol} منخفض ({volume:,.2f} USDT)")
                    continue

                send_telegram_alert(signal_5m, volume)

                cur.execute("""
                    INSERT INTO signals (symbol, entry_price, target, stop_loss, volume_15m)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    signal_5m['symbol'],
                    signal_5m['price'],
                    signal_5m['target'],
                    signal_5m['stop_loss'],
                    volume
                ))
                conn.commit()
                logger.info(f"✅ تم إدخال الإشارة لـ {symbol}")

    except Exception as e:
        logger.error(f"❌ خطأ في تحليل السوق: {e}")

# ---------------------- تتبع الإشارات ----------------------
def track_signals():
    while True:
        try:
            check_db_connection()
            cur.execute("SELECT * FROM signals WHERE closed_at IS NULL")
            active_signals = cur.fetchall()

            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss = signal[:5]
                current_price = ticker_data.get(symbol, {}).get('c', 0)

                if not current_price:
                    logger.warning(f"⚠️ لا توجد بيانات سعرية حديثة لـ {symbol}")
                    continue

                # تحديث وقف الخسارة تلقائيًا
                new_stop = current_price - (current_price * 0.015)
                if new_stop > stop_loss:
                    cur.execute("UPDATE signals SET stop_loss = %s WHERE id = %s", (new_stop, signal_id))
                    conn.commit()
                    send_telegram_alert(f"🔄 تم تحديث وقف الخسارة لـ {symbol} إلى ${new_stop:.8f}")

                # إغلاق الإشارة عند تحقيق الهدف
                if current_price >= target:
                    profit = ((current_price / entry) - 1) * 100
                    send_telegram_alert(f"🎉 تم تحقيق الهدف لـ {symbol}! ربح {profit:.2f}%")
                    cur.execute("UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                    conn.commit()

                # إغلاق الإشارة عند ضرب وقف الخسارة
                elif current_price <= stop_loss:
                    loss = ((current_price / entry) - 1) * 100
                    send_telegram_alert(f"❌ تم ضرب وقف الخسارة لـ {symbol}! خسارة {-loss:.2f}%")
                    cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                    conn.commit()

        except Exception as e:
            logger.error(f"❌ خطأ في تتبع الإشارات: {e}")
        time.sleep(60)

# ---------------------- واجهة الويب ----------------------
app = Flask(__name__)

@app.route('/')
def status():
    return "统运行中🚀", 200

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    
    # إعداد الخيوط
    threads = [
        Thread(target=run_ticker_socket_manager, daemon=True),
        Thread(target=track_signals, daemon=True),
    ]
    for thread in threads:
        thread.start()

    # جدولة التحليل كل 5 دقائق
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()

    # تشغيل الويب
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
