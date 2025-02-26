import time
import logging
import requests
from threading import Thread
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
from config import TELEGRAM_BOT_TOKEN, TIMEZONE
from db import init_db
from market import analyze_market  # دالة تحوي عملية الفحص وإصدار التوصيات
from tracking import improved_track_signals
from telegram import send_telegram_alert_special
from binance.client import Client
from binance import ThreadedWebsocketManager

# إعداد مستوى التسجيل والـ handlers حسب الحاجة
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)
logger = logging.getLogger(__name__)

# تهيئة التطبيق
app = Flask(__name__)

@app.route('/')
def home():
    return "نظام توصيات التداول اليومي يعمل بكفاءة", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update or "callback_query" not in update:
        logger.warning("تحديث Webhook غير صالح")
        return '', 400
    # معالجة الكول باك حسب الحاجة...
    return '', 200

def run_flask():
    app.run(host='0.0.0.0', port=5000)

def run_ticker_socket_manager(api_key, api_secret):
    while True:
        try:
            twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret)
            twm.start()
            twm.start_miniticker_socket(callback=lambda msg: None)  # يمكنك تعديل callback حسب الحاجة
            logger.info("تم تشغيل WebSocket")
            twm.join()
        except Exception as e:
            logger.error(f"خطأ في WebSocket، إعادة المحاولة: {e}")
            time.sleep(5)

if __name__ == '__main__':
    init_db()
    # تسجيل webhook للتلغرام (اختياري)
    set_telegram_webhook_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook?url=https://your-app-url/webhook"
    try:
        response = requests.get(set_telegram_webhook_url, timeout=10)
        if response.json().get("ok"):
            logger.info("تم تسجيل webhook بنجاح")
        else:
            logger.error("فشل تسجيل webhook")
    except Exception as e:
        logger.error(f"استثناء أثناء تسجيل webhook: {e}")

    # تشغيل المهام بالخيوط
    Thread(target=run_ticker_socket_manager, args=(Client.api_key, Client.api_secret), daemon=True).start()
    # يُفترض أن last_price_update و fetch_historical_data تم تعريفهما في market.py
    from market import fetch_historical_data
    last_price_update = {}  # يجب تحديثها عبر WebSocket
    Thread(target=improved_track_signals, args=(last_price_update, fetch_historical_data), daemon=True).start()
    Thread(target=run_flask, daemon=True).start()
    scheduler = BackgroundScheduler()
    from market import get_crypto_symbols, fetch_recent_volume, get_market_dominance
    from strategy import generate_improved_signal
    def analyze_market():
        # تنفيذ فحص السوق وإصدار التوصيات مع الاحتفاظ بالـ logs المهمة فقط
        # (يمكنك استخدام الدالة المعدلة من market.py)
        pass
    scheduler.add_job(analyze_market, 'interval', minutes=10)
    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("إيقاف النظام")
