import time
from datetime import datetime
import numpy as np
from db import get_db_connection, release_db_connection
from indicators import calculate_atr_indicator
from strategy import select_best_target_level
from config import TIMEZONE
import logging
from telegram import send_telegram_alert_special  # نفترض وجود هذه الوظيفة في telegram.py

logger = logging.getLogger(__name__)

def improved_track_signals(last_price_update, fetch_historical_data):
    logger.info("بدء خدمة تتبع التوصيات المفتوحة")
    while True:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, symbol, entry_price, target, stop_loss, dynamic_stop_loss, sent_at  
                FROM signals
                WHERE achieved_target = FALSE AND hit_stop_loss = FALSE AND closed_at IS NULL
            """)
            active_signals = cur.fetchall()
            for signal in active_signals:
                signal_id, symbol, entry, target, stop_loss, dynamic_stop_loss, sent_at = signal
                current_price = last_price_update.get(symbol)
                if current_price is None:
                    continue

                df = fetch_historical_data(symbol, interval='5m', days=1)
                if df is None or len(df) < 20:
                    logger.info(f"{symbol}: تجاهل التوصية - البيانات التاريخية غير كافية")
                    continue

                df = calculate_atr_indicator(df)
                current_atr = df['atr'].iloc[-1]
                time_in_trade = (datetime.now(TIMEZONE) - sent_at).total_seconds() / 3600
                price_change_pct = (current_price - entry) / entry * 100

                # تحديث وقف الخسارة المتحرك
                if current_price > entry:
                    pct_based_stop = entry + (current_price - entry) * 0.5
                    atr_based_stop = current_price - current_atr * 1.5
                    time_based_stop = entry + (current_price - entry) * min(0.8, time_in_trade / 24)
                    fib_based_stop = entry + (current_price - entry) * (0.382 if price_change_pct > 3 else 0.236)
                    candidate_stops = [dynamic_stop_loss, pct_based_stop, atr_based_stop, time_based_stop, fib_based_stop, stop_loss]
                    new_dynamic_stop_loss = max(candidate_stops)
                    if new_dynamic_stop_loss > dynamic_stop_loss * 1.005:
                        cur.execute("UPDATE signals SET dynamic_stop_loss = %s WHERE id = %s", 
                                    (float(new_dynamic_stop_loss), int(signal_id)))
                        conn.commit()
                        logger.info(f"{symbol}: تحديث وقف الخسارة المتحرك من {dynamic_stop_loss:.8f} إلى {new_dynamic_stop_loss:.8f}")
                        if new_dynamic_stop_loss > dynamic_stop_loss * 1.05:
                            msg = (
                                f"📊 **تحديث وقف الخسارة - {symbol}**\n"
                                f"💰 سعر الدخول: ${entry:.8f}\n"
                                f"💹 السعر الحالي: ${current_price:.8f}\n"
                                f"🛡️ وقف الخسارة الجديد: ${new_dynamic_stop_loss:.8f}\n"
                                f"📈 الربح المضمون: +{((new_dynamic_stop_loss - entry) / entry * 100):.2f}%\n"
                                f"⏰ الوقت: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                            )
                            send_telegram_alert_special(msg)
                else:
                    new_dynamic_stop_loss = stop_loss

                # إعادة حساب الهدف المتحرك
                try:
                    new_resistance = df['high'].rolling(window=20).max().iloc[-1]
                    new_support = df['low'].rolling(window=20).min().iloc[-1]
                    new_price_range = new_resistance - new_support
                    new_fib_levels = [current_price + new_price_range * level for level in [0.382, 0.618, 0.786]]
                    recent_highs = np.sort(df['high'].tail(20).values)
                    new_target = select_best_target_level(current_price, new_fib_levels, recent_highs)
                    if new_target and abs(new_target - target) / target > 0.01:
                        cur.execute("UPDATE signals SET target = %s WHERE id = %s", (float(new_target), int(signal_id)))
                        conn.commit()
                        logger.info(f"{symbol}: تغيير الهدف من {target:.8f} إلى {new_target:.8f}")
                        msg = (
                            f"🔄 **تغيير الهدف - {symbol}**\n"
                            f"🎯 الهدف القديم: ${target:.8f}\n"
                            f"🎯 الهدف الجديد: ${new_target:.8f}\n"
                            f"⏰ الوقت: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                        )
                        send_telegram_alert_special(msg)
                        target = new_target
                except Exception as e:
                    logger.error(f"{symbol}: خطأ في إعادة حساب الهدف: {e}")

                # إغلاق الصفقة عند تحقيق الهدف أو تفعيل وقف الخسارة
                if current_price >= target:
                    profit = ((current_price - entry) / entry) * 100
                    msg = (
                        f"🎉 **نجاح! تحقيق الهدف - {symbol}**\n"
                        f"💰 الدخول: ${entry:.8f}\n"
                        f"✅ الخروج: ${current_price:.8f}\n"
                        f"📈 الربح: +{profit:.2f}%\n"
                        f"⏰ الوقت: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                    )
                    send_telegram_alert_special(msg)
                    cur.execute("UPDATE signals SET achieved_target = TRUE, closed_at = NOW() WHERE id = %s", (int(signal_id),))
                    conn.commit()
                    logger.info(f"{symbol}: الصفقة أغلقت بتحقيق الهدف")
                elif current_price <= new_dynamic_stop_loss:
                    loss = ((current_price - entry) / entry) * 100
                    msg = (
                        f"⚠️ **تنبيه: تفعيل وقف الخسارة - {symbol}**\n"
                        f"💰 الدخول: ${entry:.8f}\n"
                        f"❌ الخروج: ${current_price:.8f}\n"
                        f"📉 التغيير: {loss:.2f}%\n"
                        f"⏰ الوقت: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                    )
                    send_telegram_alert_special(msg)
                    cur.execute("UPDATE signals SET hit_stop_loss = TRUE, closed_at = NOW() WHERE id = %s", (int(signal_id),))
                    conn.commit()
                    logger.info(f"{symbol}: الصفقة أغلقت بتفعيل وقف الخسارة")
        except Exception as e:
            logger.error(f"خطأ في تتبع الإشارات: {e}")
            conn.rollback()
        finally:
            release_db_connection(conn)
        time.sleep(90)
