import os
import time
import pandas as pd
import numpy as np
from binance.client import Client
from binance import BinanceSocketManager
import logging
import requests
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from decouple import config
import psycopg2
from psycopg2.extras import RealDictCursor
from threading import Thread
from flask import Flask, jsonify

# إعدادات التسجيل
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('signals.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# تحميل المتغيرات البيئية
api_key = config('BINANCE_API_KEY')
api_secret = config('BINANCE_API_SECRET')
telegram_token = config('TELEGRAM_BOT_TOKEN')
chat_id = config('TELEGRAM_CHAT_ID')
database_url = config('DATABASE_URL')

# إعداد عميل Binance
try:
    client = Client(api_key, api_secret, {"verify": True, "timeout": 20})
    bm = BinanceSocketManager(client)
    logger.info("اتصال Binance ناجح ✅")
except Exception as e:
    logger.error(f"فشل الاتصال بـ Binance: {e}")
    raise

# وظائف قاعدة البيانات
def create_signals_table():
    try:
        conn = psycopg2.connect(database_url, sslmode='require')
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            entry_price NUMERIC(18, 8) NOT NULL,
            target_price NUMERIC(18, 8) NOT NULL,
            stop_loss NUMERIC(18, 8) NOT NULL,
            status VARCHAR(10) DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        );
        """)
        conn.commit()
        logger.info("تم تهيئة الجدول بنجاح 🗂")
    except Exception as e:
        logger.error(f"خطأ في قاعدة البيانات: {e}")
        raise
    finally:
        if 'conn' in locals():
            cursor.close()
            conn.close()

# قراءة الأزواج من ملف pairs.txt
def get_pairs_from_file():
    try:
        with open('pairs.txt', 'r') as file:
            pairs = [line.strip() for line in file if line.strip()]
            logger.info(f"الأزواج المقروءة من الملف: {pairs}")
            return pairs
    except Exception as e:
        logger.error(f"خطأ في قراءة الملف: {e}")
        return []

# وظائف التحليل الفني
def get_historical_data(symbol, interval='5m', lookback='48 hours ago UTC'):
    try:
        klines = client.get_historical_klines(
            symbol=symbol,
            interval=interval,
            start_str=lookback,
            limit=1000
        )
        df = pd.DataFrame(klines, columns=[
            'time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
            'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['volume'] = df['volume'].astype(float)
        logger.info(f"تم جلب {len(df)} صف لـ {symbol}.")
        return df
    except Exception as e:
        logger.error(f"فشل جلب البيانات لـ {symbol}: {e}")
        return pd.DataFrame()

def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'].fillna(50, inplace=True)  # تعيين قيمة افتراضية لـ RSI إذا كانت NaN
    return df

def calculate_ma(df):
    df['ma_10'] = df['close'].rolling(window=10, min_periods=1).mean()
    df['ma_50'] = df['close'].rolling(window=50, min_periods=1).mean()
    return df

def clean_data(df):
    df = df.replace([np.inf, -np.inf], np.nan)
    df.dropna(inplace=True)
    # إذا كانت البيانات فارغة بعد التنظيف، قم بإرجاع DataFrame فارغ
    if df.empty:
        logger.warning("البيانات فارغة بعد التنظيف.")
        return pd.DataFrame()
    return df

# تدريب النموذج
def train_model(df):
    try:
        df = clean_data(df)
        if df.empty or 'close' not in df.columns or len(df) < 50:
            logger.warning("البيانات غير كافية للتدريب.")
            return None, None
        df['return'] = df['close'].pct_change().shift(-1)  # Shift to avoid lookahead bias
        df.dropna(inplace=True)
        df = calculate_ma(df)
        df = calculate_rsi(df)
        # فحص القيم المفقودة بعد الحسابات
        if df.isnull().values.any():
            logger.error("هناك قيم مفقودة في البيانات بعد الحسابات.")
            return None, None
        X = df[['return', 'ma_10', 'ma_50', 'rsi']]
        y = df['close']
        if X.isnull().values.any():
            logger.error("هناك قيم مفقودة في البيانات المدخلة للنموذج.")
            return None, None
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)
        test_score = model.score(X_test, y_test)
        return (model, test_score) if test_score > 0.6 else (None, None)
    except Exception as e:
        logger.error(f"خطأ في التدريب: {e}")
        return None, None

# دورة التداول
def trading_cycle():
    symbols = get_pairs_from_file()
    for symbol in symbols:
        try:
            df = get_historical_data(symbol)
            if len(df) < 100:
                logger.warning(f"البيانات غير كافية لـ {symbol}.")
                continue
            logger.info(f"البيانات الأولية لـ {symbol}:\n{df.tail()}")
            df = calculate_rsi(df)
            logger.info(f"البيانات بعد حساب RSI لـ {symbol}:\n{df.tail()}")
            df = calculate_ma(df)
            logger.info(f"البيانات بعد حساب MA لـ {symbol}:\n{df.tail()}")
            df = clean_data(df)
            logger.info(f"البيانات بعد التنظيف لـ {symbol}:\n{df.tail()}")
            model, accuracy = train_model(df)
            if not model:
                logger.warning(f"فشل تدريب النموذج لـ {symbol}.")
                continue
            last_row = df.iloc[-1]
            input_data = pd.DataFrame([[last_row['return'], last_row['ma_10'], last_row['ma_50'], last_row['rsi']]],
                                      columns=['return', 'ma_10', 'ma_50', 'rsi'])
            predicted_price = model.predict(input_data)[0]
            price_change = (predicted_price - last_row['close']) / last_row['close']
            if 30 < last_row['rsi'] < 70 and price_change > 0.005:
                logger.info(f"إشارة شراء لـ {symbol} بناءً على التوقعات")
                # هنا يمكنك إضافة المزيد من الشروط وإرسال الإشارات عبر Telegram أو إدخالها في قاعدة البيانات
        except Exception as e:
            logger.error(f"خطأ في معالجة {symbol}: {e}")

# نظام المراقبة
app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "active", "timestamp": time.time()})

def run_flask():
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    create_signals_table()
    Thread(target=run_flask, daemon=True).start()
    while True:
        logger.info("بدء دورة تحليل جديدة...")
        trading_cycle()
        time.sleep(300)
