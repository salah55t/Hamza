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

TRADE_VALUE = 10  # قيمة الصفقة الثابتة للتوصيات

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
        logger.error(f"❌ خطأ في handle_ticker_message: {e}")

def run_ticker_socket_manager():
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("✅ تم تشغيل WebSocket لتحديث التيكر لجميع الأزواج.")
    except Exception as e:
        logger.error(f"❌ خطأ في تشغيل WebSocket: {e}")

# ---------------------- دوال حساب المؤشرات الفنية ----------------------
def calculate_macd(df, fast=12, slow=26, signal=9):
    df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
    df['macd'] = df['ema_fast'] - df['ema_slow']
    df['macd_signal'] = df['macd'].ewm(span=signal, adjust=False).mean()
    df['macd_histogram'] = df['macd'] - df['macd_signal']
    return df

def calculate_atr_indicator(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df

def detect_candlestick_patterns(df):
    # نموذج Three White Soldiers
    df['three_white_soldiers'] = (
        (df['close'] > df['open']) &
        (df['close'].shift(1) > df['open'].shift(1)) &
        (df['close'].shift(2) > df['open'].shift(2)) &
        (df['open'] < df['close'].shift(1)) &
        (df['open'].shift(1) < df['close'].shift(2))
    ).astype(int)
    
    # نموذج Three Black Crows
    df['three_black_crows'] = (
        (df['close'] < df['open']) &
        (df['close'].shift(1) < df['open'].shift(1)) &
        (df['close'].shift(2) < df['open'].shift(2)) &
        (df['open'] > df['close'].shift(1)) &
        (df['open'].shift(1) > df['close'].shift(2))
    ).astype(int)
    
    # نموذج Hammer
    df['hammer'] = (
        (df['low'] < df['open']) &
        (df['low'] < df['close']) &
        (df['close'] > (df['high'] + df['low'])/2)
    ).astype(int)
    
    return df

# ---------------------- تعريف استراتيجية Freqtrade ----------------------
class FreqtradeStrategy:
    stoploss = -0.02
    minimal_roi = {"0": 0.01}

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 50:
            return df
            
        # حساب المؤشرات الفنية
        df['ema8'] = df['close'].ewm(span=8, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['stoch_rsi'] = StochRSIIndicator(df['close']).stochrsi()
        df = calculate_macd(df)
        df = calculate_atr_indicator(df)
        df = detect_candlestick_patterns(df)
        
        # حساب بولينجر باندز
        df['sma20'] = df['close'].rolling(window=20).mean()
        df['std20'] = df['close'].rolling(window=20).std()
        df['upper_band'] = df['sma20'] + (2 * df['std20'])
        df['lower_band'] = df['sma20'] - (2 * df['std20'])
        
        return df

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema8'] > df['ema21']) &
            (df['macd'] > df['macd_signal']) &
            (df['stoch_rsi'] < 0.2) &  # شراء عند تشبع بيع
            (df['three_white_soldiers'] == 1) &
            (df['hammer'] == 1)
        )
        df.loc[conditions, 'buy'] = 1
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema8'] < df['ema21']) |
            (df['macd'] < df['macd_signal']) |
            (df['stoch_rsi'] > 0.8) |  # بيع عند تشبع شراء
            (df['three_black_crows'] == 1)
        )
        df.loc[conditions, 'sell'] = 1
        return df

