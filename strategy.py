import numpy as np
import pandas as pd
from indicators import (calculate_atr_indicator, calculate_macd_indicator,
                        calculate_rsi_indicator, calculate_stochastic, calculate_ema)

import logging

logger = logging.getLogger(__name__)

class ImprovedDayTradingStrategy:
    stoploss = -0.015
    minimal_roi = {"0": 0.008, "30": 0.005, "60": 0.003}

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df['volume_change'] = df['volume'].pct_change().rolling(window=3).mean()
        df['price_momentum'] = df['close'].diff(3).rolling(window=5).mean()
        df['volatility'] = df['high'].div(df['low']).rolling(window=10).mean()
        df['ema5'] = calculate_ema(df['close'], 5)
        df['ema13'] = calculate_ema(df['close'], 13)
        df['ema21'] = calculate_ema(df['close'], 21)
        df['rsi'] = calculate_rsi_indicator(df, period=7)
        df['rsi_divergence'] = df['rsi'].diff(3)
        df['ma20'] = df['close'].rolling(window=20).mean()
        std20 = df['close'].rolling(window=20).std()
        df['upper_band'] = df['ma20'] + (std20 * 2)
        df['lower_band'] = df['ma20'] - (std20 * 2)
        df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
        df = calculate_atr_indicator(df, period=7)
        df = calculate_macd_indicator(df)
        df = calculate_stochastic(df)
        df['resistance'] = df['high'].rolling(window=20).max()
        df['support'] = df['low'].rolling(window=20).min()
        return df

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema5'] > df['ema13']) &
            (df['rsi'].between(25, 75)) &
            (df['macd'] > df['macd_signal']) &
            (df['%K'] > df['%D'])
        )
        df.loc[conditions, 'buy'] = 1
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        conditions = (
            (df['ema5'] < df['ema13']) |
            (df['rsi'] > 80) |
            (df['macd'] < df['macd_signal']) |
            (df['%K'] < df['%D'])
        )
        df.loc[conditions, 'sell'] = 1
        return df

def analyze_candle_pattern(df: pd.DataFrame) -> dict:
    last_candles = df.iloc[-3:].copy()
    last_candles['body'] = abs(last_candles['close'] - last_candles['open'])
    last_candles['upper_shadow'] = last_candles['high'] - last_candles[['open', 'close']].max(axis=1)
    last_candles['lower_shadow'] = last_candles[['open', 'close']].min(axis=1) - last_candles['low']
    doji = last_candles.iloc[-1]['body'] < (last_candles.iloc[-1]['high'] - last_candles.iloc[-1]['low']) * 0.1
    hammer = (last_candles.iloc[-1]['lower_shadow'] > last_candles.iloc[-1]['body'] * 2 and
              last_candles.iloc[-1]['upper_shadow'] < last_candles.iloc[-1]['body'] * 0.5)
    bullish_engulfing = (last_candles.iloc[-2]['close'] < last_candles.iloc[-2]['open'] and
                         last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open'] and
                         last_candles.iloc[-1]['open'] < last_candles.iloc[-2]['close'] and
                         last_candles.iloc[-1]['close'] > last_candles.iloc[-2]['open'])
    pattern = {
        'doji': doji,
        'hammer': hammer,
        'bullish_engulfing': bullish_engulfing,
        'bullish': hammer or bullish_engulfing or (not doji and last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open'])
    }
    return pattern

def select_best_target_level(current_price, fib_levels, recent_highs):
    for level in fib_levels:
        for high in recent_highs:
            if abs(high - level) / level < 0.01:
                return min(level, high * 0.998)
    return fib_levels[1]

def calculate_confidence_score(indicators, candle_pattern, risk_reward_ratio, volatility):
    score = 60
    if indicators['rsi'] < 40:
        score += 5
    elif indicators['rsi'] > 65:
        score -= 10
    if indicators['macd'] > indicators['macd_signal']:
        score += 7
    if indicators['ema5'] > indicators['ema13'] and indicators['ema13'] > indicators.get('ema21', 0):
        score += 8
    if candle_pattern['bullish_engulfing']:
        score += 10
    elif candle_pattern['hammer']:
        score += 8
    if risk_reward_ratio > 3:
        score += 10
    elif risk_reward_ratio > 2:
        score += 5
    if volatility < 0.01:
        score += 5
    elif volatility > 0.03:
        score -= 8
    return max(0, min(100, score))

def generate_improved_signal(df, symbol, trade_value) -> dict:
    # التحقق من كفاية البيانات
    if len(df) < 50:
        logger.info(f"{symbol}: رفض التوصية - البيانات غير كافية (عدد الصفوف: {len(df)})")
        return None

    strat = ImprovedDayTradingStrategy()
    df = strat.populate_indicators(df)
    df = strat.populate_buy_trend(df)
    last_row = df.iloc[-1]

    if last_row.get('buy', 0) != 1:
        logger.info(f"{symbol}: رفض التوصية - شروط الشراء غير مستوفاة")
        return None

    candle_pattern = analyze_candle_pattern(df)
    if not candle_pattern['bullish']:
        logger.info(f"{symbol}: رفض التوصية - نمط الشموع غير صاعد")
        return None

    market_volatility = df['atr'].iloc[-1] / df['close'].iloc[-1]
    current_price = last_row['close']
    atr = last_row['atr']
    resistance = last_row['resistance']
    support = last_row['support']
    price_range = resistance - support
    recent_highs = df['high'][-20:].values
    fib_levels = [current_price + price_range * level for level in [0.382, 0.618, 0.786]]
    target = select_best_target_level(current_price, fib_levels, np.sort(recent_highs))
    volatility_factor = min(1.5, max(1.0, 1.0 + market_volatility * 10))
    stop_loss = max(current_price - (atr * volatility_factor), support * 1.005)
    risk = current_price - stop_loss
    reward = target - current_price
    risk_reward_ratio = reward / risk if risk > 0 else 0
    confidence_score = calculate_confidence_score(last_row, candle_pattern, risk_reward_ratio, market_volatility)

    if risk_reward_ratio < 1.5 or confidence_score < 60:
        logger.info(f"{symbol}: رفض التوصية - نسبة المخاطرة/العائد ({risk_reward_ratio:.2f}) أو الثقة ({confidence_score}) غير كافية")
        return None
    if reward / current_price < 0.01:
        logger.info(f"{symbol}: رفض التوصية - الربح المتوقع ({reward/current_price:.4f}) أقل من الحد الأدنى")
        return None

    signal = {
        'symbol': symbol,
        'price': float(format(current_price, '.8f')),
        'target': float(format(target, '.8f')),
        'stop_loss': float(format(stop_loss, '.8f')),
        'dynamic_stop_loss': float(format(stop_loss, '.8f')),
        'strategy': 'improved_day_trading',
        'confidence': int(confidence_score),
        'market_condition': 'volatile' if market_volatility > 0.02 else 'stable',
        'indicators': {
            'ema5': float(last_row['ema5']),
            'ema13': float(last_row['ema13']),
            'rsi': float(last_row['rsi']),
            'vwap': float(last_row['vwap']),
            'atr': float(atr),
            'macd': float(last_row['macd']),
            'macd_signal': float(last_row['macd_signal']),
            '%K': float(last_row['%K']),
            '%D': float(last_row['%D']),
            'resistance': float(resistance),
            'support': float(support)
        },
        'trade_value': trade_value,
        'risk_reward_ratio': float(risk_reward_ratio)
    }
    logger.info(f"{symbol}: تم توليد التوصية - السعر: {current_price}, الهدف: {target}, وقف الخسارة: {stop_loss}")
    return signal
