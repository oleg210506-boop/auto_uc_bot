from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from .web_security import canonical_origin

@dataclass(frozen=True)
class Config:
    data_dir: Path
    secret: str
    bootstrap_user: str = "owner"
    bootstrap_password: str = ""
    public_url: str = ""
    enable_live: bool = False
    secure_cookie: bool = True
    start_worker: bool = True
    gamecore_url: str = "https://api.gamecore-api.tech"
    # Railway sends requests through its edge proxy. Outside Railway, only
    # loopback peers are trusted by default; operators may set explicit CIDRs.
    forwarded_allow_ips: str = "127.0.0.1,::1"

    def __post_init__(self):
        if self.public_url:
            public = canonical_origin(self.public_url)
            if not public.startswith("https://"):
                raise ValueError("PUBLIC_URL должен быть HTTPS-адресом вашей панели без пути и параметров.")
            object.__setattr__(self, "public_url", public)

    @classmethod
    def from_env(cls) -> "Config":
        secret = os.getenv("APP_SECRET", "")
        if len(secret) < 32:
            raise ValueError("APP_SECRET: задайте случайную строку длиной не менее 32 символов в Railway Variables.")
        public = os.getenv("PUBLIC_URL", "").strip()
        if public:
            try:
                public = canonical_origin(public)
                if not public.startswith("https://"):
                    raise ValueError("HTTPS required")
            except ValueError:
                raise ValueError("PUBLIC_URL должен быть HTTPS-адресом вашей панели без пути и параметров.") from None
        proxy_default = "*" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "127.0.0.1,::1"
        forwarded = os.getenv("FORWARDED_ALLOW_IPS", proxy_default).strip()
        data_dir=Path(os.getenv("DATA_DIR", "/data"))
        if os.getenv("RAILWAY_ENVIRONMENT_ID"):
            mount=os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
            if not mount or data_dir.resolve()!=Path(mount).resolve():
                raise ValueError("Railway: подключите постоянный Volume к этому сервису, Mount Path=/data и DATA_DIR=/data. Работа без диска запрещена.")
        return cls(data_dir, secret,
                   os.getenv("ADMIN_USERNAME", "owner"), os.getenv("ADMIN_PASSWORD", ""), public,
                   os.getenv("ENABLE_LIVE_PURCHASES") == "true",
                   os.getenv("COOKIE_SECURE", "true") == "true",
                   os.getenv("START_WORKER", "true") == "true",
                   forwarded_allow_ips=forwarded)

