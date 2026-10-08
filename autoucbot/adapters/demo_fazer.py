"""Durable, local-only FazerCards simulator. No real HTTP requests or spending."""
from __future__ import annotations
import hashlib
import time
from decimal import Decimal
from .fazer import to_micros, buy_request
from .gamecore import ProviderError

class DemoFazer:
    def __init__(self, db): self.db = db
    def get_balance(self): return int(self.db.setting('demo-fazer-wallet', 100_000_000))
    def account(self): return {'login': 'DEMO-FAZER', 'subscriptionActive': True, 'plan': 'demo'}
    def subscription(self): return {'subscriptionActive': True, 'plan': 'demo', 'planExpiresAt': None}
    def plans(self): return {'plans': []}
    def categories(self): return [{'category_id': 'demo_pubg', 'name': 'DEMO PUBG Mobile Global'}]
    def offers(self, category):
        return {'category_id': category, 'name': 'DEMO PUBG Mobile Global',
                'offers': [{'offer_id': str(n), 'name': f'{n} UC', 'price_usd': str(n / 100)} for n in (60, 325, 660, 1800)],
                'fields': [{'key': 'player_id', 'label': 'Player ID', 'type': 'text', 'required': True}]}
    def stars_quote(self): return {'price_per_star': '0.015', 'min_amount': 50, 'max_amount': 10000}
    def validation_games(self): return [{'category_id': 'demo_pubg_check', 'name': 'DEMO PUBG', 'fields': [{'key': 'player_id'}]}]
    def validate_player(self, category, fields): return {'valid': True, 'player_name': 'DEMO Player'}
    def buy_item(self, service, payload, key):
        buy_request(service, payload, key)
        # UC supports server-side idempotency; Stars deliberately does NOT in this simulation.
        old = self.db.setting('demo-fazer-key:' + key)
        if old and service == 'uc': return self.check_order_status(old)
        scenario = self.db.setting('demo_scenario', 'success')
        if scenario == 'balance': raise ProviderError('balance', 'DEMO: средств не хватает')
        if scenario == 'unknown_before': raise ProviderError('unknown', 'DEMO: неизвестно, дошёл ли запрос')
        counter = self.db.setting('demo-fazer-counter', 0) + 1
        self.db.set('demo-fazer-counter', counter)
        code = 'ord-' + str(900000000 + counter)
        value = to_micros(str(int(payload['offer_id']) / 100)) if service == 'uc' else to_micros(Decimal(payload['quantity']) * Decimal('0.015'))
        status = 'processing' if scenario == 'pending' else 'failed' if scenario == 'failed' or (scenario == 'partial' and counter % 2 == 0) else 'completed'
        response = {'id': code, 'kind': 'topup' if service == 'uc' else 'telegram_stars', 'status': status,
                    'total_usd': str(value / 1_000_000), 'created_at': str(time.time()), **payload}
        self.db.set('demo-fazer-order:' + code, response)
        self.db.set('demo-fazer-key:' + key, code)
        self.db.set('demo-fazer-wallet', self.get_balance() - value)
        if scenario == 'unknown_after': raise ProviderError('unknown', 'DEMO: деньги списаны, ответ потерян')
        return response
    def check_order_status(self, code):
        result = self.db.setting('demo-fazer-order:' + code)
        if result is None: raise ProviderError('rejected', 'DEMO: операция не найдена')
        return result
    def close(self): pass
