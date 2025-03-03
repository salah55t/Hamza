#!/usr/bin/env python
"""
📈 نظام تداول ذكي – استراتيجية التداول اليومي

تستخدم هذه النسخة استراتيجية التداول اليومي (DayTradingStrategy) المبنية على:
    - مؤشر القوة النسبية (RSI)
    - المتوسطات المتحركة الأسية (EMA_short و EMA_long)
    - متوسط المدى الحقيقي (ATR)
    - تقاطعات EMA لتحديد إشارات الدخول والخروج

تُحافظ الوظائف التالية على إرسال التوصيات والتنبيهات عبر Telegram والتقرير الشامل،
ويتم تتبع الإشارات المفعلة لتحديث وقف الخسارة وإغلاق الصفقات.
"""

import time, os, json, logging
from threading import Thread
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import talib as ta
import psycopg2
import requests
from flask import Flask, request
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance import ThreadedWebsocketManager

# ---------------------- تحميل المتغيرات البيئية ----------------------
api_key = config('BINANCE_API_KEY')
api_secret = config('BINANCE_API_SECRET')
telegram_token = config('TELEGRAM_BOT_TOKEN')
chat_id = config('TELEGRAM_CHAT_ID')
db_url = config('DATABASE_URL')
# قيمة الصفقة الثابتة للتوصيات
TRADE_VALUE = 10

# ---------------------- إعداد التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('trading_bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------------------- دالة الحصول على الوقت بتوقيت GMT+1 ----------------------
def get_gmt_plus1_time():
    return datetime.utcnow() + timedelta(hours=1)

