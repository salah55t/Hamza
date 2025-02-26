import time
import numpy as np
import pandas as pd
import requests
from config import TRADE_VALUE, TIMEZONE, BINANCE_API_KEY, BINANCE_API_SECRET
from binance.client import Client
import logging

logger = logging.getLogger(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

def get_crypto_symbols():
    try:
        exchange_info = client.get_exchange_info()
        symbols = [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        filtered = []
        for symbol in symbols:
            volume = fetch_recent_volume(symbol)
            if volume > 50000:
                filtered.append(symbol)
        logger.info(f"تم جلب {len(filtered)} زوج USDT بعد الفلترة")
        return filtered
    except Exception as e:
        logger.error(f"خطأ في جلب الأزواج: {e}")
        return []

def fetch_historical_data(symbol, interval='5m', days=3):
    for attempt in range(3):
        try:
            klines = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume',
                                                 'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                                                 'taker_buy_quote', 'ignore'])
            df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].astype(float)
            logger.info(f"{symbol}: تم جلب {len(df)} صف من البيانات التاريخية")
            return df
        except Exception as e:
            logger.error(f"{symbol}: خطأ في جلب البيانات (محاولة {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    logger.error(f"{symbol}: فشل جلب البيانات بعد 3 محاولات")
    return None

def fetch_recent_volume(symbol):
    try:
        klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "15 minutes ago UTC")
        volume = sum(float(k[5]) for k in klines)
        return volume
    except Exception as e:
        logger.error(f"{symbol}: خطأ في جلب حجم السيولة: {e}")
        return 0

def get_market_dominance():
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url, timeout=10)
        data = response.json().get("data", {})
        market_cap_percentage = data.get("market_cap_percentage", {})
        return market_cap_percentage.get("btc"), market_cap_percentage.get("eth")
    except Exception as e:
        logger.error(f"خطأ في get_market_dominance: {e}")
        return None, None
