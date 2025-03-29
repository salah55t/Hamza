#!/usr/bin/env python
# -*- coding: utf-8 -*- # لتحديد الترميز

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
from sklearn.ensemble import GradientBoostingRegressor # For price prediction (optional)

# ---------------------- إعدادات التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('crypto_bot_ar.log', encoding='utf-8'), # تحديد الترميز للملف
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
            conn.autocommit = False # مهم للمعاملات الآمنة
            cur = conn.cursor()
            # إنشاء الجدول إذا لم يكن موجودًا، مع الأعمدة المحدثة والقيود
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    initial_target DOUBLE PRECISION NOT NULL, -- الهدف الأولي
                    initial_stop_loss DOUBLE PRECISION NOT NULL, -- وقف الخسارة الأولي
                    current_target DOUBLE PRECISION NOT NULL, -- الهدف الحالي (يمكن تحديثه)
                    current_stop_loss DOUBLE PRECISION NOT NULL, -- وقف الخسارة الحالي (يمكن تحديثه)
                    r2_score DOUBLE PRECISION, -- اسم العمود تم الإبقاء عليه، قد يمثل ثقة أو مؤشر آخر
                    volume_15m DOUBLE PRECISION,
                    achieved_target BOOLEAN DEFAULT FALSE,
                    hit_stop_loss BOOLEAN DEFAULT FALSE,
                    closing_price DOUBLE PRECISION, -- السعر الذي أغلقت عنده الصفقة
                    closed_at TIMESTAMP,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    profit_percentage DOUBLE PRECISION,
                    profitable_stop_loss BOOLEAN DEFAULT FALSE, -- هل تم الإغلاق فوق سعر الدخول بواسطة وقف الخسارة
                    is_trailing_active BOOLEAN DEFAULT FALSE -- هل الوقف المتحرك مفعل؟
                )
            """)
            conn.commit() # تأكيد إنشاء الجدول فوراً

            # --- إضافة الأعمدة الجديدة إذا لم تكن موجودة ---
            new_columns = {
                "initial_target": "DOUBLE PRECISION",
                "initial_stop_loss": "DOUBLE PRECISION",
                "current_target": "DOUBLE PRECISION",
                "current_stop_loss": "DOUBLE PRECISION",
                "is_trailing_active": "BOOLEAN DEFAULT FALSE",
                "closing_price": "DOUBLE PRECISION"
            }
            table_changed = False
            for col_name, col_type in new_columns.items():
                 try:
                     cur.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
                     conn.commit() # تأكيد كل تعديل
                     logger.info(f"✅ [DB] تمت إضافة العمود '{col_name}' إلى جدول 'signals'.")
                     table_changed = True
                 except psycopg2.Error as e:
                     if e.pgcode == '42701': # duplicate_column code
                         # logger.debug(f"ℹ️ [DB] العمود '{col_name}' موجود بالفعل في 'signals'.")
                         conn.rollback() # التراجع عن محاولة الإضافة الفاشلة
                     else:
                         logger.error(f"❌ [DB] فشل في إضافة العمود '{col_name}': {e} (pgcode: {e.pgcode})")
                         conn.rollback()
                         raise # إعادة طرح الأخطاء الأخرى

            # --- إضافة قيود NOT NULL إذا كانت مفقودة ---
            not_null_columns = [
                "symbol", "entry_price", "initial_target", "initial_stop_loss",
                "current_target", "current_stop_loss"
            ]
            for col_name in not_null_columns:
                try:
                    cur.execute(f"ALTER TABLE signals ALTER COLUMN {col_name} SET NOT NULL")
                    conn.commit()
                    logger.info(f"✅ [DB] تم التأكد من أن العمود '{col_name}' يحتوي على قيد NOT NULL.")
                    table_changed = True
                except psycopg2.Error as e:
                     if "is an identity column" in str(e) or "already set" in str(e):
                          conn.rollback()
                     elif e.pgcode == '42704':
                         conn.rollback()
                     else:
                         logger.warning(f"⚠️ [DB] لم يتمكن من تعيين NOT NULL للعمود '{col_name}' (قد يكون مقبولاً): {e}")
                         conn.rollback()


            if table_changed:
                 logger.info("✅ [DB] تم تحديث/التحقق من بنية قاعدة البيانات بنجاح.")
            else:
                 logger.info("✅ [DB] بنية قاعدة البيانات محدثة.")

            logger.info("✅ [DB] تم تأسيس الاتصال بقاعدة البيانات بنجاح.")
            return # نجاح

        except (psycopg2.OperationalError, psycopg2.DatabaseError) as e:
            logger.error(f"❌ [DB] فشلت محاولة الاتصال {i+1}: {e}")
            if i < retries - 1:
                logger.info(f"[DB] إعادة المحاولة خلال {delay} ثواني...")
                time.sleep(delay)
            else:
                logger.critical("❌ [DB] فشلت جميع محاولات الاتصال. الخروج.")
                raise # طرح آخر خطأ بعد فشل جميع المحاولات
        except Exception as e:
            logger.critical(f"❌ [DB] حدث خطأ غير متوقع أثناء تهيئة قاعدة البيانات: {e}")
            raise


def check_db_connection():
    global conn, cur
    try:
        if conn is None or conn.closed != 0:
             logger.warning("⚠️ [DB] الاتصال مغلق أو غير موجود. محاولة إعادة التهيئة...")
             init_db() # محاولة إعادة تأسيس الاتصال بالكامل
             return

        # التحقق من إمكانية استخدام الاتصال
        cur.execute("SELECT 1")
        # logger.debug("[DB] فحص الاتصال ناجح.")

    except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
        logger.warning(f"⚠️ [DB] تم فقدان الاتصال ({e}). محاولة إعادة التهيئة...")
        try:
            # إغلاق كائنات الاتصال القديمة المحتملة قبل إعادة التهيئة
            if cur: cur.close()
            if conn: conn.close()
            conn, cur = None, None # إعادة تعيين المتغيرات العامة
            init_db() # إعادة التهيئة بالكامل
        except Exception as ex:
            logger.error(f"❌ [DB] فشل في إعادة تهيئة الاتصال بعد فقدانه: {ex}")
            raise # إعادة طرح الفشل الحرج
    except Exception as e:
        logger.error(f"❌ [DB] خطأ غير متوقع أثناء فحص الاتصال: {e}")
        # محاولة إعادة التهيئة كحل بديل
        try:
            if cur: cur.close()
            if conn: conn.close()
            conn, cur = None, None
            init_db()
        except Exception as ex:
            logger.error(f"❌ [DB] فشل في إعادة تهيئة الاتصال بعد خطأ غير متوقع: {ex}")
            raise

# ---------------------- إعداد عميل Binance ----------------------
try:
    client = Client(api_key, api_secret)
    client.ping() # اختبار الاتصال
    logger.info("✅ [Binance] تم تهيئة عميل Binance والتحقق من الاتصال.")
except Exception as e:
    logger.critical(f"❌ [Binance] فشل في تهيئة عميل Binance: {e}. تحقق من مفاتيح API والاتصال.")
    raise # لا يمكن المتابعة بدون عميل Binance

# ---------------------- استخدام WebSocket لتحديث بيانات التيكر ----------------------
ticker_data = {} # قاموس لتخزين أحدث بيانات التيكر لكل زوج

def handle_ticker_message(msg):
    """يعالج رسائل WebSocket الواردة ويحدث قاموس ticker_data."""
    try:
        # تدفق الميني-تيكر يرسل قائمة من القواميس
        if isinstance(msg, list):
            for m in msg:
                symbol = m.get('s')
                if symbol and 'USDT' in symbol: # معالجة أزواج USDT فقط حاليًا
                    ticker_data[symbol] = {
                        'c': m.get('c'), # السعر الحالي
                        'h': m.get('h'), # أعلى سعر 24 ساعة
                        'l': m.get('l'), # أدنى سعر 24 ساعة
                        'v': m.get('v')  # حجم التداول الكلي للأصل الأساسي 24 ساعة
                    }
        elif isinstance(msg, dict) and 'stream' not in msg and 'e' in msg and msg['e'] == 'error':
            logger.error(f"❌ [WS] تم استلام رسالة خطأ من WebSocket: {msg.get('m')}")
        # تجاهل أنواع الرسائل الأخرى حاليًا
    except Exception as e:
        logger.error(f"❌ [WS] خطأ في handle_ticker_message: {e}")
        logger.debug(f"رسالة WS التي سببت المشكلة: {msg}")


def run_ticker_socket_manager():
    """يشغل مدير WebSocket الخاص بـ Binance للحصول على تحديثات الأسعار الحية."""
    while True: # الاستمرار في محاولة الاتصال
        try:
            logger.info("ℹ️ [WS] بدء تشغيل مدير WebSocket...")
            twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
            twm.start()
            # استخدام start_miniticker_socket للحصول على تحديثات لجميع الأزواج
            twm.start_miniticker_socket(callback=handle_ticker_message)
            logger.info("✅ [WS] تم توصيل WebSocket لتحديثات الميني-تيكر.")
            twm.join() # الانتظار حتى يتوقف المدير (مثل بسبب خطأ أو إيقاف يدوي)
            logger.warning("⚠️ [WS] توقف مدير WebSocket. سيتم محاولة إعادة التشغيل...")
        except Exception as e:
            logger.error(f"❌ [WS] خطأ في تشغيل مدير WebSocket: {e}. إعادة التشغيل بعد تأخير...")
        time.sleep(15) # الانتظار قبل محاولة إعادة التشغيل


# ---------------------- دوال حساب المؤشرات الفنية ----------------------
# (الدوال كما هي في المنطق، تم الإبقاء على الأسماء الإنجليزية للمؤشرات)
def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi_indicator(df, period=14):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    avg_loss = avg_loss.replace(0, np.nan) # تجنب القسمة على صفر
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

# دوال كشف أنماط الشموع
def is_hammer(row):
    open_price, high, low, close = row['open'], row['high'], row['low'], row['close']
    if None in [open_price, high, low, close]: return 0
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0: return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.3 * body:
        return 100 # إشارة شراء (مطرقة)
    return 0

def is_shooting_star(row):
    open_price, high, low, close = row['open'], row['high'], row['low'], row['close']
    if None in [open_price, high, low, close]: return 0
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0: return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and upper_shadow >= 2 * body and lower_shadow <= 0.3 * body:
        return -100 # إشارة بيع (شهاب)
    return 0

def compute_engulfing(df, idx):
    if idx == 0: return 0
    prev = df.iloc[idx - 1]
    curr = df.iloc[idx]
    if None in [prev['close'], prev['open'], curr['close'], curr['open']]: return 0
    # ابتلاعية شرائية
    if prev['close'] < prev['open'] and curr['close'] > curr['open']:
        if curr['open'] < prev['close'] and curr['close'] > prev['open']:
            return 100
    # ابتلاعية بيعية
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
    # تجميع إشارات الشراء الشمعية
    df['BullishSignal'] = df.apply(lambda row: 100 if (row['Hammer'] == 100 or row['Engulfing'] == 100) else 0, axis=1)
    # تجميع إشارات البيع الشمعية
    df['BearishSignal'] = df.apply(lambda row: 100 if (row['ShootingStar'] == -100 or row['Engulfing'] == -100) else 0, axis=1)
    return df

# ---------------------- دوال التنبؤ وتحليل المشاعر ----------------------
def ml_predict_signal(symbol, df):
    """دالة تنبؤ بسيطة تجريبية."""
    try:
        if df.empty or 'rsi' not in df.columns or 'adx' not in df.columns: return 0.5
        rsi = df['rsi'].iloc[-1]
        adx = df['adx'].iloc[-1]
        if pd.isna(rsi) or pd.isna(adx): return 0.5
        if rsi < 40 and adx > 25: return 0.80
        elif rsi > 65 and adx > 25: return 0.20
        else: return 0.5
    except IndexError: return 0.5
    except Exception as e:
        logger.error(f"❌ [ML] خطأ في ml_predict_signal للزوج {symbol}: {e}")
        return 0.5

def get_market_sentiment(symbol):
    """دالة تحليل مشاعر تجريبية."""
    return 0.6 # إيجابي مؤقتًا

def get_fear_greed_index():
    """يجلب مؤشر الخوف والجشع من alternative.me"""
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("data"):
            fng_value = float(data["data"][0].get("value", 50))
            fng_classification_en = data["data"][0].get("value_classification", "Neutral")
            # ترجمة بسيطة للتصنيف
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
            logger.warning("⚠️ [FNG] لم يتم العثور على بيانات في استجابة مؤشر الخوف والجشع.")
            return 50.0, "محايد"
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [FNG] خطأ في طلب مؤشر الخوف والجشع: {e}")
        return 50.0, "خطأ"
    except Exception as e:
        logger.error(f"❌ [FNG] خطأ غير متوقع في جلب مؤشر الخوف والجشع: {e}")
        return 50.0, "خطأ"

# ---------------------- استراتيجية Freqtrade المحسّنة (كفئة) ----------------------
class FreqtradeStrategy:
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """يحسب جميع المؤشرات المطلوبة للاستراتيجية."""
        if len(df) < 50:
            logger.warning(f"⚠️ [Strategy] DataFrame قصير جدًا ({len(df)} شمعة) لحساب جميع المؤشرات.")
            if all(col in df.columns for col in ['open', 'high', 'low', 'close']):
                 return df
            else:
                 return pd.DataFrame() # لا يمكن المتابعة

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
            df = calculate_macd(df) # استخدام الإعدادات الافتراضية للدالة
            df = calculate_kdj(df)
            df = calculate_adx(df)
            df = detect_candlestick_patterns(df)
            # حذف الصفوف التي تحتوي على NaN ناتجة عن حساب المؤشرات
            initial_len = len(df)
            df = df.dropna()
            dropped_count = initial_len - len(df)
            if dropped_count > 0:
                logger.debug(f"ℹ️ [Strategy] تم حذف {dropped_count} صفًا يحتوي على NaN بعد حساب المؤشرات.")

            if df.empty:
                logger.warning("⚠️ [Strategy] أصبح DataFrame فارغًا بعد حساب المؤشرات وحذف NaN.")
                return pd.DataFrame()

            logger.info(f"✅ [Strategy] تم حساب المؤشرات لـ DataFrame (الحجم النهائي: {len(df)})")
            return df

        except Exception as e:
            logger.error(f"❌ [Strategy] خطأ أثناء حساب المؤشرات: {e}", exc_info=True)
            return pd.DataFrame() # إرجاع DataFrame فارغ عند الخطأ


    def composite_buy_score(self, row):
        """يحسب درجة الشراء بناءً على شروط متعددة."""
        score = 0
        required_cols = ['ema5', 'ema8', 'ema21', 'ema34', 'ema50', 'rsi', 'close', 'lower_band', 'macd', 'macd_signal', 'macd_hist', 'kdj_j', 'kdj_k', 'kdj_d', 'adx', 'BullishSignal']
        # التحقق من وجود جميع الأعمدة المطلوبة وأنها ليست None
        if any(col not in row or pd.isna(row[col]) for col in required_cols):
            # logger.warning(f"⚠️ [Strategy] بيانات مفقودة أو NaN في الصف لحساب composite_buy_score.")
            return 0 # لا يمكن حساب الدرجة بشكل موثوق

        try:
            # شروط الشراء مع أوزان مختلفة
            if row['ema5'] > row['ema8'] > row['ema21'] > row['ema34'] > row['ema50']: score += 1.5 # اتجاه صاعد قوي
            if row['rsi'] < 40: score += 1 # تشبع بيعي محتمل
            # السعر قريب من الحد السفلي لبولينجر (ضمن 2%)
            if row['close'] > row['lower_band'] and ((row['close'] - row['lower_band']) / row['close'] < 0.02): score += 1
            if row['macd'] > row['macd_signal'] and row['macd_hist'] > 0: score += 1 # زخم صاعد (MACD)
            # KDJ إيجابي (في منطقة غير مشبعة جدًا)
            if row['kdj_j'] > row['kdj_k'] and row['kdj_k'] > row['kdj_d'] and row['kdj_j'] < 80: score += 1
            if row['adx'] > 20: score += 0.5 # وجود اتجاه (ليس بالضرورة صاعد)
            if row['BullishSignal'] == 100: score += 1.5 # نمط شمعي إيجابي قوي
        except TypeError as e:
             logger.error(f"❌ [Strategy] خطأ نوع البيانات في حساب composite_buy_score: {e}. بيانات الصف: {row.to_dict()}")
             return 0 # نوع بيانات غير صالح في الصف
        except Exception as e:
             logger.error(f"❌ [Strategy] خطأ غير متوقع في composite_buy_score: {e}", exc_info=True)
             return 0
        return score

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """يحدد إشارات الشراء بناءً على درجة الشراء."""
        required_score = 4.0 # العتبة المطلوبة لتوليد إشارة شراء
        required_cols = ['ema5', 'rsi', 'lower_band', 'macd', 'kdj_j', 'adx', 'BullishSignal'] # التأكد من حسابها

        if df.empty or not all(col in df.columns for col in required_cols):
             logger.warning("⚠️ [Strategy] DataFrame يفتقد للأعمدة المطلوبة لحساب اتجاه الشراء.")
             df['buy_score'] = 0
             df['buy'] = 0
             return df

        # تطبيق حساب الدرجة بأمان
        df['buy_score'] = df.apply(lambda row: self.composite_buy_score(row) if not row.isnull().any() else 0, axis=1)
        df['buy'] = np.where(df['buy_score'] >= required_score, 1, 0)

        buy_signals_count = df['buy'].sum()
        if buy_signals_count > 0:
             logger.info(f"✅ [Strategy] تم تحديد {buy_signals_count} إشارة/إشارات شراء محتملة (الدرجة >= {required_score}).")

        return df

# ---------------------- دالة التنبؤ بالسعر المحسّنة (اختياري ومبسط) ----------------------
def improved_predict_future_price(symbol, interval='2h', days=30):
    """تنبؤ مبسط باستخدام Gradient Boosting (اختياري)."""
    try:
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50: return None

        df['ema_fast'] = calculate_ema(df['close'], 10)
        df['ema_slow'] = calculate_ema(df['close'], 30)
        df['rsi'] = calculate_rsi_indicator(df)
        df = df.dropna()
        if len(df) < 2: return None

        features = ['ema_fast', 'ema_slow', 'rsi']
        if not all(f in df.columns for f in features): return None

        X = df[features].iloc[:-1].values
        y = df['close'].iloc[1:].values
        if len(X) == 0: return None

        model = GradientBoostingRegressor(n_estimators=100, max_depth=3, random_state=42, learning_rate=0.1)
        model.fit(X, y)
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]

        if predicted_price <= 0: return None
        logger.info(f"✅ [Price Prediction] السعر المتوقع للزوج {symbol} ({interval}, {days} يوم): {predicted_price:.8f}")
        return predicted_price
    except ImportError:
         logger.error("❌ [Price Prediction] مكتبة scikit-learn غير مثبتة. التنبؤ غير متاح.")
         return None
    except Exception as e:
        logger.error(f"❌ [Price Prediction] خطأ أثناء التنبؤ بالسعر للزوج {symbol}: {e}")
        return None

# ---------------------- دالة توليد الإشارة باستخدام الاستراتيجية المحسنة ----------------------
def generate_signal_using_freqtrade_strategy(df_input, symbol):
    """تولد إشارة شراء بناءً على الاستراتيجية المحسنة وتتحقق من الشروط."""
    if df_input is None or df_input.empty:
        logger.warning(f"⚠️ [Signal Gen] تم توفير DataFrame فارغ للزوج {symbol}.")
        return None
    if len(df_input) < 50:
         logger.info(f"ℹ️ [Signal Gen] بيانات غير كافية ({len(df_input)} شمعة) للزوج {symbol} على فريم {SIGNAL_GENERATION_TIMEFRAME}.")
         return None

    strategy = FreqtradeStrategy()
    df_processed = strategy.populate_indicators(df_input.copy())
    if df_processed.empty:
        logger.warning(f"⚠️ [Signal Gen] DataFrame فارغ بعد حساب المؤشرات للزوج {symbol}.")
        return None

    df_with_signals = strategy.populate_buy_trend(df_processed)
    if df_with_signals.empty or df_with_signals['buy'].iloc[-1] != 1:
        # logger.debug(f"ℹ️ [Signal Gen] لا توجد إشارة شراء في الشمعة الأخيرة للزوج {symbol} على فريم {SIGNAL_GENERATION_TIMEFRAME}.")
        return None

    last_signal_row = df_with_signals.iloc[-1]
    current_price = last_signal_row['close']
    current_atr = last_signal_row['atr']

    # التحقق من البيانات الأساسية من صف الإشارة
    if pd.isna(current_price) or pd.isna(current_atr) or current_atr <= 0 or current_price <= 0:
        logger.warning(f"⚠️ [Signal Gen] سعر ({current_price}) أو ATR ({current_atr}) غير صالح في صف الإشارة للزوج {symbol}.")
        return None

    # حساب الهدف ووقف الخسارة الأولي
    initial_target = current_price + (ENTRY_ATR_MULTIPLIER * current_atr)
    initial_stop_loss = current_price - (ENTRY_ATR_MULTIPLIER * current_atr)

    # التأكد من أن وقف الخسارة الأولي موجب
    if initial_stop_loss <= 0:
        min_sl_price = current_price * 0.95 # حل بديل: خسارة 5%
        initial_stop_loss = max(min_sl_price, 1e-9) # تأكد من أنه موجب
        logger.warning(f"⚠️ [Signal Gen] وقف الخسارة الأولي المحسوب للزوج {symbol} كان غير موجب. تم تعديله إلى: {initial_stop_loss:.8f}")

    # التحقق من هامش الربح الأدنى
    profit_margin_pct = ((initial_target / current_price) - 1) * 100 if current_price > 0 else 0
    if profit_margin_pct < MIN_PROFIT_MARGIN_PCT:
        logger.info(f"ℹ️ [Signal Gen] تم رفض إشارة {symbol}. هامش الربح ({profit_margin_pct:.2f}%) أقل من الحد الأدنى ({MIN_PROFIT_MARGIN_PCT:.1f}%).")
        return None

    # (اختياري) التحقق من التنبؤ بالسعر (معطل حاليًا)
    # ...

    buy_score = last_signal_row.get('buy_score', 0)

    # بناء كائن الإشارة
    signal = {
        'symbol': symbol,
        'entry_price': float(f"{current_price:.8f}"),
        'initial_target': float(f"{initial_target:.8f}"),
        'initial_stop_loss': float(f"{initial_stop_loss:.8f}"),
        'current_target': float(f"{initial_target:.8f}"), # الهدف الحالي = الأولي عند الإنشاء
        'current_stop_loss': float(f"{initial_stop_loss:.8f}"),# الوقف الحالي = الأولي عند الإنشاء
        'strategy': 'freqtrade_improved_atr', # اسم الاستراتيجية
        'indicators': { # تخزين المؤشرات الرئيسية وقت الإشارة
            'rsi': round(last_signal_row['rsi'], 2) if 'rsi' in last_signal_row and pd.notna(last_signal_row['rsi']) else None,
            'macd_hist': round(last_signal_row['macd_hist'], 5) if 'macd_hist' in last_signal_row and pd.notna(last_signal_row['macd_hist']) else None,
            'adx': round(last_signal_row['adx'], 2) if 'adx' in last_signal_row and pd.notna(last_signal_row['adx']) else None,
            'atr': round(current_atr, 8),
            'buy_score': round(buy_score, 2)
        },
        'r2_score': round(buy_score, 2), # استخدام درجة الشراء كمؤشر ثقة مبدئي
        'trade_value': TRADE_VALUE,
    }

    logger.info(f"✅ [Signal Gen] تم توليد إشارة شراء للزوج {symbol} عند سعر {current_price:.8f} (الدرجة: {buy_score:.2f}).")
    # logger.debug(f"تفاصيل الإشارة: {json.dumps(signal, indent=2)}")
    return signal

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    # يمكنك إضافة المزيد من المعلومات هنا، مثل وقت التشغيل أو الحالة
    return f"🚀 بوت توصيات التداول الإصدار 4.3 (Hazem Mod) - خدمة الإشارات تعمل. {datetime.utcnow().isoformat()}Z", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """يعالج تحديثات webhook من Telegram (خاصة استعلامات الرد)."""
    try:
        update = request.get_json()
        if not update:
            logger.warning("⚠️ [Webhook] تم استلام تحديث فارغ.")
            return '', 200

        logger.debug(f"🔔 [Webhook] تم استلام تحديث: {json.dumps(update, indent=2)}")

        if "callback_query" in update:
            callback_query = update["callback_query"]
            data = callback_query.get("data")
            query_id = callback_query.get("id")
            message = callback_query.get("message")
            chat_info = message.get("chat", {}) if message else {}
            chat_id_callback = chat_info.get("id")
            user_info = callback_query.get("from", {})
            user_id = user_info.get("id")
            username = user_info.get("username", "N/A")

            if not chat_id_callback or not query_id:
                 logger.error(f"❌ [Webhook] chat_id ({chat_id_callback}) أو query_id ({query_id}) مفقود في callback_query.")
                 return 'Bad Request', 400

            # الرد على استعلام الرد فورًا لمنع انتهاء المهلة في Telegram
            answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
            try:
                 requests.post(answer_url, json={"callback_query_id": query_id}, timeout=5)
            except Exception as ans_err:
                 logger.error(f"❌ [Webhook] فشل في الرد على استعلام الرد {query_id}: {ans_err}")
                 # متابعة معالجة الطلب على أي حال

            logger.info(f"ℹ️ [Webhook] تم استلام رد '{data}' من المستخدم @{username} (ID: {user_id}) في الدردشة {chat_id_callback}.")

            # معالجة بيانات الرد المختلفة
            if data == "get_report":
                # تشغيل إنشاء التقرير في خيط منفصل لتجنب حظر استجابة webhook
                Thread(target=send_report, args=(chat_id_callback,), daemon=True).start()
            # أضف معالجات ردود أخرى هنا
            # elif data.startswith("action_"):
            #     handle_other_action(data, chat_id_callback, user_id)
            else:
                 logger.warning(f"⚠️ [Webhook] تم استلام بيانات رد غير معروفة: {data}")

            return '', 200 # استجابة نجاح

        # معالجة أنواع أخرى من التحديثات إذا لزم الأمر (مثل الأوامر)
        elif "message" in update and "text" in update["message"]:
             message = update["message"]
             chat_info = message.get("chat", {})
             chat_id_msg = chat_info.get("id")
             text = message.get("text", "").strip()
             user_info = message.get("from", {})
             user_id = user_info.get("id")
             username = user_info.get("username", "N/A")

             # تحويل النص إلى حروف صغيرة للمقارنة بالأوامر
             command = text.lower()

             if command == '/report' or command == '/stats' or command == '/تقرير':
                  logger.info(f"ℹ️ [Webhook] تم استلام الأمر '{text}' من المستخدم @{username} (ID: {user_id}) في الدردشة {chat_id_msg}.")
                  Thread(target=send_report, args=(chat_id_msg,), daemon=True).start()
                  return '', 200
             elif command == '/status' or command == '/الحالة':
                 logger.info(f"ℹ️ [Webhook] تم استلام الأمر '{text}' من المستخدم @{username} (ID: {user_id}) في الدردشة {chat_id_msg}.")
                 # إرسال رسالة حالة بسيطة
                 ws_status = 'نعم' if websocket_thread and websocket_thread.is_alive() else 'لا'
                 tracker_status = 'نعم' if tracker_thread and tracker_thread.is_alive() else 'لا'
                 scheduler_status = 'نعم' if scheduler and scheduler.running else 'لا'
                 status_msg = f"✅ **حالة البوت** ✅\n- اتصال WebSocket: {ws_status}\n- خدمة التتبع نشطة: {tracker_status}\n- المجدول نشط: {scheduler_status}"
                 send_telegram_update(status_msg, chat_id_override=chat_id_msg) # الإرسال إلى الدردشة الطالبة
                 return '', 200
             # معالجة أوامر أو رسائل أخرى

    except json.JSONDecodeError:
        logger.error("❌ [Webhook] تم استلام JSON غير صالح.")
        return 'Invalid JSON', 400
    except Exception as e:
        logger.error(f"❌ [Webhook] خطأ في معالجة التحديث: {e}", exc_info=True)
        return 'Internal Server Error', 500

    return '', 200 # استجابة نجاح افتراضية

def set_telegram_webhook():
    """تسجل عنوان URL لتطبيق Flask كـ webhook لدى Telegram."""
    render_service_name = os.environ.get("RENDER_SERVICE_NAME") # مثلاً، من متغيرات بيئة Render
    if not render_service_name:
        webhook_base_url = config('WEBHOOK_BASE_URL', default=None) # تعريف في .env إذا لزم الأمر
        if not webhook_base_url:
             logger.warning("⚠️ [Webhook] لم يتم تعيين RENDER_SERVICE_NAME أو WEBHOOK_BASE_URL. لا يمكن تعيين webhook تلقائيًا.")
             # التحقق مما إذا كان webhook معينًا بالفعل
             try:
                 get_wh_url = f"https://api.telegram.org/bot{telegram_token}/getWebhookInfo"
                 resp = requests.get(get_wh_url, timeout=10)
                 wh_info = resp.json()
                 if wh_info.get("ok") and wh_info.get("result",{}).get("url"):
                     logger.info(f"ℹ️ [Webhook] يبدو أن Webhook معين بالفعل إلى: {wh_info['result']['url']}")
                     return
                 else:
                     logger.warning("⚠️ [Webhook] لم يتمكن من تأكيد إعداد webhook الحالي.")
             except Exception as e:
                 logger.error(f"❌ [Webhook] خطأ في التحقق من webhook الحالي: {e}")
             return # الخروج إذا لم يمكن تحديد URL

        webhook_url = f"{webhook_base_url.rstrip('/')}/webhook"
    else:
         webhook_url = f"https://{render_service_name}.onrender.com/webhook"


    set_url = f"https://api.telegram.org/bot{telegram_token}/setWebhook"
    params = {'url': webhook_url}
    try:
        response = requests.get(set_url, params=params, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"✅ [Webhook] تم تعيين webhook تليجرام بنجاح إلى: {webhook_url}")
            logger.info(f"ℹ️ [Webhook] رد تليجرام: {res_json.get('description')}")
        else:
            logger.error(f"❌ [Webhook] فشل في تعيين webhook: {res_json}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Webhook] استثناء أثناء طلب إعداد webhook: {e}")
    except Exception as e:
        logger.error(f"❌ [Webhook] خطأ غير متوقع أثناء إعداد webhook: {e}")

# ---------------------- وظائف تحليل البيانات المساعدة ----------------------
def get_crypto_symbols(filename='crypto_list.txt'):
    """يقرأ قائمة رموز العملات من ملف نصي."""
    symbols = []
    try:
        # تحديد المسار المطلق للملف نسبةً لموقع السكربت
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)

        if not os.path.exists(file_path):
            logger.error(f"❌ [Data] ملف قائمة الرموز '{filename}' غير موجود في المسار: {file_path}")
            # محاولة العثور عليه في مجلد العمل الحالي كحل بديل
            alt_path = os.path.abspath(filename)
            if os.path.exists(alt_path):
                logger.warning(f"⚠️ [Data] استخدام ملف قائمة الرموز الموجود في المجلد الحالي: {alt_path}")
                file_path = alt_path
            else:
                return [] # إرجاع قائمة فارغة إذا لم يتم العثور عليه

        with open(file_path, 'r', encoding='utf-8') as f: # تحديد الترميز عند القراءة
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip() and not line.startswith('#')]
        logger.info(f"✅ [Data] تم تحميل {len(symbols)} رمزًا من '{os.path.basename(file_path)}'.")
        return symbols
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في قراءة ملف الرموز '{filename}': {e}")
        return [] # إرجاع قائمة فارغة عند الخطأ

def fetch_historical_data(symbol, interval='1h', days=10):
    """يجلب بيانات الشموع التاريخية لرمز معين من Binance."""
    try:
        # logger.debug(f"⏳ [Data] جلب البيانات التاريخية: {symbol} - {interval} - {days} يوم")
        start_str = f"{days} day ago UTC"
        klines = client.get_historical_klines(symbol, interval, start_str)

        if not klines:
            logger.warning(f"⚠️ [Data] لم يتم العثور على بيانات تاريخية للزوج {symbol} ({interval}, {days} يوم).")
            return None

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])

        # تحويل الأعمدة الأساسية إلى أرقام، مع تحويل الأخطاء إلى NaN
        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # تحويل timestamp إلى كائنات datetime (اختياري، لكن جيد)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # حذف الصفوف التي تحتوي على NaN في أعمدة الأسعار الأساسية بعد التحويل
        initial_len = len(df)
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        if len(df) < initial_len:
             logger.debug(f"ℹ️ [Data] تم حذف {initial_len - len(df)} صفًا يحتوي على أسعار NaN للزوج {symbol}.")

        if df.empty:
             logger.warning(f"⚠️ [Data] DataFrame للزوج {symbol} أصبح فارغًا بعد معالجة NaN.")
             return None

        # logger.info(f"✅ [Data] تم جلب {len(df)} شمعة للزوج {symbol} ({interval}, {days} يوم).")
        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

    except Exception as e: # يمكن تحديد استثناءات Binance المحددة
        logger.error(f"❌ [Data] خطأ في جلب البيانات للزوج {symbol} ({interval}, {days} يوم): {e}")
        return None

def fetch_recent_volume(symbol):
    """يجلب حجم تداول العملة المقابلة (Quote) لآخر 15 دقيقة."""
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=15)
        if not klines:
             logger.warning(f"⚠️ [Data] لم يتم العثور على شموع 1m للزوج {symbol} لحساب الحجم الأخير.")
             return 0.0

        # الفهرس 7 في klines هو حجم العملة المقابلة (Quote asset volume)
        volume = sum(float(k[7]) for k in klines if len(k) > 7 and k[7])
        # logger.info(f"✅ [Data] حجم التداول (Quote) للزوج {symbol} (آخر 15 دقيقة): {volume:,.2f} USDT.")
        return volume
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب الحجم الأخير للزوج {symbol}: {e}")
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
        logger.info(f"✅ [Data] نسب سيطرة السوق - BTC: {btc_dominance:.2f}%, ETH: {eth_dominance:.2f}%")
        return btc_dominance, eth_dominance
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Data] خطأ في طلب نسب السيطرة من CoinGecko: {e}")
        return None, None # إرجاع None للإشارة إلى الفشل
    except Exception as e:
        logger.error(f"❌ [Data] خطأ غير متوقع في get_market_dominance: {e}")
        return None, None

# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    """يرسل تنبيه توصية جديدة إلى Telegram."""
    try:
        entry_price = signal['entry_price']
        target_price = signal['initial_target']
        stop_loss_price = signal['initial_stop_loss']

        if entry_price <= 0:
             logger.error(f"❌ [Telegram] سعر الدخول غير صالح ({entry_price}) للزوج {signal['symbol']}. لا يمكن إرسال التنبيه.")
             return

        profit_pct = ((target_price / entry_price) - 1) * 100
        loss_pct = ((stop_loss_price / entry_price) - 1) * 100
        profit_usdt = TRADE_VALUE * (profit_pct / 100)
        loss_usdt = TRADE_VALUE * (loss_pct / 100) # ستكون قيمة سالبة

        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M') # تعديل للتوقيت المحلي +3
        fng_value, fng_label = get_fear_greed_index()

        # تهريب حروف الماركداون الحساسة في اسم الزوج
        safe_symbol = signal['symbol'].replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')

        message = (
            f"🚀 **توصية تداول جديدة** 🚀\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **سعر الدخول المقترح:** `${entry_price:.8f}`\n"
            f"🎯 **الهدف الأولي:** `${target_price:.8f}` ({profit_pct:+.2f}% / {profit_usdt:+.2f} USDT)\n"
            f"🛑 **وقف الخسارة الأولي:** `${stop_loss_price:.8f}` ({loss_pct:.2f}% / {loss_usdt:.2f} USDT)\n"
            f"⏱ **الفريم الزمني للإشارة:** {timeframe}\n"
            f"💧 **السيولة (آخر 15د):** {volume:,.0f} USDT\n"
            f"💰 **قيمة الصفقة المقترحة:** ${TRADE_VALUE}\n"
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

        send_telegram_message(chat_id, message, reply_markup=reply_markup) # استخدام الدالة المساعدة
        logger.info(f"✅ [Telegram] تم إرسال تنبيه التوصية الجديدة للزوج {signal['symbol']}.")

    except Exception as e:
        logger.error(f"❌ [Telegram] فشل في بناء أو إرسال تنبيه التوصية للزوج {signal['symbol']}: {e}", exc_info=True)


def send_telegram_update(message, chat_id_override=None):
    """يرسل رسالة تحديث عامة (مثل تحديث SL/TP أو إغلاق صفقة) إلى Telegram."""
    target_chat = chat_id_override if chat_id_override else chat_id
    try:
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }
        send_telegram_message(target_chat, message, reply_markup=reply_markup) # استخدام الدالة المساعدة
        logger.info(f"✅ [Telegram] تم إرسال رسالة التحديث بنجاح إلى الدردشة {target_chat}.")

    except Exception as e:
        logger.error(f"❌ [Telegram] فشل في إرسال رسالة التحديث إلى الدردشة {target_chat}: {e}")


def send_telegram_message(chat_id_target, text, reply_markup=None, parse_mode='Markdown', disable_web_page_preview=True, timeout=15):
    """دالة مساعدة لإرسال الرسائل إلى Telegram مع معالجة الأخطاء."""
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
        response.raise_for_status() # طرح خطأ HTTP للاستجابات السيئة (4xx أو 5xx)
        # logger.debug(f"إرسال تليجرام ناجح. الاستجابة: {response.text}")
        return response.json()
    except requests.exceptions.Timeout:
         logger.error(f"❌ [Telegram] انتهت مهلة الطلب عند إرسال رسالة إلى الدردشة {chat_id_target}.")
         raise # إعادة طرح انتهاء المهلة
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Telegram] خطأ شبكة/API عند إرسال رسالة إلى الدردشة {chat_id_target}: {e}")
        # تسجيل استجابة خطأ Telegram إذا كانت متاحة
        if e.response is not None:
            try:
                error_info = e.response.json()
                logger.error(f"❌ [Telegram] تفاصيل خطأ API: {error_info}")
            except json.JSONDecodeError:
                logger.error(f"❌ [Telegram] استجابة خطأ API ليست JSON: {e.response.text}")
        raise # إعادة طرح الاستثناء
    except Exception as e:
        logger.error(f"❌ [Telegram] خطأ غير متوقع عند إرسال رسالة إلى الدردشة {chat_id_target}: {e}")
        raise # إعادة طرح الأخطاء غير المتوقعة

# ---------------------- إرسال تقرير الأداء الشامل ----------------------
def send_report(target_chat_id):
    """يحسب ويرسل تقرير أداء شامل إلى Telegram."""
    logger.info(f"⏳ [Report] جاري إنشاء تقرير الأداء للدردشة: {target_chat_id}")
    report_message = "⚠️ فشل إنشاء تقرير الأداء." # رسالة خطأ افتراضية
    try:
        check_db_connection()

        # 1. عدد الصفقات النشطة
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]

        # 2. إحصائيات الصفقات المغلقة
        cur.execute("""
            SELECT achieved_target, hit_stop_loss, profitable_stop_loss, profit_percentage
            FROM signals WHERE closed_at IS NOT NULL
        """)
        closed_signals = cur.fetchall()

        total_closed_trades = len(closed_signals)
        if total_closed_trades == 0:
            report_message = f"📊 **تقرير الأداء**\n\nلا توجد صفقات مغلقة حتى الآن.\n⏳ **التوصيات النشطة حالياً:** {active_count}"
            send_telegram_update(report_message, chat_id_override=target_chat_id)
            logger.info("✅ [Report] تم إرسال التقرير (لا توجد صفقات مغلقة).")
            return

        # حساب الإحصائيات
        successful_target_hits = sum(1 for s in closed_signals if s[0]) # achieved_target = TRUE
        profitable_sl_hits = sum(1 for s in closed_signals if s[1] and s[2]) # hit_stop_loss = TRUE AND profitable_stop_loss = TRUE
        losing_sl_hits = sum(1 for s in closed_signals if s[1] and not s[2]) # hit_stop_loss = TRUE AND profitable_stop_loss = FALSE

        # حساب الأرباح والخسائر بناءً على النسبة المئوية المحفوظة
        total_profit_usd = 0
        total_loss_usd = 0
        profit_percentages = []
        loss_percentages = []

        for signal in closed_signals:
            profit_pct = signal[3] # profit_percentage المحفوظة
            if profit_pct is not None:
                trade_result_usd = TRADE_VALUE * (profit_pct / 100)
                if trade_result_usd > 0:
                    total_profit_usd += trade_result_usd
                    profit_percentages.append(profit_pct)
                else:
                    total_loss_usd += trade_result_usd # الخسارة سالبة
                    loss_percentages.append(profit_pct)
            else:
                 logger.warning(f"⚠️ [Report] تم العثور على توصية مغلقة بدون profit_percentage.") # قد تحتاج لمعرف التوصية هنا

        net_profit_usd = total_profit_usd + total_loss_usd
        win_rate = (successful_target_hits + profitable_sl_hits) / total_closed_trades * 100 if total_closed_trades > 0 else 0
        avg_profit_pct = np.mean(profit_percentages) if profit_percentages else 0
        avg_loss_pct = np.mean(loss_percentages) if loss_percentages else 0
        # معامل الربح (Profit Factor)
        profit_factor = abs(total_profit_usd / total_loss_usd) if total_loss_usd != 0 else float('inf')

        timestamp = (datetime.utcnow() + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M') # توقيت +3

        # بناء رسالة التقرير
        report_message = (
            f"📊 **تقرير أداء البوت** ({timestamp} توقيت +3)\n"
            f"——————————————\n"
            f"**ملخص الصفقات المغلقة ({total_closed_trades}):**\n"
            f"  ✅ تحقيق الهدف: {successful_target_hits}\n"
            f"  📈 وقف خسارة رابح: {profitable_sl_hits}\n"
            f"  📉 وقف خسارة خاسر: {losing_sl_hits}\n"
            f"  📊 معدل الربح (Win Rate): {win_rate:.2f}%\n"
            f"——————————————\n"
            f"**الأداء المالي:**\n"
            f"  💰 إجمالي الربح: +{total_profit_usd:.2f} USDT\n"
            f"  💸 إجمالي الخسارة: {total_loss_usd:.2f} USDT\n"
            f"  💵 **صافي الربح/الخسارة:** {net_profit_usd:+.2f} USDT\n"
            f"  🎯 متوسط ربح الصفقة: {avg_profit_pct:+.2f}%\n"
            f"  🛑 متوسط خسارة الصفقة: {avg_loss_pct:.2f}%\n"
            f"  ⚖️ معامل الربح (Profit Factor): {profit_factor:.2f}\n"
            f"——————————————\n"
            f"⏳ **التوصيات النشطة حالياً:** {active_count}"
        )
        send_telegram_update(report_message, chat_id_override=target_chat_id) # إرسال التقرير النهائي
        logger.info(f"✅ [Report] تم إرسال تقرير الأداء بنجاح إلى الدردشة {target_chat_id}.")

    except psycopg2.Error as db_err:
        logger.error(f"❌ [Report] خطأ في قاعدة البيانات أثناء إنشاء التقرير: {db_err}")
        if conn and not conn.closed: conn.rollback()
        report_message = f"⚠️ حدث خطأ في قاعدة البيانات أثناء إنشاء التقرير.\n`{db_err}`"
        try:
            send_telegram_update(report_message, chat_id_override=target_chat_id)
        except Exception as send_err:
             logger.error(f"❌ [Report] فشل في إرسال رسالة خطأ قاعدة البيانات: {send_err}")
    except Exception as e:
        logger.error(f"❌ [Report] فشل في إنشاء أو إرسال تقرير الأداء: {e}", exc_info=True)
        report_message = f"⚠️ حدث خطأ غير متوقع أثناء إنشاء التقرير.\n`{e}`"
        try:
            send_telegram_update(report_message, chat_id_override=target_chat_id)
        except Exception as send_err:
             logger.error(f"❌ [Report] فشل في إرسال رسالة الخطأ العامة: {send_err}")


# ---------------------- خدمة تتبع الإشارات وتحديثها (مع الحل للمشكلة) ----------------------
def track_signals():
    """
    تتابع التوصيات المفتوحة، تتحقق من تحقيق الهدف/الوصول لوقف الخسارة،
    وتطبق وقف الخسارة المتحرك (ATR Trailing Stop).
    تتضمن الحل لمشكلة تنسيق NoneType.
    """
    logger.info(f"🔄 [Tracker] بدء خدمة تتبع التوصيات (الفريم: {SIGNAL_TRACKING_TIMEFRAME}, بيانات: {SIGNAL_TRACKING_LOOKBACK_DAYS} يوم)...")

    while True:
        try:
            check_db_connection() # التأكد من سلامة الاتصال

            # جلب جميع التوصيات النشطة (لم يتم إغلاقها بعد)
            cur.execute("""
                SELECT id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active
                FROM signals
                WHERE closed_at IS NULL
            """)
            active_signals = cur.fetchall()

            if not active_signals:
                # logger.info("ℹ️ [Tracker] لا توجد توصيات نشطة لتتبعها حاليًا.")
                time.sleep(20) # نوم أطول إذا لا توجد توصيات نشطة
                continue # العودة لبداية حلقة while

            logger.info("==========================================")
            logger.info(f"🔍 [Tracker] جاري تتبع {len(active_signals)} توصية نشطة...")

            for signal_data in active_signals:
                signal_id, symbol, entry_price, current_target, current_stop_loss, is_trailing_active = signal_data
                # logger.info(f"--- [Tracker] تتبع {symbol} (ID: {signal_id}) ---") # تقليل تفاصيل السجل لكل دورة

                # 1. الحصول على السعر الحالي
                current_price = None # تهيئة السعر الحالي إلى None للأمان
                if symbol not in ticker_data or ticker_data[symbol].get('c') is None:
                    logger.warning(f"⚠️ [Tracker] لا توجد بيانات سعر حالية من WebSocket للزوج {symbol}. تخطي هذه الدورة.")
                    continue
                try:
                    price_str = ticker_data[symbol]['c']
                    if price_str is not None:
                         current_price = float(price_str)
                         if current_price <= 0:
                              logger.warning(f"⚠️ [Tracker] السعر الحالي المستلم غير صالح ({current_price}) للزوج {symbol}. تخطي.")
                              current_price = None # التأكد من أنه None إذا كان غير صالح
                    else:
                         logger.warning(f"⚠️ [Tracker] تم استلام قيمة سعر None ('c') للزوج {symbol}. تخطي.")
                except (ValueError, TypeError) as e:
                     logger.warning(f"⚠️ [Tracker] قيمة السعر المستلمة ({ticker_data[symbol].get('c')}) غير رقمية للزوج {symbol}: {e}. تخطي.")
                     current_price = None # التأكد من أنه None عند خطأ التحويل

                # ** فحص حرج 1 **: التأكد من وجود سعر حالي صالح قبل المتابعة
                if current_price is None:
                     # logger.warning(f"⚠️ [Tracker] لا يمكن المتابعة بدون سعر حالي صالح للزوج {symbol}. تخطي.")
                     continue # تخطي هذه التوصية لهذه الدورة

                # ** فحص حرج 2 **: التأكد من أن البيانات الأساسية من قاعدة البيانات ليست None
                if entry_price is None or current_target is None or current_stop_loss is None:
                    logger.error(f"❌ [Tracker] بيانات حرجة مفقودة (None) من قاعدة البيانات للتوصية ID {signal_id} ({symbol}): "
                                 f"الدخول={entry_price}, الهدف={current_target}, الوقف={current_stop_loss}. لا يمكن معالجة هذه التوصية.")
                    # يمكن إضافة منطق هنا لوضع علامة على هذه التوصية كمشكلة في قاعدة البيانات؟
                    continue # تخطي هذه التوصية بالكامل لهذه الدورة

                # تسجيل الحالة الحالية (آمن الآن بعد فحوصات None)
                logger.info(f"  [Tracker] {symbol} (ID:{signal_id}) | السعر: {current_price:.8f} | الدخول: {entry_price:.8f} | الهدف: {current_target:.8f} | الوقف: {current_stop_loss:.8f} | متحرك: {is_trailing_active}")

                # ** فحص حرج 3 **: التحقق من سعر الدخول للحسابات
                if abs(entry_price) < 1e-9: # استخدام عتبة صغيرة بدلاً من الصفر
                    logger.error(f"❌ [Tracker] سعر الدخول ({entry_price}) قريب جدًا من الصفر للتوصية ID {signal_id} ({symbol}). لا يمكن حساب النسب.")
                    continue

                # 2. التحقق من إغلاق الصفقة (الهدف أو وقف الخسارة الأولي/المتحرك)
                # --- التحقق من الهدف ---
                if current_price >= current_target:
                    profit_pct = ((current_target / entry_price) - 1) * 100
                    profit_usdt = TRADE_VALUE * (profit_pct / 100)
                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                    msg = (f"✅ **تم تحقيق الهدف!** ✅\n"
                           f"📈 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                           f"💰 أغلق عند: ${current_price:.8f} (الهدف: ${current_target:.8f})\n"
                           f"📊 الربح: +{profit_pct:.2f}% ({profit_usdt:+.2f} USDT)")
                    try:
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET achieved_target = TRUE, closed_at = NOW(), profit_percentage = %s, closing_price = %s
                            WHERE id = %s AND closed_at IS NULL
                        """, (profit_pct, current_price, signal_id)) # إضافة شرط closed_at IS NULL
                        conn.commit()
                        logger.info(f"✅ [Tracker] تم إغلاق التوصية {symbol} (ID: {signal_id}) - تحقيق الهدف.")
                    except Exception as update_err:
                        logger.error(f"❌ [Tracker] خطأ أثناء تحديث/إرسال إغلاق الهدف للتوصية {signal_id}: {update_err}")
                        if conn and not conn.closed: conn.rollback()
                    continue # الانتقال للتوصية التالية

                # --- التحقق من وقف الخسارة ---
                elif current_price <= current_stop_loss:
                    loss_pct = ((current_stop_loss / entry_price) - 1) * 100
                    loss_usdt = TRADE_VALUE * (loss_pct / 100)
                    profitable_stop = current_stop_loss > entry_price # هل كان الوقف فوق سعر الدخول؟
                    stop_type_msg = "وقف خسارة رابح" if profitable_stop else "وقف خسارة"
                    emoji = "📈" if profitable_stop else "🛑"
                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                    msg = (f"{emoji} **{stop_type_msg}** {emoji}\n"
                           f"📉 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                           f"💰 أغلق عند: ${current_price:.8f} (الوقف: ${current_stop_loss:.8f})\n"
                           f"📊 النتيجة: {loss_pct:.2f}% ({loss_usdt:.2f} USDT)")
                    try:
                        send_telegram_update(msg)
                        cur.execute("""
                            UPDATE signals
                            SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s, profitable_stop_loss = %s, closing_price = %s
                            WHERE id = %s AND closed_at IS NULL
                        """, (loss_pct, profitable_stop, current_price, signal_id)) # إضافة شرط closed_at IS NULL
                        conn.commit()
                        logger.info(f"✅ [Tracker] تم إغلاق التوصية {symbol} (ID: {signal_id}) - {stop_type_msg}.")
                    except Exception as update_err:
                        logger.error(f"❌ [Tracker] خطأ أثناء تحديث/إرسال إغلاق وقف الخسارة للتوصية {signal_id}: {update_err}")
                        if conn and not conn.closed: conn.rollback()
                    continue # الانتقال للتوصية التالية

                # 3. تطبيق/تحديث وقف الخسارة المتحرك (ATR Trailing Stop)
                # --- جلب بيانات الفريم القصير (مثل 15m) لحساب ATR الحالي ---
                df_track = fetch_historical_data(symbol, interval=SIGNAL_TRACKING_TIMEFRAME, days=SIGNAL_TRACKING_LOOKBACK_DAYS)

                if df_track is None or df_track.empty or len(df_track) < 20: # نحتاج بيانات كافية لـ ATR
                    logger.warning(f"⚠️ [Tracker] بيانات {SIGNAL_TRACKING_TIMEFRAME} غير كافية لحساب ATR للزوج {symbol}. تخطي تحديث الوقف المتحرك.")
                else:
                    # حساب ATR على بيانات التتبع
                    df_track = calculate_atr_indicator(df_track, period=14) # استخدام فترة ATR القياسية
                    if 'atr' not in df_track.columns or df_track['atr'].iloc[-1] is None or pd.isna(df_track['atr'].iloc[-1]):
                         logger.warning(f"⚠️ [Tracker] فشل في حساب ATR للزوج {symbol} على فريم {SIGNAL_TRACKING_TIMEFRAME}.")
                    else:
                        current_atr = df_track['atr'].iloc[-1]
                        if current_atr > 0:
                            # logger.debug(f"  - ATR ({SIGNAL_TRACKING_TIMEFRAME}) للزوج {symbol}: {current_atr:.8f}")

                            # --- حساب نسبة الربح الحالية ---
                            current_gain_pct = (current_price - entry_price) / entry_price

                            # --- التحقق من شرط تفعيل/تحديث الوقف المتحرك ---
                            if current_gain_pct >= TRAILING_STOP_ACTIVATION_PROFIT_PCT:
                                # حساب وقف الخسارة المتحرك المقترح
                                potential_new_stop_loss = current_price - (TRAILING_STOP_ATR_MULTIPLIER * current_atr)

                                # --- التحقق مما إذا كان الوقف المقترح أعلى من الحالي ---
                                # الوقف المتحرك يجب أن يرتفع فقط، لا ينخفض
                                if potential_new_stop_loss > current_stop_loss:
                                    new_stop_loss = potential_new_stop_loss
                                    logger.info(f"  => [Tracker] تحديث الوقف المتحرك للزوج {symbol} (ID: {signal_id})!")
                                    logger.info(f"     الوقف الجديد: {new_stop_loss:.8f} (السعر الحالي - {TRAILING_STOP_ATR_MULTIPLIER} * ATR)")

                                    # --- إرسال إشعار بالتحديث وتحديث قاعدة البيانات ---
                                    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
                                    update_msg = (
                                        f"🔄 **تحديث وقف الخسارة (متحرك)** 🔄\n"
                                        f"📈 الزوج: `{safe_symbol}` (ID: {signal_id})\n"
                                        f"   - سعر الدخول: ${entry_price:.8f}\n"
                                        f"   - السعر الحالي: ${current_price:.8f} ({current_gain_pct:+.2%})\n"
                                        f"   - الوقف القديم: ${current_stop_loss:.8f}\n"
                                        f"   - **الوقف الجديد:** `${new_stop_loss:.8f}` ✅"
                                    )
                                    try:
                                        send_telegram_update(update_msg)
                                        cur.execute("""
                                            UPDATE signals
                                            SET current_stop_loss = %s, is_trailing_active = TRUE
                                            WHERE id = %s AND closed_at IS NULL
                                        """, (new_stop_loss, signal_id)) # إضافة شرط closed_at IS NULL
                                        conn.commit()
                                        logger.info(f"✅ [Tracker] تم تحديث الوقف المتحرك للتوصية {symbol} (ID: {signal_id}) إلى {new_stop_loss:.8f}")
                                    except Exception as update_err:
                                         logger.error(f"❌ [Tracker] خطأ أثناء تحديث/إرسال تحديث الوقف المتحرك للتوصية {signal_id}: {update_err}")
                                         if conn and not conn.closed: conn.rollback()
                                # else:
                                #     logger.debug(f"  - الوقف المتحرك المقترح ({potential_new_stop_loss:.8f}) ليس أعلى من الحالي ({current_stop_loss:.8f}) للزوج {symbol}. لا تغيير.")

                            # else:
                            #      logger.debug(f"  - الربح الحالي ({current_gain_pct:.2%}) للزوج {symbol} لم يصل لعتبة تفعيل الوقف المتحرك ({TRAILING_STOP_ACTIVATION_PROFIT_PCT:.2%}).")
                        # else:
                        #      logger.warning(f"⚠️ [Tracker] قيمة ATR المحسوبة غير موجبة ({current_atr}) للزوج {symbol}. لا يمكن استخدامها للوقف المتحرك.")

                # نوم قصير بين فحص كل توصية لتجنب الضغط على APIs/DB
                time.sleep(0.2) # 200 ميلي ثانية

            # logger.debug("[Tracker] اكتملت دورة التتبع.")

        except psycopg2.Error as db_err:
            logger.error(f"❌ [Tracker] خطأ في قاعدة البيانات أثناء حلقة تتبع التوصيات: {db_err}", exc_info=True)
            if conn and not conn.closed: conn.rollback() # التراجع عن أي تغييرات جزئية
            logger.info("[Tracker] انتظار أطول بعد خطأ قاعدة البيانات...")
            time.sleep(60) # انتظار أطول بعد خطأ DB
        except Exception as e:
            logger.error(f"❌ [Tracker] خطأ عام غير متوقع في خدمة التتبع: {e}", exc_info=True)
            # التراجع كإجراء احترازي
            try:
                 if conn and not conn.closed: conn.rollback()
            except Exception as rb_err:
                 logger.error(f"❌ [Tracker] خطأ أثناء محاولة التراجع بعد خطأ عام: {rb_err}")
            logger.info("[Tracker] انتظار أطول بعد خطأ غير متوقع...")
            time.sleep(60) # انتظار أطول بعد خطأ عام

        # فترة النوم بين دورات التتبع الكاملة
        time.sleep(30) # فحص جميع التوصيات كل 30 ثانية (يمكن تعديله)

