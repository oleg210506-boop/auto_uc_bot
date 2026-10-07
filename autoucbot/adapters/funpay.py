"""Small fail-closed adapter for FunPay's website protocol (NOT an official API).
Protocol references and limitations are in PROTOCOL_SOURCES.md.
No CAPTCHA bypass, no paid-order inference from buyer messages.
"""
from __future__ import annotations
import json
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup
from ..utils import BusinessError, cents, positive_int

class FunPayError(Exception): pass

class FunPay:
    BASE = "https://funpay.com"
    def __init__(self, key, user_agent, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent or "Mozilla/5.0 autoUCbot/1.0", "Accept-Language": "ru"})
        self.session.cookies.set("golden_key", key, domain="funpay.com", path="/")
        self.session.cookies.set("cookie_prefs", "1", domain="funpay.com", path="/")
        self.user_id, self.csrf, self.connected_at = 0, "", 0
        self.tags = {}; self.counter_epoch = 0

    def request(self, method, path, **kwargs):
        if not path.startswith("/") or path.startswith("//") or "\\" in path or ".." in path:
            raise BusinessError("Некорректный путь FunPay")
        try:
            r = self.session.request(method, self.BASE+path, timeout=(8,25), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise FunPayError("FunPay не ответил; результат исходящего сообщения/изменения может быть неизвестен") from None
        if r.status_code != 200:
            raise FunPayError(f"FunPay HTTP {r.status_code}. Проверьте вход/ограничения в браузере. CAPTCHA не обходится.")
        if len(r.content) > 6_000_000: raise FunPayError("Неожиданно большой ответ FunPay")
        return r

    def connect(self):
        r = self.request("GET", "/")
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("body")
        try: data = json.loads(body.get("data-app-data", "{}")) if body else {}
        except ValueError: data = {}
        uid = data.get("userId", 0)
        if not soup.select_one(".user-link-name") or not uid or not data.get("csrf-token"):
            raise FunPayError("Не подтверждена авторизация FunPay; проверьте golden_key и вход через браузер")
        self.user_id, self.csrf, self.connected_at = int(uid), data["csrf-token"], time.time()
        return {"user_id": self.user_id, "name": soup.select_one(".user-link-name").get_text(strip=True)}

    def ensure(self):
        if not self.user_id or time.time()-self.connected_at > 2400: self.connect()

    def paid_ids(self, subcategory):
        self.ensure()
        ids, cont = [], None
        for _ in range(100):
            params = {"state": "paid", "section": f"lot-{int(subcategory)}"}
            if cont:
                r = self.request("POST", "/orders/trade", params=params, data={**params, "continue": cont})
            else: r = self.request("GET", "/orders/trade", params=params)
            soup = BeautifulSoup(r.text, "html.parser")
            if not cont:
                title = soup.select_one("h1.page-header")
                if not soup.select_one(".user-link-name") or not title or not any(s in title.get_text().lower() for s in ("мои продажи", "my sales", "мої продажі")):
                    raise FunPayError("Не распознана страница продаж FunPay")
            for a in soup.select("a.tc-item"):
                node = a.select_one(".tc-order")
                if node:
                    oid = node.get_text(strip=True).lstrip("#")
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", oid): ids.append(oid)
            nxt = soup.find("input", {"name":"continue"})
            value = nxt.get("value") if nxt else None
            if not value: return list(dict.fromkeys(ids))
            if value == cont: raise FunPayError("Повтор страницы FunPay: безопасная остановка чтения")
            cont = value
        raise FunPayError("Слишком много страниц оплаченных заказов: требуется сверка")

    def order(self, oid):
        self.ensure()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", oid): raise BusinessError("Некорректный номер заказа")
        r = self.request("POST", "/api/orders/get", json={"order_uids":[oid],"include":["details","users"]})
        try: data = r.json()
        except ValueError: raise FunPayError("FunPay вернул не JSON") from None
        if data.get("status") != "SUCCESS" or oid not in data.get("data", {}): raise FunPayError("FunPay не подтвердил данные заказа")
        result = self.normalize_order(data["data"][oid], self.user_id)
        if result["id"] != oid: raise FunPayError("Номер полученного заказа не совпал с запрошенным")
        return result

    @staticmethod
    def normalize_order(d, own_id):
        try:
            seller, buyer = d["seller"], d["buyer"]
            if int(seller["user_id"]) != int(own_id): raise ValueError()
            buyer_id = positive_int(buyer["user_id"], 2147483647)
            td = d["type_data"]
            n = Decimal(str(td["amount"]))
            if not n.is_finite() or n != n.to_integral() or not 1 <= n <= 10000: raise ValueError()
            chat = d["chat"]["node_name"]
            expected = "users-"+"-".join(map(str,sorted((int(own_id),buyer_id))))
            if chat != expected: raise ValueError()
            fields = td.get("fields", {})
            if not isinstance(fields, dict): raise ValueError()
            # Only seller-authored summary/description, never UID or arbitrary delivery fields.
            texts = []
            for key, field in fields.items():
                if re.fullmatch(r"(summary|desc|description)(?:\[(ru|en|uk)\]|_(ru|en|uk))?", key):
                    value = field.get("value") if isinstance(field, dict) else field
                    if isinstance(value, str): texts.append(value)
                    elif isinstance(value, dict): texts.extend(str(v) for v in value.values() if isinstance(v,str))
            return {"id":str(d["order_uid"]),"seller_id":int(own_id),"buyer_id":buyer_id,"buyer":str(buyer["name"]),
                    "chat_id":chat,"quantity":int(n),"status":str(d["status"]).lower(),
                    "revenue":cents(d["amount"]),"currency":{"₽":"RUB","РУБ":"RUB","РУБ.":"RUB"}.get(str(d["currency"]).upper(),str(d["currency"]).upper()),
                    "subcategory":int(d["section"]["local_id"]),"section_type":d["section"]["type_id"],
                    "description":"\n".join(texts),"raw":d}
        except (KeyError,ValueError,TypeError,InvalidOperation):
            raise FunPayError("Структура заказа/количество/продавец/чат FunPay не подтверждены; закупка заблокирована") from None

    def runner(self, chats, request=None):
        self.ensure()
        objects = [{"type":"orders_counters","id":self.user_id,"tag":self.tags.get("orders","00000000"),"data":False}]
        for chat, last in chats.items():
            objects.append({"type":"chat_node","id":chat,"tag":self.tags.get(chat,"00000000"),
                            "data":{"node":chat,"last_message":int(last),"content":""}})
        r = self.request("POST", "/runner/", headers={"X-Requested-With":"XMLHttpRequest"},
                         data={"csrf_token":self.csrf,"objects":json.dumps(objects),"request":json.dumps(request) if request else "false"})
        try: d = r.json()
        except ValueError: raise FunPayError("Неизвестный ответ FunPay runner") from None
        if not isinstance(d,dict) or "objects" not in d: raise FunPayError("Нет объектов ответа FunPay runner")
        msgs=[]
        for obj in d.get("objects",[]):
            if obj.get("type") == "orders_counters":
                tag=obj.get("tag","00000000")
                if tag!=self.tags.get("orders"):self.counter_epoch+=1
                self.tags["orders"] = tag
            if obj.get("type") != "chat_node": continue
            dat = obj.get("data",{})
            if not isinstance(dat,dict):continue
            node = dat.get("node") or {}
            chat = node.get("name") or str(obj.get("id"))
            if chat not in chats: continue
            self.tags[chat] = obj.get("tag","00000000")
            for m in dat.get("messages") or []:
                soup=BeautifulSoup(m.get("html", ""),"html.parser")
                text_node=soup.select_one(".chat-msg-text")
                if text_node:
                    for br in text_node.find_all("br"): br.replace_with("\n")
                text=text_node.get_text().strip() if text_node else ""
                msgs.append({"id":str(m["id"]),"chat_id":chat,"author":int(m.get("author",0)),"text":text})
        return msgs,d

    def messages(self, chats): return self.runner(chats)[0]

    def send(self, chat, text):
        _,d=self.runner({chat:-1},{"action":"chat_message","data":{"node":chat,"last_message":-1,"content":text}})
        response=d.get("response")
        if not isinstance(response,dict) or response.get("error"):
            raise FunPayError("Отправка сообщения FunPay не подтверждена")

    def lot(self, lot_id):
        self.ensure()
        soup=BeautifulSoup(self.request("GET","/lots/offerEdit",params={"offer":int(lot_id)}).text,"html.parser")
        form=soup.select_one("form.form-offer-editor")
        if not form: raise FunPayError("Не найдена форма вашего объявления")
        fields={}
        for e in form.select("input[name],textarea[name],select[name]"):
            if e.has_attr("disabled"): continue
            name=e["name"]
            if e.name == "textarea": fields[name]=e.get_text()
            elif e.name == "select":
                selected=e.find("option",selected=True) or e.find("option")
                if selected: fields[name]=selected.get("value","")
            elif e.get("type") in ("checkbox","radio"):
                if e.has_attr("checked"): fields[name]=e.get("value") or "on"
            else: fields[name]=e.get("value","")
        self.csrf = fields.get("csrf_token") or self.csrf
        if str(fields.get("offer_id")) != str(lot_id): raise FunPayError("ID редактируемого объявления не совпал")
        currency = "RUB" if any("₽" in e.get_text() or "руб" in e.get_text().lower() for e in form.select(".form-control-feedback")) else "UNKNOWN"
        text="\n".join(v for k,v in fields.items() if k.startswith(("fields[summary]","fields[desc]")))
        return {"fields":fields,"subcategory":int(fields.get("node_id",0)),"text":text,"active":"active" in fields,
                "currency":currency,"price":cents(fields.get("price",0))}

    def change_lot(self, p, *, active=None, price=None):
        lot=self.lot(p["fp_lot_id"])
        if lot["subcategory"] != p["fp_subcategory"] or p["marker"] not in lot["text"]:
            raise FunPayError("Объявление не прошло проверку метки/подраздела; изменение запрещено")
        fields=lot["fields"]
        if active is not None:
            if active: fields["active"]="on"
            else: fields.pop("active",None)
        if price is not None:
            if lot["currency"] != "RUB": raise FunPayError("Валюта цены объявления не подтверждена как RUB")
            fields["price"]=f"{int(price)/100:.2f}"
        fields["csrf_token"]=self.csrf
        r=self.request("POST","/lots/offerSave",data=fields,headers={"X-Requested-With":"XMLHttpRequest"})
        try: d=r.json()
        except ValueError: raise FunPayError("Изменение объявления не подтверждено") from None
        if d.get("error") or d.get("errors"): raise FunPayError("FunPay отклонил изменение объявления")
        checked=self.lot(p["fp_lot_id"])
        if (active is not None and checked["active"]!=active) or (price is not None and checked["price"]!=price):
            raise FunPayError("Повторная проверка объявления не подтвердила изменение")
        return checked

    def raise_lots(self, category, subcategories):
        self.ensure()
        if not subcategories: return 3600
        r=self.request("POST","/lots/raise",data={"game_id":int(category),"node_id":int(subcategories[0]),
                        "node_ids[]":list(map(int,subcategories)),"csrf_token":self.csrf},headers={"X-Requested-With":"XMLHttpRequest"})
        try: d=r.json()
        except ValueError: raise FunPayError("Ответ поднятия FunPay не распознан") from None
        if d.get("url"): raise FunPayError("Поднятие требует ручного действия на FunPay")
        wait=max(60,int(d.get("wait",3600)))
        if d.get("error") and not d.get("wait"): raise FunPayError("FunPay отклонил поднятие")
        return wait

    def close(self): self.session.close()
