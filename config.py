import pytz
from decouple import config

# تحميل المتغيرات البيئية
BINANCE_API_KEY = config('BINANCE_API_KEY')
BINANCE_API_SECRET = config('BINANCE_API_SECRET')
TELEGRAM_BOT_TOKEN = config('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = config('TELEGRAM_CHAT_ID')
DATABASE_URL = config('DATABASE_URL')

# إعدادات أخرى
TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADE_VALUE = 10
