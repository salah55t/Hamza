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
import ta

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
def generate_signal(symbol, df):
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

        # حساب الهدف الأولي ووقف الخسارة
        target = current_price + 2 * current_atr
        stop_loss = current_price - 1.5 * current_atr

        signal = {
            'symbol': symbol,
            'price': round(current_price, 8),
            'target': round(target, 8),
            'stop_loss': round(stop_loss, 8),
            'indicators': {
                'ema8': last_row['ema8'],
                'ema21': last_row['ema21'],
                'rsi': last_row['rsi'],
                'atr': current_atr
            }
        }
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

# ---------------------- إرسال التنبيهات ----------------------
def send_telegram_alert(signal, volume):
    try:
        profit = ((signal['target'] / signal['price']) - 1) * 100
        message = f"""
🚨 **إشارـة تجديد**  
✨ **الزوج**: {signal['symbol']}  
💰 **السعر الحالي**: ${signal['price']:.8f}  
🎯 **الهدف**: ${signal['target']:.8f} (+{profit:.2f}%)  
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
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"❌ فشل إرسال الإشعار: {e}")

def send_telegram_alert_special(message):
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"Telegram Response: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ فشل إرسال التنبيه الخاص: {e}")

# ---------------------- تتبع الإشارات ----------------------
def track_signals():
    while True:
        try:
            check_db_connection()
            cur.execute("SELECT * FROM signals WHERE closed_at IS NULL")
            active_signals = cur.fetchall()

            for signal in active_signals:
                signal_id, symbol, entry_price, current_target, current_stop, volume = signal[:6]
                current_price = ticker_data.get(symbol, {}).get('c', 0)

                if not current_price:
                    continue

                # الحصول على بيانات حديثة لحساب المؤشرات
                df = fetch_historical_data(symbol, interval='5m', days=1)
                if df is None or len(df) < 50:
                    continue

                df = FreqtradeStrategy().populate_indicators(df)
                last_row = df.iloc[-1]

                # حساب الهدف ووقف الخسارة الديناميكي
                new_atr = last_row['atr']
                new_stop = current_price - (new_atr * 1.5)
                new_target = current_price + (new_atr * 2)

                # تحديث الهدف عند تحقيقه
                if current_price > current_target:
                    new_target = current_price + (new_atr * 2)
                    current_target = new_target

                # تحريك وقف الخسارة تلقائيًا
                if current_price > entry_price and new_stop > current_stop:
                    current_stop = new_stop

                # التحقق من الحاجة لتحديث البيانات
                if current_stop != signal[4] or current_target != signal[3]:
                    # تحديث قاعدة البيانات
                    cur.execute(
                        "UPDATE signals SET target = %s, stop_loss = %s WHERE id = %s",
                        (new_target, new_stop, signal_id)
                    )
                    conn.commit()

                    # إرسال تنبيه بالتحديث
                    send_telegram_alert_special(
                        f"🔄 **تحديث الإشارات لـ {symbol}:**\n"
                        f"• الهدف الجديد: ${new_target:.8f}\n"
                        f"• وقف الخسارة الجديد: ${new_stop:.8f}"
                    )

                # إغلاق الإشارة عند تحقيق الهدف
                if current_price >= new_target:
                    profit = ((current_price / entry_price) - 1) * 100
                    send_telegram_alert_special(
                        f"🎉 **تحقيق الهدف لـ {symbol}!**\n"
                        f"• سعر الدخول: ${entry_price:.8f}\n"
                        f"• سعر الخروج: ${current_price:.8f}\n"
                        f"• ربح: {profit:.2f}%"
                    )
                    cur.execute(
                        "UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s",
                        (signal_id,)
                    )
                    conn.commit()

                # إغلاق الإشارة عند ضرب وقف الخسارة
                elif current_price <= new_stop:
                    loss = ((current_price / entry_price) - 1) * 100
                    send_telegram_alert_special(
                        f"❌ **ضرب وقف الخسارة لـ {symbol}!**\n"
                        f"• سعر الدخول: ${entry_price:.8f}\n"
                        f"• سعر الخروج: ${current_price:.8f}\n"
                        f"• خسارة: {abs(loss):.2f}%"
                    )
                    cur.execute(
                        "UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s",
                        (signal_id,)
                    )
                    conn.commit()

        except Exception as e:
            logger.error(f"❌ خطأ في تتبع الإشارات: {e}")
        time.sleep(60)  # تحديث كل دقيقة واحدة

# ---------------------- تحليل السوق ----------------------
def analyze_market():
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active = cur.fetchone()[0]
        if active >= 4:
            return

        symbols = get_crypto_symbols()
        for symbol in symbols:
            df = fetch_historical_data(symbol, interval='5m', days=2)
            if df is None or len(df) < 50:
                continue

            signal = generate_signal(symbol, df)
            if not signal:
                continue

            volume = fetch_volume(symbol)
            if volume < 40000:
                continue

            send_telegram_alert(signal, volume)
            cur.execute(
                "INSERT INTO signals (symbol, entry_price, target, stop_loss, volume_15m) VALUES (%s, %s, %s, %s, %s)",
                (signal['symbol'], signal['price'], signal['target'], signal['stop_loss'], volume)
            )
            conn.commit()

    except Exception as e:
        logger.error(f"❌ خطأ في تحليل السوق: {e}")

# ---------------------- وظائف Telegram ----------------------
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logger.info("Received update: " + str(update))
    
    if "callback_query" in update:
        callback_data = update["callback_query"]["data"]
        if callback_data == "get_report":
            send_report(update["callback_query"]["message"]["chat"]["id"])
            
    return '', 200

def send_report(target_chat_id):
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]
        
        cur.execute("SELECT achieved_target, entry_price, target, stop_loss FROM signals WHERE closed_at IS NOT NULL")
        closed_signals = cur.fetchall()
        
        success = sum(1 for row in closed_signals if row[0])
        loss = len(closed_signals) - success
        avg_profit = sum(
            (row[2]/row[1] - 1)*100 for row in closed_signals if row[0]
        ) / (success or 1)
        avg_loss = sum(
            (row[3]/row[1] - 1)*100 for row in closed_signals if not row[0]
        ) / (loss or 1)
        
        report = (
            f"📊 **تقرير الأداء**: \n"
            f"✅ توصيات ناجحة: {success}\n"
            f"❌ توصيات خاسرة: {loss}\n"
            f"⏳ نشطة: {active_count}\n"
            f"📈 متوسط الربح: {avg_profit:.2f}%\n"
            f"📉 متوسط الخسارة: {avg_loss:.2f}%"
        )
        send_telegram_alert_special(report)
    except Exception as e:
        logger.error(f"❌ فشل إرسال التقرير: {e}")

# ---------------------- تشغيل Flask ----------------------
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    
    threads = [
        Thread(target=run_ticker_socket_manager, daemon=True),
        Thread(target=track_signals, daemon=True),
        Thread(target=run_flask, daemon=True)
    ]
    for thread in threads:
        thread.start()
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()
