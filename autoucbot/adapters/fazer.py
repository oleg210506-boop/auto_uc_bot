"""FazerCards API v2: explicit contracts, decimal money, no hidden POST retries.

Both transports share validation. Async adapters are available to future asyncio
consumers; the existing single background worker uses the synchronous client.
Neither transport owns a TON wallet or receives Telegram/Fragment login cookies.
Protocol sources and the unverified Fragment requirement: PROTOCOL_SOURCES.md.
"""
from __future__ import annotations
import hashlib
import hmac
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Mapping
import httpx
from .gamecore import ProviderError
from ..utils import BusinessError, positive_int, validate_uid

BASE_URL = 'https://api.fzr.cards/api/v2'
MICROS = Decimal(1_000_000)
TERMINAL = {'completed', 'failed', 'refund'}
PENDING = {'created', 'pending', 'queued', 'processing', 'in_progress'}


def decimal_money(value: Any, *, allow_zero: bool = True) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ProviderError('schema', 'FazerCards: некорректная денежная сумма') from None
    if not amount.is_finite() or amount < 0 or amount > 1_000_000_000 or (not allow_zero and amount == 0):
        raise ProviderError('schema', 'FazerCards: сумма вне допустимого диапазона')
    return amount


def to_micros(value: Any) -> int:
    return int((decimal_money(value) * MICROS).to_integral_value(rounding=ROUND_CEILING))


def usd_to_rub(micros: int, fx: Any) -> int:
    rate = decimal_money(fx, allow_zero=False)
    return int((Decimal(micros) / MICROS * rate * 100).to_integral_value(rounding=ROUND_CEILING))


def username(value: Any) -> str:
    value = str(value).strip()
    if value.startswith('@'):
        value = value[1:]
    # Syntax only, NOT a guarantee that the username exists or is owned by the buyer.
    # Short collectible usernames are allowed. Links, spaces and numeric IDs are not.
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise BusinessError('Нужен @username Telegram (4–32 латинских символа), не ID, телефон или ссылка.')
    return '@' + value.lower()


def opaque(value: Any, label: str = 'идентификатор') -> str:
    value = str(value)
    if not value or len(value) > 250 or any(ord(c) < 32 for c in value):
        raise BusinessError('Некорректный ' + label)
    return value


def order_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'ord-[0-9]{1,24}', value):
        raise ProviderError('schema', 'FazerCards: неизвестный формат номера операции')
    return value


def unwrap_order(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get('order'), dict):
        raise ProviderError('schema', 'FazerCards не вернул объект order')
    result = data['order']
    order_id(result.get('id'))
    if not isinstance(result.get('status'), str):
        raise ProviderError('schema', 'FazerCards не вернул статус операции')
    return result


def parse_response(response: httpx.Response, monetary: bool) -> dict:
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        data = None
    unknown = 'unknown' if monetary else 'network'
    status = response.status_code
    if not isinstance(data, dict):
        raise ProviderError(unknown, f'FazerCards HTTP {status}: ответ не подтверждён')
    # Do not accept an envelope containing a possible created order as a definite rejection.
    possible_order = isinstance(data.get('order'), dict) or bool(data.get('order_id'))
    if 200 <= status < 300 and data.get('ok') is True:
        return data
    if possible_order and monetary:
        raise ProviderError('unknown', 'FazerCards: неоднозначный ответ с номером операции')
    if status in (401, 403):
        raise ProviderError('auth', 'FazerCards: ключ, подписка или доступ к направлению не разрешены')
    if status == 409:
        raise ProviderError(unknown, 'FazerCards: конфликт операции; повторная покупка запрещена')
    if status == 429 and data.get('ok') is False:
        try:
            delay = max(5, min(3600, int(response.headers.get('Retry-After', '60'))))
        except (ValueError, TypeError):
            delay = 60
        raise ProviderError('retry', 'FazerCards: ограничение частоты запросов', delay)
    error = str(data.get('code', '')) + ' ' + str(data.get('error', ''))
    if data.get('ok') is False and 400 <= status < 500:
        if any(v in error.lower() for v in ('insufficient_balance', 'insufficient funds', 'insufficient balance', 'недостаточно средств')):
            raise ProviderError('balance', 'FazerCards: недостаточно средств')
        raise ProviderError('rejected', f'FazerCards отклонил запрос, HTTP {status}. Проверьте поля, доступ и кабинет.')
    raise ProviderError(unknown if monetary else 'schema', f'FazerCards HTTP {status}: результат не подтверждён')