# ---------------------- استراتيجية التداول اليومي ----------------------
class DayTradingStrategy:
    def __init__(self, rsi_period=14, rsi_overbought=70, rsi_oversold=30, 
                 ema_short=9, ema_long=21, atr_period=14, atr_multiplier=2):
        """
        تهيئة استراتيجية التداول اليومي
        
        المعلمات:
            rsi_period (int): فترة مؤشر القوة النسبية (RSI)
            rsi_overbought (int): مستوى التشبع الشرائي في RSI
            rsi_oversold (int): مستوى التشبع البيعي في RSI
            ema_short (int): فترة المتوسط المتحرك الأسي القصير
            ema_long (int): فترة المتوسط المتحرك الأسي الطويل
            atr_period (int): فترة متوسط المدى الحقيقي (ATR)
            atr_multiplier (float): مضاعف ATR لوقف الخسارة
        """
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        
    def calculate_indicators(self, df):
        """
        حساب المؤشرات الفنية على البيانات.
        
        المعلمات:
            df (DataFrame): يحتوي على OHLCV.
            
        العائد:
            DataFrame: مع المؤشرات المحسوبة.
        """
        df = df.copy()
        df['RSI'] = ta.RSI(df['close'], timeperiod=self.rsi_period)
        df['EMA_short'] = ta.EMA(df['close'], timeperiod=self.ema_short)
        df['EMA_long'] = ta.EMA(df['close'], timeperiod=self.ema_long)
        df['ATR'] = ta.ATR(df['high'], df['low'], df['close'], timeperiod=self.atr_period)
        # حساب تقاطع المتوسطات المتحركة
        df['EMA_cross'] = 0
        df.loc[(df['EMA_short'] > df['EMA_long']) & (df['EMA_short'].shift(1) <= df['EMA_long'].shift(1)), 'EMA_cross'] = 1   # تقاطع صاعد
        df.loc[(df['EMA_short'] < df['EMA_long']) & (df['EMA_short'].shift(1) >= df['EMA_long'].shift(1)), 'EMA_cross'] = -1  # تقاطع هابط
        return df

    def find_entry_exit_points(self, df):
        """
        تحديد نقاط الدخول والخروج بناءً على الاستراتيجية.
        
        المعلمات:
            df (DataFrame): يحتوي على المؤشرات الفنية.
            
        العائد:
            DataFrame: مع إشارات الدخول والخروج ووقف الخسارة وهدف الربح.
        """
        df = df.copy()
        df['entry_signal'] = 0
        df['exit_signal'] = 0
        df['stop_loss'] = 0
        df['take_profit'] = 0
        
        for i in range(1, len(df)):
            # إشارة شراء: تقاطع EMA صاعد + RSI يخرج من منطقة التشبع البيعي
            if (df['EMA_cross'].iloc[i] == 1 and 
                df['RSI'].iloc[i-1] < self.rsi_oversold and 
                df['RSI'].iloc[i] > self.rsi_oversold):
                df.loc[df.index[i], 'entry_signal'] = 1
                stop_loss = df['close'].iloc[i] - (df['ATR'].iloc[i] * self.atr_multiplier)
                take_profit = df['close'].iloc[i] + (df['ATR'].iloc[i] * self.atr_multiplier * 1.5)
                df.loc[df.index[i], 'stop_loss'] = stop_loss
                df.loc[df.index[i], 'take_profit'] = take_profit
            # إشارة بيع: تقاطع EMA هابط + RSI يخرج من منطقة التشبع الشرائي
            elif (df['EMA_cross'].iloc[i] == -1 and 
                  df['RSI'].iloc[i-1] > self.rsi_overbought and 
                  df['RSI'].iloc[i] < self.rsi_overbought):
                df.loc[df.index[i], 'entry_signal'] = -1
                stop_loss = df['close'].iloc[i] + (df['ATR'].iloc[i] * self.atr_multiplier)
                take_profit = df['close'].iloc[i] - (df['ATR'].iloc[i] * self.atr_multiplier * 1.5)
                df.loc[df.index[i], 'stop_loss'] = stop_loss
                df.loc[df.index[i], 'take_profit'] = take_profit
        return df

    def get_latest_signal(self, df):
        """
        استخراج أحدث إشارة دخول من البيانات.
        
        العائد:
            dict: يحتوي على اتجاه الإشارة، سعر الدخول، وقف الخسارة وهدف الربح؛ أو None.
        """
        if df.empty:
            return None
        last_row = df.iloc[-1]
        if last_row['entry_signal'] != 0:
            signal = {
                'direction': int(last_row['entry_signal']),
                'price': float(last_row['close']),
                'stop_loss': float(last_row['stop_loss']),
                'target': float(last_row['take_profit'])
            }
            return signal
        return None

    def run_strategy(self, df):
        """
        تشغيل الاستراتيجية بالكامل على البيانات.
        
        العائد:
            DataFrame: يحتوي على النتائج مع المؤشرات والإشارات.
        """
        df = self.calculate_indicators(df)
        df = self.find_entry_exit_points(df)
        return df