# ---------------------- دالة التحقق من عدد التوصيات المفتوحة ----------------------
def can_generate_new_recommendation():
    """تتحقق مما إذا كان يمكن توليد توصية جديدة بناءً على حد MAX_OPEN_TRADES."""
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]
        if active_count < MAX_OPEN_TRADES:
            logger.info(f"✅ [Gate] عدد الصفقات المفتوحة ({active_count}) < الحد ({MAX_OPEN_TRADES}). يمكن توليد توصيات جديدة.")
            return True
        else:
            logger.info(f"⚠️ [Gate] تم الوصول للحد الأقصى ({MAX_OPEN_TRADES}) للصفقات المفتوحة. إيقاف توليد توصيات جديدة مؤقتًا.")
            return False
    except Exception as e:
        logger.error(f"❌ [Gate] خطأ أثناء التحقق من عدد التوصيات المفتوحة: {e}")
        # الافتراضي هو المنع (False) في حالة الخطأ للأمان
        return False

# ---------------------- تحليل السوق (البحث عن صفقات جديدة) ----------------------
def analyze_market():
    """يحلل السوق بحثًا عن فرص تداول جديدة بناءً على الاستراتيجية المحددة."""
    logger.info("==========================================")
    logger.info(f" H [Market Analysis] بدء دورة تحليل السوق (الفريم: {SIGNAL_GENERATION_TIMEFRAME}, بيانات: {SIGNAL_GENERATION_LOOKBACK_DAYS} يوم)...")

    # --- فحص البوابة: هل يمكن فتح صفقة جديدة؟ ---
    if not can_generate_new_recommendation():
        logger.info(" H [Market Analysis] تم تخطي الدورة: تم الوصول للحد الأقصى للصفقات المفتوحة.")
        return # الخروج إذا تم الوصول للحد

    # --- جلب سياق السوق ---
    btc_dominance, eth_dominance = get_market_dominance()
    if btc_dominance is None or eth_dominance is None:
        logger.warning("⚠️ [Market Analysis] فشل في جلب نسب سيطرة السوق. المتابعة بالقيم الافتراضية (0.0).")
        btc_dominance, eth_dominance = 0.0, 0.0

    # --- الحصول على الرموز للتحليل ---
    symbols_to_analyze = get_crypto_symbols()
    if not symbols_to_analyze:
        logger.warning("⚠️ [Market Analysis] قائمة الرموز فارغة. لا يمكن متابعة التحليل.")
        return

    logger.info(f" H [Market Analysis] سيتم تحليل {len(symbols_to_analyze)} زوج عملات...")
    generated_signals_count = 0
    processed_symbols_count = 0

    # --- المرور على الرموز ---
    for symbol in symbols_to_analyze:
        processed_symbols_count += 1
        # إعادة فحص البوابة قبل معالجة كل رمز (مهم إذا استغرق التحليل وقتًا)
        if not can_generate_new_recommendation():
             logger.info(f" H [Market Analysis] تم الوصول للحد الأقصى للصفقات أثناء التحليل. إيقاف البحث عن رموز جديدة.")
             break # الخروج من الحلقة إذا تم الوصول للحد

        # logger.info(f"--- [Market Analysis] تحليل الزوج: {symbol} ({processed_symbols_count}/{len(symbols_to_analyze)}) ---")

        # 1. التحقق من وجود توصية مفتوحة حالية لهذا الزوج
        try:
            check_db_connection()
            cur.execute("SELECT COUNT(*) FROM signals WHERE symbol = %s AND closed_at IS NULL", (symbol,))
            if cur.fetchone()[0] > 0:
                # logger.info(f"ℹ️ [Market Analysis] تخطي {symbol}: توجد توصية نشطة بالفعل.")
                continue # الانتقال للرمز التالي
        except Exception as e:
             logger.error(f"❌ [Market Analysis] خطأ DB أثناء التحقق من توصية حالية لـ {symbol}: {e}")
             continue # تخطي هذا الرمز عند الخطأ

        # 2. جلب البيانات التاريخية لفريم توليد الإشارة
        df_signal_gen = fetch_historical_data(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
        if df_signal_gen is None or df_signal_gen.empty:
            # logger.warning(f"⚠️ [Market Analysis] بيانات {SIGNAL_GENERATION_TIMEFRAME} غير كافية أو خطأ في جلبها لـ {symbol}. تخطي.")
            continue # تخطي إذا كانت البيانات غير صالحة

        # 3. توليد الإشارة باستخدام الاستراتيجية
        signal = generate_signal_using_freqtrade_strategy(df_signal_gen, symbol)

        if signal:
            logger.info(f"✅ [Market Analysis] تم العثور على إشارة شراء محتملة للزوج {symbol}!")

            # 4. التحقق من الحجم/السيولة
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < MIN_VOLUME_15M_USDT:
                logger.info(f"⚠️ [Market Analysis] تم رفض إشارة {symbol} بسبب سيولة منخفضة ({volume_15m:,.0f} USDT < {MIN_VOLUME_15M_USDT:,.0f} USDT).")
                continue # تخطي هذه الإشارة

            logger.info(f"✅ [Market Analysis] سيولة كافية للزوج {symbol} ({volume_15m:,.0f} USDT). متابعة الإشارة.")

            # 5. إرسال التنبيه وحفظ الإشارة في قاعدة البيانات
            try:
                # إرسال التنبيه أولاً (أكثر فورية)
                send_telegram_alert(signal, volume_15m, btc_dominance if btc_dominance is not None else 0.0, eth_dominance if eth_dominance is not None else 0.0, SIGNAL_GENERATION_TIMEFRAME)

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
                    signal['current_target'], # الحالي الأولي = الأولي
                    signal['current_stop_loss'],# الحالي الأولي = الأولي
                    signal.get('r2_score'), # استخدام r2_score من كائن الإشارة
                    volume_15m
                ))
                conn.commit()
                logger.info(f"✅ [Market Analysis] تم حفظ توصية {symbol} بنجاح في قاعدة البيانات.")
                generated_signals_count += 1

                # اختياري: تأخير قصير بعد توليد إشارة
                time.sleep(1)

            except psycopg2.Error as db_err:
                logger.error(f"❌ [Market Analysis] فشل في حفظ توصية {symbol} في قاعدة البيانات: {db_err}")
                if conn and not conn.closed: conn.rollback() # التراجع عن الإدخال الفاشل
            except Exception as e:
                logger.error(f"❌ [Market Analysis] خطأ غير متوقع أثناء معالجة توصية {symbol}: {e}", exc_info=True)
                if conn and not conn.closed: conn.rollback() # التراجع كإجراء احترازي

        # else:
             # logger.debug(f"ℹ️ [Market Analysis] لم يتم العثور على إشارة مؤهلة للزوج {symbol} في هذه الدورة.")
             # pass # لا توجد إشارة

        # تأخير بسيط بين تحليل كل زوج لتقليل الضغط على APIs
        time.sleep(0.5)

    logger.info(f" H [Market Analysis] انتهت دورة تحليل السوق. تمت معالجة {processed_symbols_count} رمزًا. تم توليد {generated_signals_count} توصية/توصيات جديدة.")
    logger.info("==========================================")

