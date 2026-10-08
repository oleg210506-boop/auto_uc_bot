"""FazerCards fulfillment extension for the original order engine.

One FunPay order -> one immutable batch -> durable individual supplier intents.
UC is sent in individual top-ups; Stars is one aggregated order, never silently
split. Persist 'sending' BEFORE HTTP. An unknown outcome NEVER creates a new
intent. Reconciliation can only bind a reviewed provider ID, not retry money.
"""
from __future__ import annotations
import functools
import hashlib
import json
import re
import secrets
import time
import uuid
from decimal import Decimal, ROUND_CEILING
from .adapters.fazer import (FazerClient, MICROS, PENDING, TERMINAL, decimal_money,
                             order_id, to_micros, usd_to_rub, username)
from .adapters.demo_fazer import DemoFazer
from .adapters.gamecore import ProviderError
from .adapters.funpay import FunPayError
from .commerce_config import SERVICE_FIELDS, default_templates
from .config import TEMPLATES
from .db import dumps
from .utils import BusinessError, cents, positive_int, render_template, validate_uid, day_start

LOCAL_SKU_OFFSET = 1_000_000_000
ACTIVE_PARTS = ('prepared', 'blocked', 'rate_limited', 'sending', 'unknown', 'processing')


def synchronized(fn):
    @functools.wraps(fn)
    def call(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return call


def is_fazer(row):
    if not row:
        return False
    if row.get('supplier') == 'fazer':
        return True
    return json.loads(row.get('snapshot', '{}')).get('supplier') == 'fazer'


def service_of(row):
    return row.get('service', json.loads(row.get('snapshot', '{}')).get('service', 'uc'))


class FazerMixin:
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._fazer_client = None
        self._fazer_key = None
        self.demo_fazer = DemoFazer(self.db)
        for service in ('uc', 'stars'):
            for key, spec in SERVICE_FIELDS.items():
                name = 'svc_' + service + '_' + key
                if self.db.setting(name) is None:
                    self.db.set(name, (service == 'uc') if key == 'enabled' else self.db.setting(key, spec[1]))
            for key, value in default_templates(service).items():
                name = 'tpl_' + service + '_' + key
                if self.db.setting(name) is None:
                    self.db.set(name, value)

    def problem(self, oid, kind, reason, state='manual', pause=False):
        row = self.db.one('SELECT * FROM orders WHERE id=?', (oid,))
        if is_fazer(row):
            self.db.execute('UPDATE orders SET state=?,hold_reason=?,updated=? WHERE id=?', (state,reason,time.time(),oid))
            self.alert(kind, 'Нужна проверка заказа', reason, oid)
            self.queue(oid, 'problem', kind)
            if pause and self.rule('pause_on_problem', row):
                self.set_direction_pause(row['service'], reason)
            return
        return super().problem(oid,kind,reason,state,pause)

    def rule(self, key, row):
        if not is_fazer(row):
            return self.db.setting(key)
        return self.service_rule(service_of(row), key)

    def service_rule(self, service, key):
        return self.db.setting('svc_' + service + '_' + key, self.db.setting(key))

    def direction_open(self, service):
        return self.service_rule(service, 'enabled') and not self.db.setting('svc_' + service + '_paused', False)

    def set_direction_pause(self, service, reason):
        self.db.set('svc_' + service + '_paused', True)
        self.db.set('svc_' + service + '_pause_reason', reason)

    def fazer(self, mode=None):
        if (mode or self.dataset()) == 'demo':
            return self.demo_fazer
        key = self.vault.get('fazer_key')
        if self._fazer_client is None or key != self._fazer_key:
            if self._fazer_client:
                self._fazer_client.close()
            self._fazer_client = FazerClient(key)
            self._fazer_key = key
        return self._fazer_client

    def fazer_account(self, mode, force=False):
        key_tag = hashlib.sha256(self.vault.get('fazer_key').encode()).hexdigest() if mode == 'live' else 'demo'
        cached = self.db.setting('fazer_profile:' + mode, {})
        if not force and cached.get('key_tag') == key_tag and time.time() - cached.get('at', 0) < 60:
            return cached
        data = self.fazer(mode).account()
        identity = data.get('login')
        if not isinstance(identity, str) or not identity:
            raise ProviderError('schema', 'Не подтверждён логин FazerCards')
        old = self.db.setting('fazer_identity:' + mode)
        if old and old != identity:
            self.pause('Изменился аккаунт FazerCards. Сверьте старые закупки и ключи.')
            raise BusinessError('Этот ключ относится к другому аккаунту FazerCards. Автоматическая смена кошелька запрещена.')
        self.db.set('fazer_identity:' + mode, identity)
        result = {'login': identity, 'subscriptionActive': data.get('subscriptionActive') is True,
                  'plan': str(data.get('plan', '')), 'planExpiresAt': data.get('planExpiresAt'),
                  'key_tag': key_tag, 'at': time.time()}
        self.db.set('fazer_profile:' + mode, result)
        return result

    @synchronized
    def read_fazer_wallet(self, mode=None):
        mode = mode or self.dataset()
        self.fazer_account(mode)
        balance = self.fazer(mode).get_balance()
        if not isinstance(balance, int) or balance < 0:
            raise ProviderError('schema', 'Баланс FazerCards не подтверждён')
        self.db.execute('INSERT INTO fazer_wallet VALUES(?,?,?,?) ON CONFLICT(mode) DO UPDATE SET micros=excluded.micros,identity=excluded.identity,updated=excluded.updated',
                        (mode, balance, self.db.setting('fazer_identity:' + mode, ''), time.time()))
        self.db.runtime('fazer_wallet:' + mode, {'ok': True, 'usd': str(Decimal(balance) / MICROS)})
        available = balance - self.fazer_reservations(mode)
        if available < to_micros(self.db.setting('fazer_low_usd', 10)):
            self.alert('balance', 'Низкий баланс FazerCards', f'Баланс ${Decimal(balance)/MICROS:.4f}; доступно после защитных резервов ${Decimal(available)/MICROS:.4f}.', key='balance:fazer:' + mode)
        else:
            self.resolve_alert('balance:fazer:' + mode)
        self.resolve_alert('fazer:wallet')
        # Only a balance-induced pause may be automatically released. Never an unknown result.
        if self.db.setting('auto_resume_balance') and balance > 0:
            for o in self.db.rows("SELECT * FROM orders WHERE mode=? AND supplier='fazer' AND state='waiting_balance'", (mode,)):
                b = self.db.one('SELECT * FROM batches WHERE order_id=?', (o['id'],))
                need = 0
                if b:
                    need = self.db.one("SELECT COALESCE(SUM(reserved_micros),0) n FROM fazer_parts WHERE batch_id=? AND state IN('prepared','blocked','rate_limited')", (b['id'],))['n']
                    if not need or available < 0:
                        continue
                    self.db.execute("UPDATE fazer_parts SET state='prepared',next_check=0 WHERE batch_id=? AND state='blocked'", (b['id'],))
                    self.db.execute("UPDATE batches SET state='retry',next_check=0 WHERE id=?", (b['id'],))
                else:
                    need = int(self.db.setting('fazer_balance_need:' + o['id'], 1))
                    if available < need:continue
                    available -= need
                self.db.execute("UPDATE orders SET state='ready',hold_reason='' WHERE id=?", (o['id'],))
                self.resolve_alert('balance:fazer-order:' + o['id'])
            if self.db.setting('pause_reason') == 'fazer_balance' and not self.db.one("SELECT id FROM orders WHERE mode=? AND state='waiting_balance'", (mode,)):

                self.db.set('paused', False)
                self.db.set('pause_reason', '')
        return balance

    def fazer_reservations(self, mode, exclude_part=None):
        # Conservative: pending/unknown supplier operations remain reserved even if
        # the provider already debited them. Temporary under-utilisation beats overspend.
        sql = """SELECT COALESCE(SUM(p.reserved_micros),0) n FROM fazer_parts p
                 JOIN batches b ON b.id=p.batch_id JOIN orders o ON o.id=b.order_id
                 WHERE o.mode=? AND p.state IN('prepared','blocked','rate_limited','sending','unknown','processing')"""
        args = [mode]
        if exclude_part is not None:
            sql += ' AND p.id!=?'
            args.append(exclude_part)
        return self.db.one(sql, args)['n']

    def wallet_view(self, mode=None):
        mode = mode or self.dataset()
        row = self.db.one('SELECT * FROM fazer_wallet WHERE mode=?', (mode,))
        if not row:
            return None
        row['reserved'] = self.fazer_reservations(mode)
        row['available'] = row['micros'] - row['reserved']
        return row

    @synchronized
    def discover_fazer(self):
        # Discovery is read-only. Do not silently choose a region for the owner.
        rows = [r for r in self.fazer('live').categories() if 'pubg' in str(r.get('name', '')).lower()]
        self.db.set('fazer_discovered', rows)
        return rows

    @synchronized
    def sync_fazer_catalog(self, mode=None):
        mode = mode or self.dataset()
        profile = self.fazer_account(mode)
        if not profile['subscriptionActive']:
            raise ProviderError('auth', 'Подписка FazerCards не активна; каталог закупки недоступен')
        fz = self.fazer(mode)
        fx = self.db.setting('fazer_usd_rub') or (100 if mode == 'demo' else 0)
        categories = ['demo_pubg'] if mode == 'demo' else [s.strip() for s in self.db.setting('fazer_categories', '').split(',') if s.strip()]
        rows, failures = [], []
        if categories:
            try:
                known = {r['category_id'] for r in fz.categories() if 'pubg' in str(r.get('name', '')).lower() and isinstance(r.get('category_id'), str)}
                if set(categories) - known:
                    raise BusinessError('Выбранная категория не найдена среди категорий PUBG. Выполните поиск категорий.')
                for category in categories:
                    page = fz.offers(category)
                    for offer in page['offers']:
                        offer_id = str(offer.get('offer_id', ''))
                        if not offer_id:
                            raise ProviderError('schema', 'В каталоге отсутствует offer_id')
                        name = str(offer.get('name', ''))
                        match = re.fullmatch(r'\s*([0-9]+)\s*UC\s*', name, re.I)
                        units = int(match[1]) if match else 0
                        if not 0 < units <= 10000000:
                            units = 0
                        price = str(decimal_money(offer.get('price_usd'), allow_zero=False))
                        rows.append(('uc', category, offer_id, name, units, page['fields'], price, {'offer': offer, 'category_name': page.get('name', '')}))
            except (ProviderError, BusinessError) as exc:
                failures.append(('uc', str(exc)))
                rows = [r for r in rows if r[0] != 'uc']
        try:
            quote = fz.stars_quote()
            rows.append(('stars', 'telegram_stars', 'stars', 'Telegram Stars · цена за 1 звезду', 1,
                         [{'key': 'telegram_username', 'required': True, 'type': 'text'}], str(quote['price_per_star']), quote))
        except (ProviderError, BusinessError) as exc:
            failures.append(('stars', str(exc)))
        now = time.time()
        with self.db.tx() as c:
            c.execute('DELETE FROM catalog WHERE mode=? AND id>=?', (mode, LOCAL_SKU_OFFSET))
            c.execute("UPDATE products SET available=0 WHERE mode=? AND supplier='fazer'", (mode,))
            for service, category, offer_id, name, units, fields, price, raw in rows:
                old = c.execute('SELECT * FROM fazer_skus WHERE mode=? AND service=? AND category_id=? AND offer_id=?', (mode, service, category, offer_id)).fetchone()
                manual_units = old and old['units_confirmed'] == 2 and old['name'] == name
                if manual_units:
                    units = old['units']
                confirmed = 2 if manual_units else int(units > 0)
                c.execute('''INSERT INTO fazer_skus(mode,service,category_id,offer_id,name,units,units_confirmed,price_usd,fields,raw,updated)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mode,service,category_id,offer_id) DO UPDATE SET
                             name=excluded.name,units=excluded.units,units_confirmed=excluded.units_confirmed,
                             price_usd=excluded.price_usd,fields=excluded.fields,raw=excluded.raw,updated=excluded.updated''',
                          (mode, service, category, offer_id, name, units, confirmed, price, dumps(fields), dumps(raw), now))
                rid = c.execute('SELECT id FROM fazer_skus WHERE mode=? AND service=? AND category_id=? AND offer_id=?', (mode, service, category, offer_id)).fetchone()[0]
                local = LOCAL_SKU_OFFSET + rid
                price_rub = usd_to_rub(to_micros(price), fx) if fx else 0
                stock = raw.get('offer', {}).get('in_stock', True) is not False and raw.get('offer', {}).get('available', True) is not False
                payload = {'supplier': 'fazer', 'service': service, 'category_id': category, 'offer_id': offer_id,
                           'price_usd': price, 'in_stock': stock, 'units_confirmed': confirmed,
                           'deliveryDataSchema': [{'id': f.get('key'), 'required': f.get('required', True), 'type': f.get('type', 'text')} for f in fields],
                           'raw': raw}
                c.execute('INSERT INTO catalog VALUES(?,?,?,?,?,?,?,?,?,?)', (local, mode, name, price_rub, 'RUB', category, units, 'id_only', dumps(payload), now))
                if stock and price_rub > 0 and units > 0:
                    c.execute("UPDATE products SET available=1,last_price=? WHERE sku_id=? AND mode=? AND supplier='fazer' AND service=? AND sku_uc=?", (price_rub, local, mode, service, units))
        for service, error in failures:
            self.alert('fazer', 'Не удалось загрузить ' + service.upper(), error, key='fazer:catalog:' + service)
        for service in {'uc', 'stars'} - {s for s, _ in failures}:
            self.resolve_alert('fazer:catalog:' + service)
        if not rows:
            raise BusinessError('Не загружен ни один каталог FazerCards. Проверьте подписку, ключ и выбранные категории.')
        self.db.runtime('catalog:' + mode, {'ok': not failures, 'supplier': 'FazerCards', 'count': len(rows), 'failures': failures})
        return len(rows)

    def sync_catalog(self, mode=None):
        mode = mode or self.dataset()
        if mode == 'demo':
            return super().sync_catalog(mode)
        return self.sync_fazer_catalog(mode)

    @synchronized
    def confirm_sku_units(self, local_id, units, actor):
        rid = positive_int(local_id, 2147483647) - LOCAL_SKU_OFFSET
        row = self.db.one('SELECT * FROM fazer_skus WHERE id=?', (rid,))
        if not row or row['service'] != 'uc':
            raise BusinessError('Это не номинал UC FazerCards')
        units = positive_int(units, 10000000)
        self.db.execute('UPDATE fazer_skus SET units=?,units_confirmed=2 WHERE id=?', (units, rid))
        c = self.db.one('SELECT * FROM catalog WHERE id=? AND mode=?', (local_id, row['mode']))
        if c:
            payload = json.loads(c['payload']); payload['units_confirmed'] = 2
            self.db.execute('UPDATE catalog SET uc=?,payload=? WHERE id=? AND mode=?', (units, dumps(payload), local_id, row['mode']))
        self.db.execute('UPDATE products SET verified=0,available=0 WHERE sku_id=? AND mode=?', (local_id, row['mode']))
        self.db.audit(actor, 'fazer.nominal.confirmed', local_id, str(units))

    def save_product(self, data, pid=None, actor='owner'):
        sku = positive_int(data.get('sku_id'), 2147483647)
        if sku < LOCAL_SKU_OFFSET:
            if self.dataset() == 'live':
                raise BusinessError('Новые закупки GameCore отключены. Выберите SKU FazerCards.')
            return super().save_product(data, pid, actor)
        cat = self.db.one('SELECT * FROM catalog WHERE id=? AND mode=?', (sku, self.dataset()))
        if not cat:
            raise BusinessError('Сначала загрузите выбранный каталог FazerCards')
        payload = json.loads(cat['payload'])
        if payload.get('supplier') != 'fazer' or not payload.get('units_confirmed'):
            raise BusinessError('Номинал не подтверждён. Уточните UC в каталоге и подтвердите его вручную.')
        selected = str(data.get('service') or payload['service'])
        if selected != payload['service']:
            raise BusinessError('Направление товара не совпадает с выбранным SKU')
        with self.lock:
            result = super().save_product(data, pid, actor)
            self.db.execute("UPDATE products SET supplier='fazer',service=? WHERE id=?", (selected, result))
        return result

    def queue(self, oid, template, suffix='', force=False):
        row = self.db.one('SELECT * FROM orders WHERE id=?', (oid,))
        if not is_fazer(row):
            return super().queue(oid, template, suffix, force)
        service = service_of(row)
        text = render_template(self.db.setting('tpl_' + service + '_' + template),
                               order_id=oid, uc=row['uc'], amount=row['uc'], unit='UC' if service == 'uc' else 'Stars',
                               quantity=row['quantity'], uid=row['uid'] or '', recipient=row['uid'] or '',
                               code=row['confirm_code'] or '', buyer=row['buyer'],
                               nickname=self.db.setting('nickname:' + oid, ''))
        self.queue_text(row, text, f'{oid}:{template}:{suffix}', 'manual' if force else 'auto')

    def validate_recipient(self, text, row, verify=False):
        if service_of(row) == 'stars':
            return username(text)
        uid = validate_uid(text)
        if is_fazer(row) and verify and self.service_rule('uc', 'validate_recipient'):
            category = self.service_rule('uc', 'validation_category')
            field = self.service_rule('uc', 'validation_field')
            games = self.fazer(row['mode']).validation_games()
            game = next((g for g in games if g.get('category_id') == category), None)
            if not game or not any(f.get('key') == field for f in game.get('fields', [])):
                raise BusinessError('Настройка проверки UID не совпала со схемой API')
            data = self.fazer(row['mode']).validate_player(category, {field: uid})
            if not data['valid']:
                raise BusinessError('Поставщик не подтвердил Player ID')
            nick = str(data.get('player_name', '')).strip()[:100]
            self.db.set('nickname:' + row['id'], 'Ник: ' + nick if nick else '')
        return uid

    def live_ready(self):
        # Reuse all the original security gates, replacing only supplier-specific ones.
        reasons = [r for r in super().live_ready() if r not in ('Не задан секрет: gamecore_key', 'Остаток GameCore не сверялся')]
        if not self.vault.get('fazer_key'):
            reasons.append('Не задан API-ключ FazerCards')
        profile = self.db.setting('fazer_profile:live', {})
        if not profile.get('subscriptionActive'):
            reasons.append('Не проверены аккаунт и активная подписка FazerCards')
        if not self.db.one("SELECT * FROM fazer_wallet WHERE mode='live'"):
            reasons.append('Баланс FazerCards ещё не прочитан по API')
        if not self.db.setting('fazer_usd_rub'):
            reasons.append('Введите фактический учётный курс USD → RUB')
        if self.db.setting('migration_frozen', False):
            reasons.append('Установка заморожена для переноса; требуется подтверждение завершения миграции')
        if not self.db.one("SELECT id FROM products WHERE mode='live' AND supplier='fazer' AND enabled=1 AND verified=1 AND available=1 AND archived=0"):
            reasons.append('Нет настроенного live-товара FazerCards')
        return reasons

    def purchase_allowed(self, mode):
        return not self.db.setting('migration_frozen', False) and super().purchase_allowed(mode)

    def _fz_batch(self, bid):
        b = self.db.one('SELECT * FROM batches WHERE id=?', (bid,))
        return b if b and json.loads(b['payload']).get('supplier') == 'fazer' else None

    def _quote(self, row, product):
        client = self.fazer(row['mode'])
        cached = self.db.one('SELECT * FROM catalog WHERE mode=? AND id=?', (row['mode'], product['sku_id']))
        if not cached:
            raise BusinessError('SKU отсутствует в загруженном каталоге')
        meta = json.loads(cached['payload'])
        if meta.get('supplier') != 'fazer' or meta.get('service') != row['service'] or cached['uc'] != product['sku_uc']:
            raise BusinessError('Изменился SKU, номинал или направление')
        if row['service'] == 'stars':
            quote = client.stars_quote()
            lo = max(50, int(self.service_rule('stars', 'min_units')), int(quote['min_amount']))
            hi = min(10000, int(self.service_rule('stars', 'max_units')), int(quote['max_amount']))
            if not lo <= row['uc'] <= hi:
                raise BusinessError(f'Заказ должен содержать от {lo} до {hi} Stars. Выдача не выполнялась.')
            cost = to_micros(decimal_money(quote['price_per_star']) * row['uc'])
            return [{'body': {'telegram_username': username(row['uid']), 'quantity': row['uc']}, 'units': row['uc'], 'cost': cost}]
        configured = ['demo_pubg'] if row['mode'] == 'demo' else [s.strip() for s in self.db.setting('fazer_categories', '').split(',')]
        if meta['category_id'] not in configured:
            raise BusinessError('Категория больше не разрешена для закупок')
        page = client.offers(meta['category_id'])
        offer = next((v for v in page['offers'] if str(v.get('offer_id')) == meta['offer_id']), None)
        if not offer or offer.get('in_stock') is False or offer.get('available') is False:
            raise BusinessError('Товара нет в текущем каталоге FazerCards')
        if str(offer.get('name', '')) != cached['name']:
            raise BusinessError('Название SKU изменилось: номинал требует повторной проверки')
        field_names = {f.get('key') for f in page['fields']}
        extra = json.loads(product['extra_delivery'])
        if product['uid_field'] not in field_names or set(extra) - field_names:
            raise BusinessError('Изменилась схема полей пополнения')
        for f in page['fields']:
            if f.get('required', True) and f.get('key') != product['uid_field'] and not extra.get(f.get('key')):
                raise BusinessError('Не заполнено обязательное поле пополнения: ' + str(f.get('key')))
        extra[product['uid_field']] = validate_uid(row['uid'])
        body = {'category_id': meta['category_id'], 'offer_id': meta['offer_id'], 'fields': extra}
        cost = to_micros(decimal_money(offer['price_usd'], allow_zero=False))
        return [{'body': body, 'units': product['sku_uc'], 'cost': cost} for _ in range(row['sku_units'])]

    def _preflight_order(self, o):
        if not self.purchase_allowed(o['mode']) or not self.direction_open(o['service']):
            return False
        control = self.db.one('SELECT * FROM chat_controls WHERE chat_id=?', (o['chat_id'],))
        if control and control['manual']:
            return False
        p = json.loads(o['snapshot'])
        current = self.db.one('SELECT * FROM products WHERE id=?', (o['product_id'],))
        if not current or not current['enabled'] or current['archived'] or not current['verified']:
            raise BusinessError('Товар выключен или не проверен')
        # Rebinding the live product never changes an already-paid immutable recipe.
        if current['supplier'] != 'fazer' or current['service'] != o['service']:
            raise BusinessError('Направление товара изменилось после оплаты')
        if not o['confirmed']:
            raise BusinessError('Получатель не подтверждён')
        self.validate_recipient(o['uid'], o)
        d = self.fp(o['mode']).order(o['id'])
        self.refresh_status(d, o)
        if d['status'] != 'paid':
            return False
        if p['marker'] not in d['description'] or p['fp_subcategory'] != d['subcategory']:
            raise BusinessError('Оплаченный заказ не совпал с разрешённым объявлением')
        if not self.fazer_account(o['mode'])['subscriptionActive']:
            raise ProviderError('auth', 'Подписка FazerCards не активна')
        return True

    def _limits(self, o, p, total_micros, reserved_micros, fx, exclude_bid=None):
        quoted_rub, reserve_rub = usd_to_rub(total_micros, fx), usd_to_rub(reserved_micros, fx)
        if reserve_rub > cents(self.rule('max_order_rub', o)) or (p['max_cost'] and quoted_rub > p['max_cost'] * o['quantity']):
            raise BusinessError('Закупка превышает лимит стоимости заказа')
        effective = int((Decimal(quoted_rub) * (1 + Decimal(str(self.rule('provider_fee_percent', o))) / 100)).to_integral_value(rounding=ROUND_CEILING))
        if o['revenue'] - o['fee'] - effective < p['min_profit'] * o['quantity']:
            raise BusinessError('Прибыль ниже заданного минимума')
        start = day_start(time.time(), self.db.setting('timezone'))
        daily = self.db.one('''SELECT COALESCE(SUM(CASE WHEN b.state IN('completed','failed','partial','rejected') THEN b.actual_cost ELSE MAX(b.actual_cost,b.quote) END),0) n
                              FROM batches b JOIN orders o ON o.id=b.order_id
                              WHERE o.mode=? AND o.service=? AND b.created>=? AND b.id!=?
                              AND b.state NOT IN('rejected','balance')''', (o['mode'], o['service'], start, exclude_bid or -1))['n']
        if daily + reserve_rub > cents(self.rule('daily_limit_rub', o)):
            raise BusinessError('Достигнут суточный лимит направления')
        return reserve_rub

    @synchronized
    def prepare(self, oid):
        o = self.db.one('SELECT * FROM orders WHERE id=?', (oid,))
        if not is_fazer(o):
            if o and o['mode'] == 'live':
                self.problem(oid, 'manual', 'Старый заказ GameCore: только ручная сверка. Автозакупка через нового поставщика запрещена.')
                return
            return super().prepare(oid)
        if o['state'] != 'ready' or self.db.one('SELECT id FROM batches WHERE order_id=?', (oid,)):
            return
        try:
            if not self._preflight_order(o):
                return
            p = json.loads(o['snapshot'])
            if o['quantity'] > self.rule('max_quantity', o):
                raise BusinessError('Превышен лимит количества направления')
            if o['service'] == 'uc' and o['sku_units'] > 1000:
                raise BusinessError('Более 1000 отдельных пополнений в заказе: требуется ручная проверка')
            fx = str(self.db.setting('fazer_usd_rub') or (100 if o['mode'] == 'demo' else 0))
            if not decimal_money(fx):
                raise BusinessError('Сначала задайте фактический курс USD → RUB')
            parts = self._quote(o, p)
            buffer = 1 + Decimal(str(self.rule('price_buffer_percent', o))) / 100
            for part in parts:
                part['reserve'] = int((Decimal(part['cost']) * buffer).to_integral_value(rounding=ROUND_CEILING))
            total, reserve = sum(v['cost'] for v in parts), sum(v['reserve'] for v in parts)
            reserve_rub = self._limits(o, p, total, reserve, fx)
            balance = self.read_fazer_wallet(o['mode']) - self.fazer_reservations(o['mode'])
            if balance < reserve:
                self.db.set('fazer_balance_need:' + oid, reserve)
                self.fazer_wait_balance(o)
                return
            now = time.time()
            key = uuid.uuid4().hex
            with self.db.tx() as c:
                if c.execute('SELECT 1 FROM batches WHERE order_id=?', (oid,)).fetchone():
                    return
                bid = c.execute('''INSERT INTO batches(order_id,key,units,uc_per_unit,payload,quote,state,created)
                                   VALUES(?,?,?,?,?,?,'retry',?)''',
                                (oid, key, o['sku_units'], p['sku_uc'], dumps({'supplier': 'fazer', 'service': o['service'], 'fx': fx}), reserve_rub, now)).lastrowid
                for i, part in enumerate(parts):
                    body = dumps(part['body'])
                    idem = f'auc_{key}_{i}'
                    c.execute('''INSERT INTO fazer_parts(batch_id,ordinal,mode,service,units,idem_key,body,body_hash,quoted_micros,reserved_micros,fx,created,updated)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (bid, i, o['mode'], o['service'], part['units'], idem, body, hashlib.sha256(body.encode()).hexdigest(), part['cost'], part['reserve'], fx, now, now))
                c.execute("UPDATE orders SET state='processing',hold_reason='',updated=? WHERE id=?", (now, oid))
            self.send_batch(bid)
        except (ProviderError, BusinessError, FunPayError) as exc:
            self.problem(oid, 'manual', str(exc))

    def fazer_wait_balance(self, o):
        self.db.execute("UPDATE orders SET state='waiting_balance',hold_reason='fazer_balance',updated=? WHERE id=?", (time.time(), o['id']))
        self.alert('balance', 'Не хватает баланса FazerCards', 'Пополните баланс в кабинете FazerCards. Автоматическая проверка остатка продолжится.', o['id'], key='balance:fazer-order:' + o['id'])
        self.pause('fazer_balance')
        self.queue(o['id'], 'waiting_balance')

    @synchronized
    def send_batch(self, bid, recovery=False):
        batch = self._fz_batch(bid)
        if not batch:
            old = self.db.one('SELECT o.mode FROM batches b JOIN orders o ON o.id=b.order_id WHERE b.id=?', (bid,))
            if old and old['mode'] == 'live':
                self.db.execute("UPDATE batches SET next_check=? WHERE id=?", (time.time()+3600,bid))
                return  # Historical GameCore operations are read-only after migration.
            return super().send_batch(bid, recovery)
        o = self.db.one('SELECT * FROM orders WHERE id=?', (batch['order_id'],))
        # Unknown parts are NEVER resent, even if a caller supplies recovery=True.
        if self.db.one("SELECT id FROM fazer_parts WHERE batch_id=? AND state IN('sending','unknown','processing')", (bid,)):
            return
        part = self.db.one("SELECT * FROM fazer_parts WHERE batch_id=? AND state IN('prepared','rate_limited','blocked') ORDER BY ordinal LIMIT 1", (bid,))
        if not part or part['next_check'] > time.time():
            return
        try:
            if not self._preflight_order(o):
                if self.db.one('SELECT fp_status FROM orders WHERE id=?', (o['id'],))['fp_status'] != 'paid':
                    self.db.execute("UPDATE fazer_parts SET state='cancelled' WHERE batch_id=? AND state IN('prepared','rate_limited','blocked')", (bid,))
                    self._rollup_fazer(bid)
                return
            # An intent is immutable. A different recipient cannot be slipped into a retry.
            if hashlib.sha256(part['body'].encode()).hexdigest() != part['body_hash']:
                raise BusinessError('Контрольная сумма закупки изменилась')
            p = json.loads(o['snapshot'])
            current_parts = self._quote(o, p)
            match = current_parts[0] if o['service'] == 'stars' else current_parts[part['ordinal']]
            if dumps(match['body']) != part['body'] or match['units'] != part['units']:
                raise BusinessError('Получатель или состав закупки изменился')
            new_cost = match['cost']
            buffer = 1 + Decimal(str(self.rule('price_buffer_percent', o))) / 100
            new_reserve = int((Decimal(new_cost) * buffer).to_integral_value(rounding=ROUND_CEILING))
            # Include already delivered components in the whole-order financial check.
            totals = self.db.one('''SELECT COALESCE(SUM(COALESCE(charged_micros,quoted_micros)),0) cost,
                                  COALESCE(SUM(CASE WHEN charged_micros IS NULL THEN reserved_micros ELSE charged_micros END),0) reserve
                                  FROM fazer_parts WHERE batch_id=? AND id!=?''', (bid, part['id']))
            self._limits(o, p, totals['cost'] + new_cost, totals['reserve'] + new_reserve, part['fx'], bid)
            balance = self.read_fazer_wallet(o['mode']) - self.fazer_reservations(o['mode'], part['id'])
            if balance < new_reserve:
                self.fazer_wait_balance(o)
                return
            now = time.time()
            with self.db.tx() as c:
                updated = c.execute("""UPDATE fazer_parts SET state='sending',attempts=attempts+1,first_sent=COALESCE(first_sent,?),
                                       quoted_micros=?,reserved_micros=?,updated=? WHERE id=? AND state IN('prepared','rate_limited','blocked')""",
                                    (now, new_cost, new_reserve, now, part['id']))
                if updated.rowcount != 1:
                    return
                c.execute("UPDATE batches SET state='sending',first_sent=COALESCE(first_sent,?),attempts=attempts+1 WHERE id=?", (now, bid))
            try:
                response = self.fazer(o['mode']).buy_item(o['service'], json.loads(part['body']), part['idem_key'])
            except ProviderError as exc:
                state = 'unknown' if exc.kind in ('unknown', 'invariant', 'network', 'schema') else 'blocked' if exc.kind == 'balance' else 'rate_limited' if exc.kind == 'retry' else 'rejected'
                self.db.execute('UPDATE fazer_parts SET state=?,error=?,next_check=?,updated=? WHERE id=?', (state, str(exc), now + exc.retry_after, time.time(), part['id']))
                if state == 'blocked':
                    self.fazer_wait_balance(o)
                elif state == 'unknown':
                    self.set_direction_pause(o['service'], 'Неизвестный результат закупки')
                self._rollup_fazer(bid)
                return
            except BaseException:
                # This also covers cancellation/shutdown after bytes may have left the process.
                self.db.execute("UPDATE fazer_parts SET state='unknown',error='Отправка прервана: результат неизвестен' WHERE id=?", (part['id'],))
                self._rollup_fazer(bid)
                raise
            try:
                self._apply_fazer_result(part['id'], response)
            except Exception:
                self.db.execute("UPDATE fazer_parts SET state='unknown',error='Ответ после закупки требует сверки' WHERE id=?", (part['id'],))
                self.set_direction_pause(o['service'], 'Ответ после закупки требует сверки')
            self.queue(o['id'], 'processing')
            self._rollup_fazer(bid)
        except (ProviderError, BusinessError, FunPayError) as exc:
            # Errors before POST do not turn previously delivered parts into failures.
            self.db.execute('UPDATE fazer_parts SET error=?,next_check=? WHERE id=?', (str(exc), time.time() + 60, part['id']))
            self.set_direction_pause(o['service'], str(exc))
            self.problem(o['id'], 'manual', str(exc))

    def _apply_fazer_result(self, part_id, response):
        part = self.db.one('SELECT * FROM fazer_parts WHERE id=?', (part_id,))
        code = order_id(response.get('id'))
        if part['provider_id'] and part['provider_id'] != code:
            raise BusinessError('Изменился номер операции поставщика')
        payload = json.loads(part['body'])
        # Validate any recipient/quantity fields the supplier does expose. Missing
        # optional fields never justify matching an UNKNOWN order by similarity.
        if part['service'] == 'stars':
            other = response.get('telegram_username', response.get('telegramUsername'))
            if other is not None and username(other) != payload['telegram_username']:
                raise BusinessError('FazerCards вернул другого получателя')
            if response.get('quantity') is not None and positive_int(response['quantity']) != part['units']:
                raise BusinessError('FazerCards вернул другое количество Stars')
        else:
            for key in ('category_id', 'offer_id'):
                if response.get(key) is not None and str(response[key]) != payload[key]:
                    raise BusinessError('SKU операции FazerCards не совпал')
            fields = response.get('fields')
            if fields is not None and fields != payload['fields']:
                raise BusinessError('Получатель пополнения не совпал')
        status = str(response.get('status', '')).lower()
        state = status if status in TERMINAL else 'processing' if status in PENDING else 'unknown'
        if part['state'] == 'completed' and state != 'completed':
            raise BusinessError('Поставщик изменил ранее подтверждённую выдачу; требуется ручная сверка')
        amount = next((response[k] for k in ('chargedUsd', 'total_usd', 'price_usd') if response.get(k) is not None), None)
        charged = to_micros(amount) if amount is not None else part['charged_micros']
        source = 'actual' if charged is not None else 'quote'
        if part['cost_source'] == 'owner_verified':
            charged, source = part['charged_micros'], 'owner_verified'
        with self.db.tx() as c:
            other = c.execute('SELECT id FROM fazer_parts WHERE mode=? AND provider_id=? AND id!=?', (part['mode'], code, part_id)).fetchone()
            if other:
                raise BusinessError('Эта операция поставщика уже привязана к другому заказу')
            c.execute('''UPDATE fazer_parts SET provider_id=?,state=?,charged_micros=?,cost_source=?,response=?,
                         next_check=?,updated=?,error=? WHERE id=?''',
                      (code, state, charged, source, dumps(response), time.time() + self.db.setting('provider_poll_seconds'), time.time(),
                       '' if state != 'unknown' else 'Неизвестный статус поставщика', part_id))
        if charged is not None and charged > part['reserved_micros']:
            b = self.db.one('SELECT order_id FROM batches WHERE id=?', (part['batch_id'],))
            self.alert('price', 'Фактическое списание превысило резерв', 'Проверьте цены и остановите дальнейшие части заказа.', b['order_id'])
            self.set_direction_pause(part['service'], 'Фактическое списание превысило резерв')

    def _rollup_fazer(self, bid):
        b = self._fz_batch(bid)
        o = self.db.one('SELECT * FROM orders WHERE id=?', (b['order_id'],))
        parts = self.db.rows('SELECT * FROM fazer_parts WHERE batch_id=? ORDER BY ordinal', (bid,))
        if not parts:
            raise BusinessError('Отсутствуют сохранённые части заказа')
        states = {p['state'] for p in parts}
        done = sum(p['units'] for p in parts if p['state'] == 'completed')
        if done > o['uc'] or done < o['delivered']:
            raise BusinessError('Нарушена целостность количества выданного товара')
        unknown = bool(states & {'unknown', 'sending'})
        failures = bool(states & {'failed', 'refund', 'rejected'})
        if failures and not unknown and 'processing' not in states:
            self.db.execute("UPDATE fazer_parts SET state='cancelled' WHERE batch_id=? AND state IN('prepared','blocked','rate_limited')", (bid,))
            state = 'partial' if done else 'failed'
        elif done == o['uc']:
            state = 'completed'
        elif unknown:
            state = 'unknown'
        elif 'processing' in states:
            state = 'processing'
        elif 'blocked' in states:
            state = 'balance'
        elif states <= {'completed', 'cancelled'}:
            state = 'partial' if done else 'rejected'
        else:
            state = 'retry'
        # Never assume that a failed delivery was refunded. Unknown/missing charges
        # are explicitly quoted estimates until a real amount is returned/reviewed.
        cost = sum(usd_to_rub(p['charged_micros'] if p['charged_micros'] is not None else p['quoted_micros'], p['fx'])
                   for p in parts if p['state'] in ('sending', 'unknown', 'processing', 'completed', 'failed', 'refund'))
        actual = sum(usd_to_rub(p['charged_micros'], p['fx']) for p in parts if p['charged_micros'] is not None)
        actual_or_estimate = max(actual, cost)
        order_state = {'retry': 'processing', 'balance': 'waiting_balance', 'rejected': 'cancelled'}.get(state, state)
        with self.db.tx() as c:
            c.execute('UPDATE batches SET state=?,delivered_units=?,net_cost=?,actual_cost=?,next_check=? WHERE id=?',
                      (state, done // b['uc_per_unit'], actual_or_estimate, actual_or_estimate, time.time() + self.db.setting('provider_poll_seconds'), bid))
            c.execute('UPDATE orders SET state=?,delivered=?,updated=? WHERE id=?', (order_state, done, time.time(), o['id']))
        if state == 'completed':
            if o['fp_status'] == 'paid':
                self.queue(o['id'], 'completed')
            else:
                self.alert('manual', 'Выдача завершена, статус оплаты изменился', 'Сверьте FunPay; не выполняйте повторное пополнение.', o['id'])
            for kind in ('unknown', 'partial', 'failed'):
                self.resolve_alert(kind + ':' + o['id'])
            self.resolve_alert('balance:fazer-order:' + o['id'])
        elif state == 'unknown':
            self.set_direction_pause(o['service'], 'Неизвестный результат закупки')
            self.problem(o['id'], 'unknown', 'Ответ поставщика не подтверждён. Повторная покупка не отправляется; сверьте кабинет FazerCards.', 'unknown')
        elif state in ('failed', 'partial'):
            self.set_direction_pause(o['service'], 'Ошибка/частичная выдача требует проверки')
            self.problem(o['id'], state, f'Подтверждено {done} из {o["uc"]} {"Stars" if o["service"] == "stars" else "UC"}. Оставшиеся части не покупаются автоматически.', state)
        elif time.time() - b['created'] > self.rule('processing_timeout_seconds', o):
            self.alert('manual', 'Длительное исполнение заказа', 'Проверка продолжается. Задержка не является основанием для повторной закупки.', o['id'])

    @synchronized
    def poll_batch(self, bid):
        b = self._fz_batch(bid)
        if not b:
            return super().poll_batch(bid)
        o = self.db.one('SELECT * FROM orders WHERE id=?', (b['order_id'],))
        self.fazer_account(o['mode'])
        # Also inspect known terminal parts when an administrator explicitly checks
        # a finished order; never reduce already acknowledged deliveries.
        for p in self.db.rows('SELECT * FROM fazer_parts WHERE batch_id=? AND provider_id IS NOT NULL', (bid,)):
            result = self.fazer(o['mode']).check_order_status(p['provider_id'])
            self._apply_fazer_result(p['id'], result)
        self._rollup_fazer(bid)

    @synchronized
    def recover_batch(self, bid):
        b = self._fz_batch(bid)
        if not b:
            return super().recover_batch(bid)
        # No automatic matching by username/time/amount: two real paid orders may
        # be identical. Only a known provider ID can be polled automatically.
        self.poll_batch(bid)

    @synchronized
    def bind_fazer_order(self, part_id, code, proof, actor):
        part = self.db.one('SELECT * FROM fazer_parts WHERE id=?', (part_id,))
        if not part or part['state'] != 'unknown' or part['provider_id']:
            raise BusinessError('Привязка разрешена только для неизвестной операции без номера')
        if len(proof.strip()) < 15:
            raise BusinessError('Укажите подтверждение сверки: заказ, получатель и количество (от 15 символов)')
        code = order_id(code)
        b = self.db.one('SELECT * FROM batches WHERE id=?', (part['batch_id'],))
        o = self.db.one('SELECT * FROM orders WHERE id=?', (b['order_id'],))
        self.fazer_account(o['mode'])
        result = self.fazer(o['mode']).check_order_status(code)
        self._apply_fazer_result(part_id, result)
        self.db.audit(actor, 'fazer.manual_binding', part_id, proof.strip()[:2000])
        self._rollup_fazer(b['id'])

    @synchronized
    def close_unsent_fazer(self, part_id, proof, actor):
        # No retry button. Even an owner's confirmation of non-delivery does not
        # create a new request. Close for manual handling/refund after supplier proof.
        part = self.db.one('SELECT * FROM fazer_parts WHERE id=?', (part_id,))
        if not part or part['state'] not in ('unknown', 'prepared', 'blocked', 'rate_limited'):
            raise BusinessError('Сначала сверьте результат известной операции')
        if part['provider_id']:
            raise BusinessError('У операции есть номер поставщика: сначала проверьте её статус')
        if len(proof.strip()) < 20:
            raise BusinessError('Нужно письменное подтверждение поставщика об отсутствии закупки (от 20 символов)')
        self.db.execute("UPDATE fazer_parts SET state='rejected',error=?,updated=? WHERE id=?", (proof.strip()[:2000], time.time(), part_id))
        self.db.audit(actor, 'fazer.closed_no_purchase', part_id, proof.strip()[:2000])
        self._rollup_fazer(part['batch_id'])

    @synchronized
    def verify_fazer_cost(self, part_id, micros, proof, actor):
        part=self.db.one('SELECT * FROM fazer_parts WHERE id=?',(part_id,))
        if not part or part['state'] not in ('completed','failed','refund','rejected','cancelled'):
            raise BusinessError('Сначала завершите сверку результата операции')
        if not isinstance(micros,int) or micros<0 or len(proof.strip())<15:
            raise BusinessError('Укажите итоговый чистый расход USD и доказательство из кабинета')
        self.db.execute("UPDATE fazer_parts SET charged_micros=?,cost_source='owner_verified',updated=? WHERE id=?",(micros,time.time(),part_id))
        self.db.audit(actor,'fazer.cost.verified',part_id,dumps({'net_usd_micros':micros,'proof':proof[:2000]}))
        self._rollup_fazer(part['batch_id'])

    @synchronized
    def seed_fazer_demo(self, service='stars', quantity=1, denomination=50):
        if self.mode() != 'demo' or service not in ('uc', 'stars'):
            raise BusinessError('Это действие доступно только в DEMO')
        self.sync_fazer_catalog('demo')
        self.db.set('svc_' + service + '_enabled', True)
        self.db.set('svc_' + service + '_paused', False)
        cat = next(c for c in self.db.rows("SELECT * FROM catalog WHERE mode='demo' AND id>=?", (LOCAL_SKU_OFFSET,))
                   if json.loads(c['payload'])['service'] == service and (service == 'stars' or c['uc'] == 60))
        marker = '[AUC:DEMOFZSTARS]' if service == 'stars' else '[AUC:DEMOFZUC]'
        old = self.db.one('SELECT id FROM products WHERE marker=?', (marker,))
        unit = 1 if service == 'stars' else 60
        nominal = positive_int(denomination, 10000)
        if service == 'uc' and nominal % 60:
            raise BusinessError('Для демо UC доступны составные номиналы, кратные 60: 60, 180, 600. Реальные номиналы выбираются в каталоге.')
        multiplier = nominal if service == 'stars' else nominal // 60
        data = {'name': 'DEMO Fazer ' + service, 'marker': marker, 'fp_lot_id': 500 if service == 'stars' else 501,
                'fp_subcategory': 2 if service == 'stars' else 1, 'fp_category': 2 if service == 'stars' else 1,
                'sku_id': cat['id'], 'sku_uc': unit, 'multiplier': multiplier,
                'uid_field': 'telegram_username' if service == 'stars' else 'player_id',
                'sale_price': 3 * multiplier if service == 'stars' else 200 * multiplier, 'enabled': True, 'service': service}
        pid = self.save_product(data, old['id'] if old else None)
        self.verify_product(pid)
        oid = 'DEMO-' + secrets.token_hex(4).upper()
        d = {'id': oid, 'buyer_id': 2, 'buyer': 'Тестовый покупатель', 'chat_id': 'users-1-2', 'quantity': positive_int(quantity),
             'status': 'paid', 'revenue': cents(data['sale_price']) * int(quantity), 'currency': 'RUB',
             'subcategory': data['fp_subcategory'], 'section_type': 'lot', 'description': marker}
        self.db.set('demo-fp:' + oid, d)
        self.import_order(d, 'demo')
        return oid

    @synchronized
    def reset_demo(self, actor):
        if self.mode() != 'demo':
            raise BusinessError('Очистка доступна только в DEMO')
        with self.db.tx() as c:
            c.execute("DELETE FROM fazer_parts WHERE batch_id IN (SELECT b.id FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode='demo')")
            c.execute("DELETE FROM fazer_wallet WHERE mode='demo'")
        return super().reset_demo(actor)
