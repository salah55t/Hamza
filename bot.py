import time
import os
import pandas as pd
import numpy as np
import psycopg2
from binance.client import Client, BinanceAPIException
from binance import ThreadedWebsocketManager
from flask import Flask, request
from threading import Thread
import logging
import requests
import json  # لاستخدام reply_markup في تنبيهات Telegram
from sklearn.model_selection import train_test_split
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler

# مكتبات التعلم العميق لنموذج LSTM
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense
from sklearn.preprocessing import MinMaxScaler

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

# قيمة الصفقة الثابتة للتوصيات
TRADE_VALUE = 10

# استخدام نموذج LSTM المتقدم بدلاً من النموذج التجميعي التقليدي
USE_LSTM_MODEL = True

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
        logger.info("تم تهيئة جدول الإشارات بنجاح")
    except Exception as e:
        logger.error(f"فشل تهيئة قاعدة البيانات: {e}")
        raise

def check_db_connection():
    global conn, cur
    try:
        cur.execute("SELECT 1")
        conn.commit()
    except Exception as e:
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
    """
    عند استقبال رسالة من WebSocket يتم تحديث البيانات في ticker_data
    بحيث يكون المفتاح هو رمز الزوج (Symbol).
    """
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
    """
    تشغيل WebSocket لتحديث بيانات التيكر لجميع الأزواج.
    """
    try:
        twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
        twm.start()
        twm.start_miniticker_socket(callback=handle_ticker_message)
        logger.info("تم تشغيل WebSocket لتحديث التيكر لجميع الأزواج")
    except Exception as e:
        logger.error(f"خطأ في تشغيل WebSocket: {e}")

# ---------------------- إعداد تطبيق Flask ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "نظام توصيات التداول يعمل بكفاءة 🚀", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logger.info("Received update: " + str(update))
    if "callback_query" in update:
        callback_data = update["callback_query"]["data"]
        chat_id_callback = update["callback_query"]["message"]["chat"]["id"]
        if callback_data == "get_report":
            send_report(chat_id_callback)
            answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": update["callback_query"]["id"]})
    return '', 200

def set_telegram_webhook():
    # عدل عنوان الـ Webhook حسب بيئتك
    webhook_url = "https://your-domain.com/webhook"
    url = f"https://api.telegram.org/bot{telegram_token}/setWebhook?url={webhook_url}"
    try:
        response = requests.get(url, timeout=10)
        res_json = response.json()
        if res_json.get("ok"):
            logger.info(f"تم تسجيل webhook بنجاح: {res_json}")
        else:
            logger.error(f"فشل تسجيل webhook: {res_json}")
    except Exception as e:
        logger.error(f"استثناء أثناء تسجيل webhook: {e}")

# ---------------------- وظائف تحليل البيانات والمؤشرات ----------------------
def get_crypto_symbols():
    """قراءة الأزواج من الملف وإضافة USDT"""
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip()]
            logger.info(f"تم الحصول على {len(symbols)} زوج من العملات")
            return symbols
    except Exception as e:
        logger.error(f"خطأ في قراءة الملف: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=3):
    """جلب البيانات التاريخية المطلوبة لحساب المؤشرات"""
    try:
        logger.info(f"بدء جلب البيانات التاريخية للزوج: {symbol}")
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
        logger.info(f"تم جلب {len(df)} صف من البيانات للزوج: {symbol}")
        return df[['timestamp', 'open', 'high', 'low', 'close']]
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
        return None

def fetch_recent_volume(symbol):
    """حساب حجم السيولة في آخر 15 دقيقة"""
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"حجم السيولة للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT")
        return volume
    except Exception as e:
        logger.error(f"خطأ في جلب حجم {symbol}: {e}")
        return 0

def calculate_volatility(df):
    """حساب التقلب باستخدام بيانات الإغلاق فقط"""
    df['returns'] = df['close'].pct_change()
    vol = df['returns'].std() * np.sqrt(24 * 60 / 5) * 100
    logger.info(f"تم حساب التقلب: {vol:.2f}%")
    return vol

