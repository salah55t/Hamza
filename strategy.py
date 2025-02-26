import numpy as np
import pandas as pd
import logging
from typing import Optional, List

# تكوين السجلات
logger = logging.getLogger(__name__)

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """حساب مؤشر الاتجاه المتوسط (ADX)"""
    df = df.copy()
    df['tr1'] = abs(df['high'] - df['low'])
    df['tr2'] = abs(df['high'] - df['close'].shift(1))
    df['tr3'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    
    df['plus_dm'] = 0.0
    df['minus_dm'] = 0.0
    df['high_diff'] = df['high'] - df['high'].shift(1)
    df['low_diff'] = df['low'].shift(1) - df['low']
    
    df.loc[(df['high_diff'] > df['low_diff']) & (df['high_diff'] > 0), 'plus_dm'] = df['high_diff']
    df.loc[(df['low_diff'] > df['high_diff']) & (df['low_diff'] > 0), 'minus_dm'] = df['low_diff']
    
    df['plus_di'] = 100 * (df['plus_dm'].rolling(window=period).mean() / df['atr'])
    df['minus_di'] = 100 * (df['minus_dm'].rolling(window=period).mean() / df['atr'])
    
    df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'])
    df['adx'] = df['dx'].rolling(window=period).mean()
    
    return df['adx']

def calculate_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """حساب مؤشر تدفق الأموال (MFI)"""
    df = df.copy()
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    raw_money_flow = typical_price * df['volume']
    
    positive_flow = pd.Series(0.0, index=df.index)
    negative_flow = pd.Series(0.0, index=df.index)
    price_diff = typical_price.diff()
    positive_flow[price_diff > 0] = raw_money_flow[price_diff > 0]
    negative_flow[price_diff < 0] = raw_money_flow[price_diff < 0]
    
    positive_mf = positive_flow.rolling(window=period).sum()
    negative_mf = negative_flow.rolling(window=period).sum()
    money_flow_ratio = positive_mf / negative_mf
    mfi = 100 - (100 / (1 + money_flow_ratio))
    
    return mfi

def calculate_higher_highs(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """تحديد إذا كانت القمم متتالية ومتزايدة"""
    highs = df['high']
    result = pd.Series(0, index=df.index)
    for i in range(period, len(df)):
        local_highs = []
        for j in range(i - period, i):
            if j > 0 and j < len(df) - 1:
                if highs[j] > highs[j - 1] and highs[j] > highs[j + 1]:
                    local_highs.append(highs[j])
        if len(local_highs) >= 2 and all(local_highs[k] <= local_highs[k + 1] for k in range(len(local_highs) - 1)):
            result[i] = 1
    return result

def calculate_higher_lows(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """تحديد إذا كانت القيعان متتالية ومتزايدة"""
    lows = df['low']
    result = pd.Series(0, index=df.index)
    for i in range(period, len(df)):
        local_lows = []
        for j in range(i - period, i):
            if j > 0 and j < len(df) - 1:
                if lows[j] < lows[j - 1] and lows[j] < lows[j + 1]:
                    local_lows.append(lows[j])
        if len(local_lows) >= 2 and all(local_lows[k] <= local_lows[k + 1] for k in range(len(local_lows) - 1)):
            result[i] = 1
    return result

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """حساب المتوسط المتحرك الأسي"""
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi_indicator(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """حساب مؤشر القوة النسبية (RSI)"""
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_atr_indicator(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """حساب متوسط المدى الحقيقي (ATR)"""
    df_copy = df.copy()
    high = df_copy['high']
    low = df_copy['low']
    prev_close = df_copy['close'].shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    df_copy['atr'] = tr.rolling(window=period).mean()
    return df_copy

def calculate_macd_indicator(df: pd.DataFrame) -> pd.DataFrame:
    """حساب مؤشر MACD"""
    df_copy = df.copy()
    df_copy['ema12'] = calculate_ema(df_copy['close'], 12)
    df_copy['ema26'] = calculate_ema(df_copy['close'], 26)
    df_copy['macd'] = df_copy['ema12'] - df_copy['ema26']
    df_copy['macd_signal'] = calculate_ema(df_copy['macd'], 9)
    df_copy['macd_hist'] = df_copy['macd'] - df_copy['macd_signal']
    return df_copy

def calculate_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """حساب مؤشر ستوكاستك"""
    df_copy = df.copy()
    low_min = df_copy['low'].rolling(window=k_period).min()
    high_max = df_copy['high'].rolling(window=k_period).max()
    df_copy['%K'] = 100 * ((df_copy['close'] - low_min) / (high_max - low_min))
    df_copy['%D'] = df_copy['%K'].rolling(window=d_period).mean()
    return df_copy

def analyze_advanced_candle_patterns(df: pd.DataFrame) -> dict:
    """تحليل أنماط الشموع المتقدمة"""
    last_candles = df.iloc[-5:].copy()
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
    
    morning_star = False
    if len(last_candles) >= 3:
        first_bearish = last_candles.iloc[-3]['close'] < last_candles.iloc[-3]['open']
        second_small = last_candles.iloc[-2]['body'] < last_candles.iloc[-3]['body'] * 0.5
        third_bullish = last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open']
        third_above_mid = last_candles.iloc[-1]['close'] > ((last_candles.iloc[-3]['open'] + last_candles.iloc[-3]['close']) / 2)
        morning_star = first_bearish and second_small and third_bullish and third_above_mid
    
    three_white_soldiers = False
    if len(last_candles) >= 3:
        all_bullish = all(last_candles.iloc[-i]['close'] > last_candles.iloc[-i]['open'] for i in range(1, 4))
        progressively_higher = all(last_candles.iloc[-i]['close'] > last_candles.iloc[-i-1]['close'] for i in range(1, 3))
        small_upper_shadows = all(last_candles.iloc[-i]['upper_shadow'] < last_candles.iloc[-i]['body'] * 0.3 for i in range(1, 4))
        three_white_soldiers = all_bullish and progressively_higher and small_upper_shadows

    pattern = {
        'doji': doji,
        'hammer': hammer,
        'bullish_engulfing': bullish_engulfing,
        'morning_star': morning_star,
        'three_white_soldiers': three_white_soldiers,
        'bullish': hammer or bullish_engulfing or morning_star or three_white_soldiers or (not doji and last_candles.iloc[-1]['close'] > last_candles.iloc[-1]['open'])
    }
    return pattern

def determine_market_condition(df: pd.DataFrame) -> str:
    """تحديد حالة السوق العامة باستخدام المتوسطات المتحركة"""
    if len(df) < 200:
        return "neutral"
    df = df.copy()
    df['ma50'] = df['close'].rolling(window=50).mean()
    df['ma200'] = df['close'].rolling(window=200).mean()
    last = df.iloc[-1]
    if last['ma50'] > last['ma200'] and last['close'] > last['ma50']:
        return "bullish"
    elif last['ma50'] < last['ma200'] and last['close'] < last['ma50']:
        return "bearish"
    else:
        # افتراضاً ATR موجود من حساب المؤشرات
        df['atr_pct'] = df['atr'] / df['close'] * 100
        avg_atr_pct = df['atr_pct'].iloc[-14:].mean()
        return "volatile" if avg_atr_pct > 3.0 else "neutral"

def select_best_target_level(current_price: float, fib_levels: List[float],
                             recent_highs: np.ndarray,
                             recent_volume_profile: Optional[pd.Series] = None) -> float:
    """تحديد أفضل مستوى هدف باستخدام مستويات فيبوناتشي والقمم وحجم التداول"""
    targets = []
    for level in fib_levels:
        if level > current_price:
            targets.append((level, 1.0))
    for high in np.sort(recent_highs):
        if high > current_price:
            found_close = False
            for level in fib_levels:
                if abs(high - level) / level < 0.01:
                    found_close = True
                    targets = [(t, w * 1.5 if abs(t - level) / level < 0.01 else w) for t, w in targets]
                    break
            if not found_close:
                targets.append((high, 0.8))
    if recent_volume_profile is not None:
        for price, vol in recent_volume_profile.items():
            if price > current_price:
                norm_vol = vol / recent_volume_profile.max()
                if norm_vol > 0.7:
                    targets.append((price, norm_vol))
    if not targets:
        return fib_levels[1] if len(fib_levels) > 1 else current_price * 1.02
    target_scores = [(t, imp * (current_price / t)) for t, imp in targets]
    best_target = max(target_scores, key=lambda x: x[1])[0]
    return best_target

def calculate_enhanced_confidence_score(indicators: dict, candle_pattern: dict,
                                        risk_reward_ratio: float, volatility: float,
                                        market_condition: str, proximity_to_support: float) -> int:
    """حساب درجة الثقة المحسنة للإشارة"""
    score = 60
    if market_condition == 'bullish':
        score = 65
    elif market_condition == 'bearish':
        score = 55
    elif market_condition == 'volatile':
        score = 60

    if indicators['rsi'] < 40:
        score += 5
    elif indicators['rsi'] > 65:
        score -= 10

    if indicators['macd'] > indicators['macd_signal']:
        score += 7

    if indicators.get('adx', 0) > 25:
        score += 8
    elif indicators.get('adx', 0) < 20:
        score -= 5

    if indicators.get('mfi', 50) > 50:
        score += 5

    if indicators['ema5'] > indicators['ema13'] and indicators['ema13'] > indicators.get('ema21', 0):
        score += 8

    if candle_pattern.get('morning_star', False):
        score += 15
    elif candle_pattern.get('three_white_soldiers', False):
        score += 12
    elif candle_pattern.get('bullish_engulfing', False):
        score += 10
    elif candle_pattern.get('hammer', False):
        score += 8

    if risk_reward_ratio > 3:
        score += 10
    elif risk_reward_ratio > 2:
        score += 5

    if volatility < 0.01:
        score += 5
    elif volatility > 0.03:
        score -= 8

    if indicators.get('volume_change', 0) > 0.2:
        score += 5
    elif indicators.get('volume_change', 0) > 0.5:
        score += 10

    if proximity_to_support < 0.02:
        score += 7

    return max(0, min(100, int(score)))

def calculate_dynamic_stop_loss(df: pd.DataFrame, current_price: float, atr: float,
                                support_level: float, volatility: float) -> float:
    """حساب وقف خسارة ديناميكي باستخدام ATR ومستوى الدعم والتقلب"""
    volatility_factor = min(2.0, max(1.0, 1.0 + volatility * 10))
    atr_stop_loss = current_price - (atr * volatility_factor)
    support_stop_loss = support_level * 0.995
    stop_loss = max(atr_stop_loss, support_stop_loss)
    max_stop_distance = current_price * 0.05
    min_stop_distance = current_price * 0.005
    stop_loss = max(current_price - max_stop_distance, min(current_price - min_stop_distance, stop_loss))
    return stop_loss

class EnhancedTradingStrategy:
    """استراتيجية التداول المحسنة"""
    stoploss = -0.015
    minimal_roi = {"0": 0.008, "30": 0.005, "60": 0.003}

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """إضافة المؤشرات الفنية والإضافية للإطار البياني"""
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
        
        # المؤشرات المحسنة
        df['adx'] = calculate_adx(df, period=14)
        df['mfi'] = calculate_mfi(df, period=14)
        df['higher_highs'] = calculate_higher_highs(df, period=20)
        df['higher_lows'] = calculate_higher_lows(df, period=20)
        
        # المؤشرات التقليدية
        df = calculate_atr_indicator(df, period=7)
        df = calculate_macd_indicator(df)
        df = calculate_stochastic(df)
        
        # مستويات الدعم والمقاومة
        df['resistance'] = df['high'].rolling(window=20).max()
        df['support'] = df['low'].rolling(window=20).min()
        
        # مؤشرات إضافية
        df['price_distance_from_vwap'] = (df['close'] - df['vwap']) / df['vwap']
        df['volume_trend'] = df['volume'].diff(5).rolling(window=10).mean()
        df['bollinger_bandwidth'] = (df['upper_band'] - df['lower_band']) / df['ma20']
        
        return df

    def populate_buy_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """تحديد شروط الشراء المحسنة"""
        basic_conditions = (
            (df['ema5'] > df['ema13']) &
            (df['rsi'].between(30, 70)) &
            (df['macd'] > df['macd_signal']) &
            (df['%K'] > df['%D'])
        )
        enhanced_conditions = (
            (df['adx'] > 20) &
            (df['mfi'] > 40) &
            (df['close'] > df['vwap'])
        )
        risk_management = (
            (df['close'] > df['lower_band']) &
            (df['bollinger_bandwidth'] > 0.03)
        )
        market_position = (
            (df['higher_lows'] > 0) |
            ((df['close'] > df['ma20']) & (df['price_distance_from_vwap'] < 0.02))
        )
        conditions = basic_conditions & enhanced_conditions & risk_management & market_position
        df.loc[conditions, 'buy'] = 1
        return df

    def populate_sell_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """تحديد شروط البيع المحسنة"""
        conditions = (
            (df['ema5'] < df['ema13']) |
            (df['rsi'] > 80) |
            (df['macd'] < df['macd_signal']) |
            (df['%K'] < df['%D']) |
            (df['close'] > df['upper_band']) |
            ((df['close'] > df['resistance'] * 0.99) & (df['rsi'] > 70))
        )
        df.loc[conditions, 'sell'] = 1
        return df

def generate_enhanced_trading_signal(df: pd.DataFrame, symbol: str, trade_value: float) -> Optional[dict]:
    """إنشاء إشارة تداول محسنة مع إدارة مخاطر أفضل"""
    if len(df) < 50:
        logger.info(f"{symbol}: رفض التوصية - البيانات غير كافية (عدد الصفوف: {len(df)})")
        return None

    market_condition = determine_market_condition(df)
    strat = EnhancedTradingStrategy()
    df = strat.populate_indicators(df)
    df = strat.populate_buy_trend(df)
    last_row = df.iloc[-1]

    if last_row.get('buy', 0) != 1:
        logger.info(f"{symbol}: رفض التوصية - شروط الشراء غير مستوفاة")
        return None

    candle_pattern = analyze_advanced_candle_patterns(df)
    if not candle_pattern.get('bullish', False):
        logger.info(f"{symbol}: رفض التوصية - نمط الشموع غير صاعد")
        return None

    market_volatility = df['atr'].iloc[-1] / df['close'].iloc[-1]
    current_price = last_row['close']
    atr = last_row['atr']
    resistance = last_row['resistance']
    support = last_row['support']
    price_range = resistance - support
    proximity_to_support = (current_price - support) / current_price

    recent_highs = df['high'][-30:].values
    fib_levels = [current_price + price_range * level for level in [0.382, 0.618, 0.786, 1.0]]

    # إنشاء بروفايل حجم بسيط
    price_bins = np.linspace(df['low'].min(), df['high'].max(), 20)
    volume_profile = {}
    for i in range(len(price_bins) - 1):
        mask = (df['close'] >= price_bins[i]) & (df['close'] < price_bins[i+1])
        volume_profile[(price_bins[i] + price_bins[i+1]) / 2] = df.loc[mask, 'volume'].sum()
    recent_volume_profile = pd.Series(volume_profile)

    target = select_best_target_level(current_price, fib_levels, recent_highs, recent_volume_profile)
    dynamic_stop_loss = calculate_dynamic_stop_loss(df, current_price, atr, support, market_volatility)
    
    risk = current_price - dynamic_stop_loss
    reward = target - current_price
    risk_reward_ratio = reward / risk if risk > 0 else 0

    confidence_score = calculate_enhanced_confidence_score(
        {
            'rsi': last_row['rsi'],
            'macd': last_row['macd'],
            'macd_signal': last_row['macd_signal'],
            'ema5': last_row['ema5'],
            'ema13': last_row['ema13'],
            'ema21': last_row.get('ema21', 0),
            'volume_change': last_row.get('volume_change', 0),
            'adx': last_row.get('adx', 0),
            'mfi': last_row.get('mfi', 50)
        },
        candle_pattern,
        risk_reward_ratio,
        market_volatility,
        market_condition,
        proximity_to_support
    )

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
        'stop_loss': float(format(dynamic_stop_loss, '.8f')),
        'dynamic_stop_loss': float(format(dynamic_stop_loss, '.8f')),
        'strategy': 'enhanced_trading',
        'confidence': int(confidence_score),
        'market_condition': market_condition,
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

    logger.info(f"{symbol}: تم توليد التوصية - السعر: {current_price}, الهدف: {target}, وقف الخسارة: {dynamic_stop_loss}")
    return signal
