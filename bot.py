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
risk_ratio = float(config('RISK_RATIO', default=0.02))  # نسبة المخاطرة لكل صفقة
custom_atr_multiplier = float(config('ATR_MULTIPLIER', default=1.5))
logger.info(f" TELEGRAM_BOT_TOKEN: {telegram_token[:10]}...")
logger.info(f" TELEGRAM_CHAT_ID: {chat_id}")
# ---------------------- إعداد الاتصال بقاعدة البيانات ----------------------
conn = None
cur = None
def init_db():
    global conn, cur
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()
        # تحديث البنية مع إضافة أعمدة جديدة
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
                profitable_stop_loss BOOLEAN DEFAULT FALSE,
                atr_multiplier DOUBLE PRECISION DEFAULT 1.5,
                risk_ratio DOUBLE PRECISION DEFAULT 0.02
            )
        """)
        conn.commit()
        logger.info("✅ [DB] تم تهيئة قاعدة البيانات بنجاح.")
    except Exception as e:
        logger.error(f"❌ [DB] فشل تهيئة قاعدة البيانات: {e}")
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
    return rsi
def calculate_atr_indicator(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
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
    return df
# ---------------------- دوال التنبؤ وتحليل المشاعر ----------------------
def ml_predict_signal(symbol, df):
    try:
        rsi = df['rsi'].iloc[-1]
        adx = df['adx'].iloc[-1]
        sentiment = get_market_sentiment(symbol)
        if rsi < 45 and adx > 25 and sentiment > 0.6:
            return 0.9
        elif rsi < 50 and adx > 20 and sentiment > 0.5:
            return 0.75
        else:
            return 0.6
    except Exception as e:
        logger.error(f"❌ [ML] خطأ في ml_predict_signal لـ {symbol}: {e}")
        return 0.6
def get_market_sentiment(symbol):
    # هنا يمكنك دمج بيانات مشاعر من مواقع مثل Twitter أو Google Trends
    return 0.7
def get_fear_greed_index():
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("data"):
                fng_value = float(data["data"][0].get("value"))
                return fng_value
        return 50.0
    except Exception as e:
        logger.error(f"❌ [FNG] خطأ في جلب مؤشر الخوف والجشع: {e}")
        return 50.0
# ---------------------- استراتيجية Freqtrade المحسّنة ----------------------
class FreqtradeStrategy:
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 50:
            return df
        df['ema5'] = calculate_ema(df['close'], 5)
        df['ema8'] = calculate_ema(df['close'], 8)
        df['ema21'] = calculate_ema(df['close'], 21)
        df['ema34'] = calculate_ema(df['close'], 34)
        df['ema50'] = calculate_ema(df['close'], 50)
        df['rsi'] = calculate_rsi_indicator(df)
        df = calculate_atr_indicator(df)
        df = calculate_macd(df, fast_period=10, slow_period=21, signal_period=8)
        df = calculate_kdj(df)
        df = calculate_adx(df)
        df = detect_candlestick_patterns(df)
        return df
    def composite_buy_score(self, row):
        score = 0
        if row['ema5'] > row['ema8'] > row['ema21'] > row['ema34'] > row['ema50']:
            score += 1
        if row['rsi'] < 40:
            score += 1
        if (row['close'] - row['lower_band']) / row['close'] < 0.03:
            score += 1
        if row['macd'] > row['macd_signal']:
            score += 1
        if row['kdj_j'] > row['kdj_k'] > row['kdj_d']:
            score += 1
        if row['adx'] > 25:
            score += 1
        if row['Bullish'] == 100:
            score += 1
        return score
    def composite_sell_score(self, row):
        score = 0
        if row['ema5'] < row['ema8'] < row['ema21'] < row['ema34'] < row['ema50']:
            score += 1
        if row['rsi'] > 65:
            score += 1
        if (row['upper_band'] - row['close']) / row['close'] < 0.03:
            score += 1
        if row['macd'] < row['macd_signal']:
            score += 1
        if row['kdj_j'] < row['kdj_k'] < row['kdj_d']:
            score += 1
        if row['adx'] > 25:
            score += 1
        if row['Bearish'] == 100:
            score += 1
        return score
    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        df['buy_score'] = df.apply(self.composite_buy_score, axis=1)
        conditions = (df['buy_score'] >= 4)
        df.loc[conditions, 'buy'] = 1
        return df
    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        df['sell_score'] = df.apply(self.composite_sell_score, axis=1)
        conditions = (df['sell_score'] >= 4)
        df.loc[conditions, 'sell'] = 1
        return df
# ---------------------- دالة التنبؤ بالسعر المحسّنة ----------------------
def improved_predict_future_price(symbol, interval='2h', days=60):
    try:
        df = fetch_historical_data(symbol, interval, days)
        if df is None or len(df) < 50:
            return None
        strategy = FreqtradeStrategy()
        df = strategy.populate_indicators(df)
        df = df.dropna().reset_index(drop=True)
        features = [
            'close', 'ema8', 'ema21', 'ema50', 'rsi', 
            'upper_band', 'lower_band', 'atr', 
            'macd', 'macd_signal', 'macd_hist', 
            'kdj_j', 'kdj_k', 'kdj_d', 
            'adx', 'Bullish', 'Bearish'
        ]
        X = df[features].iloc[:-1].values
        y = df['close'].iloc[1:].values
        from sklearn.ensemble import GradientBoostingRegressor
        model = GradientBoostingRegressor(n_estimators=200, max_depth=4, random_state=42)
        model.fit(X, y)
        last_features = df[features].iloc[-1].values.reshape(1, -1)
        predicted_price = model.predict(last_features)[0]
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
        atr_multiplier = custom_atr_multiplier  
        target = current_price + atr_multiplier * current_atr
        stop_loss = current_price - atr_multiplier * current_atr
        profit_margin = (target / current_price - 1) * 100
        if profit_margin < 1:
            return None
        predicted_price = improved_predict_future_price(symbol, interval='2h', days=60)
        if predicted_price is None or predicted_price <= current_price * 1.01:
            return None
        signal = {
            'symbol': symbol,
            'price': current_price,
            'target': target,
            'stop_loss': stop_loss,
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
            'predicted_price': predicted_price
        }
        return signal
    else:
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
    webhook_url = "https://your-render-url.onrender.com/webhook"
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
            return symbols
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في قراءة الملف: {e}")
        return []
def fetch_historical_data(symbol, interval='2h', days=10):
    try:
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
        return df[['timestamp', 'open', 'high', 'low', 'close']]
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب البيانات لـ {symbol}: {e}")
        return None
def fetch_recent_volume(symbol):
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        return volume
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في جلب حجم {symbol}: {e}")
        return 0
# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance, timeframe):
    try:
        equity = 10000  # قيمة الرصيد الإجمالي (يمكن استبدالها بطلب حقيقي)
        trade_size = calculate_risk_amount(equity, signal['price'], signal['stop_loss'], risk_ratio)
        profit_pct = signal['target'] / signal['price'] - 1
        loss_pct = signal['stop_loss'] / signal['price'] - 1
        profit_usdt = round(trade_size * profit_pct, 2)
        loss_usdt = round(trade_size * loss_pct, 2)
        timestamp = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
        message = (
            f"🚀 **إشارة تداول جديدة**
"
            f"——————————————
"
            f"**زوج:** {signal['symbol']}
"
            f"**سعر الدخول:** `${signal['price']:.8f}`
"
            f"**🎯 الهدف:** `${signal['target']:.8f}` (+{profit_pct*100:.2f}% / +{profit_usdt} USDT)
"
            f"**🛑 وقف الخسارة:** `${signal['stop_loss']:.8f}` ({loss_pct*100:.2f}% / {loss_usdt} USDT)
"
            f"**⏱ الفريم:** {timeframe}
"
            f"**💧 السيولة:** {volume:,.2f} USDT
"
            f"**💵 حجم الصفقة:** ${trade_size:.2f}
"
            f"——————————————
"
            f"📈 **نسب السيطرة (15m):**
"
            f"   • BTC: {btc_dominance:.2f}%
"
            f"   • ETH: {eth_dominance:.2f}%
"
            f"📊 **مؤشر الخوف والجشع:** {get_fear_greed_index():.2f}
"
            f"——————————————
"
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
        requests.post(url, json=payload, timeout=10)
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
            "📊 **تقرير الأداء الشامل**
"
            "——————————————
"
            f"✅ **التوصيات الناجحة:** {success_count}
"
            f"🔹 **وقف الخسارة الرابح:** {profitable_stop_loss_count}
"
            f"❌ **التوصيات ذات وقف الخسارة:** {stop_loss_count}
"
            f"⏳ **التوصيات النشطة:** {active_count}
"
            f"📝 **الصفقات المغلقة:** {total_trades}
"
            f"💹 **متوسط الربح:** {avg_profit_usd:.2f} USDT
"
            f"📉 **متوسط الخسارة:** {avg_loss_usd:.2f} USDT
"
            f"💵 **صافي الربح/الخسارة:** {net_profit_usd:.2f} USDT
"
            f"⭐ **تقييم البوت:** {bot_rating:.2f}%
"
            "——————————————
"
            f"⏰ **{timestamp}**"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            'chat_id': target_chat_id,
            'text': report_message,
            'parse_mode': 'Markdown'
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"❌ [Report] فشل إرسال تقرير الأداء: {e}")
# ---------------------- خدمة تتبع الإشارات (فحص التوصيات المفتوحة) ----------------------
def track_signals():
    while True:
        try:
            check_db_connection()
            cur.execute("""
                SELECT id, symbol, entry_price, target, stop_loss, atr_multiplier, risk_ratio
                FROM signals 
                WHERE achieved_target = FALSE 
                  AND hit_stop_loss = FALSE 
                  AND closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss, atr_multiplier, risk_ratio = signal
                if symbol in ticker_data:
                    current_price = float(ticker_data[symbol].get('c', 0))
                else:
                    continue
                df = fetch_historical_data(symbol, interval='15m', days=2)
                if df is None or len(df) < 50:
                    continue
                strategy = FreqtradeStrategy()
                df = strategy.populate_indicators(df)
                df = detect_candlestick_patterns(df)
                df = calculate_macd(df, fast_period=10, slow_period=21, signal_period=8)
                df = calculate_kdj(df)
                last_row = df.iloc[-1]
                macd_bullish = df['macd'].iloc[-1] > df['macd_signal'].iloc[-1]
                kdj_bullish = (df['kdj_j'].iloc[-1] > 50) and (df['kdj_k'].iloc[-1] > df['kdj_d'].iloc[-1])
                bullish_signal = macd_bullish and kdj_bullish
                current_gain_pct = (current_price - entry) / entry
                if current_price >= target:
                    profit_pct = target / entry - 1
                    cur.execute("""
                        UPDATE signals 
                        SET achieved_target = TRUE, closed_at = NOW(), profit_percentage = %s 
                        WHERE id = %s
                    """, (profit_pct * 100, signal_id))
                    conn.commit()
                    continue
                elif current_price <= stop_loss:
                    loss_pct = stop_loss / entry - 1
                    cur.execute("""
                        UPDATE signals 
                        SET hit_stop_loss = TRUE, closed_at = NOW(), profit_percentage = %s 
                        WHERE id = %s
                    """, (loss_pct * 100, signal_id))
                    conn.commit()
                    continue
                if bullish_signal and current_gain_pct >= 0.01:
                    new_target = entry * (1 + (current_gain_pct + 0.01))
                    new_stop_loss = entry * (1 + (current_gain_pct - 0.01))
                    cur.execute(
                        "UPDATE signals SET target = %s, stop_loss = %s WHERE id = %s",
                        (new_target, new_stop_loss, signal_id)
                    )
                    conn.commit()
        except Exception as e:
            logger.error(f"❌ [Track] خطأ في خدمة تتبع الإشارات: {e}")
        time.sleep(60)
# ---------------------- دالة حساب حجم الصفقة بناءً على المخاطرة ----------------------
def calculate_risk_amount(equity, entry_price, stop_loss, risk_ratio):
    risk_per_trade = equity * risk_ratio
    position_size = risk_per_trade / (entry_price - stop_loss)
    return round(position_size, 8)
# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    set_telegram_webhook()
    Thread(target=run_flask, daemon=True).start()
    Thread(target=track_signals, daemon=True).start()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    test_telegram()
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    try:
        while True:
            time.sleep(3)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
