# Проверки

Linux + Python 3.13:
```
python -m pip install -r requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_cov --cov=autoucbot --cov-report=term-missing
python -m compileall -q autoucbot tests scripts
```

test_fazer_protocol: контракт, цены Decimal, HTTP ошибки, async-адаптеры, подпись.
test_fazer_orders: оба направления, кратность, pending/unknown/partial, два
реальных по модели заказа, дубли событий, конкуренция, баланс/резерв, отмены.
test_v2_portability: миграция схемы, экспорт/импорт, APP_SECRET, HMAC, подмена
архива, блокировка запущенной базы, sending→unknown, история и ключи.
test_v2_web: отдельные формы, роли, вебхуки, интерфейс на наполненной базе,
заморозка, прайс, ограничения, настройки и безопасное возобновление.
test_v2_edgecases: два заказа в одном живом по модели чате, непрочитанный UID,
неактивная подписка, запрос ника, динамическая цена и rate-limit.
Сохранены исходные 313 регрессионных проверок.

Точные результаты последнего запуска лежат во внешней папке TEST_RESULTS.
Внешние реальные подключения отсутствуют; тестовые HTTP-контракты и заглушки
не заменяют пробное пополнение на личный UID/username. Docker build, DNS/TLS
конкретного VPS и фактическое исполнение FunPay/Fazer/Telegram отдельно
проверяются владельцем по контрольному списку, не объявляются пройденными.
