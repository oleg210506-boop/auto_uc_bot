"""Per-direction settings. Wallet credentials are shared, fulfillment rules are not."""
from .config import FIELDS, TEMPLATES

SERVICE_NAMES = {'uc': 'PUBG Mobile UC', 'stars': 'Telegram Stars'}
SERVICE_KEYS = (
    'confirm_uid', 'auto_raise', 'auto_hide', 'reminders', 'reminder_seconds',
    'reminder_limit', 'uid_timeout_seconds', 'processing_timeout_seconds',
    'max_order_rub', 'daily_limit_rub', 'max_quantity', 'price_buffer_percent',
    'provider_fee_percent', 'funpay_fee_percent', 'funpay_sum_is_net',
    'raise_seconds', 'pause_on_problem', 'pause_chat_on_help', 'help_words',
)
SERVICE_FIELDS = {key: FIELDS[key] for key in SERVICE_KEYS}
SERVICE_FIELDS = {'enabled': ('bool', True, 'Разрешить это направление', 'Общая пауза и разрешение live остаются обязательными.'), **SERVICE_FIELDS}
SERVICE_FIELDS.update({
    'alert_chat_ids': ('text', '', 'Отдельные получатели тревог', 'Пусто — использовать общих получателей. Числовые chat_id через запятую.'),
    'min_units': ('int', 50, 'Минимум Stars в одном заказе', 'Для Stars минимум 50; для UC это поле не используется.', 50, 10000),
    'max_units': ('int', 10000, 'Максимум Stars в одном заказе', 'Также учитывается ограничение API. Заказ не дробится для обхода лимита.', 50, 10000),
    'validate_recipient': ('bool', False, 'Проверять UID через FazerCards (только UC)', 'Сначала загрузите доступные игры проверки. Для Stars проверяется только формат @username.'),
    'validation_category': ('text', '', 'category_id проверки UID', 'Это отдельный ID из /topups/validate-id, не ID каталога закупки.'),
    'validation_field': ('text', 'player_id', 'Ключ поля проверки UID', 'Точный key из схемы проверки.'),
})

def default_templates(service):
    result = {k: v[1] for k, v in TEMPLATES.items()}
    if service == 'uc':
        result['confirm_uid'] = 'Заказ #{order_id}: {amount} UC.\nID: {recipient}. {nickname}\nДля подтверждения напишите: ПОДТВЕРЖДАЮ {code}'
        return result
    result.update({
        'request_uid': 'Оплата получена. Заказ #{order_id}: {amount} Stars ({quantity} шт. лота).\nПришлите @username получателя Telegram. Телефон, пароль и код входа не нужны.',
        'confirm_uid': 'Заказ #{order_id}: {amount} Stars.\nПолучатель: {recipient}. Проверьте написание — принадлежность аккаунта автоматически не подтверждена.\nНапишите: ПОДТВЕРЖДАЮ {code}',
        'processing': 'Заказ #{order_id}: {amount} Stars для {recipient} отправлен поставщику. Ожидаем подтверждения результата.',
        'completed': '{amount} Stars отправлены получателю {recipient}. Заказ #{order_id}. Проверьте поступление в Telegram и только после этого подтвердите выполнение на FunPay.',
        'invalid_uid': 'Пришлите только @username Telegram: латинские буквы, цифры и _. Не ссылку, телефон или числовой ID. Для помощи напишите «оператор».',
        'reminder': 'Для заказа #{order_id} на {amount} Stars нужен @username получателя или подтверждение из предыдущего сообщения.',
    })
    return result
