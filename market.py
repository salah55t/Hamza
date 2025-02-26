import time
import logging
from db import get_db_connection, release_db_connection
from strategy import generate_enhanced_trading_signal  # تعديل الاستيراد هنا من strategy.py
from telegram import send_telegram_alert
from config import TRADE_VALUE

logger = logging.getLogger(__name__)

# تعريف دوال السوق المستخدمة
def get_crypto_symbols():
    """
    هنا يتم تنفيذ منطق جلب الأزواج المناسبة.
    يمكن تعديل هذه الدالة لتعمل وفقاً لاحتياجاتك.
    """
    return ["BTCUSDT", "ETHUSDT"]

def fetch_historical_data(symbol, interval='5m', days=3):
    """
    هنا يتم تنفيذ منطق جلب البيانات التاريخية للزوج.
    في هذا المثال نعيد DataFrame تجريبي باستخدام pandas.
    """
    import pandas as pd
    data = {
        "timestamp": [i for i in range(60)],
        "open": [100 + i for i in range(60)],
        "high": [105 + i for i in range(60)],
        "low": [95 + i for i in range(60)],
        "close": [102 + i for i in range(60)],
        "volume": [1000 + i*10 for i in range(60)]
    }
    return pd.DataFrame(data)

def fetch_recent_volume(symbol):
    """
    تنفيذ منطق جلب حجم السيولة للزوج في الفترة الأخيرة.
    هنا نعيد قيمة ثابتة للتجربة.
    """
    return 60000

def get_market_dominance():
    """
    تنفيذ منطق جلب نسب سيطرة السوق.
    هنا نعيد قيم ثابتة للتجربة.
    """
    return 45.0, 18.0

def analyze_market():
    """
    تقوم هذه الدالة بفحص السوق من خلال:
    - التأكد من أن عدد التوصيات المفتوحة أقل من الحد المسموح.
    - جلب الأزواج المناسبة وفحص البيانات التاريخية وحجم السيولة.
    - توليد توصيات التداول وإرسال التنبيهات وحفظ التوصية في قاعدة البيانات.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # التأكد من عدم وجود أكثر من 4 توصيات مفتوحة
        cur.execute("SELECT COUNT(*) FROM signals WHERE closed_at IS NULL")
        active_signals_count = cur.fetchone()[0]
        if active_signals_count >= 4:
            logger.info("الحد الأقصى للتوصيات المفتوحة (4) تم الوصول إليه")
            return

        btc_dominance, eth_dominance = get_market_dominance() or (0.0, 0.0)
        symbols = get_crypto_symbols()
        for symbol in symbols:
            df = fetch_historical_data(symbol)
            if df is None or len(df) < 100:
                logger.info(f"{symbol}: رفض التوصية - البيانات التاريخية غير كافية")
                continue
            volume_15m = fetch_recent_volume(symbol)
            if volume_15m < 50000:
                logger.info(f"{symbol}: رفض التوصية - السيولة ({volume_15m:,.2f} USDT) أقل من 50000")
                continue
            signal = generate_enhanced_trading_signal(df, symbol, TRADE_VALUE)
            if signal:
                send_telegram_alert(signal, volume_15m, btc_dominance, eth_dominance)
                cur.execute(
                    """
                    INSERT INTO signals 
                    (symbol, entry_price, target, stop_loss, dynamic_stop_loss, r2_score, volume_15m, risk_reward_ratio)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        signal['symbol'],
                        float(signal['price']),
                        float(signal['target']),
                        float(signal['stop_loss']),
                        float(signal['dynamic_stop_loss']),
                        int(signal.get('confidence', 100)),
                        float(volume_15m),
                        float(signal['risk_reward_ratio'])
                    )
                )
                conn.commit()
                logger.info(f"{symbol}: توصية جديدة تم حفظها في قاعدة البيانات")
            else:
                logger.info(f"{symbol}: لم يتم توليد توصية")
            time.sleep(1)
    except Exception as e:
        logger.error(f"خطأ في analyze_market: {e}")
        conn.rollback()
    finally:
        release_db_connection(conn)