def calculate_ichimoku(df, tenkan=9, kijun=26, senkou_b=52, displacement=26):
    """حساب مؤشر الاشموكو"""
    logger.info("بدء حساب مؤشر الاشموكو")
    period9_high = df['high'].rolling(window=tenkan).max()
    period9_low = df['low'].rolling(window=tenkan).min()
    df['tenkan_sen'] = (period9_high + period9_low) / 2
    period26_high = df['high'].rolling(window=kijun).max()
    period26_low = df['low'].rolling(window=kijun).min()
    df['kijun_sen'] = (period26_high + period26_low) / 2
    df['senkou_span_a'] = ((df['tenkan_sen'] + df['kijun_sen']) / 2).shift(displacement)
    period52_high = df['high'].rolling(window=senkou_b).max()
    period52_low = df['low'].rolling(window=senkou_b).min()
    df['senkou_span_b'] = ((period52_high + period52_low) / 2).shift(displacement)
    df['chikou_span'] = df['close'].shift(-displacement)
    logger.info("انتهى حساب مؤشر الاشموكو")
    return df

def calculate_rsi(df, period=14):
    """حساب مؤشر RSI"""
    logger.info("بدء حساب مؤشر RSI")
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    last_rsi = rsi.iloc[-1]
    logger.info(f"تم حساب RSI: {last_rsi:.2f}")
    return rsi

def calculate_atr(df, period=14):
    """حساب مؤشر ATR (متوسط المدى الحقيقي)"""
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(window=period).mean().iloc[-1]
    logger.info(f"تم حساب ATR: {atr:.8f}")
    return atr

def calculate_atr_series(df, period=14):
    """حساب سلسلة ATR لكل صف"""
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr_series = true_range.rolling(window=period).mean()
    return atr_series

# ---------------------- نماذج التنبؤ وإدارة المخاطر ----------------------
def generate_signal_improved(df, symbol):
    """
    إنشاء إشارة تداول باستخدام نموذج تجميعي مع ميزات إضافية.
    (هذا النموذج التقليدي متاح في حال رغبتك بالمقارنة)
    """
    logger.info(f"بدء توليد إشارة تداول محسنة للزوج: {symbol}")
    try:
        df = df.dropna().reset_index(drop=True)
        if len(df) < 100:
            logger.warning(f"بيانات {symbol} غير كافية للنموذج المحسن")
            return None

        # حساب بعض الميزات الفنية
        df['prev_close'] = df['close'].shift(1)
        df['sma10'] = df['close'].rolling(window=10).mean().shift(1)
        df['sma20'] = df['close'].rolling(window=20).mean().shift(1)
        df['ema10'] = df['close'].ewm(span=10, adjust=False).mean().shift(1)
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean().shift(1)
        df['rsi_feature'] = calculate_rsi(df).shift(1)
        df['atr_feature'] = calculate_atr_series(df, period=14).shift(1)
        df['volatility'] = df['close'].pct_change().rolling(window=10).std().shift(1)
        df['momentum'] = df['close'] - df['close'].shift(10)
        
        features = ['prev_close', 'sma10', 'sma20', 'ema10', 'ema20',
                    'rsi_feature', 'atr_feature', 'volatility', 'momentum']
        df_features = df.dropna().reset_index(drop=True)
        if len(df_features) < 50:
            logger.warning(f"بيانات الميزات لـ {symbol} غير كافية")
            return None

        X = df_features[features]
        y = df_features['close']

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

        from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor, ExtraTreesRegressor, VotingRegressor
        from sklearn.linear_model import Ridge

        model1 = RandomForestRegressor(n_estimators=100, random_state=42)
        model2 = GradientBoostingRegressor(n_estimators=100, random_state=42)
        model3 = ExtraTreesRegressor(n_estimators=100, random_state=42)
        model4 = Ridge()

        voting_reg = VotingRegressor([
            ('rf', model1),
            ('gbr', model2),
            ('etr', model3),
            ('ridge', model4)
        ])

        voting_reg.fit(X_train, y_train)
        score = voting_reg.score(X_test, y_test)
        confidence = round(score * 100, 2)
        logger.info(f"ثقة النموذج المحسن لـ {symbol}: {confidence}%")

        current_price = df['close'].iloc[-1]
        current_atr = calculate_atr(df, period=14)
        if current_atr / current_price > 0.03:
            logger.info(f"تجاهل {symbol} - تقلب مرتفع (ATR/S= {current_atr/current_price:.4f})")
            return None

        atr_multiplier_target = 1.5
        atr_multiplier_stop = 1.0
        target = current_price + atr_multiplier_target * current_atr
        stop_loss = current_price - atr_multiplier_stop * current_atr

        decimals = 8 if current_price < 1 else 4
        rounded_price = float(format(current_price, f'.{decimals}f'))
        rounded_target = float(format(target, f'.{decimals}f'))
        rounded_stop_loss = float(format(stop_loss, f'.{decimals}f'))

        reward_percent = (rounded_target - rounded_price) / rounded_price
        risk_percent = (rounded_price - rounded_stop_loss) / rounded_price
        desired_ratio = 1.5
        if risk_percent == 0 or (reward_percent / risk_percent) < desired_ratio:
            logger.info(f"تجاهل {symbol} - نسبة المخاطرة/العائد أقل من {desired_ratio}")
            return None

        if confidence < 97:
            logger.info(f"تجاهل {symbol} - ثقة النموذج ({confidence}%) أقل من المطلوب")
            return None

        signal = {
            'symbol': symbol,
            'price': rounded_price,
            'target': rounded_target,
            'stop_loss': rounded_stop_loss,
            'confidence': confidence,
            'atr': round(current_atr, 8),
            'trade_value': TRADE_VALUE
        }
        logger.info(f"تم توليد الإشارة المحسنة للزوج {symbol}: {signal}")
        return signal

    except Exception as e:
        logger.error(f"خطأ في توليد إشارة محسن للزوج {symbol}: {e}")
        return None

