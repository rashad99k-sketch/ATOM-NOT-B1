# تدقيق المعمارية الهندسية للنسخ المرجعية

**المصادر:** `NAISO-LIVE-main(20261123-151100).zip`، `AXA 3.zip`، `GGP_INSTITUTIONAL_UNIFIED_TRADE_MANAGEMENT_RECOVERY_STATS_RELEASE(4).zip`.

## 1. نتيجة المقارنة

نسختا NAISO-LIVE وAXA 3 متقاربتان جدًا في ملفات GGP الأساسية. تطابق محتوى ملفات مثل `ggp_market_data.py` و`ggp_recovery.py` و`ggp_profit_brain.py` و`roro_trade_journal.py` و`bingx_execution.py` وملفات الماسح في النسختين. لذلك فهما ليستا معماريتين منفصلتين ينبغي دمجهما معًا؛ AXA 3 تبدو نسخة تغليف/تجميع أخرى لنفس خط GGP.

حزمة GGP الأقدم تختلف في بعض الملفات، خصوصًا سجل التداول وإدارة الأحداث وطبقة التنفيذ. لا يصح نسخها فوق الملفات الأحدث دون إثبات توافق API والسلوك.

## 2. المعمارية الجيدة الموجودة بالفعل في MAIN

بعد مطابقة الملفات مع مستودع ATOM-NOT-B1، تبيّن أن العناصر الهندسية الأساسية التالية موجودة بالفعل؛ لذلك احتُفظ بها بدل إنشاء نسخ موازية:

| الطبقة | الموجود في MAIN | قرار الدمج |
|---|---|---|
| حماية تعدد العمليات | `ggp_instance_lock.py` بقفل نظام تشغيل يتحرر عند موت العملية | الاحتفاظ به وإعادة استخدامه في WSGI |
| إدارة الموارد | `roro_resource_manager.py` بكاش محدود، History محدود، وقياسات psutil اختيارية | الاحتفاظ؛ لا حاجة إلى مدير موارد ثانٍ |
| سجل الصفقات | `roro_trade_journal.py` مع SQLite/WAL وأحداث idempotent وruntime state | الاحتفاظ؛ لا استبدال بسجل أقدم |
| المصالحة والاسترداد | `ggp_recovery.py` ومسارات مزامنة BingX في `ggpbot.py` | الاحتفاظ؛ لا تجاوز لحالات UNKNOWN أو Recovery Hold |
| تنفيذ BingX | `bingx_execution.py` و`bingx_protection.py` | الاحتفاظ؛ لا نسخ طبقة تنفيذ أقدم |
| القرار المؤسسي | `ggp_institutional_bridge.py` و`roro_opportunity_pipeline.py` | الاحتفاظ بمصدر دخول موحد |
| إدارة الصفقة | `ggp_profit_brain.py` وTradeManagementBoard/LiveTradeManager | الاحتفاظ بسلطة إدارة واحدة |
| الماسحات | Scanner 1 وScanner 2 وShared OHLCV cache | عدم إضافة polling مستقل أو تغيير cadence |

## 3. المبادئ المرجعية التي تستحق الاحتفاظ

- Intent دائم قبل إرسال MARKET، وربط الأمر بمعرّف عميل، وعدم إعادة إرسال أمر MARKET بعد نتيجة Timeout غامضة.
- لا تصبح الصفقة نشطة بمجرد قبول الطلب؛ يلزم إثبات Fill ثم Position ثم Native SL.
- عند غموض حالة المركز أو التنفيذ، يمنع الدخول الجديد بدل التخمين.
- الاسترداد يعيد بناء مراحل إدارة الصفقة من السجل، ولا يخترع TP/SL أو كمية منفذة.
- بيانات الموارد والتشخيص محدودة الذاكرة، ولا يُسمح لفشل القياس الاختياري بإيقاف النظام.
- الماسحات تزود فرصًا فقط؛ لا تنشئ سلطة تنفيذ ثانية.

هذه المبادئ متوافقة مع تقارير التدقيق المرفقة، ولا تعني أن كل سيناريوهات BingX الحية قد اجتازت قبول Demo.

## 4. الفجوة الفعلية التي عالجها هذا التغيير

كان تشغيل المحرك داخل كتلة `if __name__ == "__main__"` في `ggpbot.py`. استيراد `ggpbot:app` من WSGI/Gunicorn يشغّل Flask، لكنه لا ينفذ تلك الكتلة؛ وبالتالي يمكن أن تظهر لوحة الويب دون تشغيل `safe_main_loop`. كذلك كان `/health` يعلن `ok=true` دون التحقق من حياة حلقة التداول.

أضيفت طبقة bootstrap واحدة:
- `wsgi.py` يبدأ `safe_main_loop` في خيط supervisor منفصل عند استيراد التطبيق عبر Gunicorn.
- `ggp_runtime_bootstrap.py` يعيد استخدام قفل GGP نفسه، ويمنع بدء محرك تداول ثانٍ، ويسجل حالات STARTING/RUNNING/FAILED/LOCKED.
- `Procfile` يحدد Gunicorn بعامل واحد وثمانية خيوط HTTP ويربطه بمنفذ Render. العامل الواحد مقصود حتى لا تتحول كل عملية WSGI إلى محرك تداول مستقل.
- كل دورة من الحلقة الرئيسية تنشر heartbeat.
- `/health` و`/runtime-health` لا يعلنان جاهزية محرك التداول إذا لم تصل heartbeat حديثة.

## 5. ما لم يتم تغييره عمدًا

لم يتم استبدال أو تعديل استراتيجية الدخول، cadence الماسحات، API pacing، الرافعة، Position Sizing، SL/TP، محرك التنفيذ، أو مدير الأرباح. لم يتم تشغيل أوامر على BingX ولم يتم إجراء LIVE أو Demo من بيئة البناء.

## 6. شرط نشر Render

إذا كان للخدمة Start Command صريح في إعدادات Render، يجب تغييره إلى:

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile -
```

يوصى بتعيين Health Check Path إلى `/runtime-health`. اضبط عدد Instances في Render على واحد؛ قفل نظام التشغيل محلي لكل مضيف وليس قفلًا موزعًا بين مضيفين. وجود `Procfile` لا يستبدل Start Command صريحًا موجودًا في لوحة Render.

## 7. حدود التحقق

اختبارات GitHub Actions تتحقق من bootstrap والقفل والـheartbeat وتكامل WSGI. لكنها لا تثبت أن إعداد Render الفعلي قد تغيّر، ولا تثبت اتصال BingX أو سلامة LIVE. يجب التأكد من ظهور `status=RUNNING` وارتفاع `loop_cycles` في `/runtime-health` بعد النشر.
