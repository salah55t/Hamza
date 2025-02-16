نظرة عامة على البوت
هذا البوت مخصص لتوليد توصيات تداول العملات الرقمية على أساس بيانات السوق والمؤشرات الفنية. يعمل البوت على النحو التالي:

جمع البيانات:
يقوم البوت بجلب البيانات التاريخية (آخر 3 أيام) وأسعار العملات الرقمية من منصة Binance باستخدام REST API وWebSocket.
تحليل البيانات:
تُحسب مؤشرات فنية مثل Ichimoku، RSI، ATR، والتقلبات والزخم. كما يتم استخدام نموذج تعلم آلة تجميعي (Ensemble) يضم عدة نماذج (مثل RandomForest، GradientBoosting، ExtraTrees، Ridge وXGBoost) لتحليل البيانات وتوليد إشارات تداول.
التنبيهات والتخزين:
عند تحقق شروط التوصية يتم إرسال تنبيه عبر Telegram، وتُخزن الإشارات في قاعدة بيانات PostgreSQL لمتابعة الأداء.
نشر البوت على Render
يمكنك نشر البوت بسهولة على Render باتباع الخطوات التالية:

إنشاء مستودع على GitHub:

ضع الكود الكامل للبوت في مستودع GitHub.
إنشاء خدمة ويب على Render:

سجّل في Render.
أنشئ Web Service جديداً واربطه بالمستودع الخاص بك.
قم بتعيين أمر التشغيل (على سبيل المثال: python bot.py).
تأكد من اختيار البيئة المناسبة (Python version).
إعداد المتغيرات البيئية (Environment Variables):
في لوحة تحكم Render، أضف المتغيرات التالية مع القيم المناسبة:

BINANCE_API_SECRET
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
DATABASE_URL
إضافة قاعدة البيانات على Render:

قم بإنشاء خدمة PostgreSQL Database من Render.
بعد الإنشاء، ستحصل على رابط الاتصال الخاص بقاعدة البيانات (DATABASE_URL).
استخدم هذا الرابط في المتغير البيئي DATABASE_URL في إعدادات الخدمة.
إعداد Webhook لـ Telegram:

بعد نشر الخدمة، سيكون لديك عنوان URL عام (مثلاً: https://your-app.onrender.com).
لتعيين Webhook، استخدم الرابط التالي مع استبدال <TELEGRAM_BOT_TOKEN> وyour-app.onrender.com بالقيم الخاصة بك:
bash
نسخ الكود
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://your-app.onrender.com/webhook
يمكنك تنفيذ هذا الطلب عبر متصفح الإنترنت أو باستخدام أدوات مثل cURL/Postman.
لتعيين Webhook، استخدم الرابط التالي مع استبدال <TELEGRAM_BOT_TOKEN> وyour-app.onrender.com بالقيم الخاصة بك:
bash
نسخ الكود
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://your-app.onrender.com/webhook
يمكنك تنفيذ هذا الطلب عبر متصفح الإنترنت أو باستخدام أدوات مثل cURL/Postman.
تشغيل البوت ومتابعة السجلات:

عند نشر الخدمة على Render، يبدأ البوت بالعمل تلقائياً.
راقب سجلات التطبيق (logs) للتأكد من بدء التشغيل بشكل صحيح واستقبال التنبيهات.
