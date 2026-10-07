# Контракты внешних сервисов

Проверка опубликованных материалов при подготовке релиза: 08.10.2026. Собственный B2B-аккаунт, реальные деньги и live-сессия продавца не использовались. Документация поставщика — основание реализации, но не независимая проверка надёжности поставщика.

## GameCore — первичная документация

- https://gamecore-api.tech/ru/docs/quickstart
- https://gamecore-api.tech/ru/docs/authentication
- https://gamecore-api.tech/ru/docs/catalog
- https://gamecore-api.tech/ru/docs/orders
- https://gamecore-api.tech/ru/docs/direct-top-up-api
- https://gamecore-api.tech/ru/docs/webhooks
- https://gamecore-api.tech/ru/docs/idempotency-and-errors
- https://gamecore-api.tech/ru/pricing
- https://gamecore-api.tech/ru/contact

Реализовано: X-Api-Key, каталог PUBG id_only, товар, POST заказа с X-Idempotency-Key и externalOrderId, GET кода/списка заказов, все подзаказы, статусы позиций, подписанный webhook. Нет выдуманного GET /balance, отмены закупки, проверки nickname или бесплатного B2B sandbox.

## FunPay — неофициальный протокол сайта

Структуры сверялись с первичным исходным кодом актуального клиента, самостоятельно реализованы в адаптере этого проекта. Библиотека FunPayCardinal/FunPayAPI не включена в runtime и не выдаётся за официальный поддерживаемый API.

- https://github.com/sidor0912/FunPayCardinal/blob/main/FunPayAPI/account.py
- https://github.com/sidor0912/FunPayCardinal/blob/main/FunPayAPI/types.py
- https://funpayapi.readthedocs.io/ru/latest/index.html

Особенно важны: POST /api/orders/get; order type_data.amount; seller-authored summary/desc; chat_node runner; own offer form; /lots/raise game_id + выбранные node_ids. Формат может измениться — адаптер в таком случае должен безопасно остановить выдачу.

## Railway и Telegram

- https://docs.railway.com/pricing/plans
- https://docs.railway.com/guides/volumes
- https://docs.railway.com/networking/public-networking
- https://docs.railway.com/deployments/serverless
- https://docs.railway.com/networking/static-outbound-ips
- https://core.telegram.org/bots/api
- https://core.telegram.org/bots/tutorial

Тесты используют synthetic fixtures, часть числовых примеров из документации. Это НЕ коммерческий прайс и НЕ результат фактической выдачи.