# ---------------------- تهيئة قاعدة البيانات وتحديث الأعمدة ----------------------
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
                confidence DOUBLE PRECISION,
                volume_15m DOUBLE PRECISION,
                achieved_target BOOLEAN DEFAULT FALSE,
                hit_stop_loss BOOLEAN DEFAULT FALSE,
                closed_at TIMESTAMP,
                sent_at TIMESTAMP DEFAULT NOW(),
                stage INTEGER DEFAULT 1,
                target_multiplier DOUBLE PRECISION DEFAULT 1.5,
                stop_loss_multiplier DOUBLE PRECISION DEFAULT 0.75,
                CONSTRAINT unique_symbol_time UNIQUE (symbol, sent_at)
            )
        """)
        conn.commit()
        alter_queries = [
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION DEFAULT 100",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS stage INTEGER DEFAULT 1",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS target_multiplier DOUBLE PRECISION DEFAULT 1.5",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS stop_loss_multiplier DOUBLE PRECISION DEFAULT 0.75"
        ]
        for query in alter_queries:
            cur.execute(query)
        conn.commit()
        logger.info("تم تهيئة قاعدة البيانات وتحديث الأعمدة بنجاح")
    except Exception as e:
        logger.error(f"فشل تهيئة قاعدة البيانات: {e}")
        raise

def check_db_connection():
    global conn, cur
    try:
        cur.execute("SELECT 1")
        conn.commit()
    except Exception:
        logger.warning("إعادة الاتصال بقاعدة البيانات...")
        try:
            if conn:
                conn.close()
            init_db()
        except Exception as ex:
            logger.error(f"فشل إعادة الاتصال: {ex}")
            raise

# ---------------------- إعداد عميل Binance وتحديث التيكر ----------------------
client = Client(api_key, api_secret)
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

# ---------------------- وظائف جلب البيانات ----------------------
def get_crypto_symbols():
    try:
        with open('crypto_list.txt', 'r') as f:
            symbols = [f"{line.strip().upper()}USDT" for line in f if line.strip()]
            logger.info(f"تم الحصول على {len(symbols)} زوج من العملات")
            return symbols
    except Exception as e:
        logger.error(f"خطأ في قراءة الملف: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', lookback='1 day ago UTC'):
    try:
        klines = client.get_historical_klines(symbol, interval, lookback)
        if not klines:
            return None
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
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
            btc = market_cap_percentage.get("btc", 0)
            eth = market_cap_percentage.get("eth", 0)
            logger.info(f"BTC Dominance: {btc}%, ETH Dominance: {eth}%")
            return btc, eth
        else:
            logger.error(f"خطأ في جلب نسب السيطرة: {response.status_code}")
            return 0.0, 0.0
    except Exception as e:
        logger.error(f"خطأ في get_market_dominance: {e}")
        return 0.0, 0.0

# ---------------------- إرسال التنبيهات والتقرير ----------------------
def send_telegram_alert(signal, volume, btc_dominance, eth_dominance):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        rtl_mark = "\u200F"
        message = (
            f"{rtl_mark}🚨 **إشارة تداول - {signal['symbol']} (DayTrading)**\n\n"
            f"▫️ سعر الدخول: ${signal['price']}\n"
            f"🎯 الهدف: ${signal['target']} (+{profit}%)\n"
            f"🛑 وقف الخسارة: ${signal['stop_loss']} ({loss}%)\n"
            f"💧 السيولة (15 دقيقة): {volume:,.2f} USDT\n"
            f"💵 قيمة الصفقة: ${TRADE_VALUE}\n\n"
            f"📈 **نسب السيطرة (4H):**\n"
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
        if response.status_code != 200:
            logger.error(f"فشل إرسال التنبيه للزوج {signal['symbol']}: {response.status_code}")
        else:
            logger.info(f"تم إرسال التنبيه للزوج {signal['symbol']} بنجاح")
    except Exception as e:
        logger.error(f"فشل إرسال التنبيه للزوج {signal['symbol']}: {e}")

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
        logger.info(f"رد Telegram: {response.status_code}")
    except Exception as e:
        logger.error(f"فشل إرسال التنبيه الخاص: {e}")

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
            achieved, entry, target_val, stop_loss_val = row
            if achieved:
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
        avg_profit = sum(profit_percentages)/len(profit_percentages) if profit_percentages else 0
        avg_loss = sum(loss_percentages)/len(loss_percentages) if loss_percentages else 0
        net_profit = total_profit + total_loss

        report_message = (
            f"📊 **تقرير الأداء الشامل**\n\n"
            f"✅ توصيات ناجحة: {success_count}\n"
            f"❌ توصيات بوقف الخسارة: {stop_loss_count}\n"
            f"💹 متوسط الربح: {avg_profit:.2f}%\n"
            f"📉 متوسط الخسارة: {avg_loss:.2f}%\n"
            f"💵 صافي الربح/الخسارة: ${net_profit:.2f}"
        )
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': target_chat_id, 'text': report_message, 'parse_mode': 'Markdown'}
        requests.post(url, json=payload, timeout=10)
        logger.info("تم إرسال تقرير الأداء")
    except Exception as e:
        logger.error(f"فشل إرسال تقرير الأداء: {e}")

# ---------------------- تتبع الإشارات وتحديث وقف الخسارة ----------------------
def track_signals():
    logger.info("بدء خدمة تتبع الإشارات...")
    while True:
        try:
            check_db_connection()
            cur.execute("""
                SELECT id, symbol, entry_price, target, stop_loss, stage, target_multiplier, stop_loss_multiplier
                FROM signals 
                WHERE achieved_target = FALSE AND hit_stop_loss = FALSE AND closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            logger.info(f"تم العثور على {len(active_signals)} إشارة نشطة للتتبع")
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss, stage, tgt_mult, sl_mult = signal
                try:
                    if symbol in ticker_data:
                        current_price = float(ticker_data[symbol].get('c', 0))
                    else:
                        logger.warning(f"لا يوجد تحديث أسعار لـ {symbol} من WebSocket")
                        continue
                    logger.info(f"فحص {symbol}: السعر الحالي {current_price}, الدخول {entry}, الهدف {target}, وقف الخسارة {stop_loss}, المرحلة {stage}")
                    
                    if current_price >= target:
                        df = fetch_historical_data(symbol, interval='5m', lookback='3 day ago UTC')
                        if df is None or len(df) < 50:
                            logger.warning(f"بيانات غير كافية لتحديث {symbol}")
                            continue
                        df = pd.DataFrame(df)  # التأكد من صيغة البيانات
                        df = df.astype({'open': float, 'high': float, 'low': float, 'close': float})
                        df = df.reset_index(drop=True)
                        # حساب ATR لتحديث الهدف ووقف الخسارة
                        df = ta.ATR(df['high'], df['low'], df['close'], timeperiod=14)
                        atr = df.iloc[-1] if not df.empty else 0
                        old_target = target
                        if stage == 1:
                            new_stop_loss = entry
                        else:
                            new_stop_loss = target
                        new_target = target + tgt_mult * atr
                        new_stage = stage + 1
                        msg = (
                            f"🎯 **تحديث {symbol}**\n"
                            f"• الهدف السابق: ${old_target:.8f}\n"
                            f"• وقف الخسارة الجديد: ${new_stop_loss:.8f}\n"
                            f"• الهدف الجديد: ${new_target:.8f}\n"
                            f"• المرحلة: {new_stage}\n"
                            f"⏱ {get_gmt_plus1_time().strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        try:
                            cur.execute("""
                                UPDATE signals 
                                SET target = %s, stop_loss = %s, stage = %s
                                WHERE id = %s
                            """, (new_target, new_stop_loss, new_stage, signal_id))
                            conn.commit()
                            logger.info(f"تم تحديث {symbol}: الهدف {new_target}, وقف الخسارة {new_stop_loss}, المرحلة {new_stage}")
                        except Exception as e:
                            logger.error(f"فشل تحديث {symbol}: {e}")
                            conn.rollback()
                    
                    elif current_price <= stop_loss:
                        loss_pct = ((current_price - entry) / entry) * 100
                        msg = (
                            f"🛑 **تفعيل وقف الخسارة - {symbol}**\n"
                            f"• الدخول: ${entry:.8f}\n"
                            f"• الخروج: ${current_price:.8f}\n"
                            f"• الخسارة: {loss_pct:.2f}%\n"
                            f"⏱ {get_gmt_plus1_time().strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        try:
                            cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (signal_id,))
                            conn.commit()
                            logger.info(f"تم إغلاق {symbol} بتفعيل وقف الخسارة")
                        except Exception as e:
                            logger.error(f"فشل تحديث {symbol} بعد وقف الخسارة: {e}")
                            conn.rollback()
                except Exception as e:
                    logger.error(f"خطأ في تتبع {symbol}: {e}")
                    conn.rollback()
                    continue
            time.sleep(60)
        except Exception as e:
            logger.error(f"خطأ في خدمة تتبع الإشارات: {e}")
            conn.rollback()
            time.sleep(60)

# ---------------------- تحليل السوق وإرسال التوصيات ----------------------
def analyze_market():
    logger.info("بدء تحليل السوق باستخدام استراتيجية التداول اليومي...")
    check_db_connection()
    cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
    active_count = cur.fetchone()[0]
    if active_count >= 4:
        logger.info("عدد التوصيات النشطة وصل للحد الأقصى (4). لن يتم إرسال توصيات جديدة الآن.")
        return
    btc_dom, eth_dom = get_market_dominance()
    symbols = get_crypto_symbols()
    if not symbols:
        logger.warning("لا توجد أزواج في الملف!")
        return
    for symbol in symbols:
        logger.info(f"فحص {symbol}...")
        try:
            df = fetch_historical_data(symbol, interval='5m', lookback='1 day ago UTC')
            if df is None or len(df) < 50:
                logger.warning(f"تجاهل {symbol} - بيانات تاريخية غير كافية")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 100000:
                logger.info(f"تجاهل {symbol} - سيولة منخفضة: {volume_15m:,.2f} USDT")
                continue
            # تشغيل استراتيجية التداول اليومي
            strategy = DayTradingStrategy(rsi_period=14, rsi_overbought=70, rsi_oversold=30,
                                            ema_short=9, ema_long=21, atr_period=14, atr_multiplier=2)
            df_strategy = strategy.run_strategy(df)
            latest_signal = strategy.get_latest_signal(df_strategy)
            if not latest_signal:
                logger.info(f"{symbol}: لم يتم العثور على إشارة دخول جديدة")
                continue
            # إعداد بيانات الإشارة
            signal = {
                'symbol': symbol,
                'price': latest_signal['price'],
                'target': latest_signal['target'],
                'stop_loss': latest_signal['stop_loss'],
                'confidence': 100,
                'trade_value': TRADE_VALUE,
                'stage': 1,
                'indicators': {}
            }
            logger.info(f"الشروط مستوفاة؛ سيتم إرسال تنبيه للزوج {symbol}")
            send_telegram_alert(signal, volume_15m, btc_dom, eth_dom)
            try:
                cur.execute("""
                    INSERT INTO signals 
                    (symbol, entry_price, target, stop_loss, confidence, volume_15m, stage, target_multiplier, stop_loss_multiplier)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    signal['symbol'], signal['price'], signal['target'], signal['stop_loss'],
                    signal.get('confidence', 100), volume_15m, signal['stage'], 1.5, 0.75
                ))
                conn.commit()
                logger.info(f"تم تسجيل إشارة {symbol} بنجاح")
            except Exception as e:
                logger.error(f"فشل تسجيل إشارة {symbol}: {e}")
                conn.rollback()
            time.sleep(1)
        except Exception as e:
            logger.error(f"خطأ في معالجة {symbol}: {e}")
            conn.rollback()
            continue
    logger.info("انتهى تحليل جميع الأزواج")

# ---------------------- إعداد تطبيق Flask وخدمة Webhook ----------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "نظام توصيات التداول باستخدام استراتيجية التداول اليومي يعمل بكفاءة 🚀", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logger.info("استقبال تحديث: " + str(update))
    if "callback_query" in update:
        callback_data = update["callback_query"]["data"]
        chat_id_callback = update["callback_query"]["message"]["chat"]["id"]
        if callback_data == "get_report":
            send_report(chat_id_callback)
            answer_url = f"https://api.telegram.org/bot{telegram_token}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": update["callback_query"]["id"]})
    return '', 200

def set_telegram_webhook():
    webhook_url = "https://hamza-drs4.onrender.com/webhook"
    url = f"https://api.telegram.org/bot{telegram_token}/setWebhook?url={webhook_url}"
    try:
        response = requests.get(url, timeout=10)
        res_json = response.json()
        if res_json.get("ok"):
            logger.info("تم تسجيل webhook بنجاح")
        else:
            logger.error(f"فشل تسجيل webhook: {res_json}")
    except Exception as e:
        logger.error(f"استثناء أثناء تسجيل webhook: {e}")

def test_telegram():
    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {'chat_id': chat_id, 'text': 'رسالة اختبار من البوت', 'parse_mode': 'Markdown'}
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"رد اختبار Telegram: {response.status_code}")
    except Exception as e:
        logger.error(f"فشل إرسال رسالة الاختبار: {e}")

# ---------------------- الجزء الرئيسي ----------------------
if __name__ == '__main__':
    init_db()
    set_telegram_webhook()
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    Thread(target=track_signals, daemon=True).start()
    Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000))), daemon=True).start()
    test_telegram()
    logger.info("✅ تم بدء التشغيل بنجاح باستخدام استراتيجية التداول اليومي")
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()
    
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