# ---------------------- اختبار Telegram ----------------------
def test_telegram():
    """يرسل رسالة اختبار بسيطة إلى Telegram للتحقق من الإعدادات."""
    logger.info("🧪 [Test] محاولة إرسال رسالة اختبار إلى تليجرام...")
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        test_message = f'🚀 **اختبار تشغيل البوت** 🚀\nتم بدء الخدمة بنجاح.\nالوقت: {timestamp}'
        send_telegram_message(chat_id, test_message) # استخدام الدالة المساعدة
        logger.info(f"✅ [Test] تم إرسال رسالة اختبار تليجرام بنجاح إلى معرف الدردشة {chat_id}.")
    except Exception as e:
        logger.error(f"❌ [Test] فشل في إرسال رسالة اختبار تليجرام: {e}")

# ---------------------- تشغيل Flask (في خيط منفصل) ----------------------
def run_flask():
    """يشغل خادم ويب Flask لمعالجة Webhooks الواردة."""
    port = int(os.environ.get("PORT", 10000)) # المنفذ الافتراضي لـ Render أو للتطوير المحلي
    logger.info(f"🌍 [Flask] بدء تشغيل خادم Flask على المضيف 0.0.0.0، المنفذ {port}...")
    try:
        # استخدام 'waitress' للإنتاج بدلاً من خادم تطوير Flask
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=4) # تعديل عدد الخيوط حسب الحاجة
        # app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False) # خادم التطوير
    except ImportError:
         logger.warning("⚠️ [Flask] لم يتم تثبيت 'waitress'. العودة إلى خادم تطوير Flask (غير مستحسن للإنتاج).")
         app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
         logger.critical(f"❌ [Flask] فشل خادم Flask في البدء: {e}", exc_info=True)


