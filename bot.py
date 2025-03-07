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
import ta
from ta.momentum import StochRSIIndicator

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

TRADE_VALUE = 10  # قيمة الصفقة الثابتة

# ---------------------- إعداد قاعدة البيانات ----------------------
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
                r2_score DOUBLE PRECISION,
                volume_15m DOUBLE PRECISION,
                achieved_target BOOLEAN DEFAULT FALSE,
                hit_stop_loss BOOLEAN DEFAULT FALSE,
                closed_at TIMESTAMP,
                sent_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_symbol_time UNIQUE (symbol, sent_at)
            )
        """)
        conn.commit()
        logger.info("✅ تم تهيئة قاعدة البيانات بنجاح.")
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

# ---------------------- إعداد Binance ----------------------
client = Client(api_key, api_secret)

# ---------------------- WebSocket للأسعار ----------------------
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
                ticker_data[symbol] = m
    except Exception as e:
        logger.error(f"❌ خطأ في استقبال البيانات: {e}")

def run_ticker_socket_manager():
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("✅ تم تشغيل WebSocket للأسعار.")
    except Exception as e:
        logger.error(f"❌ خطأ في WebSocket: {e}")

# ---------------------- المؤشرات الفنية ----------------------
def calculate_macd(df, fast=12, slow=26, signal=9):
    df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
    df['macd'] = df['ema_fast'] - df['ema_slow']
    df['macd_signal'] = df['macd'].ewm(span=signal, adjust=False).mean()
    return df

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(period).mean()
    return df

def detect_candlestick_patterns(df):
    # نموذج المطرقة
    df['hammer'] = (
        (df['close'] > df['open']) &
        (df['low'] < df['open']) &
        ((df['close'] - df['low']) > 2*(df['high'] - df['low']))
    ).astype(int)
    
    # نموذج الانغلفينغ
    df['engulfing'] = 0
    for i in range(1, len(df)):
        prev = df.iloc[i-1]
        curr = df.iloc[i]
        if prev['close'] < prev['open'] and curr['close'] > curr['open']:
            if curr['open'] < prev['close'] and curr['close'] > prev['open']:
                df.at[df.index[i], 'engulfing'] = 100
        elif prev['close'] > prev['open'] and curr['close'] < curr['open']:
            if curr['open'] > prev['close'] and curr['close'] < prev['open']:
                df.at[df.index[i], 'engulfing'] = -100
    return df

# ---------------------- استراتيجية التداول ----------------------
class FreqtradeStrategy:
    def populate_indicators(self, df):
        df['ema8'] = df['close'].ewm(span=8).mean()
        df['ema21'] = df['close'].ewm(span=21).mean()
        df['rsi'] = ta.momentum.RSIIndicator(df['close']).rsi()
        df['stoch_rsi'] = StochRSIIndicator(df['close']).stochrsi()
        df = calculate_macd(df)
        df = calculate_atr(df)
        df = detect_candlestick_patterns(df)
        return df

    def check_buy_signal(self, df):
        last = df.iloc[-1]
        return (
            (last['ema8'] > last['ema21']) &
            (last['rsi'] > 50) &
            (last['macd'] > last['macd_signal']) &
            (last['hammer'] == 1)
        )

    def check_sell_signal(self, df):
        last = df.iloc[-1]
        return (
            (last['ema8'] < last['ema21']) |
            (last['rsi'] < 30) |
            (last['engulfing'] == -100)
        )

# ---------------------- إرسال الإشعارات ----------------------
def send_telegram_alert(signal):
    try:
        profit_pct = (signal['target'] / signal['price'] - 1) * 100
        loss_pct = (signal['stop_loss'] / signal['price'] - 1) * 100
        message = (
            f"🚨 إشارة جديدة: {signal['symbol']}\n"
            f"السعر: {signal['price']}\n"
            f"الهدف: {signal['target']} (+{profit_pct:.2f}%)\n"
            f"وقف الخسارة: {signal['stop_loss']} ({loss_pct:.2f}%)\n"
            f"السيولة: {signal['volume']:,.2f} USDT\n"
            f"الإطار الزمني: 5 دقائق"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        requests.post(url, json={'chat_id': chat_id, 'text': message})
        logger.info(f"✅ تم إرسال إشعار {signal['symbol']}")
    except Exception as e:
        logger.error(f"❌ خطأ في الإرسال: {e}")

# ---------------------- تتبع الإشارات ----------------------
def track_signals():
    while True:
        try:
            check_db_connection()
            cur.execute("SELECT * FROM signals WHERE closed_at IS NULL")
            signals = cur.fetchall()
            
            for signal in signals:
                symbol = signal[1]
                entry = signal[2]
                target = signal[3]
                stop = signal[4]
                
                if symbol not in ticker_data:
                    continue
                    
                current_price = float(ticker_data[symbol]['c'])
                df = fetch_historical_data(symbol)
                df = FreqtradeStrategy().populate_indicators(df)
                
                # تحديث الهدف ووقف الخسارة
                atr = df['atr'].iloc[-1]
                new_target = max(target, current_price + 1.5*atr)
                new_stop = min(stop, current_price - 1.5*atr)
                
                if new_target != target or new_stop != stop:
                    cur.execute(
                        "UPDATE signals SET target = %s, stop_loss = %s WHERE symbol = %s",
                        (new_target, new_stop, symbol)
                    )
                    conn.commit()
                    send_telegram_alert({
                        'symbol': symbol,
                        'price': entry,
                        'target': new_target,
                        'stop_loss': new_stop,
                        'volume': signal[6]
                    })
                
                # التحقق من الإغلاق
                if current_price >= new_target:
                    cur.execute(
                        "UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE symbol = %s",
                        (symbol,)
                    )
                    conn.commit()
                elif current_price <= new_stop:
                    cur.execute(
                        "UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE symbol = %s",
                        (symbol,)
                    )
                    conn.commit()
        except Exception as e:
            logger.error(f"❌ خطأ في التتبع: {e}")
        time.sleep(60)

# ---------------------- جلب البيانات ----------------------
def fetch_historical_data(symbol, interval='5m', days=2):
    klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    df = df.astype(float)
    return df[['open', 'high', 'low', 'close', 'volume']]

# ---------------------- التحليل الدوري ----------------------
def analyze_market():
    symbols = get_crypto_symbols()
    for symbol in symbols:
        try:
            df = fetch_historical_data(symbol)
            strategy = FreqtradeStrategy()
            df = strategy.populate_indicators(df)
            
            if strategy.check_buy_signal(df):
                volume = df['volume'].iloc[-15:].sum()
                if volume > 40000:
                    signal = {
                        'symbol': symbol,
                        'price': df['close'].iloc[-1],
                        'target': df['close'].iloc[-1] + 2*df['atr'].iloc[-1],
                        'stop_loss': df['close'].iloc[-1] - 2*df['atr'].iloc[-1],
                        'volume': volume
                    }
                    send_telegram_alert(signal)
                    save_signal_to_db(signal)
        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")

def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            return [line.strip().upper() + 'USDT' for line in f if line.strip()]
    except Exception as e:
        logger.error(f"❌ خطأ في قراءة الملف: {e}")
        return []

def save_signal_to_db(signal):
    try:
        cur.execute("""
            INSERT INTO signals 
            (symbol, entry_price, target, stop_loss, volume_15m)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            signal['symbol'],
            signal['price'],
            signal['target'],
            signal['stop_loss'],
            signal['volume']
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ خطأ في حفظ الإشارة: {e}")
        conn.rollback()

# ---------------------- Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 البوت يعمل!", 200

# ---------------------- التشغيل ----------------------
if __name__ == '__main__':
    init_db()
    Thread(target=run_ticker_socket_manager).start()
    Thread(target=track_signals).start()
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
