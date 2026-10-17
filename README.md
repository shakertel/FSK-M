# FSK M

این بسته فقط ۶ فایل دارد: main.py، telegram_bot.py، engine.py، requirements.txt، render.yaml و همین README.

main.py و telegram_bot.py عین فایل‌های نمونهٔ ربات سریال شما هستند. فقط engine.py برای دیتابیس فیلم و موتور جست‌وجوی کولب ارسالی تنظیم شده است: مدل gemini-3.5-flash-lite، بچ ۴۰۰، دو لاین و استخر مشترک کلیدها. پرامپت تحلیل درخواست، غربالگری و داوری از همان کولب آمده‌اند؛ کانسپت‌ها و عبارت‌های ویژه هر دو وارد جست‌وجو می‌شوند. پاسخ جمینی کش یا روی دیسک ذخیره نمی‌شود؛ هر جست‌وجو از نو اجرا می‌شود.

دو نسخهٔ supabase و google-genai در requirements مطابق کولب ارسالی تنظیم شده‌اند؛ همان ۶ کتابخانهٔ نمونهٔ قبلی استفاده می‌شوند.

## راه‌اندازی

محتویات این پوشه را مستقیم در ریشهٔ مخزن GitHub قرار بده. فایل‌های اضافهٔ نسخه‌های قبلی مثل fsk_*.py، webhook_bridge.py و health_server.py دیگر لازم نیستند. این موتور فقط کتابخانه‌ها را import می‌کند و به آن فایل‌ها وابسته نیست.

در Render یک Web Service با پلن Free و این تنظیمات:

- Build Command: `pip install -r requirements.txt`
- Start Command: `python main.py`
- Root Directory: خالی
- Health Check Path: `/health`

متغیرها مثل نمونهٔ قبلی:

- TELEGRAM_TOKEN
- SUPABASE_URL
- SUPABASE_SECRET_KEY
- GEMINI_KEY_1 تا GEMINI_KEY_14، هرکدام جدا؛ حداقل یک کلید لازم است.

ربات مثل نمونهٔ قبلی polling است و Flask روی پورت ۱۰۰۰۰ دارد. توکن را هم‌زمان در دو سرویس فعال نکن. اگر قبلاً نسخهٔ webhook را اجرا کرده‌ای، شروع polling آن webhook را حذف می‌کند؛ BOT_MODE و WEBHOOK_URL در این بسته استفاده نمی‌شوند و لازم نیستند.

رابط، تأیید، لغو، نمایش سه‌تایی، پوستر و محدودیت ۱۵ دقیقه همان نمونهٔ قبلی‌اند. در این نسخه موتور فقط جدول‌های movies و movie_concepts و movie_special_phrases را می‌خواند و چیزی در سوپابیس نمی‌نویسد.
