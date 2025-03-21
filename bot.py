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

# ---------------------- دالة حساب RSI ----------------------
def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # حساب المتوسط باستخدام طريقة الحركة الأسية أو المتوسط البسيط
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    # لتجنب القسمة على صفر
    avg_loss.replace(0, 1e-10, inplace=True)
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

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
        # إنشاء الجدول مع إضافة حقل "strategy" لتخزين نوع الاستراتيجية المستخدمة
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
                strategy TEXT
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

# ---------------------- دوال التنبؤ والتحليل ----------------------
def get_market_sentiment(symbol):
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

# ---------------------- استراتيجية Hummingbot المحسنة باستخدام RSI مع فيبوناتشي والمرشحات الاتجاهية وحجم التداول ----------------------
def generate_signal_with_rsi_fib(df, symbol):
    """
    تحسين الاستراتيجية بإضافة:
    - حساب مستوى فيبوناتشي (38.2%) لتحديد منطقة التصحيح.
    - حساب متوسط متحرك (SMA50) لتأكيد الاتجاه الصعودي.
    - حساب مؤشر القوة النسبية (RSI) لفترة 14 شمعة لتحديد حالة إفراط البيع.
    - تحليل حجم التداول بمقارنة حجم الشمعة الأخيرة بمتوسط حجم آخر 50 شمعة.
    في حال تحقق الشروط (السعر عند فيبوناتشي مع RSI في منطقة إفراط البيع، السعر فوق SMA50 وحجم التداول التصاعدي)،
    يتم توليد إشارة شراء.
    """
    df = df.dropna().reset_index(drop=True)
    window = 50
    if len(df) < window:
        logger.warning(f"⚠️ [RSI_Fib] بيانات غير كافية للزوج {symbol}.")
        return None

    current_price = df['close'].iloc[-1]
    recent_window = df.tail(window)
    L = recent_window['close'].min()
    H = recent_window['close'].max()
    
    if H - L < 1e-8:
        logger.warning(f"⚠️ [RSI_Fib] تغير السعر ضئيل جداً للزوج {symbol}.")
        return None

    fib_38 = L + 0.382 * (H - L)
    
    # حساب المتوسط المتحرك لفترة 50 شمعة لتأكيد الاتجاه
    df['SMA50'] = df['close'].rolling(window=50).mean()
    current_sma = df['SMA50'].iloc[-1]
    
    # حساب مؤشر القوة النسبية (RSI) باستخدام الدالة المخصصة compute_rsi
    df['RSI'] = compute_rsi(df['close'], period=14)
    current_rsi = df['RSI'].iloc[-1]
    
    # تحليل حجم التداول: حساب متوسط حجم التداول للشموع الأخيرة ومقارنته بالشمعة الحالية
    avg_volume = recent_window['volume'].mean()
    current_volume = df['volume'].iloc[-1]
    
    logger.info(f"⚙️ [RSI_Fib] {symbol} => السعر الحالي: {current_price:.8f}, fib_38: {fib_38:.8f}, SMA50: {current_sma:.8f}, RSI: {current_rsi:.2f}, حجم الشمعة: {current_volume:.2f}, متوسط الحجم: {avg_volume:.2f}")
    
    # شرط توليد الإشارة:
    # 1. السعر عند أو أقل من مستوى فيبوناتشي.
    # 2. مؤشر RSI في منطقة إفراط البيع (< 30).
    # 3. السعر أعلى من المتوسط المتحرك (تأكيد الاتجاه الصعودي).
    # 4. حجم الشمعة الحالية أكبر من أو يساوي المتوسط (دلالة على تصاعد الحجم).
    if current_price <= fib_38 and current_rsi < 30 and current_price > current_sma and current_volume >= avg_volume:
        entry_price = current_price
        target = H  # الهدف عند أعلى سعر خلال الفترة
        # حساب وقف الخسارة الأولي وفق الصيغة القديمة مع هامش أمان
        raw_stop_loss = L * 0.995  
        min_buffer = 0.01 * entry_price  
        stop_loss = entry_price - min_buffer if (entry_price - raw_stop_loss) < min_buffer else raw_stop_loss
        signal = {
            'symbol': symbol,
            'price': float(format(entry_price, '.8f')),
            'target': float(format(target, '.8f')),
            'stop_loss': float(format(stop_loss, '.8f')),
            'strategy': 'hummingbot_rsi_fib',
            'trade_value': TRADE_VALUE
        }
        logger.info(f"✅ [RSI_Fib] تم توليد إشارة للزوج {symbol} باستخدام الاستراتيجية المحسنة:\n{signal}")
        return signal
    else:
        logger.info(f"ℹ️ [RSI_Fib] لم تتحقق شروط الإشارة للزوج {symbol} (السعر: {current_price:.8f}, RSI: {current_rsi:.2f}, SMA50: {current_sma:.8f}).")
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
    webhook_url = "https://hamza-2.onrender.com/webhook"  # تأكد من تحديث الرابط حسب النشر
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
        df['volume'] = df['volume'].astype(float)
        logger.info(f"✅ [Data] تم جلب {len(df)} صف من البيانات للزوج: {symbol}.")
        return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
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
            f"⏰ **{timestamp}**\n\n"
            f"نشر بواسطة str2hamza"
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
        full_message = f"{ltr_mark}{message}\n\nنشر بواسطة str2hamza"
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

                    df = fetch_historical_data(symbol, interval='15m', days=2)
                    if df is None or len(df) < 10:
                        logger.warning(f"⚠️ [Track] بيانات الشموع غير كافية للزوج {symbol}.")
                        continue

                    current_gain_pct = (current_price - entry) / entry

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

                    # تحديث الهدف والوقف بناءً على التحرك الإيجابي (Trailing Stop)
                    if current_gain_pct >= 0.01:
                        n = int(current_gain_pct * 100)
                        new_target = entry * (1 + (n + 1) / 100)
                        new_stop_loss = entry if n == 1 else entry * (1 + (n - 1) / 100)
                        update_flag = False
                        if new_target > target:
                            target = new_target
                            update_flag = True
                        if new_stop_loss > stop_loss:
                            stop_loss = new_stop_loss
                            update_flag = True
                        if update_flag:
                            msg = (
                                f"🔄 [Track] تحديث توصية {symbol}:\n"
                                f"▫️ سعر الدخول: ${entry:.8f}\n"
                                f"▫️ السعر الحالي: ${current_price:.8f}\n"
                                f"▫️ نسبة الزيادة: {current_gain_pct*100:.2f}%\n"
                                f"▫️ الهدف الجديد: ${target:.8f}\n"
                                f"▫️ وقف الخسارة الجديد: ${stop_loss:.8f}"
                            )
                            send_telegram_alert_special(msg)
                            cur.execute(
                                "UPDATE signals SET target = %s, stop_loss = %s WHERE id = %s",
                                (target, stop_loss, signal_id)
                            )
                            conn.commit()
                            logger.info(f"✅ [Track] تم تحديث توصية {symbol} بنجاح.")
                    else:
                        logger.info(f"ℹ️ [Track] {symbol} لم تصل نسبة الزيادة لـ 1% بعد.")
                except Exception as e:
                    logger.error(f"❌ [Track] خطأ أثناء تتبع {symbol}: {e}")
                    conn.rollback()
        except Exception as e:
            logger.error(f"❌ [Track] خطأ في خدمة تتبع الإشارات: {e}")
        time.sleep(60)
        
# ---------------------- التحقق من عدد التوصيات المفتوحة ----------------------
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

# ---------------------- تحليل السوق (فريم 1h) باستخدام استراتيجية Hummingbot المحسنة ----------------------
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
            # استخدام فريم 1 ساعة مع بيانات 4 أيام
            df_1h = fetch_historical_data(symbol, interval='1h', days=4)
            if df_1h is not None and len(df_1h) >= 50:
                signal = generate_signal_with_rsi_fib(df_1h, symbol)
                if signal:
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
                    (symbol, entry_price, target, stop_loss, r2_score, volume_15m, strategy)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    signal['symbol'],
                    signal['price'],
                    signal['target'],
                    signal['stop_loss'],
                    100,  # قيمة افتراضية للثقة
                    volume_15m,
                    signal['strategy']
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
