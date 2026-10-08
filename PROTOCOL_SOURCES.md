# Первичные источники протокола — проверка 8 октября 2026

- https://reseller.fazercards.com/en/docs — база api.fzr.cards/api/v2,
  X-API-Key, /me, /balance (USD), /subscription/plans, /topups/offers,
  /topups/order, /topups/validate-id, /telegram/stars, /telegram/stars/buy, /orders/:id.
- https://reseller.fazercards.com/en/docs/cookbook — Idempotency-Key и срок 7 дней
  для документированных purchase endpoints. Stars в этом списке не указан.
- https://reseller.fazercards.com/en/docs/webhooks — подпись сырого тела
  X-Webhook-Signature sha256=HMAC и события с data.order_id.
- https://github.com/FZR-cards/fazercards-python — официальный SDK.
- https://raw.githubusercontent.com/FZR-cards/fazercards-python/main/src/fazercards/client.py
  — сверка точных тел и методов; buy_stars не принимает idempotency_key.
- https://funpay.com/lots/2418/ — публичный раздел Telegram Stars типа lot.
  ID объявления продавца и другие параметры берутся из его реальной формы.
- https://docs.docker.com/compose/install/linux/ — установка Compose plugin.
- https://caddyserver.com/docs/running — Docker и автоматический HTTPS.
- https://docs.railway.com/variables — переменные сервиса.

Доступ к личному кабинету не выполнялся. Публичные JSON-цены и номера товаров в
документации — примеры, не оферта и не прайс владельца. /subscription/plans в
актуальной документации показывает пример Bronze $29, Silver $49, Gold $99 за
30 дней; конкретную цену уточняет реальный ответ его аккаунта. Старое заявление
про обязательные $9.99 не считается проверенным актуальным тарифом.

Протокол "No-KYC, без сессии, произвольный TON-перевод в контракт Fragment →
Stars указанному username" не подтверждён первичной документацией. Он не
реализован. Вместо него используется документированный Stars API FazerCards;
условия верификации аккаунта поставщика определяет сам поставщик.
