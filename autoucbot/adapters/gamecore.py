"""GameCore B2B adapter. Protocol: https://gamecore-api.tech/ru/docs.
Only documented endpoints are used; no automatic retry of monetary POSTs.
"""
from __future__ import annotations
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit
import requests
from ..utils import BusinessError, cents, positive_int

class ProviderError(Exception):
    def __init__(self, kind, message, retry_after=30):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after

class GameCore:
    def __init__(self, key, base="https://api.gamecore-api.tech", session=None):
        self.key, self.base = key, base.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"X-Api-Key": key, "Accept": "application/json", "User-Agent": "autoUCbot/1.0"})

    def request(self, method, path, *, body=None, idem=None):
        if not self.key: raise ProviderError("auth", "Ключ GameCore не задан")
        if not path.startswith("/b2b/") or ".." in path or "\\" in path or urlsplit(path).netloc:
            raise BusinessError("Допустим только относительный путь /b2b/…")
        headers = {"X-Idempotency-Key": idem} if idem else {}
        try:
            r = self.session.request(method, self.base + path, json=body, headers=headers,
                                     timeout=(8, 25), allow_redirects=False)
        except requests.RequestException:
            raise ProviderError("unknown" if method == "POST" else "network", "GameCore: ответ не получен") from None
        try: d = r.json()
        except ValueError: d = {}
        if r.status_code in (401, 403): raise ProviderError("auth", f"GameCore: доступ запрещён, HTTP {r.status_code}")
        if r.status_code == 402:
            message = str(d.get("error", "")) + " " + str(d.get("message", ""))
            if "concurrent" in message.lower():
                raise ProviderError("retry", "Одновременное изменение баланса", 10)
            if any(x in message.lower() for x in ("insufficient", "balance", "credit", "недостат")):
                raise ProviderError("balance", "Недостаточно средств на балансе GameCore")
            raise ProviderError("rejected", "GameCore HTTP 402: причина не распознана; требуется проверка")
        if r.status_code == 409:
            message = str(d)
            if "different_body" in message:
                raise ProviderError("invariant", "Ключ операции уже использован с другим телом запроса")
            raise ProviderError("unknown", "Предыдущая операция ещё обрабатывается", 30)
        if r.status_code == 429:
            try: delay = max(10, min(3600, int(r.headers.get("Retry-After", "60"))))
            except ValueError: delay = 60
            raise ProviderError("retry", "Ограничение частоты запросов GameCore", delay)
        if r.status_code >= 500 or 300 <= r.status_code < 400:
            raise ProviderError("unknown" if method == "POST" else "network", f"GameCore HTTP {r.status_code}: результат не подтверждён")
        if r.status_code not in (200, 201) or d.get("success") is not True:
            raise ProviderError("rejected", f"GameCore отклонил запрос, HTTP {r.status_code}")
        if "data" not in d:
            raise ProviderError("unknown" if method == "POST" else "schema", "GameCore: неизвестный формат ответа")
        return d["data"]

    def catalog(self):
        data = self.request("GET", "/b2b/catalog/games/pubg-mobile/products?deliveryType=id_only")
        if not isinstance(data, list): raise ProviderError("schema", "GameCore: каталог не является списком")
        return [self.normalize_product(p) for p in data]

    @staticmethod
    def normalize_product(p):
        if not isinstance(p, dict): raise ProviderError("schema", "Неизвестный формат товара")
        amount = p.get("amountType", {})
        n = amount.get("value") if isinstance(amount, dict) and amount.get("type") == "fixed" else None
        # Never parse monetary prices or variable bonuses as UC. Unknown denominations must be reviewed.
        uc = 0
        if n is not None:
            try:
                v = Decimal(str(n))
                if v == v.to_integral() and 0 < v <= 10000000: uc = int(v)
            except Exception: pass
        return {"id": positive_int(p.get("id"), 2147483647), "name": str(p.get("name", "")),
                "price": cents(p.get("wholesalePrice")), "currency": str(p.get("currency", "")).upper(),
                "region": str(p.get("region") or ""), "uc": uc,
                "delivery_type": p.get("deliveryType", ""), "in_stock": p.get("inStock") is True,
                "payload": p}

    def product(self, product_id):
        return self.normalize_product(self.request("GET", f"/b2b/catalog/products/{positive_int(product_id,2147483647)}"))

    def create(self, payload, key):
        data = self.request("POST", "/b2b/orders", body=payload, idem=key)
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list) or not data["orders"]:
            raise ProviderError("unknown", "Заказ принят без списка операций; требуется сверка")
        codes = [x.get("code") for x in data["orders"]]
        if any(not isinstance(c, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", c) for c in codes) or len(set(codes)) != len(codes):
            raise ProviderError("unknown", "Неизвестный формат номеров операций")
        if cents(data.get("totalAmount")) <= 0: raise ProviderError("unknown", "Неизвестная сумма списания")
        return data

    def order(self, code):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(code)): raise BusinessError("Некорректный номер GameCore")
        return self.request("GET", "/b2b/orders/"+code)

    def recover(self, external_id):
        """Find EVERY suborder. No unsupported externalOrderId query parameter is invented."""
        found = []
        for page in range(1, 101):
            result = self.request("GET", f"/b2b/orders?page={page}&limit=100")
            if isinstance(result, dict):
                items = result.get("orders", result.get("items", result.get("data")))
                pagination = result.get("pagination", {})
                total = result.get("total", pagination.get("total"))
            else:
                items, total = result, None
            if not isinstance(items, list): raise ProviderError("schema", "Неизвестная структура списка заказов для сверки")
            for item in items:
                if item.get("externalOrderId") == external_id: found.append(item)
            if len(items) < 100 or (total is not None and page*100 >= int(total)): return found
        raise ProviderError("schema", "Сверка превысила 10 000 записей; обратитесь к поставщику")

    def balance(self, path, field):
        if not path or not re.fullmatch(r"[A-Za-z0-9_.]+", field or ""):
            raise BusinessError("Метод остатка должен быть подтверждён GameCore и настроен владельцем")
        # request() returns the envelope's data, so allow both data.balance and balance.
        result = self.request("GET", path)
        parts = field.split(".")
        if parts[0] == "data": parts = parts[1:]
        for part in parts:
            if not isinstance(result, dict) or part not in result: raise ProviderError("schema", "В ответе нет выбранного поля остатка")
            result = result[part]
        return cents(result)

    def close(self): self.session.close()