# ---------------------- دالة توليد الإشارة باستخدام استراتيجية Freqtrade ----------------------
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
        
        # حساب الهدف ووقف الخسارة الديناميكي
        target = current_price + 2 * current_atr
        stop_loss = current_price - 1.5 * current_atr
        
        # التأكد من نسبة الربح المطلوبة
        profit_margin = (target / current_price - 1) * 100
        if profit_margin < 1:
            logger.info(f"⚠️ إشارة {symbol} لا تضمن ربح أكثر من 1% (الربح المتوقع: {profit_margin:.2f}%).")
            return None

        signal = {
            'symbol': symbol,
            'price': float(format(current_price, '.8f')),
            'target': float(format(target, '.8f')),
            'stop_loss': float(format(stop_loss, '.8f')),
            'strategy': 'freqtrade_day_trade',
            'indicators': {
                'ema8': last_row['ema8'],
                'ema21': last_row['ema21'],
                'ema200': last_row['ema200'],
                'rsi': last_row['rsi'],
                'stoch_rsi': last_row['stoch_rsi'],
                'macd': last_row['macd'],
                'macd_signal': last_row['macd_signal'],
                'atr': current_atr,
            },
            'trade_value': TRADE_VALUE
        }
        logger.info(f"✅ تم توليد إشارة من استراتيجية Freqtrade للزوج {symbol}: {signal}")
        return signal
    else:
        logger.info(f"[{symbol}] الشروط غير مستوفاة في استراتيجية Freqtrade.")
        return None

