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

# قيمة الصفقة الثابتة للتوصيات
TRADE_VALUE = 10

# متغيّر عالمي للتحكم بتوليد توصيات جديدة
allow_new_recommendations = True

# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
conn = None
cur = None

def init_db():
    global conn, cur
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()
        # إنشاء الجدول إذا لم يكن موجودًا، مع إضافة الأعمدة الخاصة بالربح والخسارة
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
                profit_percentage DOUBLE PRECISION,
                profitable_stop_loss BOOLEAN DEFAULT FALSE
            )
        """)
        conn.commit()
        logger.info("✅ [DB] تم تهيئة قاعدة البيانات بنجاح مع تحديث البنية.")
    except Exception as e:
        logger.error(f"❌ [DB] فشل تهيئة قاعدة البيانات: {e}")
        raise

def check_db_connection():
    global conn, cur
    try:
        cur.execute("SELECT 1")
        conn.commit()
    except Exception as e:
        logger.warning("⚠️ [DB] إعادة الاتصال بقاعدة البيانات...")
        try:
            if conn:
                conn.close()
            init_db()
        except Exception as ex:
            logger.error(f"❌ [DB] فشل إعادة الاتصال: {ex}")
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
        logger.error(f"❌ [WS] خطأ في handle_ticker_message: {e}")

def run_ticker_socket_manager():
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("✅ [WS] تم تشغيل WebSocket لتحديث التيكر لجميع الأزواج.")
    except Exception as e:
        logger.error(f"❌ [WS] خطأ في تشغيل WebSocket: {e}")

# ---------------------- دوال حساب المؤشرات الفنية ----------------------
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
    logger.info(f"✅ [Indicator] تم حساب RSI: {rsi.iloc[-1]:.2f}")
    return rsi

def calculate_atr_indicator(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    logger.info(f"✅ [Indicator] تم حساب ATR: {df['atr'].iloc[-1]:.8f}")
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
    df['rsv'] = (df['close'] - low_min) / (high_max - low_min) * 100
    df['kdj_k'] = df['rsv'].ewm(com=(k_period - 1), adjust=False).mean()
    df['kdj_d'] = df['kdj_k'].ewm(com=(d_period - 1), adjust=False).mean()
    df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']
    return df

def calculate_adx(df, period=14):
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    df['+dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['-dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
    df['tr'] = pd.concat([
        (df['high'] - df['low']),
        (df['high'] - df['close'].shift(1)).abs(),
        (df['low'] - df['close'].shift(1)).abs()
    ], axis=1).max(axis=1)
    df['tr_smooth'] = df['tr'].rolling(window=period).sum()
    df['+dm_smooth'] = df['+dm'].rolling(window=period).sum()
    df['-dm_smooth'] = df['-dm'].rolling(window=period).sum()
    df['+di'] = 100 * (df['+dm_smooth'] / df['tr_smooth'])
    df['-di'] = 100 * (df['-dm_smooth'] / df['tr_smooth'])
    df['dx'] = 100 * (abs(df['+di'] - df['-di']) / (df['+di'] + df['-di'] + 1e-10))
    df['adx'] = df['dx'].rolling(window=period).mean()
    logger.info(f"✅ [Indicator] تم حساب ADX: {df['adx'].iloc[-1]:.2f}")
    return df

# دوال الكشف عن الأنماط الشمعية
def is_hammer(row):
    open_price = row['open']
    high = row['high']
    low = row['low']
    close = row['close']
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range == 0:
        return 0
    lower_shadow = min(open_price, close) - low
    upper_shadow = high - max(open_price, close)
    if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.1 * body:
        return 100
    return 0

def compute_engulfing(df, idx):
    if idx == 0:
        return 0
    prev = df.iloc[idx - 1]
    curr = df.iloc[idx]
    if prev['close'] < prev['open'] and curr['close'] > curr['open']:
        if curr['open'] < prev['close'] and curr['close'] > prev['open']:
            return 100
    if prev['close'] > prev['open'] and curr['close'] < curr['open']:
        if curr['open'] > prev['close'] and curr['close'] < prev['open']:
            return -100
    return 0

def detect_candlestick_patterns(df):
    df['Hammer'] = df.apply(is_hammer, axis=1)
    df['Engulfing'] = [compute_engulfing(df, i) for i in range(len(df))]
    df['Bullish'] = df.apply(lambda row: 100 if (row['Hammer'] == 100 or row['Engulfing'] == 100) else 0, axis=1)
    df['Bearish'] = df.apply(lambda row: 100 if row['Engulfing'] == -100 else 0, axis=1)
    logger.info("✅ [Candles] تم تحليل الأنماط الشمعية.")
    return df

# ---------------------- دوال التنبؤ وتحليل المشاعر ----------------------
def ml_predict_signal(symbol, df):
    """
    دالة تنبؤية تجريبية تعتمد على مؤشر RSI وبعض المؤشرات الأخرى.
    ترجع قيمة ثقة من 0 إلى 1.
    """
    try:
        rsi = df['rsi'].iloc[-1]
        adx = df['adx'].iloc[-1]
        if rsi < 45 and adx > 25:
            return 0.85
        return 0.6
    except Exception as e:
        logger.error(f"❌ [ML] خطأ في ml_predict_signal لـ {symbol}: {e}")
        return 0.6

def get_market_sentiment(symbol):
    """
    دالة تحليل مشاعر تجريبية.
    هنا نُعيد قيمة ثابتة إيجابية كتجربة.
    """
    return 0.7

def get_fear_greed_index():
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("data"):
                fng_value = float(data["data"][0].get("value"))
                if fng_value <= 25:
                    label = "خوف شديد"
                elif fng_value <= 50:
                    label = "خوف"
                elif fng_value <= 75:
                    label = "جشع"
                else:
                    label = "جشع شديد"
                logger.info(f"✅ [FNG] مؤشر الخوف والجشع: {fng_value} - {label}")
                return fng_value, label
        logger.warning("⚠️ [FNG] لم يتم الحصول على مؤشر الخوف والجشع، تعيين القيمة 50.")
        return 50.0, "غير محدد"
    except Exception as e:
        logger.error(f"❌ [FNG] خطأ في جلب مؤشر الخوف والجشع: {e}")
        return 50.0, "غير محدد"

# ---------------------- استراتيجية Freqtrade المحسّنة ----------------------
class FreqtradeStrategy:
    stoploss = -0.02
    minimal_roi = {"0": 0.01}
    
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 50:
            return df
        
        # حساب المتوسطات المتحركة المطلوبة
        df['ema5'] = calculate_ema(df['close'], 5)
        df['ema8'] = calculate_ema(df['close'], 8)
        df['ema21'] = calculate_ema(df['close'], 21)
        df['ema34'] = calculate_ema(df['close'], 34)
        df['ema50'] = calculate_ema(df['close'], 50)
        
        # حساب RSI مع تعديل الحد الأدنى لإشارة الشراء (<40)
        df['rsi'] = calculate_rsi_indicator(df)
        
        # حساب بولينجر باند مع معامل انحراف 2.5
        df['sma20'] = df['close'].rolling(window=20).mean()
        df['std20'] = df['close'].rolling(window=20).std()
        df['upper_band'] = df['sma20'] + (2.5 * df['std20'])
        df['lower_band'] = df['sma20'] - (2.5 * df['std20'])
        
        # حساب ATR
        df = calculate_atr_indicator(df)
        
        # حساب MACD باستخدام الفترات الجديدة (10, 21, 8)
        df = calculate_macd(df, fast_period=10, slow_period=21, signal_period=8)
        
        # حساب KDJ
        df = calculate_kdj(df)
        
        # حساب ADX
        df = calculate_adx(df)
        
        # تحليل الشموع
        df = detect_candlestick_patterns(df)
        
        logger.info("✅ [Strategy] تم حساب كافة المؤشرات في الاستراتيجية المحسنة.")
        return df

    def composite_buy_score(self, row):
        score = 0
        # شرط الاتجاه الصعودي الكامل: EMA5 > EMA8 > EMA21 > EMA34 > EMA50
        if row['ema5'] > row['ema8'] and row['ema8'] > row['ema21'] and row['ema21'] > row['ema34'] and row['ema34'] > row['ema50']:
            score += 1
        # شرط RSI منخفض (<40)
        if row['rsi'] < 40:
            score += 1
        # شرط السعر قريب من الحد الأدنى لبولينجر باند (ضمن 3% فوقه)
        if (row['close'] - row['lower_band']) / row['close'] < 0.03:
            score += 1
        # شرط MACD إيجابي (MACD أعلى من خط الإشارة)
        if row['macd'] > row['macd_signal']:
            score += 1
        # شرط KDJ صعودي (kdj_j > kdj_k > kdj_d)
        if row['kdj_j'] > row['kdj_k'] and row['kdj_k'] > row['kdj_d']:
            score += 1
        # شرط ADX قوي (>25)
        if row['adx'] > 25:
            score += 1
        # شرط ظهور نمط شمعي Bullish
        if row['Bullish'] == 100:
            score += 1
        return score

    def composite_sell_score(self, row):
        score = 0
        # شرط الاتجاه الهابط: EMA5 < EMA8 < EMA21 < EMA34 < EMA50
        if row['ema5'] < row['ema8'] and row['ema8'] < row['ema21'] and row['ema21'] < row['ema34'] and row['ema34'] < row['ema50']:
            score += 1
        # شرط RSI مرتفع (>65)
        if row['rsi'] > 65:
            score += 1
        # شرط السعر قريب من الحد الأعلى لبولينجر باند (ضمن 3% دونه)
        if (row['upper_band'] - row['close']) / row['close'] < 0.03:
            score += 1
        # شرط MACD سلبي (MACD أقل من خط الإشارة)
        if row['macd'] < row['macd_signal']:
            score += 1
        # شرط KDJ هبوطي (kdj_j < kdj_k < kdj_d)
        if row['kdj_j'] < row['kdj_k'] and row['kdj_k'] < row['kdj_d']:
            score += 1
        # شرط ADX قوي للتأكيد على الاتجاه
        if row['adx'] > 25:
            score += 1
        # شرط ظهور نمط شمعي Bearish
        if row['Bearish'] == 100:
            score += 1
        return score

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        df['buy_score'] = df.apply(self.composite_buy_score, axis=1)
        # تحديد عتبة الشراء؛ تصدر الإشارة عند جمع نقاط تساوي أو تزيد عن 4
        conditions = (df['buy_score'] >= 4)
        df.loc[conditions, 'buy'] = 1
        logger.info("✅ [Strategy] تم تحديد شروط الشراء بناءً على المجموع التراكمي (الاستراتيجية المحسنة).")
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        df['sell_score'] = df.apply(self.composite_sell_score, axis=1)
        # تحديد عتبة البيع؛ تصدر الإشارة عند جمع نقاط تساوي أو تزيد عن 4
        conditions = (df['sell_score'] >= 4)
        df.loc[conditions, 'sell'] = 1
        logger.info("✅ [Strategy] تم تحديد شروط البيع بناءً على المجموع التراكمي (الاستراتيجية المحسنة).")
        return df

# ---------------------- دالة التنبؤ بالسعر المحسّنة ----------------------
def improved_predict_future_price(symbol, interval='2h', days=30):
    try:
        # جلب البيانات التاريخية
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50:
            logger.error(f"❌ [Price Prediction] بيانات غير كافية للزوج {symbol}.")
            return None

        # حساب المؤشرات الفنية باستخدام الاستراتيجية
        strategy = FreqtradeStrategy()
        df = strategy.populate_indicators(df)
        df = df.dropna().reset_index(drop=True)
        if len(df) < 50:
            logger.error(f"❌ [Price Prediction] بيانات غير كافية بعد حساب المؤشرات للزوج {symbol}.")
            return None

        # تعريف مجموعة الخصائص (features) المستخدمة للتدريب
        features = [
            'close', 'ema8', 'ema21', 'ema50', 'rsi', 
            'upper_band', 'lower_band', 'atr', 
            'macd', 'macd_signal', 'macd_hist', 
            'kdj_j', 'kdj_k', 'kdj_d', 
            'adx', 'Bullish', 'Bearish'
        ]
        for col in features:
            if col not in df.columns:
                logger.warning(f"⚠️ [Price Prediction] العمود {col} غير موجود للزوج {symbol}.")

        # إعداد بيانات التدريب: الخصائص من الصف الحالي والهدف هو سعر الإغلاق للصف التالي
        X = df[features].iloc[:-1].values
        y = df['close'].iloc[1:].values

        from sklearn.ensemble import GradientBoostingRegressor
        model = GradientBoostingRegressor(n_estimators=200, max_depth=4, random_state=42)
        model.fit(X, y)

        # التنبؤ باستخدام آخر صف من البيانات
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]
        logger.info(f"✅ [Price Prediction] السعر المتوقع للزوج {symbol}: {predicted_price:.8f}")
        return predicted_price
    except Exception as e:
        logger.error(f"❌ [Price Prediction] خطأ أثناء تنبؤ السعر للزوج {symbol}: {e}")
        return None

# ---------------------- دالة توليد الإشارة باستخدام استراتيجية Freqtrade المحسنة ----------------------
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
        # استخدام معامل ATR (يمكن تعديله إلى 1.2 أو 1.8 حسب التحليل)
        atr_multiplier = 1.5  
        target = current_price + atr_multiplier * current_atr
        stop_loss = current_price - atr_multiplier * current_atr

        profit_margin = (target / current_price - 1) * 100
        if profit_margin < 1:
            logger.info(f"⚠️ [Signal] إشارة {symbol} لا تضمن ربح أكثر من 1% (الربح المتوقع: {profit_margin:.2f}%).")
            return None

        # استخدام نموذج تنبؤ متقدم للتحقق من الإشارة
        predicted_price = improved_predict_future_price(symbol, interval='2h', days=30)
        # شرط التأكيد: يجب أن يكون السعر المتوقع أعلى بنسبة 1% على الأقل من السعر الحالي
        if predicted_price is None or predicted_price <= current_price * 1.01:
            logger.info(f"⚠️ [Signal] السعر المتوقع للزوج {symbol} ({predicted_price}) لا يشير إلى ارتفاع كافٍ عن السعر الحالي ({current_price}).")
            return None

        signal = {
            'symbol': symbol,
            'price': float(format(current_price, '.8f')),
            'target': float(format(target, '.8f')),
            'stop_loss': float(format(stop_loss, '.8f')),
            'strategy': 'freqtrade_day_trade_improved',
            'indicators': {
                'ema5': last_row['ema5'],
                'ema8': last_row['ema8'],
                'ema21': last_row['ema21'],
                'ema34': last_row['ema34'],
                'ema50': last_row['ema50'],
                'rsi': last_row['rsi'],
                'upper_band': last_row['upper_band'],
                'lower_band': last_row['lower_band'],
                'atr': current_atr,
                'buy_score': last_row.get('buy_score', 0),
                'adx': last_row['adx']
            },
            'trade_value': TRADE_VALUE,
            'predicted_price': float(format(predicted_price, '.8f'))
        }
        
        logger.info(f"✅ [Signal] تم توليد إشارة من الاستراتيجية المحسنة للزوج {symbol}:\n{signal}")
        return signal
    else:
        logger.info(f"[Signal] الشروط غير مستوفاة للزوج {symbol} في الاستراتيجية المحسنة.")
        return None

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 نظام توصيات التداول يعمل بكفاءة.", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logger.info("🔔 [Webhook] Received update: " + str(update))
    if "callback_query" in update:
        callback_data = update["callback_query"]["data"]
        chat_id_callback = update["callback_query"]["message"]["chat"]["id"]
        if callback_data == "get_report":
            send_report(chat_id_callback)
            answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": update["callback_query"]["id"]})
    return '', 200

def set_telegram_webhook():
    webhook_url = "https://four-3-9w83.onrender.com/webhook"  # تأكد من تحديث الرابط حسب النشر
    url = f"https://api.telegram.org/bot{telegram_token}/setWebhook?url={webhook_url}"
    try:
        response = requests.get(url, timeout=10)
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"✅ [Webhook] تم تسجيل webhook بنجاح: {res_json}")
        else:
            logger.error(f"❌ [Webhook] فشل تسجيل webhook: {res_json}")
    except Exception as e:
        logger.error(f"❌ [Webhook] استثناء أثناء تسجيل webhook: {e}")

# ---------------------- وظائف تحليل البيانات ----------------------
def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip()]
            logger.info(f"✅ [Data] تم الحصول على {len(symbols)} زوج من العملات.")
            return symbols
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في قراءة الملف: {e}")
        return []

def fetch_historical_data(symbol, interval='2h', days=10):
    try:
        logger.info(f"⏳ [Data] بدء جلب البيانات التاريخية للزوج: {symbol} - الفريم {interval} لمدة {days} يوم/أيام.")
        klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        logger.info(f"✅ [Data] تم جلب {len(df)} صف من البيانات للزوج: {symbol}.")
        return df[['timestamp', 'open', 'high', 'low', 'close']]
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب البيانات لـ {symbol}: {e}")
        return None

def fetch_recent_volume(symbol):
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"✅ [Data] حجم السيولة للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT.")
        return volume
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب حجم {symbol}: {e}")
        return 0

def get_market_dominance():
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json().get("data", {})
            market_cap_percentage = data.get("market_cap_percentage", {})
            btc_dominance = market_cap_percentage.get("btc")
            eth_dominance = market_cap_percentage.get("eth")
            logger.info(f"✅ [Data] BTC Dominance: {btc_dominance}%, ETH Dominance: {eth_dominance}%")
            return btc_dominance, eth_dominance
        else:
            logger.error(f"❌ [Data] خطأ في جلب نسب السيطرة: {response.status_code} {response.text}")
            return None, None
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في get_market_dominance: {e}")
        return None, None

# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    try:
        profit_pct = signal['target'] / signal['price'] - 1
        loss_pct = signal['stop_loss'] / signal['price'] - 1
        profit_pct_display = round(profit_pct * 100, 2)
        loss_pct_display = round(loss_pct * 100, 2)
        profit_usdt = round(TRADE_VALUE * profit_pct, 2)
        loss_usdt = round(TRADE_VALUE * loss_pct, 2)
        
        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
        fng_value, fng_label = get_fear_greed_index()
        
        message = (
            f"🚀 **إشارة تداول جديدة**\n"
            f"——————————————\n"
            f"**زوج:** {signal['symbol']}\n"
            f"**سعر الدخول:** `${signal['price']:.8f}`\n"
            f"**السعر الحالي:** `${signal['price']:.8f}`\n"
            f"**🎯 الهدف:** `${signal['target']:.8f}` (+{profit_pct_display}% / +{profit_usdt} USDT)\n"
            f"**🛑 وقف الخسارة:** `${signal['stop_loss']:.8f}` ({loss_pct_display}% / {loss_usdt} USDT)\n"
            f"**⏱ الفريم:** {timeframe}\n"
            f"**💧 السيولة:** {volume:,.2f} USDT\n"
            f"**💵 قيمة الصفقة:** ${TRADE_VALUE}\n"
            f"——————————————\n"
            f"📈 **نسب السيطرة (15m):**\n"
            f"   • BTC: {btc_dominance:.2f}%\n"
            f"   • ETH: {eth_dominance:.2f}%\n"
            f"📊 **مؤشر الخوف والجشع:** {fng_value:.2f} - {fng_label}\n"
            f"——————————————\n"
            f"⏰ **{timestamp}**"
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
        logger.info(f"✅ [Telegram] رد: {response.status_code} {response.text}")
        if response.status_code != 200:
            logger.error(f"❌ [Telegram] فشل إرسال إشعار للزوج {signal['symbol']}: {response.status_code} {response.text}")
        else:
            logger.info(f"✅ [Telegram] تم إرسال إشعار للزوج {signal['symbol']} بنجاح.")
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال إشعار للزوج {signal['symbol']}: {e}")

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
        logger.info(f"✅ [Telegram] رد: {response.status_code} {response.text}")
        if response.status_code != 200:
            logger.error(f"❌ [Telegram] فشل إرسال التنبيه: {response.status_code} {response.text}")
        else:
            logger.info("✅ [Telegram] تم إرسال التنبيه بنجاح.")
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال التنبيه: {e}")

# ---------------------- إرسال تقرير الأداء الشامل ----------------------
def send_report(target_chat_id):
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_count = cur.fetchone()[0]

        cur.execute("""
            SELECT achieved_target, profitable_stop_loss, profit_percentage 
            FROM signals WHERE closed_at IS NOT NULL
        """)
        closed_signals = cur.fetchall()
        total_trades = len(closed_signals)
        success_count = sum(1 for s in closed_signals if s[0])
        profitable_stop_loss_count = sum(1 for s in closed_signals if not s[0] and s[1])
        stop_loss_count = total_trades - success_count - profitable_stop_loss_count

        profit_usd_list = [TRADE_VALUE * (s[2] / 100) for s in closed_signals if s[2] and s[2] > 0]
        loss_usd_list = [TRADE_VALUE * (s[2] / 100) for s in closed_signals if s[2] and s[2] < 0]
        avg_profit_usd = np.mean(profit_usd_list) if profit_usd_list else 0
        avg_loss_usd = np.mean(loss_usd_list) if loss_usd_list else 0
        net_profit_usd = sum(TRADE_VALUE * (s[2] / 100) for s in closed_signals if s[2])

        bot_rating = (net_profit_usd / (TRADE_VALUE * total_trades) * 100) if total_trades > 0 else 0

        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')

        report_message = (
            "📊 **تقرير الأداء الشامل**\n"
            "——————————————\n"
            f"✅ **التوصيات الناجحة:** {success_count}\n"
            f"🔹 **وقف الخسارة الرابح:** {profitable_stop_loss_count}\n"
            f"❌ **التوصيات ذات وقف الخسارة:** {stop_loss_count}\n"
            f"⏳ **التوصيات النشطة:** {active_count}\n"
            f"📝 **الصفقات المغلقة:** {total_trades}\n"
            f"💹 **متوسط الربح:** {avg_profit_usd:.2f} USDT\n"
            f"📉 **متوسط الخسارة:** {avg_loss_usd:.2f} USDT\n"
            f"💵 **صافي الربح/الخسارة:** {net_profit_usd:.2f} USDT\n"
            f"⭐ **تقييم البوت:** {bot_rating:.2f}%\n"
            "——————————————\n"
            f"⏰ **{timestamp}**"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': target_chat_id,
            'text': report_message,
            'parse_mode': 'Markdown'
        }
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"✅ [Report] تم إرسال تقرير الأداء: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"❌ [Report] فشل إرسال تقرير الأداء: {e}")

# ---------------------- خدمة تتبع الإشارات (فحص التوصيات المفتوحة) ----------------------
def track_signals():
    logger.info("⏳ [Track] بدء خدمة تتبع الإشارات (فريم 15m مع بيانات يومين)...")
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
            logger.info("==========================================")
            logger.info(f"✅ [Track] عدد التوصيات المفتوحة: {len(active_signals)}")
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss = signal
                try:
                    if symbol in ticker_data:
                        current_price = float(ticker_data[symbol].get('c', 0))
                    else:
                        logger.warning(f"⚠️ [Track] لا يوجد تحديث أسعار للزوج {symbol} من WebSocket.")
                        continue
                    logger.info(f"⏳ [Track] {symbol}: السعر الحالي {current_price}, الدخول {entry}")
                    if abs(entry) < 1e-8:
                        logger.error(f"❌ [Track] سعر الدخول للزوج {symbol} قريب من الصفر، تخطي الحساب.")
                        continue

                    # جلب بيانات الشموع لفريم 15 دقيقة لمدة يومين
                    df = fetch_historical_data(symbol, interval='15m', days=2)
                    if df is None or len(df) < 50:
                        logger.warning(f"⚠️ [Track] بيانات الشموع غير كافية للزوج {symbol}.")
                        continue

                    # حساب المؤشرات الفنية وتحليل الأنماط الشمعية
                    strategy = FreqtradeStrategy()
                    df = strategy.populate_indicators(df)
                    df = detect_candlestick_patterns(df)
                    df = calculate_macd(df, fast_period=10, slow_period=21, signal_period=8)
                    df = calculate_kdj(df)
                    last_row = df.iloc[-1]

                    ml_confidence = ml_predict_signal(symbol, df)
                    sentiment = get_market_sentiment(symbol)

                    macd_bullish = df['macd'].iloc[-1] > df['macd_signal'].iloc[-1]
                    macd_bearish = df['macd'].iloc[-1] < df['macd_signal'].iloc[-1]
                    kdj_bullish = (df['kdj_j'].iloc[-1] > 50) and (df['kdj_k'].iloc[-1] > df['kdj_d'].iloc[-1])
                    kdj_bearish = (df['kdj_j'].iloc[-1] < 50) and (df['kdj_k'].iloc[-1] < df['kdj_d'].iloc[-1])
                    
                    bullish_signal = (last_row['Bullish'] != 0) or (macd_bullish and kdj_bullish)
                    bearish_signal = (last_row['Bearish'] != 0) or (macd_bearish and kdj_bearish)
                    
                    current_gain_pct = (current_price - entry) / entry

                    # التحقق من إغلاق الصفقة عند تحقيق الهدف أو الوصول للوقف
                    if current_price >= target:
                        profit_pct = target / entry - 1
                        profit_usdt = TRADE_VALUE * profit_pct
                        profit_pct_display = round(profit_pct * 100, 2)
                        msg = f"✅ [Track] توصية {symbol} حققت الهدف عند {current_price:.8f} بربح {profit_pct_display}% ({round(profit_usdt,2)} USDT)"
                        send_telegram_alert_special(msg)
                        cur.execute("""
                            UPDATE signals 
                            SET achieved_target = TRUE, closed_at = NOW(), profit_percentage = %s 
                            WHERE id = %s
                        """, (profit_pct_display, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Track] تم إغلاق توصية {symbol} عند تحقيق الهدف.")
                        continue

                    elif current_price <= stop_loss:
                        loss_pct = stop_loss / entry - 1
                        loss_usdt = TRADE_VALUE * loss_pct
                        loss_pct_display = round(loss_pct * 100, 2)
                        profitable_stop_loss = current_price > entry
                        stop_type = "وقف خسارة رابح" if profitable_stop_loss else "وقف خسارة"
                        msg = f"⚠️ [Track] توصية {symbol} أغلقت عند {current_price:.8f} ({stop_type}) بخسارة {loss_pct_display}% ({round(loss_usdt,2)} USDT)"
                        send_telegram_alert_special(msg)
                        cur.execute("""
                            UPDATE signals 
                            SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s, profitable_stop_loss = %s 
                            WHERE id = %s
                        """, (loss_pct_display, profitable_stop_loss, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Track] تم إغلاق توصية {symbol} عند وقف الخسارة.")
                        continue

                    # إذا تم الكشف عن إشارة بيع (Bearish) بناءً على الأنماط الشمعية:
                    if bearish_signal:
                        loss_pct = current_price / entry - 1
                        loss_usdt = TRADE_VALUE * loss_pct
                        loss_pct_display = round(loss_pct * 100, 2)
                        msg = f"🚫 [Track] تم إغلاق توصية {symbol} بسبب إشارة بيع (Bearish) عند {current_price:.8f} بخسارة {loss_pct_display}%."
                        send_telegram_alert_special(msg)
                        cur.execute("""
                            UPDATE signals 
                            SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s, profitable_stop_loss = FALSE 
                            WHERE id = %s
                        """, (loss_pct_display, signal_id))
                        conn.commit()
                        logger.info(f"✅ [Track] تم إغلاق توصية {symbol} بناءً على إشارة بيع (Bearish).")
                        continue

                    # تحديث الهدف ووقف الخسارة إذا استمرت إشارة الشراء (Bullish) مع تحقق زيادة بنسبة 1%
                    if bullish_signal and current_gain_pct >= 0.01:
                        update_flag = False
                        # تحديث الهدف ليصبح 1% من سعر الدخول إذا لم يتم تحديده بعد
                        if target < entry * 1.01:
                            target = entry * 1.01
                            update_flag = True
                        # تحديث وقف الخسارة ليصبح 1% من سعر الدخول عند بلوغ الزيادة 2%
                        if current_gain_pct >= 0.02 and stop_loss < entry * 1.01:
                            stop_loss = entry * 1.01
                            update_flag = True
                        if update_flag:
                            msg = (
                                f"🔄 [Track] تحديث توصية {symbol}:\n"
                                f"▫️ سعر الدخول: ${entry:.8f}\n"
                                f"▫️ السعر الحالي: ${current_price:.8f}\n"
                                f"▫️ نسبة الزيادة: {current_gain_pct*100:.2f}%\n"
                                f"▫️ الهدف الجديد: ${target:.8f}\n"
                                f"▫️ وقف الخسارة الجديد: ${stop_loss:.8f}\n"
                                f"▫️ (ML: {ml_confidence:.2f}, Sentiment: {sentiment:.2f})"
                            )
                            send_telegram_alert_special(msg)
                            cur.execute(
                                "UPDATE signals SET target = %s, stop_loss = %s WHERE id = %s",
                                (target, stop_loss, signal_id)
                            )
                            conn.commit()
                            logger.info(f"✅ [Track] تم تحديث توصية {symbol} بنجاح.")
                    else:
                        logger.info(f"ℹ️ [Track] {symbol} لم تصل نسبة الزيادة لـ 1% أو لم يتم تأكيد إشارة شراء.")
                except Exception as e:
                    logger.error(f"❌ [Track] خطأ أثناء تتبع {symbol}: {e}")
                    conn.rollback()
        except Exception as e:
            logger.error(f"❌ [Track] خطأ في خدمة تتبع الإشارات: {e}")
        time.sleep(60)
        
# ---------------------- دالة التحقق من عدد التوصيات المفتوحة ----------------------
def check_open_recommendations():
    global allow_new_recommendations
    while True:
        try:
            check_db_connection()
            cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
            active_count = cur.fetchone()[0]
            if active_count >= 4:
                logger.info(f"⚠️ [Open Check] يوجد {active_count} توصية مفتوحة. لن يُسمح بتوليد توصيات جديدة.")
                allow_new_recommendations = False
            else:
                logger.info(f"✅ [Open Check] عدد التوصيات المفتوحة: {active_count}. يمكن توليد توصيات جديدة.")
                allow_new_recommendations = True
        except Exception as e:
            logger.error(f"❌ [Open Check] خطأ أثناء التحقق من التوصيات المفتوحة: {e}")
        time.sleep(60)

# ---------------------- تحليل السوق (البحث عن صفقات جديدة بفريم 1h) ----------------------
def analyze_market():
    global allow_new_recommendations
    logger.info("==========================================")
    logger.info("⏳ [Market] بدء تحليل السوق (فريم 1h مع بيانات 4 أيام)...")
    try:
        check_db_connection()
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        if cur.fetchone()[0] >= 4:
            logger.info("⚠️ [Market] يوجد 4 توصية مفتوحة. لن يتم توليد توصيات جديدة حتى يتم إغلاق واحدة منها.")
            return

        btc_dominance, eth_dominance = get_market_dominance()
        if btc_dominance is None or eth_dominance is None:
            logger.warning("⚠️ [Market] لم يتم جلب نسب السيطرة؛ تعيينها كـ 0.0")
            btc_dominance, eth_dominance = 0.0, 0.0

        symbols = get_crypto_symbols()
        if not symbols:
            logger.warning("⚠️ [Market] لا توجد أزواج في الملف!")
            return

        for symbol in symbols:
            cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
            if cur.fetchone()[0] >= 4:
                logger.info("⚠️ [Market] تجاوز عدد التوصيات المفتوحة أثناء المعالجة. إيقاف توليد توصيات جديدة.")
                break

            logger.info("==========================================")
            logger.info(f"⏳ [Market] بدء فحص الزوج: {symbol} (فريم 1h)")
            signal = None
            # استخدام فريم 1 ساعة للبحث عن صفقات جديدة مع جلب بيانات 4 أيام
            df_1h = fetch_historical_data(symbol, interval='1h', days=4)
            if df_1h is not None and len(df_1h) >= 50:
                signal_1h = generate_signal_using_freqtrade_strategy(df_1h, symbol)
                if signal_1h:
                    signal = signal_1h
                    logger.info(f"✅ [Market] تم الحصول على إشارة شراء على فريم 1h للزوج {symbol}.")
                else:
                    logger.info(f"⚠️ [Market] لم يتم الحصول على إشارة شراء على فريم 1h للزوج {symbol}.")
            else:
                logger.warning(f"⚠️ [Market] تجاهل {symbol} - بيانات 1h غير كافية.")
            if signal is None:
                continue

            cur.execute("SELECT COUNT(*) FROM signals WHERE symbol = %s AND closed_at IS NULL", (signal['symbol'],))
            if cur.fetchone()[0] > 0:
                logger.info(f"⚠️ [Market] توجد توصية مفتوحة للزوج {signal['symbol']}، تخطي التوصية الجديدة.")
                continue

            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 500000:
                logger.info(f"⚠️ [Market] تجاهل {symbol} - سيولة منخفضة: {volume_15m:,.2f} USDT.")
                continue
            logger.info(f"✅ [Market] الشروط مستوفاة؛ إرسال تنبيه للزوج {symbol} (فريم 1h).")
            send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance, "1h")
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
                    signal.get('confidence', 100),
                    volume_15m
                ))
                conn.commit()
                logger.info(f"✅ [Market] تم إدخال الإشارة بنجاح للزوج {symbol}.")
            except Exception as e:
                logger.error(f"❌ [Market] فشل إدخال الإشارة للزوج {symbol}: {e}")
                conn.rollback()
            time.sleep(1)
        logger.info("==========================================")
        logger.info("✅ [Market] انتهى فحص جميع الأزواج.")
    except Exception as e:
        logger.error(f"❌ [Market] خطأ في تحليل السوق: {e}")

# ---------------------- اختبار Telegram ----------------------
def test_telegram():
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': chat_id, 'text': '🚀 [Test] رسالة اختبار من البوت.', 'parse_mode': 'Markdown'}
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"✅ [Test] رد Telegram: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"❌ [Test] فشل إرسال رسالة الاختبار: {e}")

# ---------------------- تشغيل Flask ----------------------
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    set_telegram_webhook()
    Thread(target=run_flask, daemon=True).start()
    Thread(target=track_signals, daemon=True).start()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    Thread(target=check_open_recommendations, daemon=True).start()
    test_telegram()
    logger.info("✅ [Main] تم بدء التشغيل بنجاح!")
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    try:
        while True:
            time.sleep(3)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
