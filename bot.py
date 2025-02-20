import time
import os
import pandas as pd
import numpy as np
import logging
import requests
import json
from threading import Thread
from decouple import config
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
from binance import ThreadedWebsocketManager

# ---------------------- إعدادات التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('crypto_simulator.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------------------- تحميل المتغيرات البيئية ----------------------
api_key = config('BINANCE_API_KEY')
api_secret = config('BINANCE_API_SECRET')
# قيمة الصفقة الثابتة الافتراضية للتداول الشبكي
TRADE_VALUE = 10

# ---------------------- إعداد عميل Binance وتحديثات التيكر ----------------------
client = Client(api_key, api_secret)
ticker_data = {}  # سيخزن بيانات التيكر المُحدّثة لكل رمز

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

def fetch_historical_data(symbol, interval='15m', days=2):
    """
    جلب بيانات الشموع التاريخية مع استخدام ذاكرة تخزين مؤقت بسيطة لمدة 5 دقائق.
    """
    cache_duration = 300  # 5 دقائق
    current_time = time.time()
    if symbol in fetch_historical_data.cache:
        cached_timestamp, cached_df = fetch_historical_data.cache[symbol]
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
        fetch_historical_data.cache[symbol] = (current_time, df[['timestamp', 'open', 'high', 'low', 'close']])
        return fetch_historical_data.cache[symbol][1]
    except Exception as e:
        logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
        return None
fetch_historical_data.cache = {}

def fetch_recent_volume(symbol):
    """
    جلب حجم السيولة لآخر 15 دقيقة مع ذاكرة تخزين مؤقت لمدة 30 ثانية.
    """
    cache_duration = 30  # 30 ثانية
    current_time = time.time()
    if symbol in fetch_recent_volume.cache:
        cached_timestamp, cached_volume = fetch_recent_volume.cache[symbol]
        if current_time - cached_timestamp < cache_duration:
            logger.info(f"استخدام حجم السيولة المؤقت للزوج {symbol}")
            return cached_volume
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        logger.info(f"حجم السيولة للزوج {symbol} في آخر 15 دقيقة: {volume:,.2f} USDT")
        fetch_recent_volume.cache[symbol] = (current_time, volume)
        return volume
    except Exception as e:
        logger.error(f"خطأ في جلب حجم {symbol}: {e}")
        return 0
fetch_recent_volume.cache = {}

# ---------------------- استراتيجية التداول الشبكي الافتراضية ----------------------
def generate_grid_signal(df, symbol):
    """
    تحليل بيانات اليوم (آخر 24 ساعة) على فريم 15 دقيقة لتحديد ما إذا كان السوق عرضياً.
    إذا كان السعر الحالي صفرًا أو غير صالح، يتم تسجيل الخطأ وتجاهل الحساب.
    
    تُحسب مستويات شبكة (grid) من 5 مستويات.
    يتم إنشاء الإشارة في حال كان السعر الحالي قريباً (ضمن 1%) من أدنى مستوى في الشبكة.
    """
    # استخدام بيانات اليوم (آخر 24 ساعة)
    day_df = df.tail(96) if len(df) >= 96 else df
    current_price = day_df['close'].iloc[-1]
    
    # التحقق من أن السعر الحالي أكبر من صفر
    if current_price <= 0:
        logger.error(f"سعر الإغلاق للزوج {symbol} صفر أو غير صالح، تجاهل الحساب.")
        return None

    avg_high = day_df['high'].mean()
    avg_low = day_df['low'].mean()
    mid_price = (avg_high + avg_low) / 2

    # التأكد من أن السوق عرضي (نطاق صغير أقل من 5% من المتوسط)
    if (avg_high - avg_low) / mid_price > 0.05:
        logger.info(f"تجاهل {symbol} - السوق ليس عرضياً (النطاق {(avg_high - avg_low):.4f} غير مناسب)")
        return None

    # إعداد شبكة من 5 مستويات
    grid_count = 5
    grid_spacing = (avg_high - avg_low) / (grid_count - 1)
    grid_levels = [avg_low + i * grid_spacing for i in range(grid_count)]
    
    # تحديد مستوى الدخول: اختيار أقرب مستوى أقل من السعر الحالي
    entry_index = 0
    for i, level in enumerate(grid_levels):
        if current_price >= level:
            entry_index = i

    # شرط: يجب أن يكون السعر الحالي قريباً (ضمن 1%) من المستوى الأدنى المحدد
    if current_price > grid_levels[entry_index] * 1.01:
        logger.info(f"تجاهل {symbol} - السعر الحالي {current_price:.4f} ليس قريباً من مستوى الشبكة السفلي {grid_levels[entry_index]:.4f}")
        return None

    entry_price = current_price
    target = grid_levels[-1]
    stop_loss = grid_levels[0] * 0.99  # وقف خسارة بنسبة 1% تحت أدنى مستوى في الشبكة

    signal = {
        'symbol': symbol,
        'entry_price': float(format(entry_price, '.4f')),
        'target': float(format(target, '.4f')),
        'stop_loss': float(format(stop_loss, '.4f')),
        'grid_levels': grid_levels,
        'grid_index': entry_index,  # الفهرس الحالي في الشبكة
        'trade_value': TRADE_VALUE,
        'strategy': 'GridTrading',
        'closed': False
    }
    return signal

