import pandas as pd

def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi_indicator(df, period=7):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean().replace(0, 1e-10)
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_atr_indicator(df, period=7):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df

def calculate_macd_indicator(df, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(df['close'], fast)
    ema_slow = calculate_ema(df['close'], slow)
    macd = ema_fast - ema_slow
    df['macd'] = macd
    df['macd_signal'] = macd.ewm(span=signal, adjust=False).mean()
    return df

def calculate_stochastic(df, k_period=14, d_period=3):
    lowest_low = df['low'].rolling(window=k_period).min()
    highest_high = df['high'].rolling(window=k_period).max()
    df['%K'] = 100 * ((df['close'] - lowest_low) / (highest_high - lowest_low))
    df['%D'] = df['%K'].rolling(window=d_period).mean()
    return df
