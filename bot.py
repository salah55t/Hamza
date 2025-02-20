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
        logger.info("تم تهيئة قاعدة البيانات بنجاح")
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
    logger.info(f"تم حساب RSI: {rsi.iloc[-1]:.2f}")
    return rsi

def calculate_atr_indicator(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    logger.info(f"تم حساب ATR: {df['atr'].iloc[-1]:.8f}")
    return df

# ---------------------- تعريف استراتيجية نموذج المنحنى البارابولي ----------------------
class ParabolicCurveStrategy:
    """
    استراتيجية نموذج المنحنى البارابولي (Parabolic Curve Pattern)
    يُعرف هذا النمط أيضًا باسم "الخمس تدبيلات"، حيث يمر السعر بخمس مراحل:
      1. صعود قوي.
      2. تصحيح أو حركة عرضية.
      3. صعود أقوى مع زخم عالي.
      4. تصحيح آخر.
      5. تأكيد استمرار الصعود.
    تُعتبر الشمعة الخامسة إشارة دخول قوية إذا توافرت جميع الشروط.
    """
    stoploss = -0.02
    minimal_roi = {"0": 0.02}

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 5:
            return df
        # حساب نسبة التغير لكل شمعة لتسهيل التحليل
        df['pct_change'] = df['close'].pct_change() * 100
        # حساب ATR لاستخدامه في تحديد الهدف ووقف الخسارة
        df = calculate_atr_indicator(df)
        return df

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 5:
            return df
        recent = df.iloc[-5:]
        # الشمعة الأولى: صعود قوي (أكثر من 1%)
        cond1 = (recent.iloc[0]['close'] > recent.iloc[0]['open']) and (((recent.iloc[0]['close'] - recent.iloc[0]['open']) / recent.iloc[0]['open']) > 0.01)
        # الشمعة الثانية: حركة تصحيحية (إغلاق أقل من الشمعة الأولى)
        cond2 = recent.iloc[1]['close'] < recent.iloc[0]['close']
        # الشمعة الثالثة: صعود أقوى؛ يغلق السعر أعلى من سعر الشمعة الأولى بنسبة تزيد عن 2%
        cond3 = (recent.iloc[2]['close'] > recent.iloc[0]['close']) and (((recent.iloc[2]['close'] - recent.iloc[0]['close']) / recent.iloc[0]['close']) > 0.02)
        # الشمعة الرابعة: حركة تصحيحية بعد الصعود القوي (إغلاق أقل من الشمعة الثالثة)
        cond4 = recent.iloc[3]['close'] < recent.iloc[2]['close']
        # الشمعة الخامسة: تأكيد استمرار الصعود (شمعة صاعدة)
        cond5 = recent.iloc[4]['close'] > recent.iloc[4]['open']
        if cond1 and cond2 and cond3 and cond4 and cond5:
            df.loc[df.index[-1], 'buy'] = 1
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        # شرط خروج مبسط: إذا هبط السعر دون متوسط حركة بسيطة للشموع الثلاث الأخيرة
        if len(df) < 3:
            return df
        df['sma3'] = df['close'].rolling(window=3).mean()
        conditions = df['close'] < df['sma3']
        df.loc[conditions, 'sell'] = 1
        return df

# ---------------------- دالة توليد الإشارة باستخدام استراتيجية المنحنى البارابولي ----------------------
def generate_signal_using_freqtrade_strategy(df, symbol):
    df = df.dropna().reset_index(drop=True)
    if len(df) < 5:
        return None

    # استخدام استراتيجية المنحنى البارابولي بدلاً من Freqtrade
    strategy = ParabolicCurveStrategy()
    df = strategy.populate_indicators(df)
    df = strategy.populate_buy_trend(df)
    last_row = df.iloc[-1]
    # التحقق من وجود إشارة شراء في آخر صف
    if last_row.get('buy', 0) == 1:
        current_price = last_row['close']
        current_atr = last_row['atr']
        # استخدام مضاعف ATR أكبر نظرًا لطبيعة النمط
        atr_multiplier = 2.0
        target = current_price + atr_multiplier * current_atr
        stop_loss = current_price * 0.98

        profit_margin = (target / current_price - 1) * 100
        if profit_margin < 1:
            target = current_price * 1.01

        signal = {
            'symbol': symbol,
            'price': float(format(current_price, '.8f')),
            'target': float(format(target, '.8f')),
            'stop_loss': float(format(stop_loss, '.8f')),
            'strategy': 'parabolic_curve_pattern',
            'indicators': {
                'atr': current_atr,
            },
            'trade_value': TRADE_VALUE
        }
        logger.info(f"تم توليد إشارة من استراتيجية المنحنى البارابولي للزوج {symbol}: {signal}")
        return signal
    else:
        logger.info(f"[{symbol}] الشروط غير مستوفاة في استراتيجية المنحنى البارابولي")
        return None

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
    webhook_url = "https://hamza-1.onrender.com/webhook"
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

# ---------------------- وظائف تحليل البيانات ----------------------
def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip()]
            logger.info(f"تم الحصول على {len(symbols)} زوج من العملات")
            return symbols
    except Exception as e:
        logger.error(f"خطأ في قراءة الملف: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=3):
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
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"حجم السيولة للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT")
        return volume
    except Exception as e:
        logger.error(f"خطأ في جلب حجم {symbol}: {e}")
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
            logger.info(f"BTC Dominance: {btc_dominance}%, ETH Dominance: {eth_dominance}%")
            return btc_dominance, eth_dominance
        else:
            logger.error(f"خطأ في جلب نسب السيطرة: {response.status_code} {response.text}")
            return None, None
    except Exception as e:
        logger.error(f"خطأ في get_market_dominance: {e}")
        return None, None

# ---------------------- إرسال التنبيهات عبر Telegram ----------------------
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
            f"📏 ATR: {signal['indicators'].get('atr', 'N/A')}\n"
            f"💧 السيولة (15 دقيقة): {volume:,.2f} USDT\n"
            f"💵 قيمة الصفقة: ${TRADE_VALUE}\n\n"
            f"📈 **نسب السيطرة على السوق (4H):**\n"
            f"   - BTC: {btc_dominance:.2f}%\n"
            f"   - ETH: {eth_dominance:.2f}%\n\n"
            f"⏰ {get_gmt_plus1_time().strftime('%Y-%m-%d %H:%M')}"

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

# ---------------------- خدمة تتبع الإشارات ----------------------
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

# ---------------------- فحص الأزواج بشكل دوري ----------------------
def analyze_market():
    logger.info("بدء فحص الأزواج الآن...")
    check_db_connection()
    
    cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
    active_signals_count = cur.fetchone()[0]
    if active_signals_count >= 4:
        logger.info("عدد التوصيات النشطة وصل إلى الحد الأقصى (4). لن يتم إرسال توصيات جديدة حتى إغلاق توصية حالية.")
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

            # استخدام استراتيجية المنحنى البارابولي لتوليد الإشارة
            signal = generate_signal_using_freqtrade_strategy(df, symbol)
            if not signal:
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
                    signal.get('confidence', 100),
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