# ---------------------- خدمة تتبع الإشارات مع وقف خسارة وتحقيق أهداف متحركين ----------------------
def track_signals():
    logger.info("⏳ بدء خدمة تتبع الإشارات...")
    while True:
        try:
            check_db_connection()
            cur.execute("""
                SELECT id, symbol, entry_price, target, stop_loss 
                FROM signals 
                WHERE achieved_target = FALSE 
                  AND hit_stop_loss = FALSE 
                  AND closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            logger.info(f"✅ تم العثور على {len(active_signals)} إشارة نشطة للتتبع.")
            
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss = signal
                try:
                    if symbol not in ticker_data:
                        logger.warning(f"⚠️ لا يوجد تحديث أسعار لحظة {symbol} من WebSocket.")
                        continue
                        
                    current_price = float(ticker_data[symbol].get('c', 0))
                    df = fetch_historical_data(symbol, interval='5m', days=1)
                    df = FreqtradeStrategy().populate_indicators(df)
                    last_row = df.iloc[-1]
                    
                    # تحديث الهدف ووقف الخسارة بناءً على التقلبات
                    volatility = last_row['atr'] * 2
                    new_target = max(target, current_price + volatility)
                    new_stop = min(stop_loss, current_price - volatility * 0.5)
                    
                    # تعديل باستخدام MACD
                    if last_row['macd'] > last_row['macd_signal']:
                        new_target *= 1.05  # زيادة الهدف بنسبة 5% عند صعود MACD
                        new_stop = max(new_stop, current_price - volatility * 0.3)
                    
                    # تحديث بناءً على الاتجاه العام
                    if last_row['ema200'] < current_price:
                        new_target *= 1.1
                        new_stop = max(new_stop, current_price - volatility * 0.2)
                    
                    # تحديث قيم الهدف والوقف في قاعدة البيانات
                    if new_target != target or new_stop != stop_loss:
                        cur.execute(
                            "UPDATE signals SET target = %s, stop_loss = %s WHERE id = %s",
                            (new_target, new_stop, signal_id)
                        )
                        conn.commit()
                        logger.info(f"🔄 تم تحديث الهدف/وقف الخسارة للزوج {symbol}: الهدف={new_target}, وقف={new_stop}")
                        send_telegram_alert_special(
                            f"🔄 تحديث الهدف/وقف الخسارة لـ {symbol}\n"
                            f"الهدف الجديد: {new_target:.8f}\n"
                            f"وقف الخسارة الجديد: {new_stop:.8f}"
                        )
                    
                    # التحقق من تحقيق الهدف أو وقف الخسارة
                    if current_price >= new_target:
                        cur.execute(
                            "UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s",
                            (signal_id,)
                        )
                        conn.commit()
                        send_telegram_alert_special(f"🎉 تحقيق الهدف لـ {symbol} عند {current_price:.8f}")
                        logger.info(f"✅ تم إغلاق التوصية للزوج {symbol} بعد تحقيق الهدف.")
                    
                    elif current_price <= new_stop:
                        cur.execute(
                            "UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s",
                            (signal_id,)
                        )
                        conn.commit()
                        send_telegram_alert_special(f"🛑 ضرب وقف الخسارة لـ {symbol} عند {current_price:.8f}")
                        logger.info(f"❌ تم إغلاق التوصية للزوج {symbol} عند وقف الخسارة.")
                        
                except Exception as inner_e:
                    logger.error(f"❌ خطأ في تتبع الإشارة {symbol}: {inner_e}")
                    conn.rollback()
                    continue

        except Exception as e:
            logger.error(f"❌ خطأ عام في تتبع الإشارات: {e}")
            time.sleep(10)

# ---------------------- إرسال التنبيهات عبر Telegram مع المؤشرات الجديدة ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        message = (
            f"🚨 **إشارة تداول جديدة - {signal['symbol']}**\n\n"
            f"▫️ السعر الحالي: ${signal['price']}\n"
            f"🎯 الهدف: ${signal['target']} (+{profit}%)\n"
            f"🛑 وقف الخسارة: ${signal['stop_loss']} ({loss}%)\n"
            f"⏱ الفريم: {timeframe}\n"
            f"💧 السيولة (15 دقيقة): {volume:,.2f} USDT\n"
            f"💵 قيمة الصفقة: ${TRADE_VALUE}\n\n"
            f"📈 **المؤشرات الفنية:**\n"
            f"- EMA8: {signal['indicators']['ema8']:.2f}\n"
            f"- EMA21: {signal['indicators']['ema21']:.2f}\n"
            f"- EMA200: {signal['indicators']['ema200']:.2f}\n"
            f"- RSI: {signal['indicators']['rsi']:.2f}\n"
            f"- Stochastic RSI: {signal['indicators']['stoch_rsi']:.2f}\n"
            f"- MACD: {signal['indicators']['macd']:.2f} / Signal: {signal['indicators']['macd_signal']:.2f}\n\n"
            f"⏰ {time.strftime('%Y-%m-%d %H:%M')}"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        requests.post(url, json=payload, timeout=10)
        logger.info(f"✅ تم إرسال إشعار التوصية للزوج {signal['symbol']} بنجاح.")
    except Exception as e:
        logger.error(f"❌ فشل إرسال إشعار التوصية للزوج {signal['symbol']}: {e}")

# ---------------------- تحليل السوق الرئيسي ----------------------
def analyze_market():
    try:
        symbols = get_crypto_symbols()
        active_signals_count = get_active_signals_count()
        
        if active_signals_count >= 4:
            logger.info("⚠️ عدد التوصيات النشطة وصل إلى الحد الأقصى (4).")
            return

        btc_dominance, eth_dominance = get_market_dominance()
        for symbol in symbols:
            try:
                df_5m = fetch_historical_data(symbol, interval='5m', days=2)
                signal = generate_signal_using_freqtrade_strategy(df_5m, symbol)
                
                if not signal:
                    continue
                    
                volume_15m = fetch_recent_volume(symbol)
                if volume_15m < 40000:
                    logger.info(f"⚠️ تجاهل {symbol} - سيولة منخفضة: {volume_15m:,.2f}.")
                    continue

                send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance, '5m')
                save_signal_to_db(signal, volume_15m)
                
            except Exception as e:
                logger.error(f"❌ خطأ في تحليل الزوج {symbol}: {e}")
                continue

    except Exception as e:
        logger.error(f"❌ خطأ في تحليل السوق: {e}")

# ---------------------- دوال مساعدة ----------------------
def get_active_signals_count():
    check_db_connection()
    cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
    return cur.fetchone()[0]

def save_signal_to_db(signal, volume):
    try:
        cur.execute("""
            INSERT INTO signals 
            (symbol, entry_price, target, stop_loss, r2_score, volume_15m)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            signal['symbol'],
            signal['price'],
            signal['target'],
            signal['stop_loss'],
            100,  # قيمة افتراضية لـ r2_score
            volume
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ فشل إدخال الإشارة: {e}")
        conn.rollback()

# ---------------------- تشغيل Flask وباقي الخدمات ----------------------
if __name__ == '__main__':
    init_db()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    Thread(target=track_signals, daemon=True).start()
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    app = Flask(__name__)
    @app.route('/')
    def home():
        return "🚀 نظام التوصيات يعمل بكفاءة.", 200
        
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