def buy_request(service: str, payload: Mapping[str, Any], key: str) -> tuple[str, dict, str | None]:
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,200}', key or ''):
        raise BusinessError('Нужен сохранённый уникальный ключ операции')
    if service == 'uc':
        if set(payload) != {'category_id', 'offer_id', 'fields'}:
            raise BusinessError('Неизвестные поля запроса UC')
        fields = payload['fields']
        if not isinstance(fields, dict) or not fields or any(not isinstance(k, str) or not isinstance(v, str) for k, v in fields.items()):
            raise BusinessError('fields должен быть непустым объектом строк')
        body = {'category_id': opaque(payload['category_id']), 'offer_id': opaque(payload['offer_id']), 'fields': dict(fields)}
        return '/topups/order', body, key
    if service == 'stars':
        if set(payload) != {'telegram_username', 'quantity'}:
            raise BusinessError('Неизвестные поля запроса Stars')
        quantity = positive_int(payload['quantity'], 10000)
        if quantity < 50:
            raise BusinessError('Минимальная покупка — 50 Stars')
        body = {'telegram_username': username(payload['telegram_username']), 'quantity': quantity}
        # Official SDK and the Stars route do not expose Idempotency-Key support.
        # Persist the LOCAL key, but never pretend that the server deduplicates it.
        return '/telegram/stars/buy', body, None
    raise BusinessError('Неизвестное направление закупки')


class FazerClient:
    """Synchronous bridge for the existing worker. No network IO in page rendering."""
    def __init__(self, key: str, client: httpx.Client | None = None):
        self.key = key
        self.client = client or httpx.Client(timeout=httpx.Timeout(25, connect=8), follow_redirects=False)

    def request(self, method: str, path: str, *, body=None, params=None, idem=None, monetary=False) -> dict:
        if not self.key:
            raise ProviderError('auth', 'API-ключ FazerCards не задан')
        if not path.startswith('/') or any(x in path for x in ('..', '\\', '://', '?', '#')):
            raise BusinessError('Недопустимый путь FazerCards API')
        headers = {'X-API-Key': self.key, 'Accept': 'application/json', 'User-Agent': 'autoUCbot/2.0'}
        if idem:
            headers['Idempotency-Key'] = idem
        try:
            response = self.client.request(method, BASE_URL + path, json=body, params=params, headers=headers, follow_redirects=False)
        except httpx.HTTPError:
            raise ProviderError('unknown' if monetary else 'network', 'FazerCards: ответ не получен') from None
        return parse_response(response, monetary)

    def buy_item(self, service: str, payload: dict, key: str) -> dict:
        path, body, idem = buy_request(service, payload, key)
        data = self.request('POST', path, body=body, idem=idem, monetary=True)
        try:
            return unwrap_order(data)
        except ProviderError:
            raise ProviderError('unknown', 'FazerCards принял запрос, но номер/статус не подтверждён') from None

    def check_order_status(self, code: str) -> dict:
        return unwrap_order(self.request('GET', '/orders/' + order_id(code)))

    def get_balance(self) -> int:
        data = self.request('GET', '/balance')
        if data.get('currency') != 'USD':
            raise ProviderError('schema', 'FazerCards: неожиданная валюта баланса')
        return to_micros(data.get('balance'))

    def account(self) -> dict:
        data = self.request('GET', '/me')
        if not isinstance(data.get('login'), str) or not data['login']:
            raise ProviderError('schema', 'FazerCards: не подтверждён аккаунт поставщика')
        return data

    def subscription(self) -> dict:
        return self.request('GET', '/subscription')

    def plans(self) -> dict:
        return self.request('GET', '/subscription/plans')

    def categories(self) -> list[dict]:
        result, cursor, seen = [], None, set()
        for _ in range(50):
            page = self.request('GET', '/topups', params={'limit': 200, **({'cursor': cursor} if cursor else {})})
            if not isinstance(page.get('items'), list) or not isinstance(page.get('meta'), dict):
                raise ProviderError('schema', 'FazerCards: неизвестная пагинация каталога')
            result.extend(page['items'])
            meta = page['meta']
            if not meta.get('has_more'):
                return result
            cursor = meta.get('next_cursor')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ProviderError('schema', 'FazerCards: повтор/отсутствие курсора каталога')
            seen.add(cursor)
        raise ProviderError('schema', 'Слишком много страниц каталога FazerCards')

    def offers(self, category: str) -> dict:
        data = self.request('GET', '/topups/offers', params={'category_id': opaque(category)})
        if data.get('category_id') != category or not isinstance(data.get('offers'), list) or not isinstance(data.get('fields'), list):
            raise ProviderError('schema', 'FazerCards: категория или схема полей не совпала')
        return data

    def stars_quote(self) -> dict:
        data = self.request('GET', '/telegram/stars')
        decimal_money(data.get('price_per_star'), allow_zero=False)
        lo = max(50, positive_int(data.get('min_amount'), 10000))
        hi = min(10000, positive_int(data.get('max_amount'), 10000))
        if lo > hi:
            raise ProviderError('schema', 'Неверные ограничения Stars')
        return {**data, 'min_amount': lo, 'max_amount': hi}

    def validation_games(self) -> list:
        data = self.request('GET', '/topups/validate-id')
        if not isinstance(data.get('items'), list):
            raise ProviderError('schema', 'Неизвестная схема проверки UID')
        return data['items']

    def validate_player(self, category: str, fields: dict) -> dict:
        data = self.request('POST', '/topups/validate-id', body={'category_id': category, 'fields': fields})
        if not isinstance(data.get('valid'), bool):
            raise ProviderError('schema', 'UID не удалось проверить')
        return data

    def close(self):
        self.client.close()