def generate_signal_lstm(df, symbol):
    """
    إنشاء إشارة تداول باستخدام نموذج LSTM المتقدم.
    يعتمد النموذج على بيانات أسعار الإغلاق للتنبؤ بالسعر التالي،
    ومن ثم تحديد الهدف ووقف الخسارة بناءً على مؤشر ATR.
    """
    logger.info(f"بدء توليد إشارة LSTM للزوج: {symbol}")
    try:
        df = df.dropna().reset_index(drop=True)
        if len(df) < 120:
            logger.warning(f"بيانات {symbol} غير كافية للنموذج LSTM")
            return None

        # استخدام عمود 'close' للتنبؤ بالسعر
        prices = df['close'].values.reshape(-1, 1)
        scaler = MinMaxScaler(feature_range=(0, 1))
        scaled_prices = scaler.fit_transform(prices)

        window_size = 20
        X, y = [], []
        for i in range(window_size, len(scaled_prices)):
            X.append(scaled_prices[i - window_size:i, 0])
            y.append(scaled_prices[i, 0])
        X, y = np.array(X), np.array(y)
        X = np.reshape(X, (X.shape[0], X.shape[1], 1))

        split = int(0.8 * len(X))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        # بناء نموذج LSTM
        model = Sequential()
        model.add(LSTM(50, return_sequences=True, input_shape=(X_train.shape[1], 1)))
        model.add(LSTM(50))
        model.add(Dense(1))
        model.compile(optimizer='adam', loss='mean_squared_error')

        model.fit(X_train, y_train, epochs=20, batch_size=32, verbose=0)
        loss = model.evaluate(X_test, y_test, verbose=0)
        confidence = round((1 - loss) * 100, 2)
        logger.info(f"ثقة نموذج LSTM لـ {symbol}: {confidence}%")

        # التنبؤ بالسعر التالي
        last_window = scaled_prices[-window_size:]
        last_window = np.reshape(last_window, (1, window_size, 1))
        predicted_scaled = model.predict(last_window)
        predicted_price = scaler.inverse_transform(predicted_scaled)[0][0]
        current_price = df['close'].iloc[-1]

        current_atr = calculate_atr(df, period=14)
        atr_multiplier_target = 1.5
        atr_multiplier_stop = 1.0
        target = current_price + atr_multiplier_target * current_atr
        stop_loss = current_price - atr_multiplier_stop * current_atr

        if confidence < 97:
            logger.info(f"تجاهل {symbol} - ثقة نموذج LSTM ({confidence}%) أقل من المطلوب")
            return None

        signal = {
            'symbol': symbol,
            'price': float(format(current_price, '.4f')),
            'target': float(format(target, '.4f')),
            'stop_loss': float(format(stop_loss, '.4f')),
            'confidence': confidence,
            'atr': round(current_atr, 8),
            'trade_value': TRADE_VALUE
        }
        logger.info(f"تم توليد إشارة LSTM للزوج {symbol}: {signal}")
        return signal

    except Exception as e:
        logger.error(f"خطأ في توليد إشارة LSTM للزوج {symbol}: {e}")
        return None

