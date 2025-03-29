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
from sklearn.ensemble import GradientBoostingRegressor # For price prediction

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

logger.info(f" TELEGRAM_BOT_TOKEN: {telegram_token[:10]}...")
logger.info(f" TELEGRAM_CHAT_ID: {chat_id}")

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

# متغيّر عالمي للتحكم بتوليد توصيات جديدة (تتم إدارته الآن بواسطة check_open_recommendations)
# allow_new_recommendations = True # No longer needed, logic moved to check function

# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
conn = None
cur = None

def init_db():
    global conn, cur
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False # مهم للمعاملات الآمنة
        cur = conn.cursor()
        # إنشاء الجدول إذا لم يكن موجودًا، مع الأعمدة المحدثة
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL,
                initial_target DOUBLE PRECISION NOT NULL, -- الهدف الأولي
                initial_stop_loss DOUBLE PRECISION NOT NULL, -- وقف الخسارة الأولي
                current_target DOUBLE PRECISION NOT NULL, -- الهدف الحالي (قد يتم تحديثه)
                current_stop_loss DOUBLE PRECISION NOT NULL, -- وقف الخسارة الحالي (قد يتم تحديثه)
                r2_score DOUBLE PRECISION, -- اسم العمود تم الإبقاء عليه، قد يمثل ثقة أو مؤشر آخر
                volume_15m DOUBLE PRECISION,
                achieved_target BOOLEAN DEFAULT FALSE,
                hit_stop_loss BOOLEAN DEFAULT FALSE,
                closed_at TIMESTAMP,
                sent_at TIMESTAMP DEFAULT NOW(),
                profit_percentage DOUBLE PRECISION,
                profitable_stop_loss BOOLEAN DEFAULT FALSE, -- هل تم الإغلاق فوق سعر الدخول بواسطة وقف الخسارة
                is_trailing_active BOOLEAN DEFAULT FALSE -- هل الوقف المتحرك مفعل؟
            )
        """)
        # التأكد من إضافة الأعمدة الجديدة إذا كان الجدول موجوداً من قبل
        try:
            cur.execute("ALTER TABLE signals ADD COLUMN initial_target DOUBLE PRECISION")
            cur.execute("ALTER TABLE signals ADD COLUMN initial_stop_loss DOUBLE PRECISION")
            cur.execute("ALTER TABLE signals ADD COLUMN current_target DOUBLE PRECISION")
            cur.execute("ALTER TABLE signals ADD COLUMN current_stop_loss DOUBLE PRECISION")
            cur.execute("ALTER TABLE signals ADD COLUMN is_trailing_active BOOLEAN DEFAULT FALSE")
            # تحديث الأعمدة القديمة إذا لزم الأمر (مثال)
            # cur.execute("UPDATE signals SET current_target = target, current_stop_loss = stop_loss WHERE current_target IS NULL")
            logger.info("✅ [DB] تم تحديث بنية جدول 'signals' بالأعمدة الجديدة.")
        except psycopg2.Error as e:
            # تجاهل الخطأ إذا كانت الأعمدة موجودة بالفعل (duplicate_column)
            if e.pgcode == '42701': # duplicate_column code
                 logger.info("ℹ️ [DB] الأعمدة الجديدة موجودة بالفعل في جدول 'signals'.")
                 conn.rollback() # مهم للتراجع عن محاولة الإضافة الفاشلة
            else:
                 logger.error(f"❌ [DB] فشل في تعديل جدول 'signals': {e}")
                 conn.rollback()
                 raise
        conn.commit()
        logger.info("✅ [DB] تم تهيئة قاعدة البيانات بنجاح.")
    except Exception as e:
        logger.error(f"❌ [DB] فشل تهيئة قاعدة البيانات: {e}")
        if conn:
             conn.rollback() # التراجع في حالة الفشل
        raise

def check_db_connection():
    global conn, cur
    try:
        if conn is None or conn.closed != 0:
             logger.warning("⚠️ [DB] الاتصال مغلق، محاولة إعادة الاتصال...")
             init_db()
             return

        cur.execute("SELECT 1")
        # لا حاجة لـ commit هنا لأنها مجرد قراءة
    except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
        logger.warning(f"⚠️ [DB] فقدان الاتصال بقاعدة البيانات ({e}). محاولة إعادة الاتصال...")
        try:
            # لا تغلق الاتصال صراحة هنا، init_db ستتعامل معه
            init_db()
        except Exception as ex:
            logger.error(f"❌ [DB] فشل إعادة الاتصال: {ex}")
            raise
    except Exception as e:
        logger.error(f"❌ [DB] خطأ غير متوقع في فحص الاتصال: {e}")
        # ربما محاولة إعادة الاتصال هنا أيضًا؟
        try:
            init_db()
        except Exception as ex:
            logger.error(f"❌ [DB] فشل إعادة الاتصال بعد خطأ غير متوقع: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
client = Client(api_key, api_secret)

# ---------------------- استخدام WebSocket لتحديث بيانات التيكر ----------------------
ticker_data = {} # قاموس لتخزين أحدث بيانات التيكر لكل زوج

def handle_ticker_message(msg):
    """يعالج رسائل WebSocket الواردة ويحدث قاموس ticker_data."""
    try:
        # يمكن أن تأتي البيانات كقائمة أو ككائن واحد
        if isinstance(msg, list):
            for m in msg:
                symbol = m.get('s')
                if symbol:
                    ticker_data[symbol] = {
                        'c': m.get('c'), # السعر الحالي
                        'h': m.get('h'), # أعلى سعر في 24 ساعة
                        'l': m.get('l'), # أدنى سعر في 24 ساعة
                        'v': m.get('v')  # حجم التداول في 24 ساعة
                    }
        elif isinstance(msg, dict) and 'stream' not in msg: # التأكد من أنها رسالة بيانات وليست رسالة خطأ/اتصال
            symbol = msg.get('s')
            if symbol:
                 ticker_data[symbol] = {
                        'c': msg.get('c'),
                        'h': msg.get('h'),
                        'l': msg.get('l'),
                        'v': msg.get('v')
                    }
    except Exception as e:
        logger.error(f"❌ [WS] خطأ في handle_ticker_message: {e}")

def run_ticker_socket_manager():
    """يشغل WebSocket Manager للحصول على تحديثات الأسعار الحية."""
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        # استخدام start_miniticker_socket للحصول على تحديثات لجميع الأزواج
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("✅ [WS] تم تشغيل WebSocket لتحديث التيكر لجميع الأزواج.")
        twm.join() # الانتظار حتى يتم إيقاف المدير (مهم لمنع الخروج المبكر)
    except Exception as e:
        logger.error(f"❌ [WS] خطأ في تشغيل WebSocket: {e}")

# ---------------------- دوال حساب المؤشرات الفنية ----------------------
# (الدوال كما هي في الملف الأصلي، مع التأكد من وجود اللوجر في النقاط المهمة)
def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi_indicator(df, period=14):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    # تجنب القسمة على صفر
    avg_loss = avg_loss.replace(0, np.nan)
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # logger.debug(f"✅ [Indicator] تم حساب RSI: {rsi.iloc[-1]:.2f}") # Use debug level for frequent logs
    return rsi

def calculate_atr_indicator(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1, skipna=False)
    atr = tr.rolling(window=period, min_periods=period).mean()
    df['atr'] = atr
    # logger.debug(f"✅ [Indicator] تم حساب ATR: {df['atr'].iloc[-1]:.8f}")
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
    rsv = (df['close'] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    df['kdj_k'] = rsv.ewm(com=(k_period - 1), adjust=False).mean()
    df['kdj_d'] = df['kdj_k'].ewm(com=(d_period - 1), adjust=False).mean()
    df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']
    return df

def calculate_adx(df, period=14):
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    df['+dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['-dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)

    # استخدام ewm لحساب المتوسط المتحرك الأسي بدلاً من rolling sum لـ TR, +DM, -DM
    # هذا يتوافق أكثر مع التعريف القياسي لـ ADX
    tr = pd.concat([
        (df['high'] - df['low']),
        (df['high'] - df['close'].shift(1)).abs(),
        (df['low'] - df['close'].shift(1)).abs()
    ], axis=1).max(axis=1, skipna=False)

    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (df['+dm'].ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (df['-dm'].ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1/period, adjust=False).mean()

    df['+di'] = plus_di
    df['-di'] = minus_di
    df['adx'] = adx
    # logger.debug(f"✅ [Indicator] تم حساب ADX: {df['adx'].iloc[-1]:.2f}")
    return df

# دوال الكشف عن الأنماط الشمعية (كما هي)
def is_hammer(row):
    open_price = row['open']
    high = row['high']
    low = row['low']
    close = row['close']
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0: return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    # تعريف المطرقة: جسم صغير، ظل سفلي طويل (ضعف الجسم على الأقل)، ظل علوي قصير جداً
    if body > 0 and candle_range > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.3 * body: # تخفيف شرط الظل العلوي قليلاً
        return 100 # إشارة شراء
    return 0

def is_shooting_star(row):
    open_price = row['open']
    high = row['high']
    low = row['low']
    close = row['close']
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0: return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    # تعريف الشهاب: جسم صغير، ظل علوي طويل (ضعف الجسم على الأقل)، ظل سفلي قصير جداً
    if body > 0 and candle_range > 0 and upper_shadow >= 2 * body and lower_shadow <= 0.3 * body:
        return -100 # إشارة بيع
    return 0

def compute_engulfing(df, idx):
    if idx == 0: return 0
    prev = df.iloc[idx - 1]
    curr = df.iloc[idx]
    # ابتلاعية شرائية: شمعة سابقة هابطة، شمعة حالية صاعدة تبتلع جسم السابقة
    if prev['close'] < prev['open'] and curr['close'] > curr['open']:
        if curr['open'] < prev['close'] and curr['close'] > prev['open']:
            return 100 # إشارة شراء
    # ابتلاعية بيعية: شمعة سابقة صاعدة، شمعة حالية هابطة تبتلع جسم السابقة
    if prev['close'] > prev['open'] and curr['close'] < curr['open']:
        if curr['open'] > prev['close'] and curr['close'] < prev['open']:
            return -100 # إشارة بيع
    return 0

def detect_candlestick_patterns(df):
    df['Hammer'] = df.apply(is_hammer, axis=1)
    df['ShootingStar'] = df.apply(is_shooting_star, axis=1)
    # التأكد من عدم وجود أخطاء في المؤشرات إذا كانت السلسلة قصيرة
    engulfing_values = [0] * len(df)
    if len(df) > 1:
        engulfing_values = [compute_engulfing(df, i) for i in range(len(df))]
    df['Engulfing'] = engulfing_values

    # تجميع الإشارات: أي إشارة شراء قوية تعتبر Bullish
    df['BullishSignal'] = df.apply(lambda row: 100 if (row['Hammer'] == 100 or row['Engulfing'] == 100) else 0, axis=1)
    # تجميع الإشارات: أي إشارة بيع قوية تعتبر Bearish
    df['BearishSignal'] = df.apply(lambda row: 100 if (row['ShootingStar'] == -100 or row['Engulfing'] == -100) else 0, axis=1) # Use 100 for intensity, sign is implicit
    # logger.debug("✅ [Candles] تم تحليل الأنماط الشمعية.")
    return df

# ---------------------- دوال التنبؤ وتحليل المشاعر ----------------------
# (يمكن تحسينها بشكل كبير، حالياً هي نماذج بسيطة)
def ml_predict_signal(symbol, df):
    """
    دالة تنبؤية تجريبية بسيطة تعتمد على مؤشر RSI و ADX.
    ترجع قيمة ثقة تقديرية (0 إلى 1).
    """
    try:
        if df.empty or 'rsi' not in df.columns or 'adx' not in df.columns:
            return 0.5 # قيمة محايدة إذا كانت البيانات غير كافية
        rsi = df['rsi'].iloc[-1]
        adx = df['adx'].iloc[-1]
        if pd.isna(rsi) or pd.isna(adx):
            return 0.5
        if rsi < 40 and adx > 25: # شروط شراء محتملة
            return 0.80
        elif rsi > 65 and adx > 25: # شروط بيع محتملة
            return 0.20
        else:
            return 0.5 # حالة محايدة
    except IndexError:
         logger.warning(f"⚠️ [ML] IndexError في ml_predict_signal لـ {symbol}. ربما DataFrame قصير جداً.")
         return 0.5
    except Exception as e:
        logger.error(f"❌ [ML] خطأ في ml_predict_signal لـ {symbol}: {e}")
        return 0.5 # قيمة محايدة في حالة الخطأ

def get_market_sentiment(symbol):
    """
    دالة تحليل مشاعر تجريبية. يمكن ربطها بمصادر خارجية لاحقاً.
    ترجع قيمة تقديرية (0 إلى 1، حيث > 0.5 إيجابي).
    """
    # يمكن إضافة منطق أكثر تعقيداً هنا، مثل تحليل الأخبار أو وسائل التواصل الاجتماعي
    return 0.6 # قيمة إيجابية ثابتة مؤقتًا

def get_fear_greed_index():
    """يجلب مؤشر الخوف والجشع من alternative.me"""
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        response = requests.get(url, timeout=10)
        response.raise_for_status() # يثير خطأ لأكواد الحالة السيئة (4xx or 5xx)
        data = response.json()
        if data.get("data"):
            fng_value = float(data["data"][0].get("value", 50)) # قيمة افتراضية 50 (محايد)
            fng_classification = data["data"][0].get("value_classification", "Neutral")
            logger.info(f"✅ [FNG] مؤشر الخوف والجشع: {fng_value:.0f} - {fng_classification}")
            return fng_value, fng_classification
        else:
            logger.warning("⚠️ [FNG] لم يتم العثور على بيانات في استجابة مؤشر الخوف والجشع.")
            return 50.0, "Neutral"
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [FNG] خطأ في طلب مؤشر الخوف والجشع: {e}")
        return 50.0, "Error"
    except Exception as e:
        logger.error(f"❌ [FNG] خطأ غير متوقع في جلب مؤشر الخوف والجشع: {e}")
        return 50.0, "Error"

# ---------------------- استراتيجية Freqtrade المحسّنة (كفئة) ----------------------
class FreqtradeStrategy:
    # يمكن تعريف بعض المعلمات هنا إذا كانت ثابتة للاستراتيجية
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """يحسب جميع المؤشرات المطلوبة للاستراتيجية."""
        if len(df) < 50: # الحد الأدنى لعدد الشموع المطلوب لحساب المؤشرات
            logger.warning(f"⚠️ [Strategy] DataFrame قصير جداً ({len(df)} شمعة) لحساب المؤشرات.")
            return df # إرجاع DataFrame الأصلي إذا كان قصيراً جداً

        try:
            # المتوسطات المتحركة الأسية
            df['ema5'] = calculate_ema(df['close'], 5)
            df['ema8'] = calculate_ema(df['close'], 8)
            df['ema21'] = calculate_ema(df['close'], 21)
            df['ema34'] = calculate_ema(df['close'], 34)
            df['ema50'] = calculate_ema(df['close'], 50)

            # RSI
            df['rsi'] = calculate_rsi_indicator(df)

            # بولينجر باند
            df['sma20'] = df['close'].rolling(window=20).mean()
            df['std20'] = df['close'].rolling(window=20).std()
            df['upper_band'] = df['sma20'] + (2.5 * df['std20'])
            df['lower_band'] = df['sma20'] - (2.5 * df['std20'])

            # ATR
            df = calculate_atr_indicator(df) # ATR يعتمد على الفترة المحددة في الدالة

            # MACD (بفترات مخصصة)
            df = calculate_macd(df, fast_period=10, slow_period=21, signal_period=8)

            # KDJ
            df = calculate_kdj(df)

            # ADX
            df = calculate_adx(df) # ADX يعتمد على الفترة المحددة في الدالة

            # أنماط الشموع
            df = detect_candlestick_patterns(df)

            logger.info(f"✅ [Strategy] تم حساب المؤشرات لـ DataFrame بحجم {len(df)}")
            return df.dropna() # إزالة الصفوف التي تحتوي على NaN بعد حساب المؤشرات

        except Exception as e:
            logger.error(f"❌ [Strategy] خطأ أثناء حساب المؤشرات: {e}")
            return pd.DataFrame() # إرجاع DataFrame فارغ في حالة الخطأ الفادح


    def composite_buy_score(self, row):
        """يحسب درجة الشراء بناءً على شروط متعددة."""
        score = 0
        try:
            # شرط 1: ترتيب المتوسطات المتحركة (إشارة اتجاه صاعد قوي)
            if row['ema5'] > row['ema8'] > row['ema21'] > row['ema34'] > row['ema50']:
                score += 1.5 # وزن أعلى للاتجاه الواضح

            # شرط 2: RSI منخفض (إشارة تشبع بيعي محتمل)
            if row['rsi'] < 40:
                score += 1

            # شرط 3: السعر قرب الحد السفلي لبولينجر (ارتداد محتمل)
            # استخدام نسبة مئوية لتجنب مشاكل العملات ذات الأسعار المنخفضة جداً
            if row['close'] > row['lower_band'] and ((row['close'] - row['lower_band']) / row['close'] < 0.02): # ضمن 2% فوق الحد السفلي
                score += 1

            # شرط 4: تقاطع MACD إيجابي (زخم صاعد)
            if row['macd'] > row['macd_signal'] and row['macd_hist'] > 0: # Histogram يعزز الإشارة
                score += 1

            # شرط 5: KDJ في منطقة شراء أو تقاطع إيجابي (J > K > D)
            if row['kdj_j'] > row['kdj_k'] and row['kdj_k'] > row['kdj_d'] and row['kdj_j'] < 80: # تجنب مناطق التشبع الشرائي الشديد بـ J
                 score += 1

            # شرط 6: ADX يشير إلى وجود اتجاه (ولكن ليس بالضرورة صاعداً، لذا نستخدمه مع شروط أخرى)
            if row['adx'] > 20: # عتبة أقل لـ ADX للسماح بفرص أكثر
                score += 0.5 # وزن أقل لأنه لا يحدد الاتجاه بمفرده

            # شرط 7: نمط شمعي إيجابي
            if row['BullishSignal'] == 100:
                score += 1.5 # وزن أعلى للأنماط القوية

        except KeyError as e:
            # يحدث هذا إذا لم يتم حساب مؤشر ما بنجاح
            logger.warning(f"⚠️ [Strategy] KeyError في composite_buy_score: {e}. قد يكون المؤشر مفقوداً.")
            return 0 # إرجاع صفر لتجنب إشارة خاطئة
        return score

    # يمكن إضافة composite_sell_score بنفس الطريقة إذا لزم الأمر لتحليل الخروج

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """يحدد إشارات الشراء بناءً على درجة الشراء."""
        required_score = 4.0 # تحديد العتبة المطلوبة لتوليد إشارة شراء

        if df.empty or not all(col in df.columns for col in ['ema5', 'rsi', 'lower_band', 'macd', 'kdj_j', 'adx', 'BullishSignal']):
             logger.warning("⚠️ [Strategy] DataFrame يفتقد لأعمدة مطلوبة لتحديد إشارات الشراء.")
             df['buy'] = 0
             return df

        df['buy_score'] = df.apply(self.composite_buy_score, axis=1)
        # توليد إشارة الشراء (1) فقط إذا تجاوزت الدرجة العتبة
        df['buy'] = np.where(df['buy_score'] >= required_score, 1, 0)

        # تسجيل عدد إشارات الشراء التي تم العثور عليها
        buy_signals_count = df['buy'].sum()
        if buy_signals_count > 0:
             logger.info(f"✅ [Strategy] تم تحديد {buy_signals_count} إشارة/إشارات شراء محتملة (Score >= {required_score}).")
        # else:
        #      logger.debug(f"ℹ️ [Strategy] لم يتم العثور على إشارات شراء تستوفي الشروط (Score >= {required_score}).")

        return df

    # يمكن إضافة populate_sell_trend بنفس الطريقة

# ---------------------- دالة التنبؤ بالسعر المحسّنة (باستخدام نموذج أبسط كمثال) ----------------------
def improved_predict_future_price(symbol, interval='2h', days=30):
    """
    دالة تنبؤ مبسطة باستخدام نموذج الانحدار الخطي كمثال.
    يمكن استبدالها بنموذج أكثر تعقيداً مثل GradientBoostingRegressor أو LSTM.
    """
    try:
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50:
            logger.warning(f"⚠️ [Price Prediction] بيانات غير كافية للتنبؤ لـ {symbol}.")
            return None

        # استخدام مؤشرات بسيطة كميزات
        df['ema_fast'] = calculate_ema(df['close'], 10)
        df['ema_slow'] = calculate_ema(df['close'], 30)
        df['rsi'] = calculate_rsi_indicator(df)
        df = df.dropna()

        if len(df) < 2: # نحتاج على الأقل صفين للتدريب والتنبؤ
            logger.warning(f"⚠️ [Price Prediction] بيانات غير كافية بعد حساب المؤشرات لـ {symbol}.")
            return None

        features = ['ema_fast', 'ema_slow', 'rsi']
        if not all(f in df.columns for f in features):
             logger.error(f"❌ [Price Prediction] الأعمدة المطلوبة للميزات غير موجودة في DataFrame لـ {symbol}.")
             return None

        X = df[features].iloc[:-1].values # استخدام كل البيانات ما عدا الصف الأخير كميزات
        y = df['close'].iloc[1:].values   # استخدام سعر الإغلاق التالي كهدف

        if len(X) == 0:
            logger.warning(f"⚠️ [Price Prediction] لا توجد بيانات كافية للتدريب (X فارغ) لـ {symbol}.")
            return None

        # استخدام نموذج أبسط وأسرع كمثال - Gradient Boosting
        model = GradientBoostingRegressor(n_estimators=100, max_depth=3, random_state=42, learning_rate=0.1)
        model.fit(X, y)

        # التنبؤ باستخدام آخر صف من الميزات
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]

        # التحقق من صحة السعر المتوقع (ليس سالبًا أو غير منطقي)
        if predicted_price <= 0:
            logger.warning(f"⚠️ [Price Prediction] السعر المتوقع غير منطقي ({predicted_price:.8f}) للزوج {symbol}. إرجاع None.")
            return None

        logger.info(f"✅ [Price Prediction] السعر المتوقع للزوج {symbol} ({interval}, {days}d): {predicted_price:.8f}")
        return predicted_price

    except ImportError:
         logger.error("❌ [Price Prediction] مكتبة scikit-learn غير مثبتة. التنبؤ غير ممكن.")
         return None
    except Exception as e:
        logger.error(f"❌ [Price Prediction] خطأ أثناء تنبؤ السعر للزوج {symbol}: {e}")
        return None

# ---------------------- دالة توليد الإشارة باستخدام الاستراتيجية المحسنة ----------------------
def generate_signal_using_freqtrade_strategy(df_input, symbol):
    """
    تولد إشارة شراء بناءً على استراتيجية Freqtrade المحسنة وتحقق من الشروط الإضافية.
    """
    if df_input is None or df_input.empty:
        logger.warning(f"⚠️ [Signal Gen] DataFrame فارغ للزوج {symbol}.")
        return None

    # التأكد من أن DataFrame يحتوي على عدد كافٍ من الصفوف قبل إرساله إلى الاستراتيجية
    if len(df_input) < 50:
         logger.info(f"ℹ️ [Signal Gen] بيانات غير كافية ({len(df_input)} شمعة) للزوج {symbol} على الفريم المستخدم لتوليد الإشارة.")
         return None

    strategy = FreqtradeStrategy()
    # حساب المؤشرات أولاً
    df_processed = strategy.populate_indicators(df_input.copy()) # استخدام نسخة لتجنب تعديل الأصلي

    if df_processed.empty:
        logger.warning(f"⚠️ [Signal Gen] DataFrame فارغ بعد حساب المؤشرات للزوج {symbol}.")
        return None

    # تحديد إشارات الشراء المحتملة
    df_with_signals = strategy.populate_buy_trend(df_processed)

    # التحقق من وجود إشارة في آخر شمعة مكتملة
    if df_with_signals.empty or df_with_signals['buy'].iloc[-1] != 1:
        # logger.debug(f"ℹ️ [Signal Gen] لا توجد إشارة شراء في الشمعة الأخيرة للزوج {symbol}.")
        return None

    # استخراج بيانات الشمعة الأخيرة التي تحتوي على إشارة شراء
    last_signal_row = df_with_signals.iloc[-1]
    current_price = last_signal_row['close']
    current_atr = last_signal_row['atr']

    if pd.isna(current_price) or pd.isna(current_atr) or current_atr <= 0 or current_price <= 0:
        logger.warning(f"⚠️ [Signal Gen] بيانات سعر ({current_price}) أو ATR ({current_atr}) غير صالحة للزوج {symbol}.")
        return None

    # --- حساب الهدف ووقف الخسارة الأولي بناءً على ATR ---
    initial_target = current_price + (ENTRY_ATR_MULTIPLIER * current_atr)
    initial_stop_loss = current_price - (ENTRY_ATR_MULTIPLIER * current_atr)

    # التحقق من أن وقف الخسارة ليس سالبًا أو صفرًا
    if initial_stop_loss <= 0:
        logger.warning(f"⚠️ [Signal Gen] وقف الخسارة المحسوب ({initial_stop_loss:.8f}) غير صالح للزوج {symbol}. تعديل طفيف.")
        # تعديل بسيط: استخدام نسبة مئوية من السعر كحد أدنى لوقف الخسارة
        min_sl_price = current_price * 0.95 # خسارة 5% كحد أقصى مبدئي
        initial_stop_loss = max(min_sl_price, 1e-9) # تأكد من أنه ليس صفرًا أو سالبًا

    # --- التحقق من هامش الربح الأدنى ---
    profit_margin_pct = ((initial_target / current_price) - 1) * 100
    if profit_margin_pct < MIN_PROFIT_MARGIN_PCT:
        logger.info(f"ℹ️ [Signal Gen] إشارة {symbol} لا تحقق الحد الأدنى للربح ({MIN_PROFIT_MARGIN_PCT:.1f}%). الربح المتوقع: {profit_margin_pct:.2f}%.")
        return None

    # --- (اختياري) التحقق باستخدام نموذج التنبؤ بالسعر ---
    # predicted_price = improved_predict_future_price(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
    # prediction_threshold = 1.005 # السعر المتوقع يجب أن يكون أعلى بنسبة 0.5% على الأقل
    # if predicted_price is None or predicted_price <= current_price * prediction_threshold:
    #     logger.info(f"ℹ️ [Signal Gen] السعر المتوقع ({predicted_price}) للزوج {symbol} لا يدعم إشارة الشراء (الحالي: {current_price}).")
    #     return None
    # logger.info(f"✅ [Signal Gen] التنبؤ بالسعر يدعم الإشارة لـ {symbol} (المتوقع: {predicted_price:.8f})")


    # --- بناء كائن الإشارة ---
    signal = {
        'symbol': symbol,
        'entry_price': float(f"{current_price:.8f}"), # تنسيق لـ 8 منازل عشرية
        'initial_target': float(f"{initial_target:.8f}"),
        'initial_stop_loss': float(f"{initial_stop_loss:.8f}"),
        'current_target': float(f"{initial_target:.8f}"), # يبدأ الهدف الحالي كالأولي
        'current_stop_loss': float(f"{initial_stop_loss:.8f}"), # يبدأ وقف الخسارة الحالي كالأولي
        'strategy': 'freqtrade_improved_atr',
        'indicators': { # إضافة بعض المؤشرات الرئيسية للإشارة لمزيد من السياق
            'ema_cross': last_signal_row['ema5'] > last_signal_row['ema50'],
            'rsi': round(last_signal_row['rsi'], 2),
            'macd_hist': round(last_signal_row['macd_hist'], 5),
            'kdj_j': round(last_signal_row['kdj_j'], 2),
            'adx': round(last_signal_row['adx'], 2),
            'atr': round(current_atr, 8),
            'buy_score': round(last_signal_row.get('buy_score', 0), 2)
        },
        'trade_value': TRADE_VALUE,
        # 'predicted_price': float(f"{predicted_price:.8f}") if predicted_price else None
    }

    logger.info(f"✅ [Signal Gen] تم توليد إشارة شراء للزوج {symbol} بناءً على الاستراتيجية المحسنة.")
    # logger.debug(f"Signal Details: {signal}") # Log full details only in debug mode
    return signal

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Crypto Trading Bot v4.3 - Signal Service Running.", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """معالج Webhook لتلقي التحديثات من Telegram (خاصة Callback Queries)."""
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
            chat_id_callback = message.get("chat", {}).get("id") if message else None

            if not chat_id_callback:
                 logger.error("❌ [Webhook] Could not extract chat_id from callback_query.")
                 # لا نزال نجيب على الكويري لمنع ظهور علامة التحميل في تيليجرام
                 if query_id:
                     answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
                     requests.post(answer_url, json={"callback_query_id": query_id}, timeout=5)
                 return '', 200

            # الرد على الكويري أولاً لمنع انتهاء المهلة في تيليجرام
            if query_id:
                answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
                requests.post(answer_url, json={"callback_query_id": query_id}, timeout=5) # استجابة سريعة

            # معالجة الأوامر المختلفة من الأزرار
            if data == "get_report":
                logger.info(f"ℹ️ [Webhook] Received 'get_report' callback from chat_id: {chat_id_callback}")
                # تشغيل إرسال التقرير في خيط منفصل لتجنب حظر الـ webhook
                Thread(target=send_report, args=(chat_id_callback,), daemon=True).start()
            # يمكن إضافة أزرار أوامر أخرى هنا
            # elif data == "some_other_action":
            #     handle_other_action(chat_id_callback)

            return '', 200 # تم الرد على الكويري بنجاح

        # يمكن إضافة معالجة لأنواع أخرى من الرسائل هنا إذا لزم الأمر
        # elif "message" in update:
        #     # Handle regular messages if needed
        #     pass

    except json.JSONDecodeError:
        logger.error("❌ [Webhook] Received invalid JSON.")
        return 'Invalid JSON', 400
    except Exception as e:
        logger.error(f"❌ [Webhook] Error processing update: {e}")
        # حاول الإجابة على الكويري حتى في حالة الخطأ إذا كان متاحاً
        try:
            query_id = update.get("callback_query", {}).get("id")
            if query_id:
                 answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
                 requests.post(answer_url, json={"callback_query_id": query_id}, timeout=5)
        except:
            pass # فشلت محاولة الرد على الكويري
        return 'Internal Server Error', 500

    return '', 200 # استجابة افتراضية

def set_telegram_webhook():
    """يسجل عنوان URL الخاص بـ Flask كـ webhook لدى Telegram."""
    # تأكد من أن الرابط يبدأ بـ https وأن الخدمة متاحة للعامة
    # استخدم اسم الخدمة في render.com أو عنوان IP العام أو اسم النطاق
    render_service_name = "four-3-9w83" # اسم الخدمة الخاص بك في Render
    webhook_url = f"https://hamza-o84o.onrender.com/webhook"
    url = f"https://api.telegram.org/bot{telegram_token}/setWebhook"
    params = {'url': webhook_url}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"✅ [Webhook] تم تسجيل webhook بنجاح على: {webhook_url}")
            logger.info(f"ℹ️ [Webhook] رد Telegram: {res_json.get('description')}")
        else:
            logger.error(f"❌ [Webhook] فشل تسجيل webhook: {res_json}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Webhook] استثناء أثناء تسجيل webhook: {e}")
    except Exception as e:
        logger.error(f"❌ [Webhook] خطأ غير متوقع أثناء تسجيل webhook: {e}")


# ---------------------- وظائف تحليل البيانات المساعدة ----------------------
def get_crypto_symbols(filename='crypto_list.txt'):
    """يقرأ قائمة رموز العملات من ملف نصي."""
    try:
        # تحديد المسار بالنسبة لموقع السكربت الحالي
        script_dir = os.path.dirname(__file__)
        file_path = os.path.join(script_dir, filename)
        if not os.path.exists(file_path):
             logger.error(f"❌ [Data] ملف قائمة العملات '{filename}' غير موجود في المسار: {file_path}")
             # محاولة البحث في المجلد الحالي كحل بديل
             file_path = filename
             if not os.path.exists(file_path):
                 logger.error(f"❌ [Data] ملف قائمة العملات '{filename}' غير موجود في المجلد الحالي أيضًا.")
                 return []

        with open(file_path, 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip() and not line.startswith('#')]
            logger.info(f"✅ [Data] تم تحميل {len(symbols)} زوج عملات من '{filename}'.")
            return symbols
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في قراءة ملف العملات '{filename}': {e}")
        return []

def fetch_historical_data(symbol, interval='1h', days=10):
    """يجلب البيانات التاريخية لزوج معين من Binance."""
    try:
        # logger.debug(f"⏳ [Data] بدء جلب البيانات التاريخية: {symbol} - {interval} - {days}d")
        # تحويل عدد الأيام إلى صيغة يفهمها Binance (e.g., "10 day ago UTC")
        start_str = f"{days} day ago UTC"
        klines = client.get_historical_klines(symbol, interval, start_str)

        if not klines:
            logger.warning(f"⚠️ [Data] لم يتم العثور على بيانات تاريخية للزوج {symbol} بالفترة المحددة.")
            return None

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])

        # تحويل الأعمدة الرئيسية إلى أرقام عشرية
        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # تحويل timestamp إلى كائن datetime (اختياري, لكن مفيد)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # إزالة الصفوف التي قد تحتوي على NaN بعد التحويل (نادر)
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)

        logger.info(f"✅ [Data] تم جلب {len(df)} شمعة للزوج {symbol} ({interval}, {days}d).")
        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']] # إرجاع الأعمدة المطلوبة فقط

    except Exception as e: # كن أكثر تحديدًا إذا أمكن (e.g., BinanceAPIException)
        logger.error(f"❌ [Data] خطأ في جلب البيانات لـ {symbol} ({interval}, {days}d): {e}")
        return None

def fetch_recent_volume(symbol):
    """يجلب حجم التداول (Quote Volume) في آخر 15 دقيقة."""
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=15)
        if not klines:
             logger.warning(f"⚠️ [Data] لم يتم العثور على بيانات 1m لـ {symbol} لحساب حجم التداول.")
             return 0.0

        # klines[7] هو Quote asset volume
        volume = sum(float(k[7]) for k in klines)
        logger.info(f"✅ [Data] حجم التداول (Quote) للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT.")
        return volume
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب حجم التداول لـ {symbol}: {e}")
        return 0.0

def get_market_dominance():
    """يجلب نسب سيطرة BTC و ETH من CoinGecko."""
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json().get("data", {})
        market_cap_percentage = data.get("market_cap_percentage", {})
        btc_dominance = market_cap_percentage.get("btc", 0.0)
        eth_dominance = market_cap_percentage.get("eth", 0.0)
        logger.info(f"✅ [Data] نسب السيطرة الحالية - BTC: {btc_dominance:.2f}%, ETH: {eth_dominance:.2f}%")
        return btc_dominance, eth_dominance
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Data] خطأ في طلب نسب السيطرة من CoinGecko: {e}")
        return None, None # إرجاع None للإشارة إلى الفشل
    except Exception as e:
        logger.error(f"❌ [Data] خطأ غير متوقع في get_market_dominance: {e}")
        return None, None

# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    """يرسل إشعار توصية جديدة إلى Telegram."""
    try:
        # حساب النسب المئوية والأرباح/الخسائر المتوقعة بناءً على الهدف/الوقف الأولي
        entry_price = signal['entry_price']
        target_price = signal['initial_target']
        stop_loss_price = signal['initial_stop_loss']

        if entry_price <= 0: # تجنب القسمة على صفر
             logger.error(f"❌ [Telegram] سعر الدخول غير صالح ({entry_price}) للزوج {signal['symbol']}. لا يمكن إرسال الإشعار.")
             return

        profit_pct = ((target_price / entry_price) - 1) * 100
        loss_pct = ((stop_loss_price / entry_price) - 1) * 100
        profit_usdt = TRADE_VALUE * (profit_pct / 100)
        loss_usdt = TRADE_VALUE * (loss_pct / 100) # ستكون قيمة سالبة

        # الحصول على الوقت الحالي بتوقيت UTC+1 وتنسيقه
        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
        # الحصول على مؤشر الخوف والجشع
        fng_value, fng_label = get_fear_greed_index()

        # بناء نص الرسالة
        message = (
            f"🚀 **إشارة تداول جديدة** 🚀\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{signal['symbol']}`\n"
            f"📈 **سعر الدخول المقترح:** `${entry_price:.8f}`\n"
            # السعر الحالي قد يختلف قليلاً عند الإرسال، يمكن إضافته إذا كان مهماً
            # f"📊 **السعر الحالي:** `${current_price_at_send_time:.8f}`\n"
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

        # إضافة زر "عرض التقرير"
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }

        # إرسال الرسالة
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown',
            'reply_markup': json.dumps(reply_markup),
            'disable_web_page_preview': True # لمنع المعاينة إذا كان هناك روابط
        }

        response = requests.post(url, json=payload, timeout=15) # زيادة المهلة قليلاً
        response.raise_for_status() # التحقق من نجاح الطلب

        logger.info(f"✅ [Telegram] تم إرسال إشعار التوصية للزوج {signal['symbol']} بنجاح.")
        # logger.debug(f"Telegram response: {response.text}")

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Telegram] فشل إرسال إشعار للزوج {signal['symbol']} بسبب خطأ في الشبكة أو API: {e}")
        # محاولة تحليل رد الخطأ من Telegram إذا كان متاحًا
        try:
            error_info = e.response.json()
            logger.error(f"❌ [Telegram] تفاصيل خطأ API: {error_info}")
        except:
            pass # لا يوجد رد أو الرد ليس JSON
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال إشعار للزوج {signal['symbol']} بسبب خطأ غير متوقع: {e}")


def send_telegram_update(message):
    """يرسل رسالة تحديث عامة (مثل تحديث SL/TP أو إغلاق صفقة) إلى Telegram."""
    try:
        # إضافة علامة LTR لضمان العرض الصحيح في Telegram
        # ltr_mark = "\u200E"
        # full_message = f"{ltr_mark}{message}" # قد لا تكون ضرورية دائمًا، اختبر

        # إضافة زر "عرض التقرير" للرسائل التحديثية أيضًا
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }

        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message, # استخدام الرسالة مباشرة
            'parse_mode': 'Markdown', # افتراض أن الرسائل مهيأة بـ Markdown
            'reply_markup': json.dumps(reply_markup),
            'disable_web_page_preview': True
        }

        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()

        logger.info(f"✅ [Telegram] تم إرسال رسالة التحديث بنجاح.")
        # logger.debug(f"Telegram update response: {response.text}")

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Telegram] فشل إرسال رسالة التحديث بسبب خطأ في الشبكة أو API: {e}")
        try:
            error_info = e.response.json()
            logger.error(f"❌ [Telegram] تفاصيل خطأ API: {error_info}")
        except:
            pass
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال رسالة التحديث بسبب خطأ غير متوقع: {e}")


# ---------------------- إرسال تقرير الأداء الشامل ----------------------
def send_report(target_chat_id):
    """يحسب ويرسل تقرير أداء شامل إلى Telegram."""
    logger.info(f"⏳ [Report] بدء إنشاء تقرير الأداء لـ chat_id: {target_chat_id}")
    try:
        check_db_connection()

        # 1. عدد الصفقات النشطة
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]

        # 2. إحصائيات الصفقات المغلقة
        cur.execute("""
            SELECT achieved_target, hit_stop_loss, profitable_stop_loss, profit_percentage, entry_price,
                   CASE WHEN achieved_target = TRUE THEN current_target
                        WHEN hit_stop_loss = TRUE THEN current_stop_loss
                        ELSE NULL END AS closing_price
            FROM signals WHERE closed_at IS NOT NULL
        """)
        closed_signals = cur.fetchall()

        total_closed_trades = len(closed_signals)
        if total_closed_trades == 0:
            logger.info("ℹ️ [Report] لا توجد صفقات مغلقة لإعداد التقرير.")
            report_message = "📊 **تقرير الأداء**\n\nلا توجد صفقات مغلقة حتى الآن.\n"
            report_message += f"⏳ **التوصيات النشطة حالياً:** {active_count}"
            send_telegram_update(report_message) # استخدام دالة التحديث لإرسال الرسالة
            return

        # حساب الإحصائيات
        successful_target_hits = sum(1 for s in closed_signals if s[0]) # achieved_target = TRUE
        profitable_sl_hits = sum(1 for s in closed_signals if s[1] and s[2]) # hit_stop_loss = TRUE AND profitable_stop_loss = TRUE
        losing_sl_hits = sum(1 for s in closed_signals if s[1] and not s[2]) # hit_stop_loss = TRUE AND profitable_stop_loss = FALSE

        # حساب الأرباح والخسائر
        total_profit_usd = 0
        total_loss_usd = 0
        profit_percentages = []
        loss_percentages = []

        for signal in closed_signals:
            # achieved_target, hit_stop_loss, profitable_stop_loss, profit_percentage, entry_price, closing_price
            profit_pct = signal[3] # profit_percentage (تم حسابه بالفعل عند الإغلاق)

            if profit_pct is not None:
                trade_result_usd = TRADE_VALUE * (profit_pct / 100)
                if trade_result_usd > 0:
                    total_profit_usd += trade_result_usd
                    profit_percentages.append(profit_pct)
                else:
                    total_loss_usd += trade_result_usd # الخسارة قيمة سالبة
                    loss_percentages.append(profit_pct)
            # else: # حالة نادرة: لم يتم حساب النسبة عند الإغلاق
            #     entry = signal[4]
            #     closing = signal[5]
            #     if entry and closing and entry > 0:
            #         profit_pct = ((closing / entry) - 1) * 100
            #         trade_result_usd = TRADE_VALUE * (profit_pct / 100)
            #         if trade_result_usd > 0:
            #              total_profit_usd += trade_result_usd
            #              profit_percentages.append(profit_pct)
            #         else:
            #              total_loss_usd += trade_result_usd
            #              loss_percentages.append(profit_pct)


        net_profit_usd = total_profit_usd + total_loss_usd
        win_rate = (successful_target_hits + profitable_sl_hits) / total_closed_trades * 100 if total_closed_trades > 0 else 0
        avg_profit_pct = np.mean(profit_percentages) if profit_percentages else 0
        avg_loss_pct = np.mean(loss_percentages) if loss_percentages else 0
        profit_factor = abs(total_profit_usd / total_loss_usd) if total_loss_usd != 0 else float('inf') # مقياس الربح إلى الخسارة


        # حساب تقييم البوت (مثال بسيط يعتمد على صافي الربح كنسبة من إجمالي رأس المال المستخدم نظرياً)
        # رأس المال النظري المستخدم = عدد الصفقات * قيمة الصفقة
        total_capital_risked_theoretically = total_closed_trades * TRADE_VALUE
        bot_rating_pct = (net_profit_usd / total_capital_risked_theoretically) * 100 if total_capital_risked_theoretically > 0 else 0

        # الوقت الحالي
        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')

        # بناء رسالة التقرير
        report_message = (
            f"📊 **تقرير أداء البوت** ({timestamp} UTC+1)\n"
            f"——————————————\n"
            f"**ملخص الصفقات المغلقة ({total_closed_trades}):**\n"
            f"  ✅ تحقيق الهدف: {successful_target_hits}\n"
            f"  📈 وقف خسارة رابح: {profitable_sl_hits}\n"
            f"  📉 وقف خسارة خاسر: {losing_sl_hits}\n"
            f"  勝率 (Win Rate): {win_rate:.2f}%\n"
            f"——————————————\n"
            f"**الأداء المالي:**\n"
            f"  💰 إجمالي الربح: +{total_profit_usd:.2f} USDT\n"
            f"  💸 إجمالي الخسارة: {total_loss_usd:.2f} USDT\n"
            f"  💵 **صافي الربح/الخسارة:** {net_profit_usd:+.2f} USDT\n"
            f"  🎯 متوسط ربح الصفقة: +{avg_profit_pct:.2f}%\n"
            f"  🛑 متوسط خسارة الصفقة: {avg_loss_pct:.2f}%\n"
            f"  ⚖️ معامل الربح (Profit Factor): {profit_factor:.2f}\n"
            f"——————————————\n"
            f"⏳ **التوصيات النشطة حالياً:** {active_count}\n"
            # f"⭐ **تقييم الأداء:** {bot_rating_pct:.2f}% (بناءً على الربح الصافي من رأس المال النظري)\n" # يمكن تعديل هذا المقياس
        )

        # إرسال التقرير
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': target_chat_id,
            'text': report_message,
            'parse_mode': 'Markdown'
        }
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info(f"✅ [Report] تم إرسال تقرير الأداء بنجاح إلى chat_id: {target_chat_id}")

    except psycopg2.Error as db_err:
        logger.error(f"❌ [Report] خطأ في قاعدة البيانات أثناء إنشاء التقرير: {db_err}")
        conn.rollback() # التراجع عن أي معاملة قد تكون مفتوحة
        send_telegram_update(f"⚠️ حدث خطأ في قاعدة البيانات أثناء إنشاء التقرير. يرجى المحاولة لاحقاً.\n`{db_err}`")
    except requests.exceptions.RequestException as req_err:
        logger.error(f"❌ [Report] خطأ في الشبكة أو API أثناء إرسال التقرير: {req_err}")
        # لا يمكن إرسال رسالة خطأ عبر Telegram إذا كانت المشكلة في الإرسال نفسه
    except Exception as e:
        logger.error(f"❌ [Report] فشل إنشاء أو إرسال تقرير الأداء: {e}")
        # محاولة إرسال رسالة خطأ عامة
        try:
            send_telegram_update(f"⚠️ حدث خطأ غير متوقع أثناء إنشاء تقرير الأداء.\n`{e}`")
        except:
            pass # فشل إرسال رسالة الخطأ أيضًا

# ---------------------- خدمة تتبع الإشارات وتحديثها (المنطق المحسّن) ----------------------
def track_signals():
    """
    تتابع التوصيات المفتوحة، تتحقق من تحقيق الهدف أو الوصول لوقف الخسارة،
    وتطبق وقف الخسارة المتحرك (ATR Trailing Stop) عند تحقيق شروط معينة.
    """
    logger.info(f"🔄 [Tracker] بدء خدمة تتبع الإشارات (فريم {SIGNAL_TRACKING_TIMEFRAME}, بيانات {SIGNAL_TRACKING_LOOKBACK_DAYS} أيام)...")

    while True:
        try:
            check_db_connection() # التأكد من أن الاتصال سليم

            # جلب جميع التوصيات النشطة (لم يتم إغلاقها بعد)
            cur.execute("""
                SELECT id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active
                FROM signals
                WHERE closed_at IS NULL
            """)
            active_signals = cur.fetchall()

            if not active_signals:
                # logger.info("ℹ️ [Tracker] لا توجد توصيات نشطة لتتبعها حالياً.")
                pass # لا تفعل شيئًا إذا لم تكن هناك توصيات نشطة
            else:
                logger.info("==========================================")
                logger.info(f"🔍 [Tracker] عدد التوصيات النشطة قيد التتبع: {len(active_signals)}")

                for signal_data in active_signals:
                    signal_id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active = signal_data
                    logger.info(f"--- [Tracker] تتبع {symbol} (ID: {signal_id}) ---")

                    # 1. الحصول على السعر الحالي
                    if symbol not in ticker_data or ticker_data[symbol].get('c') is None:
                        logger.warning(f"⚠️ [Tracker] لا توجد بيانات سعر حالية من WebSocket للزوج {symbol}. تخطي هذه الدورة.")
                        continue
                    try:
                        current_price = float(ticker_data[symbol]['c'])
                        if current_price <= 0:
                             logger.warning(f"⚠️ [Tracker] السعر الحالي المستلم غير صالح ({current_price}) للزوج {symbol}.")
                             continue
                    except (ValueError, TypeError):
                         logger.warning(f"⚠️ [Tracker] قيمة السعر المستلمة ({ticker_data[symbol].get('c')}) غير رقمية للزوج {symbol}.")
                         continue

                    logger.info(f"  - السعر الحالي: {current_price:.8f}, الدخول: {entry_price:.8f}, الهدف: {current_target:.8f}, الوقف: {current_stop_loss:.8f}")

                    # التحقق من صحة سعر الدخول لتجنب القسمة على صفر
                    if entry_price is None or abs(entry_price) < 1e-9: # استخدام قيمة صغيرة جداً بدلاً من الصفر
                        logger.error(f"❌ [Tracker] سعر الدخول غير صالح أو قريب جداً من الصفر ({entry_price}) للتوصية ID {signal_id} ({symbol}). لا يمكن المتابعة.")
                        # قد نحتاج إلى آلية لتصحيح هذا أو إغلاق الصفقة يدويًا
                        continue

                    # 2. التحقق من إغلاق الصفقة (الهدف أو الوقف الأولي/المتحرك)
                    # --- تحقق من تحقيق الهدف ---
                    if current_price >= current_target:
                        profit_pct = ((current_target / entry_price) - 1) * 100
                        profit_usdt = TRADE_VALUE * (profit_pct / 100)
                        msg = (f"✅ **هدف محقق!** ✅\n"
                               f"📈 الزوج: `{symbol}` (ID: {signal_id})\n"
                               f"💰 أغلق عند: ${current_price:.8f} (الهدف: ${current_target:.8f})\n"
                               f"📊 الربح: +{profit_pct:.2f}% (+{profit_usdt:.2f} USDT)")
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET achieved_target = TRUE, closed_at = NOW(), profit_percentage = %s, closing_price = %s
                            WHERE id = %s
                        """, (profit_pct, current_price, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Tracker] تم إغلاق توصية {symbol} (ID: {signal_id}) عند تحقيق الهدف.")
                        continue # الانتقال للتوصية التالية

                    # --- تحقق من الوصول لوقف الخسارة ---
                    elif current_price <= current_stop_loss:
                        loss_pct = ((current_stop_loss / entry_price) - 1) * 100
                        loss_usdt = TRADE_VALUE * (loss_pct / 100)
                        profitable_stop = current_stop_loss > entry_price # هل الوقف كان فوق سعر الدخول؟
                        stop_type_msg = "وقف خسارة رابح" if profitable_stop else "وقف خسارة"
                        emoji = "📈" if profitable_stop else "🛑"
                        msg = (f"{emoji} **{stop_type_msg}** {emoji}\n"
                               f"📉 الزوج: `{symbol}` (ID: {signal_id})\n"
                               f"💰 أغلق عند: ${current_price:.8f} (الوقف: ${current_stop_loss:.8f})\n"
                               f"📊 النتيجة: {loss_pct:.2f}% ({loss_usdt:.2f} USDT)")
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s, profitable_stop_loss = %s, closing_price = %s
                            WHERE id = %s
                        """, (loss_pct, profitable_stop, current_price, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Tracker] تم إغلاق توصية {symbol} (ID: {signal_id}) عند {stop_type_msg}.")
                        continue # الانتقال للتوصية التالية

                    # 3. تطبيق أو تحديث وقف الخسارة المتحرك (ATR Trailing Stop)
                    # --- جلب بيانات الفريم القصير (15m) لحساب ATR الحالي ---
                    df_track = fetch_historical_data(symbol, interval=SIGNAL_TRACKING_TIMEFRAME, days=SIGNAL_TRACKING_LOOKBACK_DAYS)

                    if df_track is None or df_track.empty or len(df_track) < 20: # نحتاج بيانات كافية لحساب ATR
                        logger.warning(f"⚠️ [Tracker] بيانات {SIGNAL_TRACKING_TIMEFRAME} غير كافية لحساب ATR للزوج {symbol}. تخطي تحديث الوقف المتحرك.")
                    else:
                        # حساب ATR على بيانات التتبع
                        df_track = calculate_atr_indicator(df_track, period=14) # استخدام فترة ATR القياسية
                        if 'atr' not in df_track.columns or df_track['atr'].iloc[-1] is None or pd.isna(df_track['atr'].iloc[-1]):
                             logger.warning(f"⚠️ [Tracker] لم يتم حساب ATR بنجاح للزوج {symbol} على فريم {SIGNAL_TRACKING_TIMEFRAME}.")
                        else:
                            current_atr = df_track['atr'].iloc[-1]
                            if current_atr > 0:
                                logger.info(f"  - ATR ({SIGNAL_TRACKING_TIMEFRAME}): {current_atr:.8f}")

                                # --- حساب الربح الحالي كنسبة مئوية ---
                                current_gain_pct = (current_price - entry_price) / entry_price

                                # --- التحقق من شرط تفعيل الوقف المتحرك ---
                                if current_gain_pct >= TRAILING_STOP_ACTIVATION_PROFIT_PCT:
                                    # حساب وقف الخسارة المتحرك المقترح
                                    potential_new_stop_loss = current_price - (TRAILING_STOP_ATR_MULTIPLIER * current_atr)

                                    # --- التحقق مما إذا كان الوقف المقترح أعلى من الوقف الحالي ---
                                    # الوقف المتحرك يجب أن يرتفع فقط، لا ينخفض
                                    if potential_new_stop_loss > current_stop_loss:
                                        new_stop_loss = potential_new_stop_loss
                                        logger.info(f"  => تفعيل/تحديث الوقف المتحرك لـ {symbol}!")
                                        logger.info(f"     السعر الحالي: {current_price:.8f} (> تفعيل عند {entry_price * (1 + TRAILING_STOP_ACTIVATION_PROFIT_PCT):.8f})")
                                        logger.info(f"     الوقف القديم: {current_stop_loss:.8f}")
                                        logger.info(f"     الوقف الجديد المقترح: {new_stop_loss:.8f} (Current - {TRAILING_STOP_ATR_MULTIPLIER} * ATR)")

                                        # --- إرسال إشعار بالتحديث وتحديث قاعدة البيانات ---
                                        update_msg = (
                                            f"🔄 **تحديث وقف الخسارة (Trailing Stop)** 🔄\n"
                                            f"📈 الزوج: `{symbol}` (ID: {signal_id})\n"
                                            f"   - سعر الدخول: ${entry_price:.8f}\n"
                                            f"   - السعر الحالي: ${current_price:.8f} ({current_gain_pct:+.2%})\n"
                                            f"   - الوقف القديم: ${current_stop_loss:.8f}\n"
                                            f"   - **الوقف الجديد:** `${new_stop_loss:.8f}` ✅"
                                        )
                                        send_telegram_update(update_msg)
                                        cur.execute("""
                                            UPDATE signals
                                            SET current_stop_loss = %s, is_trailing_active = TRUE
                                            WHERE id = %s
                                        """, (new_stop_loss, signal_id))
                                        conn.commit()
                                        logger.info(f"✅ [Tracker] تم تحديث وقف الخسارة المتحرك لـ {symbol} (ID: {signal_id}) إلى {new_stop_loss:.8f}")
                                        # تحديث المتغير المحلي للاستخدام في الدورات القادمة داخل الحلقة (إذا لزم الأمر)
                                        # current_stop_loss = new_stop_loss
                                    # else:
                                        # logger.debug(f"  - الوقف المتحرك المقترح ({potential_new_stop_loss:.8f}) ليس أعلى من الحالي ({current_stop_loss:.8f}). لا تغيير.")

                                # else:
                                     # logger.debug(f"  - الربح الحالي ({current_gain_pct:.2%}) لم يصل لنسبة تفعيل الوقف المتحرك ({TRAILING_STOP_ACTIVATION_PROFIT_PCT:.2%}).")
                            # else:
                                # logger.warning(f"⚠️ [Tracker] قيمة ATR المحسوبة غير صالحة ({current_atr}) للزوج {symbol}.")
                    # إضافة فاصل زمني بسيط بين فحص كل توصية لتجنب التحميل الزائد على الـ API أو الـ DB
                    time.sleep(0.5) # 500 ميلي ثانية

            # انتظر قبل الدورة التالية من التتبع
            # logger.debug("[Tracker] اكتملت دورة التتبع. انتظار للدورة التالية...")

        except psycopg2.Error as db_err:
            logger.error(f"❌ [Tracker] خطأ في قاعدة البيانات أثناء تتبع الإشارات: {db_err}")
            conn.rollback() # التراجع عن أي تغييرات غير مكتملة
            # قد تحتاج إلى إعادة محاولة الاتصال هنا أو في check_db_connection
        except Exception as e:
            logger.error(f"❌ [Tracker] خطأ عام غير متوقع في خدمة تتبع الإشارات: {e}", exc_info=True) # إضافة تفاصيل الاستثناء
            # التراجع في حالة حدوث أي خطأ غير متوقع أثناء معالجة الإشارات
            try:
                 conn.rollback()
            except Exception as rb_err:
                 logger.error(f"❌ [Tracker] خطأ أثناء محاولة التراجع: {rb_err}")

        # تحديد فترة النوم بين دورات التتبع الكاملة
        time.sleep(45) # فحص كل التوصيات كل 45 ثانية (يمكن تعديله)

# ---------------------- دالة التحقق من عدد التوصيات المفتوحة (تُستخدم كبوابة لتوليد توصيات جديدة) ----------------------
def can_generate_new_recommendation():
    """
    تتحقق مما إذا كان يمكن توليد توصية جديدة بناءً على الحد الأقصى للصفقات المفتوحة.
    ترجع True إذا كان العدد أقل من الحد المسموح به، وإلا False.
    """
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]
        if active_count < MAX_OPEN_TRADES:
            logger.info(f"✅ [Gate] عدد التوصيات المفتوحة ({active_count}) أقل من الحد ({MAX_OPEN_TRADES}). يمكن توليد توصيات جديدة.")
            return True
        else:
            logger.info(f"⚠️ [Gate] الحد الأقصى ({MAX_OPEN_TRADES}) للتوصيات المفتوحة تم الوصول إليه. لن يتم توليد توصيات جديدة حالياً.")
            return False
    except Exception as e:
        logger.error(f"❌ [Gate] خطأ أثناء التحقق من عدد التوصيات المفتوحة: {e}")
        # افتراضياً، لا نسمح بتوليد توصيات جديدة في حالة الخطأ لتوخي الحذر
        return False

# ---------------------- تحليل السوق (البحث عن صفقات جديدة) ----------------------
def analyze_market():
    """
    يحلل السوق بحثًا عن فرص تداول جديدة بناءً على الاستراتيجية المحددة والفريم الزمني.
    """
    logger.info("==========================================")
    logger.info(f" H [Market Analysis] بدء دورة تحليل السوق (فريم {SIGNAL_GENERATION_TIMEFRAME}, بيانات {SIGNAL_GENERATION_LOOKBACK_DAYS} أيام)...")

    # --- التحقق من إمكانية فتح توصية جديدة ---
    if not can_generate_new_recommendation():
        logger.info(" H [Market Analysis] تم إيقاف التحليل مؤقتًا بسبب الوصول للحد الأقصى للصفقات المفتوحة.")
        return # الخروج من الدالة إذا لم يكن مسموحًا بفتح صفقات جديدة

    # --- جلب بيانات السوق العامة ---
    btc_dominance, eth_dominance = get_market_dominance()
    if btc_dominance is None or eth_dominance is None:
        logger.warning("⚠️ [Market Analysis] فشل في جلب نسب السيطرة. سيتم استخدام قيم افتراضية (0.0).")
        btc_dominance, eth_dominance = 0.0, 0.0 # استخدام قيم افتراضية أو التعامل مع الحالة بشكل مختلف

    # --- الحصول على قائمة الرموز للتحليل ---
    symbols_to_analyze = get_crypto_symbols()
    if not symbols_to_analyze:
        logger.warning("⚠️ [Market Analysis] قائمة رموز العملات فارغة. لا يمكن إجراء التحليل.")
        return

    logger.info(f" H [Market Analysis] سيتم تحليل {len(symbols_to_analyze)} زوج عملات.")
    generated_signals_count = 0

    # --- المرور على كل رمز وتحليله ---
    for symbol in symbols_to_analyze:

        # التحقق مرة أخرى قبل معالجة كل رمز (قد يتم فتح صفقة بواسطة تكرار آخر)
        if not can_generate_new_recommendation():
             logger.info(f" H [Market Analysis] تم الوصول للحد الأقصى للصفقات أثناء التحليل. إيقاف البحث عن إشارات جديدة.")
             break # الخروج من الحلقة إذا تم الوصول للحد

        logger.info(f"--- [Market Analysis] تحليل الزوج: {symbol} ---")

        # 1. التحقق مما إذا كان هناك توصية مفتوحة بالفعل لهذا الزوج
        try:
            check_db_connection()
            cur.execute("SELECT COUNT(*) FROM signals WHERE symbol = %s AND closed_at IS NULL", (symbol,))
            if cur.fetchone()[0] > 0:
                logger.info(f"ℹ️ [Market Analysis] توجد توصية مفتوحة بالفعل للزوج {symbol}. تخطي...")
                continue # الانتقال للرمز التالي
        except Exception as e:
             logger.error(f"❌ [Market Analysis] خطأ أثناء التحقق من التوصيات المفتوحة للزوج {symbol}: {e}")
             continue # تخطي هذا الرمز في حالة الخطأ

        # 2. جلب البيانات التاريخية للفريم المحدد لتوليد الإشارة
        df_signal_gen = fetch_historical_data(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)

        if df_signal_gen is None or df_signal_gen.empty or len(df_signal_gen) < 50:
            logger.warning(f"⚠️ [Market Analysis] بيانات غير كافية أو خطأ في جلب بيانات {SIGNAL_GENERATION_TIMEFRAME} للزوج {symbol}. تخطي...")
            continue

        # 3. توليد الإشارة باستخدام الاستراتيجية
        signal = generate_signal_using_freqtrade_strategy(df_signal_gen, symbol)

        if signal:
            logger.info(f"✅ [Market Analysis] تم العثور على إشارة شراء محتملة للزوج {symbol}!")

            # 4. التحقق من حجم التداول (السيولة)
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < MIN_VOLUME_15M_USDT:
                logger.info(f"⚠️ [Market Analysis] تجاهل إشارة {symbol} بسبب سيولة منخفضة ({volume_15m:,.0f} USDT) أقل من الحد ({MIN_VOLUME_15M_USDT:,.0f} USDT).")
                continue # تخطي هذه الإشارة

            logger.info(f"✅ [Market Analysis] سيولة كافية للزوج {symbol} ({volume_15m:,.0f} USDT).")

            # 5. إرسال التنبيه وحفظ الإشارة في قاعدة البيانات
            try:
                # إرسال التنبيه أولاً
                send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance, SIGNAL_GENERATION_TIMEFRAME)

                # حفظ الإشارة في قاعدة البيانات
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
                    signal['current_target'], # الهدف الحالي = الأولي عند الإنشاء
                    signal['current_stop_loss'],# الوقف الحالي = الأولي عند الإنشاء
                    signal.get('indicators', {}).get('buy_score'), # استخدام درجة الشراء كمؤشر ثقة مبدئي
                    volume_15m
                ))
                conn.commit()
                logger.info(f"✅ [Market Analysis] تم حفظ إشارة {symbol} بنجاح في قاعدة البيانات.")
                generated_signals_count += 1

                # إضافة انتظار بسيط بعد إيجاد إشارة لمنع إشارات متتالية سريعة جداً لنفس الزوج (إذا لزم الأمر)
                time.sleep(2)

            except psycopg2.Error as db_err:
                logger.error(f"❌ [Market Analysis] فشل حفظ إشارة {symbol} في قاعدة البيانات: {db_err}")
                conn.rollback() # التراجع عن الإدخال الفاشل
                # قد ترغب في عدم إرسال التنبيه إذا فشل الحفظ؟ أو العكس؟
            except Exception as e:
                logger.error(f"❌ [Market Analysis] خطأ غير متوقع أثناء معالجة إشارة {symbol}: {e}")
                conn.rollback() # التراجع كإجراء احترازي

        else:
             logger.info(f"ℹ️ [Market Analysis] لم يتم العثور على إشارة شراء تستوفي الشروط للزوج {symbol} على فريم {SIGNAL_GENERATION_TIMEFRAME}.")
             pass # لا توجد إشارة لهذا الرمز

        # فاصل بسيط بين تحليل كل زوج
        time.sleep(1)

    logger.info(f" H [Market Analysis] اكتملت دورة تحليل السوق. تم توليد {generated_signals_count} إشارة/إشارات جديدة.")
    logger.info("==========================================")


# ---------------------- اختبار Telegram ----------------------
def test_telegram():
    """يرسل رسالة اختبار بسيطة إلى Telegram للتحقق من الإعدادات."""
    logger.info("🧪 [Test] محاولة إرسال رسالة اختبار إلى Telegram...")
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        payload = {
            'chat_id': chat_id,
            'text': f'🚀 **البوت يعمل!** 🚀\nرسالة اختبار من خدمة توصيات التداول.\nالوقت: {timestamp}',
            'parse_mode': 'Markdown'
            }
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"✅ [Test] تم إرسال رسالة الاختبار بنجاح. رد Telegram: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Test] فشل إرسال رسالة الاختبار: {e}")
        if e.response is not None:
             logger.error(f"❌ [Test] تفاصيل الخطأ من Telegram: {e.response.text}")
    except Exception as e:
         logger.error(f"❌ [Test] خطأ غير متوقع أثناء اختبار Telegram: {e}")

# ---------------------- تشغيل Flask (في خيط منفصل) ----------------------
def run_flask():
    """يشغل تطبيق Flask لاستقبال Webhooks."""
    port = int(os.environ.get("PORT", 10000)) # منفذ Render الافتراضي أو 10000 محلياً
    logger.info(f"🌍 [Flask] بدء تشغيل خادم Flask على المنفذ {port}...")
    # يجب تعطيل وضع التصحيح (debug=False) وإعادة التحميل (use_reloader=False) في بيئة الإنتاج
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ---------------------- التشغيل الرئيسي والتزامن ----------------------
if __name__ == '__main__':
    logger.info("==========================================")
    logger.info("🚀 بدء تشغيل بوت توصيات التداول الإصدار 4.3 (Hazem Mod)...")
    logger.info("==========================================")

    # 1. تهيئة قاعدة البيانات
    try:
        init_db()
    except Exception as e:
        logger.critical(f"❌ [Main] فشل حاسم في تهيئة قاعدة البيانات عند البدء: {e}. الخروج...")
        exit(1) # الخروج إذا لم تعمل قاعدة البيانات

    # 2. تسجيل Webhook مع Telegram (يجب أن يتم بعد التأكد من أن Flask سيعمل)
    # تأكد من تشغيل هذا *قبل* بدء Flask إذا كنت تعتمد على Render لتوفير اسم النطاق تلقائيًا
    # أو قم بتشغيله بعد بدء Flask إذا كنت تعرف عنوان URL مسبقًا.
    # من الأفضل تشغيله هنا قبل بدء الخيوط الأخرى.
    set_telegram_webhook()

    # 3. بدء تشغيل خادم Flask في خيط منفصل
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("✅ [Main] تم بدء خيط Flask.")

    # 4. بدء تشغيل WebSocket Manager في خيط منفصل
    websocket_thread = Thread(target=run_ticker_socket_manager, daemon=True)
    websocket_thread.start()
    logger.info("✅ [Main] تم بدء خيط WebSocket Manager.")
    # انتظر قليلاً للسماح لـ WebSocket بالاتصال وتلقي بعض البيانات الأولية
    time.sleep(10)

    # 5. بدء خدمة تتبع الإشارات في خيط منفصل
    tracker_thread = Thread(target=track_signals, daemon=True)
    tracker_thread.start()
    logger.info("✅ [Main] تم بدء خيط تتبع الإشارات.")

    # 6. إرسال رسالة اختبار أولية
    test_telegram()

    # 7. إعداد وتشغيل المهام المجدولة (مثل تحليل السوق)
    scheduler = BackgroundScheduler(timezone="UTC") # استخدام UTC للمهام المجدولة
    # جدولة تحليل السوق كل 5 دقائق (يمكن تعديلها)
    # 'interval', minutes=5
    # 'cron', minute='*/5' # طريقة أخرى لجدولة كل 5 دقائق
    scheduler.add_job(analyze_market, 'interval', minutes=5, id='market_analyzer', replace_existing=True)
    logger.info("✅ [Main] تم جدولة مهمة تحليل السوق (analyze_market) للعمل كل 5 دقائق.")

    # يمكن إضافة مهام مجدولة أخرى هنا، مثل إرسال تقرير دوري
    # scheduler.add_job(send_report, 'cron', hour=8, minute=0, args=[chat_id], id='daily_report') # تقرير يومي الساعة 8 صباحًا UTC
    # logger.info("✅ [Main] تم جدولة مهمة التقرير اليومي.")

    scheduler.start()
    logger.info("✅ [Main] تم بدء تشغيل المجدول (Scheduler).")
    logger.info("==========================================")
    logger.info("✅ [Main] البوت قيد التشغيل الآن...")
    logger.info("==========================================")

    # حلقة رئيسية لإبقاء البرنامج قيد التشغيل ومراقبة الخيوط (اختياري)
    try:
        while True:
            # يمكن إضافة فحوصات لحالة الخيوط هنا إذا لزم الأمر
            if not flask_thread.is_alive():
                 logger.critical("❌ [Main] خيط Flask توقف! محاولة إعادة التشغيل غير مدعومة حالياً. الخروج...")
                 # في بيئة الإنتاج، قد تحتاج إلى نظام مراقبة خارجي لإعادة التشغيل
                 break
            if not websocket_thread.is_alive():
                 logger.error("❌ [Main] خيط WebSocket توقف! محاولة إعادة تشغيله...")
                 # محاولة إعادة تشغيل WebSocket قد تكون معقدة، الأفضل إعادة تشغيل البوت كاملاً
                 break # الخروج للسماح لنظام مثل systemd بإعادة التشغيل
            if not tracker_thread.is_alive():
                 logger.error("❌ [Main] خيط تتبع الإشارات توقف! محاولة إعادة تشغيله...")
                 break # الخروج للسماح بإعادة التشغيل
            time.sleep(60) # تحقق كل دقيقة
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 [Main] تم استلام إشارة إيقاف (KeyboardInterrupt/SystemExit). بدء إيقاف التشغيل...")
        scheduler.shutdown()
        # يمكن إضافة منطق إيقاف نظيف للخيوط الأخرى إذا لزم الأمر
        logger.info("✅ [Main] تم إيقاف المجدول. الخروج.")
    except Exception as e:
        logger.critical(f"❌ [Main] حدث خطأ فادح في الحلقة الرئيسية: {e}", exc_info=True)
        scheduler.shutdown()
        logger.info("✅ [Main] تم إيقاف المجدول بسبب خطأ. الخروج.")
