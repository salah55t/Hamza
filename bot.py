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
from sklearn.preprocessing import StandardScaler
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------- إعدادات التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('crypto_bot.log'), logging.StreamHandler()]
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

# ---------------------- آلية التخزين المؤقت ----------------------
historical_data_cache = {}   # يخزن: { symbol: (timestamp, dataframe) }
volume_data_cache = {}       # يخزن: { symbol: (timestamp, volume) }

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
    # تأكد من تعديل الرابط ليناسب عنوان التطبيق المنشور (مثلاً على Render)
    webhook_url = "https://your-app.onrender.com/webhook"
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

# ---------------------- دوال جلب البيانات والتحليل الفني ----------------------
def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip()]
            logger.info(f"تم الحصول على {len(symbols)} زوج من العملات")
            return symbols
    except Exception as e:
        logger.error(f"خطأ في قراءة الملف: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=2):
    """
    جلب البيانات التاريخية لمدة يومين على فريم 5 دقائق مع استخدام التخزين المؤقت لمدة 5 دقائق.
    """
    cache_duration = 300  # 5 دقائق
    current_time = time.time()
    if symbol in historical_data_cache:
        cached_timestamp, cached_df = historical_data_cache[symbol]
        if current_time - cached_timestamp < cache_duration:
            logger.info(f"استخدام البيانات المؤقتة للزوج {symbol}")
            return cached_df

    try:
        logger.info(f"بدء جلب البيانات التاريخية للزوج: {symbol}")
        klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume',
                                             'close_time', 'quote_volume', 'trades',
                                             'taker_buy_base', 'taker_buy_quote', 'ignore'])
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        logger.info(f"تم جلب {len(df)} صف من البيانات للزوج: {symbol}")
        historical_data_cache[symbol] = (current_time, df[['timestamp', 'open', 'high', 'low', 'close']])
        return historical_data_cache[symbol][1]
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
        return None

def fetch_recent_volume(symbol):
    """
    جلب حجم السيولة في آخر 15 دقيقة مع استخدام تخزين مؤقت لمدة 30 ثانية.
    """
    cache_duration = 30  # 30 ثانية
    current_time = time.time()
    if symbol in volume_data_cache:
        cached_timestamp, cached_volume = volume_data_cache[symbol]
        if current_time - cached_timestamp < cache_duration:
            logger.info(f"استخدام حجم السيولة المؤقت للزوج {symbol}")
            return cached_volume

    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"حجم السيولة للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT")
        volume_data_cache[symbol] = (current_time, volume)
        return volume
    except Exception as e:
        logger.error(f"خطأ في جلب حجم {symbol}: {e}")
        return 0

def calculate_volatility(df):
    df['returns'] = df['close'].pct_change()
    vol = df['returns'].std() * np.sqrt(24 * 60 / 5) * 100
    logger.info(f"تم حساب التقلب: {vol:.2f}%")
    return vol

def calculate_ichimoku(df, tenkan=9, kijun=26, senkou_b=52, displacement=26):
    logger.info("بدء حساب مؤشر الاشموكو")
    df['tenkan_sen'] = (df['high'].rolling(window=tenkan).max() + df['low'].rolling(window=tenkan).min()) / 2
    df['kijun_sen'] = (df['high'].rolling(window=kijun).max() + df['low'].rolling(window=kijun).min()) / 2
    df['senkou_span_a'] = ((df['tenkan_sen'] + df['kijun_sen']) / 2).shift(displacement)
    df['senkou_span_b'] = ((df['high'].rolling(window=senkou_b).max() + df['low'].rolling(window=senkou_b).min()) / 2).shift(displacement)
    df['chikou_span'] = df['close'].shift(-displacement)
    logger.info("انتهى حساب مؤشر الاشموكو")
    return df

def calculate_rsi(df, period=14):
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
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(window=period).mean().iloc[-1]
    logger.info(f"تم حساب ATR: {atr:.8f}")
    return atr

