import json
import requests
import logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

def send_telegram_alert(signal, volume, btc_dominance, eth_dominance):
    try:
        profit = round((signal['target'] / signal['price'] - 1) * 100, 2)
        loss = round((signal['stop_loss'] / signal['price'] - 1) * 100, 2)
        message = (
            f"🌟 **توصية تداول - {signal['symbol']}**\n"
            f"💰 سعر الدخول: ${signal['price']}\n"
            f"🎯 الهدف: ${signal['target']} (+{profit}%)\n"
            f"🛑 وقف الخسارة: ${signal['stop_loss']} ({loss}%)\n"
            f"🔄 وقف الخسارة المتحرك: ${signal['dynamic_stop_loss']}\n"
            f"⚖️ نسبة المخاطرة/العائد: {signal['risk_reward_ratio']:.2f}\n"
        )
        reply_markup = {"inline_keyboard": [[{"text": "عرض التقرير", "callback_data": "get_report"}]]}
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "reply_markup": json.dumps(reply_markup)}
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"{signal['symbol']}: تم إرسال التوصية بنجاح")
        else:
            logger.error(f"{signal['symbol']}: فشل إرسال التوصية: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في send_telegram_alert: {e}")

def send_telegram_alert_special(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("تم إرسال التنبيه الخاص بنجاح")
        else:
            logger.error(f"فشل إرسال التنبيه الخاص: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في send_telegram_alert_special: {e}")