def get_market_dominance():
    """
    الحصول على نسب السيطرة على السوق للبيتكوين والإيثيريوم من CoinGecko.
    """
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json().get("data", {})
            market_cap_percentage = data.get("market_cap_percentage", {})
            btc_dominance = market_cap_percentage.get("btc")
            eth_dominance = market_cap_percentage.get("eth")
            logger.info(f"BTC Dominance: {btc_dominance}%, ETH Dominance: {eth_dominance}%")
            return btc_dominance, eth_dominance
        else:
            logger.error(f"خطأ في جلب نسب السيطرة: {response.status_code} {response.text}")
            return None, None
    except Exception as e:
        logger.error(f"خطأ في get_market_dominance: {e}")
        return None, None

cached_btc_trend = None
cached_btc_trend_timestamp = 0
BTC_TREND_CACHE_INTERVAL = 3600  # ساعة

def check_btc_trend():
    logger.info("بدء فحص اتجاه BTCUSDT على فريم 4H")
    global cached_btc_trend, cached_btc_trend_timestamp
    try:
        now = time.time()
        if cached_btc_trend is not None and (now - cached_btc_trend_timestamp < BTC_TREND_CACHE_INTERVAL):
            logger.info("استخدام نتيجة الاتجاه المخزنة مؤقتاً")
            return cached_btc_trend

        btc_df = fetch_historical_data("BTCUSDT", interval="4h", days=10)
        if btc_df is None or len(btc_df) < 50:
            logger.warning("بيانات BTCUSDT غير كافية لفحص الاتجاه")
            return False

        btc_df['close'] = btc_df['close'].astype(float)
        sma50 = btc_df['close'].rolling(window=50).mean()
        current_btc_price = btc_df['close'].iloc[-1]
        current_sma50 = sma50.iloc[-1]
        trend = current_btc_price >= current_sma50
        cached_btc_trend = trend
        cached_btc_trend_timestamp = now
        logger.info(f"BTCUSDT: السعر الحالي {current_btc_price}, SMA50 {current_sma50}, الاتجاه: {'صعودي/مستقر' if trend else 'هبوطي'}")
        return trend
    except Exception as e:
        logger.error(f"خطأ في فحص اتجاه BTC: {e}")
        return False