# ---------------------- التشغيل الرئيسي والتزامن ----------------------
if __name__ == '__main__':
    logger.info("==========================================")
    logger.info("🚀 بدء تشغيل بوت توصيات التداول الإصدار 4.3 (Hazem Mod)...")
    logger.info("==========================================")

    # مراجع عامة للخيوط لفحص الحالة
    flask_thread = None
    websocket_thread = None
    tracker_thread = None
    scheduler = None

    try:
        # 1. تهيئة قاعدة البيانات
        init_db() # سيطرح استثناءات إذا فشل الاتصال بعد إعادة المحاولة

        # 2. تعيين Webhook تليجرام (أفضل محاولة)
        # تشغيل هذا مبكرًا، يعتمد على متغيرات البيئة أو .env لتحديد URL
        set_telegram_webhook()

        # 3. بدء خيط خادم Flask
        flask_thread = Thread(target=run_flask, name="FlaskThread", daemon=True)
        flask_thread.start()
        logger.info("✅ [Main] تم بدء خيط خادم Flask.")
        time.sleep(2) # إعطاء Flask لحظة للبدء المحتمل في الاستماع

        # 4. بدء خيط مدير WebSocket
        websocket_thread = Thread(target=run_ticker_socket_manager, name="WebSocketThread", daemon=True)
        websocket_thread.start()
        logger.info("✅ [Main] تم بدء خيط مدير WebSocket.")
        logger.info("ℹ️ [Main] السماح بـ 15 ثانية لـ WebSocket للاتصال واستقبال البيانات الأولية...")
        time.sleep(15) # السماح بوقت لاتصال WS والبيانات الأولية

        # 5. بدء خيط تتبع التوصيات
        tracker_thread = Thread(target=track_signals, name="TrackerThread", daemon=True)
        tracker_thread.start()
        logger.info("✅ [Main] تم بدء خيط تتبع التوصيات.")

        # 6. إرسال رسالة اختبار بدء التشغيل
        test_telegram()

        # 7. إعداد وبدء المهام المجدولة (APScheduler)
        scheduler = BackgroundScheduler(timezone="UTC") # استخدام UTC للمهام المجدولة
        # جدولة تحليل السوق كل 5 دقائق
        scheduler.add_job(analyze_market, 'interval', minutes=5, id='market_analyzer', replace_existing=True, misfire_grace_time=60) # السماح بتأخير 60 ثانية إذا فات الموعد
        logger.info("✅ [Main] تم جدولة مهمة تحليل السوق (كل 5 دقائق).")

        # أضف مهام مجدولة أخرى هنا (مثل التقرير اليومي)
        # scheduler.add_job(send_report, 'cron', hour=8, minute=0, args=[chat_id], id='daily_report', replace_existing=True)
        # logger.info("✅ [Main] تم جدولة مهمة التقرير اليومي (08:00 UTC).")

        scheduler.start()
        logger.info("✅ [Main] تم بدء تشغيل APScheduler.")
        logger.info("==========================================")
        logger.info("✅ النظام متصل ويعمل الآن")
        logger.info("==========================================")

        # إبقاء الخيط الرئيسي حيًا ومراقبة الخيوط الأخرى
        while True:
            if flask_thread and not flask_thread.is_alive():
                 logger.critical("❌ [Main] توقف خيط Flask! إعادة التشغيل غير مطبقة. الخروج.")
                 # في بيئة إنتاج حقيقية، يجب أن يتولى مدير عمليات (مثل systemd, supervisor) إعادة التشغيل.
                 break
            if websocket_thread and not websocket_thread.is_alive():
                 logger.critical("❌ [Main] توقف خيط WebSocket! الخروج للسماح بإعادة التشغيل الخارجية.")
                 # محاولة إعادة التشغيل داخل السكربت معقدة؛ الاعتماد على مدير خارجي.
                 break
            if tracker_thread and not tracker_thread.is_alive():
                 logger.critical("❌ [Main] توقف خيط تتبع التوصيات! الخروج للسماح بإعادة التشغيل الخارجية.")
                 break
            if scheduler and not scheduler.running:
                logger.critical("❌ [Main] المجدول APScheduler لا يعمل! الخروج.")
                break

            time.sleep(30) # فحص صحة الخيوط بشكل دوري

    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 [Main] تم استلام إشارة إيقاف (KeyboardInterrupt/SystemExit)...")
    except Exception as e:
        logger.critical(f"❌ [Main] استثناء حرج غير معالج في كتلة التنفيذ الرئيسية: {e}", exc_info=True)
    finally:
        logger.info("------------------------------------------")
        logger.info("ℹ️ [Main] بدء الإيقاف التدريجي...")
        if scheduler and scheduler.running:
            logger.info("   - جاري إيقاف APScheduler...")
            scheduler.shutdown(wait=False) # عدم انتظار انتهاء المهام
            logger.info("   - تم إيقاف APScheduler.")
        # الخيوط معرفة كـ daemon، ستنتهي عند انتهاء الخيط الرئيسي.
        # أضف أي تنظيف آخر مطلوب هنا (مثل إغلاق اتصال DB صراحة؟)
        if conn and not conn.closed:
            logger.info("   - جاري إغلاق اتصال قاعدة البيانات...")
            conn.close()
            logger.info("   - تم إغلاق اتصال قاعدة البيانات.")
        logger.info("✅ [Main] اكتمل الإيقاف. الخروج.")
        logger.info("==========================================")