class AsyncFazerClient:
    """Native async transport. A cancelled money request must be reconciled by caller."""
    def __init__(self, key: str, client: httpx.AsyncClient | None = None):
        self.key = key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False)

    async def request(self, method, path, *, body=None, idem=None, monetary=False):
        if not self.key:
            raise ProviderError('auth', 'API-ключ FazerCards не задан')
        if not path.startswith('/') or any(x in path for x in ('..','\\','://','?','#')):
            raise BusinessError('Недопустимый путь FazerCards API')
        headers = {'X-API-Key': self.key, 'Accept': 'application/json', 'User-Agent': 'autoUCbot/2.0'}
        if idem:
            headers['Idempotency-Key'] = idem
        try:
            response = await self.client.request(method, BASE_URL + path, json=body, headers=headers, follow_redirects=False)
        except httpx.HTTPError:
            raise ProviderError('unknown' if monetary else 'network', 'FazerCards: ответ не получен') from None
        return parse_response(response, monetary)

    async def buy_item(self, service: str, payload: dict, key: str) -> dict:
        path, body, idem = buy_request(service, payload, key)
        data = await self.request('POST', path, body=body, idem=idem, monetary=True)
        try:
            return unwrap_order(data)
        except ProviderError:
            raise ProviderError('unknown', 'FazerCards: неизвестный результат после отправки') from None

    async def check_order_status(self, code):
        return unwrap_order(await self.request('GET', '/orders/' + order_id(code)))

    async def get_balance(self):
        data = await self.request('GET', '/balance')
        if data.get('currency') != 'USD':
            raise ProviderError('schema', 'Неожиданная валюта баланса')
        return to_micros(data.get('balance'))

    async def aclose(self):
        await self.client.aclose()


class FazerCardsUCAdapter:
    def __init__(self, client: AsyncFazerClient):
        self.client = client

    async def buy_item(self, category_id: str, offer_id: str, fields: dict, idempotency_key: str):
        return await self.client.buy_item('uc', {'category_id': category_id, 'offer_id': offer_id, 'fields': fields}, idempotency_key)

    async def check_order_status(self, code):
        return await self.client.check_order_status(code)

    async def get_balance(self):
        return await self.client.get_balance()


class FazerCardsStarsAdapter:
    """Stars backend without browser sessions. This is NOT a direct TON transfer."""
    def __init__(self, client: AsyncFazerClient):
        self.client = client

    async def buy_item(self, telegram_username: str, quantity: int, idempotency_key: str):
        return await self.client.buy_item('stars', {'telegram_username': telegram_username, 'quantity': quantity}, idempotency_key)

    async def check_order_status(self, code):
        return await self.client.check_order_status(code)

    async def get_balance(self):
        return await self.client.get_balance()


def webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not re.fullmatch(r'sha256=[0-9a-f]{64}', signature or ''):
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