def simulate_grid_trade(signal):
    """
    تحاكي هذه الدالة حركة الصفقة الافتراضية باستخدام مستويات الشبكة.
    - إذا تجاوز السعر المستوى التالي في الشبكة (بيع افتراضي) يتم تسجيل ربح وتحديث الفهرس.
    - إذا انخفض السعر دون المستوى الحالي (إعادة شراء افتراضية) يتم تحديث الفهرس لتقليل متوسط التكلفة.
    - تُغلق الصفقة عند تحقيق الهدف (أعلى مستوى) أو عند الوصول إلى وقف الخسارة.
    """
    symbol = signal['symbol']
    if symbol not in ticker_data:
        logger.warning(f"لا توجد بيانات تيكر حالية للزوج {symbol}")
        return signal  # لا يوجد تحديث للسعر

    current_price = float(ticker_data[symbol].get('c', 0))
    grid_levels = signal['grid_levels']
    index = signal['grid_index']

    # محاكاة البيع عند الصعود في الشبكة
    if index < len(grid_levels) - 1 and current_price >= grid_levels[index + 1]:
        profit = (grid_levels[index + 1] - grid_levels[index]) * (TRADE_VALUE / grid_levels[index])
        logger.info(f"{symbol}: تجاوز السعر {grid_levels[index+1]:.4f} – بيع افتراضي، ربح ${profit:.2f}")
        signal['grid_index'] += 1

    # محاكاة إعادة الشراء عند الهبوط
    elif index > 0 and current_price < grid_levels[index]:
        loss = (grid_levels[index] - current_price) * (TRADE_VALUE / grid_levels[index])
        logger.info(f"{symbol}: انخفاض السعر تحت {grid_levels[index]:.4f} – إعادة شراء افتراضية، خسارة تقريبية ${loss:.2f}")
        signal['grid_index'] -= 1

    # فحص وقف الخسارة
    if current_price <= signal['stop_loss']:
        logger.info(f"{symbol}: وصل السعر إلى وقف الخسارة {signal['stop_loss']:.4f}. إغلاق الصفقة بخسارة.")
        signal['closed'] = True
        signal['result'] = 'stop_loss'

    # فحص تحقيق الهدف (أعلى مستوى في الشبكة)
    if signal['grid_index'] == len(grid_levels) - 1:
        logger.info(f"{symbol}: تم الوصول إلى الهدف {signal['target']:.4f}. إغلاق الصفقة بربح.")
        signal['closed'] = True
        signal['result'] = 'profit'

    return signal

# ---------------------- إدارة الصفقات الافتراضية ----------------------
# تخزين الصفقات النشطة (لا يزيد عددها عن 4)
active_trades = {}  # المفتاح: الرمز، والقيمة: بيانات الصفقة (signal)

def analyze_market():
    """
    تفحص هذه الدالة الأزواج من الملف، وتستخدم البيانات التاريخية وحجم السيولة لتوليد إشارة تداول.
    في حال تحقق الشروط وتكون الصفقات النشطة أقل من 4، يتم فتح صفقة افتراضية جديدة.
    """
    if len(active_trades) >= 4:
        logger.info("عدد الصفقات النشطة وصل إلى الحد الأقصى (4). لن يتم فتح صفقة جديدة.")
        return

    symbols = get_crypto_symbols()
    for symbol in symbols:
        logger.info(f"فحص الزوج {symbol}...")
        df = fetch_historical_data(symbol)
        if df is None or len(df) < 96:
            logger.info(f"{symbol}: بيانات تاريخية غير كافية.")
            continue
        volume_15m = fetch_recent_volume(symbol)
        if volume_15m < 40000:
            logger.info(f"{symbol}: سيولة منخفضة ({volume_15m:,.2f} USDT).")
            continue
        signal = generate_grid_signal(df, symbol)
        if not signal:
            continue
        if symbol in active_trades:
            logger.info(f"{symbol}: صفقة مفتوحة بالفعل.")
            continue
        # محاكاة الشراء الافتراضي بقيمة 10 دولار
        logger.info(f"{symbol}: تنفيذ شراء افتراضي بقيمة ${TRADE_VALUE} عند السعر {signal['entry_price']}")
        active_trades[symbol] = signal
        if len(active_trades) >= 4:
            break

def simulation_loop():
    """
    حلقة متكررة لتحديث حالة الصفقات الافتراضية عبر محاكاة حركة السعر ضمن شبكة التداول.
    تُحدّث الصفقات المفتوحة وتُغلق عند تحقيق الهدف أو تفعيل وقف الخسارة.
    """
    while True:
        symbols_to_close = []
        for symbol, trade in active_trades.items():
            updated_trade = simulate_grid_trade(trade)
            active_trades[symbol] = updated_trade
            if updated_trade.get('closed'):
                logger.info(f"{symbol}: الصفقة أُغلقت، النتيجة: {updated_trade.get('result')}")
                symbols_to_close.append(symbol)
        # إزالة الصفقات المُغلقة من القاموس
        for s in symbols_to_close:
            del active_trades[s]
        time.sleep(10)  # تحديث كل 10 ثوانٍ

# ---------------------- التشغيل الرئيسي ----------------------
if __name__ == '__main__':
    # بدء تحديث التيكر عبر WebSocket في مسار منفصل
    Thread(target=run_ticker_socket_manager, daemon=True).start()
    # بدء حلقة المحاكاة لتحديث الصفقات الافتراضية
    Thread(target=simulation_loop, daemon=True).start()

    # جدولة فحص السوق بشكل دوري (كل 5 دقائق)
    scheduler = BackgroundScheduler()
    scheduler.add_job(analyze_market, 'interval', minutes=5)
    scheduler.start()

    logger.info("✅ بدأ تشغيل المحاكي للاستراتيجيات الافتراضية!")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()
        logger.info("تم إيقاف المحاكي.")