def send_telegram_alert(signal, volume, btc_dominance, eth_dominance):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        rtl_mark = "\u200F"
        message = (
            f"{rtl_mark}🚨 **إشارة تداول جديدة - {signal['symbol']}**\n\n"
            f"▫️ السعر الحالي: ${signal['price']}\n"
            f"🎯 الهدف: ${signal['target']} (+{profit}%)\n"
            f"🛑 وقف الخسارة: ${signal['stop_loss']}\n"
            f"📊 ثقة النموذج: {signal['confidence']}%\n"
            f"📏 ATR: {signal['atr']}\n"
            f"💧 السيولة (15 دقيقة): {volume:,.2f} USDT\n"
            f"💵 قيمة الصفقة: ${TRADE_VALUE}\n\n"
            f"📈 **نسب السيطرة على السوق (4H):**\n"
            f"   - BTC: {btc_dominance:.2f}%\n"
            f"   - ETH: {eth_dominance:.2f}%\n\n"
            f"⏰ {time.strftime('%Y-%m-%d %H:%M')}"
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
        logger.info(f"رد Telegram: {response.status_code} {response.text}")
        if response.status_code != 200:
            logger.error(f"فشل إرسال إشعار توصية الشراء للزوج {signal['symbol']}: {response.status_code} {response.text}")
        else:
            logger.info(f"تم إرسال إشعار توصية الشراء للزوج {signal['symbol']} بنجاح")
    except Exception as e:
        logger.error(f"فشل إرسال إشعار توصية الشراء للزوج {signal['symbol']}: {e}")

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
        logger.info(f"رد Telegram: {response.status_code} {response.text}")
        if response.status_code != 200:
            logger.error(f"فشل إرسال التنبيه: {response.status_code} {response.text}")
        else:
            logger.info("تم إرسال التنبيه الخاص بنجاح")
    except Exception as e:
        logger.error(f"فشل إرسال التنبيه: {e}")

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
            achieved_target, entry, target_val, stop_loss_val = row
            if achieved_target:
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
        avg_profit_pct = sum(profit_percentages)/len(profit_percentages) if profit_percentages else 0
        avg_loss_pct = sum(loss_percentages)/len(loss_percentages) if loss_percentages else 0
        net_profit = total_profit + total_loss

        report_message = (
            f"📊 **تقرير الأداء الشامل**\n\n"
            f"✅ عدد التوصيات الناجحة: {success_count}\n"
            f"❌ عدد التوصيات التي حققت وقف الخسارة: {stop_loss_count}\n"
            f"💹 متوسط نسبة الربح للتوصيات الناجحة: {avg_profit_pct:.2f}%\n"
            f"📉 متوسط نسبة الخسارة للتوصيات مع وقف الخسارة: {avg_loss_pct:.2f}%\n"
            f"💵 إجمالي الربح/الخسارة: ${net_profit:.2f}"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': target_chat_id,
            'text': report_message,
            'parse_mode': 'Markdown'
        }
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"تم إرسال تقرير الأداء: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"فشل إرسال تقرير الأداء: {e}")

def track_signals():
    """
    تتبع الإشارات النشطة وإرسال التنبيهات عند تحقيق الهدف أو تفعيل وقف الخسارة.
    """
    logger.info("بدء خدمة تتبع الإشارات...")
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
            logger.info(f"تم العثور على {len(active_signals)} إشارة نشطة للتتبع")
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss = signal
                try:
                    if symbol in ticker_data:
                        current_price = float(ticker_data[symbol].get('c', 0))
                    else:
                        logger.warning(f"لا يوجد تحديث أسعار لحظة {symbol} من WebSocket")
                        continue
                    logger.info(f"فحص الزوج {symbol}: السعر الحالي {current_price}, سعر الدخول {entry}")
                    if abs(entry) < 1e-8:
                        logger.error(f"سعر الدخول للزوج {symbol} صفر تقريباً، يتم تخطي الحساب.")
                        continue
                    if current_price >= target:
                        profit = ((current_price - entry) / entry) * 100
                        msg = (
                            f"🎉 **تحقيق الهدف - {symbol}**\n"
                            f"• سعر الدخول: ${entry:.8f}\n"
                            f"• سعر الخروج: ${current_price:.8f}\n"
                            f"• الربح: +{profit:.2f}%\n"
                            f"⏱ {time.strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        try:
                            cur.execute("UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                            conn.commit()
                            logger.info(f"تم إغلاق التوصية للزوج {symbol} بعد تحقيق الهدف")
                        except Exception as e:
                            logger.error(f"فشل تحديث الإشارة بعد تحقيق الهدف للزوج {symbol}: {e}")
                            conn.rollback()
                    elif current_price <= stop_loss:
                        loss = ((current_price - entry) / entry) * 100
                        msg = (
                            f"🔴 **تفعيل وقف الخسارة - {symbol}**\n"
                            f"• سعر الدخول: ${entry:.8f}\n"
                            f"• سعر الخروج: ${current_price:.8f}\n"
                            f"• الخسارة: {loss:.2f}%\n"
                            f"⏱ {time.strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        try:
                            cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                            conn.commit()
                            logger.info(f"تم إغلاق التوصية للزوج {symbol} بعد تفعيل وقف الخسارة")
                        except Exception as e:
                            logger.error(f"فشل تحديث الإشارة بعد تفعيل وقف الخسارة للزوج {symbol}: {e}")
                            conn.rollback()
                except Exception as e:
                    logger.error(f"خطأ في تتبع الزوج {symbol}: {e}")
                    conn.rollback()
                    continue
            time.sleep(60)
        except Exception as e:
            logger.error(f"خطأ في خدمة تتبع الإشارات: {e}")
            conn.rollback()
            time.sleep(60)

def analyze_market():
    """
    فحص جميع الأزواج وفق الشروط المحددة وإرسال التنبيهات.
    """
    logger.info("بدء فحص الأزواج الآن...")
    check_db_connection()
    
    cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
    active_signals_count = cur.fetchone()[0]
    if active_signals_count >= 4:
        logger.info("عدد التوصيات النشطة وصل إلى الحد الأقصى (4). لن يتم إرسال توصيات جديدة حتى إغلاق توصية حالية.")
        return

    btc_trend = check_btc_trend()
    if not btc_trend:
        logger.info("اتجاه BTC (4H) هبوطي؛ لن يتم إرسال التوصيات.")
        return

    btc_dominance, eth_dominance = get_market_dominance()
    if btc_dominance is None or eth_dominance is None:
        logger.warning("لم يتم جلب نسب السيطرة؛ سيتم تعيينها كـ 0.0")
        btc_dominance, eth_dominance = 0.0, 0.0

    symbols = get_crypto_symbols()
    if not symbols:
        logger.warning("لا توجد أزواج في الملف!")
        return
    for symbol in symbols:
        logger.info(f"بدء فحص الزوج: {symbol}")
        try:
            df = fetch_historical_data(symbol)
            if df is None or len(df) < 100:
                logger.warning(f"تجاهل {symbol} - بيانات تاريخية غير كافية")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 40000:
                logger.info(f"تجاهل {symbol} - سيولة منخفضة: {volume_15m:,.2f}")
                continue
            # اختيار النموذج المناسب بناءً على الإعداد
            if USE_LSTM_MODEL:
                signal = generate_signal_lstm(df, symbol)
            else:
                signal = generate_signal_improved(df, symbol)
            if not signal:
                continue
            ichimoku_df = calculate_ichimoku(df.copy())
            last_row = ichimoku_df.iloc[-1]
            if last_row['close'] <= max(last_row['senkou_span_a'], last_row['senkou_span_b']):
                logger.info(f"تجاهل {symbol} - السعر ليس فوق السحابة وفق مؤشر الاشموكو")
                continue
            if last_row['tenkan_sen'] <= last_row['kijun_sen']:
                logger.info(f"تجاهل {symbol} - تقاطع مؤشر الاشموكو غير صعودي")
                continue
            rsi_series = calculate_rsi(df)
            last_rsi = rsi_series.iloc[-1]
            if last_rsi > 30:
                logger.info(f"تجاهل {symbol} - شرط RSI غير مستوفى (RSI = {last_rsi:.2f})")
                continue
            logger.info(f"الشروط مستوفاة؛ سيتم إرسال تنبيه للزوج {symbol}")
            send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance)
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
                    signal['confidence'],
                    volume_15m
                ))
                conn.commit()
                logger.info(f"تم إدخال الإشارة بنجاح للزوج {symbol}")
            except Exception as e:
                logger.error(f"فشل إدخال الإشارة للزوج {symbol}: {e}")
                conn.rollback()
            time.sleep(1)
        except Exception as e:
            logger.error(f"خطأ في معالجة الزوج {symbol}: {e}")
            conn.rollback()
            continue
    logger.info("انتهى فحص جميع الأزواج")

def test_telegram():
    """دالة لاختبار إرسال رسالة عبر Telegram"""
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': chat_id, 'text': 'رسالة اختبار من البوت', 'parse_mode': 'Markdown'}
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"رد اختبار Telegram: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"فشل إرسال رسالة الاختبار: {e}")

def run_flask():
    """تشغيل خادم الويب باستخدام متغير البيئة PORT إن وجد"""
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    init_db()
    set_telegram_webhook()
    Thread(target=run_flask, daemon=True).start()
    Thread(target=track_signals, daemon=True).start()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    test_telegram()
    logger.info("✅ تم بدء التشغيل بنجاح!")
    
    # جدولة فحص الأزواج كل 5 دقائق
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