def calculate_atr_series(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr_series = true_range.rolling(window=period).mean()
    return atr_series

# ---------------------- دوال حساب القناة السعرية ----------------------
def calculate_price_channel(df_day):
    """
    حساب القناة السعرية باستخدام بيانات اليوم على فريم 15 دقيقة:
      - الدعم هو أدنى سعر خلال الفترة.
      - المقاومة هو أعلى سعر خلال الفترة.
    """
    lower_channel = df_day['low'].min()
    upper_channel = df_day['high'].max()
    return lower_channel, upper_channel

# ---------------------- دوال إضافية للاستراتيجية 2 (MACD, Bollinger Bands) ----------------------
def calculate_MACD(df, short_period=12, long_period=26, signal_period=9):
    df['ema_short'] = df['close'].ewm(span=short_period, adjust=False).mean()
    df['ema_long'] = df['close'].ewm(span=long_period, adjust=False).mean()
    df['MACD'] = df['ema_short'] - df['ema_long']
    df['MACD_signal'] = df['MACD'].ewm(span=signal_period, adjust=False).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']
    return df[['MACD', 'MACD_signal', 'MACD_hist']]

def calculate_Bollinger_Bands(df, period=20, std_multiplier=2):
    sma = df['close'].rolling(window=period).mean()
    std = df['close'].rolling(window=period).std()
    upper_band = sma + std_multiplier * std
    lower_band = sma - std_multiplier * std
    return lower_band, sma, upper_band

# ---------------------- الاستراتيجية 1: نموذج تجميعي + قناة دونتشين ----------------------
def generate_signal_strategy1(df, symbol):
    df = df.dropna().reset_index(drop=True)
    if len(df) < 100:
        logger.warning(f"بيانات {symbol} غير كافية للاستراتيجية 1")
        return None
    df['prev_close'] = df['close'].shift(1)
    df['sma10'] = df['close'].rolling(window=10).mean().shift(1)
    df['sma20'] = df['close'].rolling(window=20).mean().shift(1)
    df['sma50'] = df['close'].rolling(window=50).mean().shift(1)
    df['ema10'] = df['close'].ewm(span=10, adjust=False).mean().shift(1)
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean().shift(1)
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean().shift(1)
    df['rsi_feature'] = calculate_rsi(df).shift(1)
    df['atr_feature'] = calculate_atr_series(df, period=14).shift(1)
    df['volatility'] = df['close'].pct_change().rolling(window=10).std().shift(1)
    df['momentum'] = df['close'] - df['close'].shift(10)
    features = ['prev_close', 'sma10', 'sma20', 'sma50', 'ema10', 'ema20', 'ema50',
                'rsi_feature', 'atr_feature', 'volatility', 'momentum']
    df_features = df.dropna().reset_index(drop=True)
    if len(df_features) < 50:
        logger.warning(f"بيانات الميزات لـ {symbol} غير كافية للاستراتيجية 1")
        return None
    X = df_features[features]
    y = df_features['close']
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor, ExtraTreesRegressor, VotingRegressor
    from sklearn.linear_model import Ridge
    from xgboost import XGBRegressor
    model1 = RandomForestRegressor(n_estimators=200, random_state=42)
    model2 = GradientBoostingRegressor(n_estimators=200, random_state=42)
    model3 = ExtraTreesRegressor(n_estimators=200, random_state=42)
    model4 = Ridge()
    model5 = XGBRegressor(n_estimators=200, random_state=42, objective='reg:squarederror')
    voting_reg = VotingRegressor([
        ('rf', model1),
        ('gbr', model2),
        ('etr', model3),
        ('ridge', model4),
        ('xgb', model5)
    ])
    voting_reg.fit(X_train_scaled, y_train)
    score = voting_reg.score(X_test_scaled, y_test)
    confidence = round(score * 100, 2)
    logger.info(f"ثقة الاستراتيجية 1 لـ {symbol}: {confidence}%")
    current_price = df['close'].iloc[-1]
    if len(df) >= 96:
        day_df = df.tail(96)
    else:
        day_df = df
    lower_channel, upper_channel = calculate_price_channel(day_df)
    if not (lower_channel < current_price < upper_channel):
        logger.info(f"تجاهل {symbol} - السعر الحالي خارج قناة دونتشين")
        return None
    stop_loss = lower_channel
    target = upper_channel
    rounded_price = float(format(current_price, '.4f'))
    rounded_target = float(format(target, '.4f'))
    rounded_stop_loss = float(format(stop_loss, '.4f'))
    return {
        'symbol': symbol,
        'price': rounded_price,
        'target': rounded_target,
        'stop_loss': rounded_stop_loss,
        'confidence': confidence,
        'trade_value': TRADE_VALUE,
        'strategy': 'Strategy1'
    }

# ---------------------- الاستراتيجية 2: MACD + Bollinger Bands + RSI ----------------------
def generate_signal_strategy2(df, symbol):
    df = df.dropna().reset_index(drop=True)
    if len(df) < 50:
        logger.warning(f"بيانات {symbol} غير كافية للاستراتيجية 2")
        return None
    if len(df) >= 96:
        day_df = df.tail(96)
    else:
        day_df = df
    macd_df = calculate_MACD(day_df.copy())
    macd_latest = macd_df.iloc[-1]
    lower_bb, middle_bb, upper_bb = calculate_Bollinger_Bands(day_df.copy(), period=20, std_multiplier=2)
    rsi_val = calculate_rsi(day_df.copy(), period=14).iloc[-1]
    current_price = day_df['close'].iloc[-1]
    logger.info(f"{symbol} - MACD: {macd_latest['MACD']:.4f}, Signal: {macd_latest['MACD_signal']:.4f}, RSI: {rsi_val:.2f}")
    if macd_latest['MACD'] <= macd_latest['MACD_signal']:
        logger.info(f"تجاهل {symbol} - MACD لم يتقاطع صعودياً")
        return None
    if rsi_val > 30:
        logger.info(f"تجاهل {symbol} - RSI غير مناسب (RSI = {rsi_val:.2f})")
        return None
    if current_price > lower_bb.iloc[-1] * 1.05:
        logger.info(f"تجاهل {symbol} - السعر ليس قريباً من الفرقة السفلية لـ Bollinger Bands")
        return None
    stop_loss = lower_bb.iloc[-1]
    target = upper_bb.iloc[-1]
    confidence = round((macd_latest['MACD'] - macd_latest['MACD_signal']) * 100, 2)
    rounded_price = float(format(current_price, '.4f'))
    rounded_target = float(format(target, '.4f'))
    rounded_stop_loss = float(format(stop_loss, '.4f'))
    return {
        'symbol': symbol,
        'price': rounded_price,
        'target': rounded_target,
        'stop_loss': rounded_stop_loss,
        'confidence': confidence,
        'trade_value': TRADE_VALUE,
        'strategy': 'Strategy2'
    }

def generate_trade_signal(df, symbol):
    signal1 = generate_signal_strategy1(df.copy(), symbol)
    signal2 = generate_signal_strategy2(df.copy(), symbol)
    if signal1 and signal2:
        return signal1 if signal1['confidence'] >= signal2['confidence'] else signal2
    elif signal1:
        return signal1
    elif signal2:
        return signal2
    else:
        return None

# ---------------------- دالة الحصول على نسب السيطرة على السوق ----------------------
def get_market_dominance():
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

# ---------------------- دوال إرسال التنبيهات والتقارير ----------------------
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
            f"📊 ثقة الاستراتيجية ({signal['strategy']}): {signal['confidence']}%\n"
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
            logger.error(f"فشل إرسال إشعار التوصية للزوج {signal['symbol']}: {response.status_code} {response.text}")
        else:
            logger.info(f"تم إرسال إشعار التوصية للزوج {signal['symbol']} بنجاح")
    except Exception as e:
        logger.error(f"فشل إرسال إشعار التوصية للزوج {signal['symbol']}: {e}")

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

# ---------------------- دوال تتبع الإشارات ----------------------
def track_signals():
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
                    logger.info(f"فحص {symbol}: السعر الحالي {current_price}, سعر الدخول {entry}")
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

# ---------------------- دالة تحليل السوق ----------------------
def analyze_market():
    logger.info("بدء فحص الأزواج الآن...")
    check_db_connection()
    
    cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
    active_signals_count = cur.fetchone()[0]
    if active_signals_count >= 4:
        logger.info("عدد التوصيات النشطة وصل إلى الحد الأقصى (4). لن يتم إرسال توصيات جديدة حتى إغلاق توصية حالية.")
        return

    # إزالة شرط اختبار البيتكوين؛ نعتبره دائماً True.
    btc_trend = True

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
            df = fetch_historical_data(symbol)  # بيانات لمدة يومين على فريم 5 دقائق
            if df is None or len(df) < 100:
                logger.warning(f"تجاهل {symbol} - بيانات تاريخية غير كافية")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 40000:
                logger.info(f"تجاهل {symbol} - سيولة منخفضة: {volume_15m:,.2f}")
                continue
            signal = generate_trade_signal(df, symbol)
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
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': chat_id, 'text': 'رسالة اختبار من البوت', 'parse_mode': 'Markdown'}
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"رد اختبار Telegram: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"فشل إرسال رسالة الاختبار: {e}")

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
    test_telegram()
    logger.info("✅ تم بدء التشغيل بنجاح!")
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