# kind, default, label, hint, min, max. All financial values here are RUB, not cents.
FIELDS = {
    "confirm_uid": ("bool", True, "Подтверждение UID", "Покупатель подтверждает ID одноразовым кодом."),
    "auto_raise": ("bool", False, "Поднимать предложения", "Только выбранные подразделы; ограничения FunPay соблюдаются."),
    "auto_hide": ("bool", True, "Скрывать управляемые лоты при паузе", "Не касается других товаров. Работает только в live."),
    "auto_resume_balance": ("bool", True, "Продолжать после пополнения", "Только после обновления остатка и повторной проверки оплаты."),
    "pause_on_problem": ("bool", True, "Пауза новых закупок при неопределённом результате", "Проверка уже отправленных операций продолжается."),
    "pause_chat_on_help": ("bool", True, "Передавать чат оператору по просьбе покупателя", "Автоответы в этом чате прекращаются."),
    "reminders": ("bool", True, "Напоминать об UID", "Ограниченное количество, без бесконечного спама."),
    "reminder_seconds": ("int", 600, "Интервал напоминаний, секунд", "", 60, 86400),
    "reminder_limit": ("int", 2, "Максимум напоминаний", "", 0, 10),
    "uid_timeout_seconds": ("int", 3600, "Ожидание UID до тревоги, секунд", "Не означает автоматический возврат.", 300, 604800),
    "processing_timeout_seconds": ("int", 600, "Зависший заказ: тревога через, секунд", "Не повторная покупка.", 60, 86400),
    "funpay_poll_seconds": ("int", 6, "Опрос FunPay, секунд", "Слишком частые запросы не нужны.", 4, 60),
    "provider_poll_seconds": ("int", 30, "Проверка выдачи, секунд", "Вебхуки дополняют, но не заменяют проверку.", 10, 600),
    "catalog_seconds": ("int", 900, "Обновление каталога, секунд", "Перед покупкой цена читается заново.", 300, 86400),
    "raise_seconds": ("int", 3600, "Минимальная пауза поднятия, секунд", "Если FunPay требует больше, бот ждёт дольше.", 600, 86400),
    "balance_low_rub": ("money", 500, "Низкий баланс, ₽", "В ручном режиме остаток оценочный.", 0, 10000000),
    "max_order_rub": ("money", 2000, "Лимит закупки на заказ, ₽", "Локальная предварительная проверка, не потолок цены в API.", 1, 1000000),
    "daily_limit_rub": ("money", 5000, "Лимит закупок в сутки, ₽", "Сутки определяются в выбранном часовом поясе.", 1, 10000000),
    "max_quantity": ("int", 100, "Максимум штук в заказе FunPay", "Большее количество уходит на ручную проверку.", 1, 10000),
    "price_buffer_percent": ("number", 3.0, "Резерв на изменение цены, %", "GameCore не принимает максимальную цену в запросе.", 0, 100),
    "funpay_fee_percent": ("number", 0.0, "Учитываемая комиссия FunPay, %", "Введите фактическую ставку; 0 не является обещанием отсутствия комиссии.", 0, 90),
    "provider_fee_percent": ("number", 0.0, "Комиссия пополнения/конвертации GameCore, %", "Используется в оценке себестоимости.", 0, 90),
    "funpay_sum_is_net": ("bool", False, "Сумма заказа уже после комиссии", "Включите только после сверки реального заказа."),
    "alert_repeat_seconds": ("int", 1800, "Повтор критической тревоги, секунд", "Подтверждённая тревога не повторяется.", 60, 86400),
    "alert_chat_ids": ("text", "", "Telegram chat_id получателей", "Числа через запятую; каждый пользователь должен нажать Start у бота."),
    "alert_kinds": ("text", "balance,manual,unknown,partial,failed,funpay,gamecore,security,price,stock", "Типы тревог", "Имена через запятую; можно отключать отдельные типы."),
    "help_words": ("text", "оператор,админ,помощь,возврат,refund,help", "Слова вызова оператора", "Отдельные слова через запятую."),
    "timezone": ("text", "Europe/Amsterdam", "Часовой пояс", "Например Europe/Amsterdam, Europe/Kyiv или Europe/Moscow."),
    "backup_hours": ("int", 24, "Резервная копия каждые, часов", "Копии на том же диске нужно дополнительно скачивать вне Railway.", 1, 168),
    "backup_keep": ("int", 7, "Хранить резервных копий", "", 2, 30),
    "message_retention_days": ("int", 90, "Хранить сообщения, дней", "Заказы и защита от дублей не удаляются.", 7, 730),
    "balance_mode": ("choice", "manual", "Источник остатка", "manual: вы сверяете остаток; api: только согласованный с поддержкой метод.", ["manual", "api"]),
    "balance_path": ("text", "", "Подтверждённый путь API остатка", "Пусто по умолчанию. Публичная документация такого метода не содержит."),
    "balance_json_field": ("text", "", "Путь к числу в JSON остатка", "Например data.balance — только если это подтвердит GameCore."),
    "balance_ttl_seconds": ("int", 120, "Максимальный возраст API-остатка, секунд", "Просроченный API-остаток запрещает новую закупку.", 30, 3600),
    "fees_confirmed": ("bool", False, "Тарифы и комиссии сверены с поставщиком", "Условие включения live."),
    "dynamic_price_accepted": ("bool", False, "Понимаю риск изменения цены при создании заказа", "API не фиксирует котировку и не принимает верхний предел."),
    "provider_terms_confirmed": ("bool", False, "GameCore подтвердил предоплату и работу без IP-привязки", "Без кредитного лимита; постоянный IP в этой сборке не используется."),
    "stats_from": ("text", "", "Начало статистического периода", "Дата YYYY-MM-DD. Фильтр, а не удаление истории."),
}
TEMPLATES = {
    "request_uid": ("Запрос UID", "Оплата получена. Заказ #{order_id}: {uc} UC ({quantity} шт.).\nОтправьте только числовой ID аккаунта PUBG Mobile. Пароль и коды входа не нужны."),
    "confirm_uid": ("Подтверждение UID", "Заказ #{order_id}: {uc} UC.\nID: {uid}.\nПроверьте ID: GameCore не проверяет ник игрока. Для подтверждения напишите: ПОДТВЕРЖДАЮ {code}"),
    "processing": ("Пополнение отправлено", "Заказ #{order_id} принят в обработку: {uc} UC на ID {uid}. Сообщим после проверки результата."),
    "completed": ("Успешная выдача", "{uc} UC зачислены на ID {uid}. Заказ #{order_id}. Проверьте поступление в игре и после этого подтвердите выполнение на FunPay."),
    "waiting_balance": ("Ожидание баланса", "Заказ #{order_id} сохранён. Возникла задержка пополнения, администратор уже уведомлён. Повторно оплачивать ничего не нужно."),
    "problem": ("Проблемный заказ", "Заказ #{order_id} передан администратору для проверки. Не оплачивайте повторно; подтверждать выполнение пока не нужно."),
    "invalid_uid": ("Неправильный формат UID", "Пришлите только ID PUBG Mobile: от 6 до 15 цифр. Для помощи напишите «оператор»."),
    "selection": ("Несколько заказов в чате", "У вас несколько заказов: {orders}. В начале сообщения укажите #НОМЕР_ЗАКАЗА, затем ID или текст подтверждения."),
    "help": ("Вызов оператора", "Передаю чат администратору. Автоматические ответы в этом чате приостановлены."),
    "reminder": ("Напоминание", "Для заказа #{order_id} на {uc} UC всё ещё нужен ID или подтверждение ID. Следуйте предыдущему сообщению."),
    "cancelled": ("Отменённый заказ", "Заказ #{order_id} больше не ожидает выдачи. Если пополнение уже было отправлено, его результат проверит администратор."),
}
SECRET_ENV = {"gamecore_key": "GAMECORE_API_KEY", "webhook_secret": "GAMECORE_WEBHOOK_SECRET", "funpay_key": "FUNPAY_GOLDEN_KEY", "funpay_user_agent": "FUNPAY_USER_AGENT", "telegram_token": "TELEGRAM_BOT_TOKEN"}
