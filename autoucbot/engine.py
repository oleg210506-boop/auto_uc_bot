from __future__ import annotations
import functools
import json
import re
import secrets
import threading
import time
import uuid
from decimal import Decimal, ROUND_CEILING
from .config import FIELDS,TEMPLATES
from .db import dumps
from .utils import BusinessError,cents,positive_int,validate_uid,render_template,day_start
from .adapters.gamecore import GameCore,ProviderError
from .adapters.funpay import FunPay,FunPayError
from .adapters.demo import DemoGameCore,DemoFunPay

FINAL={"completed","failed","partial","cancelled","manual","observed"}
INFLIGHT={"sending","unknown","accepted","processing"}

def locked(fn):
    @functools.wraps(fn)
    def call(self,*a,**kw):
        with self.lock: return fn(self,*a,**kw)
    return call

class Engine:
    def __init__(self,db,vault,config):
        self.db,self.vault,self.config=db,vault,config
        self.lock=threading.RLock()
        self._provider=None;self._fp=None;self._provider_key=None;self._fp_keys=None
        self.demo_provider=DemoGameCore(db);self.demo_fp=DemoFunPay(db)
        for key,spec in FIELDS.items():
            if db.setting(key) is None: db.set(key,spec[1])
        for key,(_,value) in TEMPLATES.items():
            if db.setting("tpl_"+key) is None: db.set("tpl_"+key,value)

    def mode(self): return self.db.setting("mode","demo")
    def dataset(self): return "demo" if self.mode()=="demo" else "live"
    def provider(self,mode=None):
        if (mode or self.dataset())=="demo": return self.demo_provider
        key=self.vault.get("gamecore_key")
        if self._provider_key != key or self._provider is None:
            if self._provider: self._provider.close()
            self._provider=GameCore(key,self.config.gamecore_url);self._provider_key=key
        return self._provider
    def fp(self,mode=None):
        if (mode or self.dataset())=="demo": return self.demo_fp
        keys=(self.vault.get("funpay_key"),self.vault.get("funpay_user_agent"))
        if self._fp_keys != keys or self._fp is None:
            if self._fp:self._fp.close()
            self._fp=FunPay(*keys);self._fp_keys=keys
        return self._fp

    def alert(self,kind,title,detail="",order_id=None,key=None):
        key=key or kind+":"+(order_id or "system")
        now=time.time()
        self.db.execute("""INSERT INTO alerts(key,kind,title,detail,order_id,created,updated) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET title=excluded.title,detail=excluded.detail,active=1,
            acked=CASE WHEN alerts.active=0 THEN 0 ELSE alerts.acked END,updated=excluded.updated""",
            (key,kind,title,str(detail)[:2000],order_id,now,now))
    def resolve_alert(self,key): self.db.execute("UPDATE alerts SET active=0 WHERE key=?",(key,))
    def pause(self,reason):
        self.db.set("paused",True);self.db.set("pause_reason",reason)
        self.db.audit("system","pause",detail=reason)
    def problem(self,oid,kind,reason,state="manual",pause=False):
        self.db.execute("UPDATE orders SET state=?,hold_reason=?,updated=? WHERE id=?",(state,reason,time.time(),oid))
        self.alert(kind,"Заказ требует проверки",reason,oid)
        self.queue(oid,"problem",kind)
        if pause and self.db.setting("pause_on_problem"): self.pause(reason)

    def queue(self,oid,template,suffix="",force=False):
        o=self.db.one("SELECT * FROM orders WHERE id=?",(oid,))
        if not o:return
        text=render_template(self.db.setting("tpl_"+template),order_id=o["id"],uc=o["uc"],quantity=o["quantity"],uid=o["uid"] or "",code=o["confirm_code"] or "",buyer=o["buyer"])
        self.queue_text(o,text,f"{oid}:{template}:{suffix}","manual" if force else "auto")
    def queue_text(self,o,text,dedupe,kind="auto"):
        self.db.execute("INSERT OR IGNORE INTO outbox(dedupe,order_id,chat_id,text,kind,mode,created) VALUES(?,?,?,?,?,?,?)",(dedupe,o["id"],o["chat_id"],text,kind,o["mode"],time.time()))

    @locked
    def sync_catalog(self,mode=None):
        mode=mode or self.dataset();data=self.provider(mode).catalog();now=time.time()
        ids=[]
        with self.db.tx() as c:
            c.execute("DELETE FROM catalog WHERE mode=?",(mode,))
            c.execute("UPDATE products SET available=0 WHERE mode=?",(mode,))
            for p in data:
                c.execute("INSERT INTO catalog VALUES(?,?,?,?,?,?,?,?,?,?)",(p["id"],mode,p["name"],p["price"],p["currency"],p["region"],p["uc"],p["delivery_type"],dumps(p["payload"]),now))
                good=p["price"]>0 and p["currency"]=="RUB" and p["delivery_type"]=="id_only" and p["in_stock"]
                if good:
                    c.execute("UPDATE products SET available=1,last_price=?,updated=? WHERE sku_id=? AND mode=? AND sku_uc=? AND region=?",(p["price"],now,p["id"],mode,p["uc"],p["region"]))
                ids.append(p["id"])
        self.db.runtime("catalog:"+mode,{"ok":True,"count":len(data)})
        self.resolve_alert("gamecore:catalog")
        return len(data)

    @locked
    def save_product(self,data,pid=None,actor="owner"):
        mode=self.dataset()
        name=str(data.get("name","")).strip()[:160]
        marker=str(data.get("marker","")).strip()
        if not name or not re.fullmatch(r"\[AUC:[A-Z0-9_-]{4,40}\]",marker): raise BusinessError("Название обязательно; метка вида [AUC:PUBG60]")
        sku=positive_int(data.get("sku_id"),2147483647)
        cat=self.db.one("SELECT * FROM catalog WHERE id=? AND mode=?",(sku,mode))
        if not cat: raise BusinessError("Сначала обновите каталог и выберите SKU из него")
        uc=positive_int(data.get("sku_uc"),10000000)
        if uc!=cat["uc"]: raise BusinessError("Номинал UC не совпал с фиксированным amountType каталога; требуется уточнение GameCore")
        mult=positive_int(data.get("multiplier"))
        uid_field=str(data.get("uid_field","")).strip()
        try: extra=json.loads(data.get("extra_delivery","{}") or "{}")
        except ValueError: raise BusinessError("Дополнительные поля: требуется JSON-объект") from None
        if not isinstance(extra,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in extra.items()): raise BusinessError("Поля deliveryData должны быть строка → строка")
        schema=json.loads(cat["payload"]).get("deliveryDataSchema",[])
        if not any(x.get("id")==uid_field for x in schema): raise BusinessError("Поле UID не найдено в deliveryDataSchema")
        allowed={x["id"] for x in schema}
        if set(extra)-allowed or uid_field in extra:raise BusinessError("Лишнее поле deliveryData или UID прописан в постоянных полях")
        for field in schema:
            if field.get("required") and field["id"]!=uid_field and not extra.get(field["id"]): raise BusinessError("Заполните обязательное поле: "+field["id"])
        values={"name":name,"marker":marker,"fp_lot_id":positive_int(data.get("fp_lot_id"),2147483647),
                "fp_subcategory":positive_int(data.get("fp_subcategory"),2147483647),"fp_category":positive_int(data.get("fp_category"),2147483647),
                "sku_id":sku,"sku_uc":uc,"multiplier":mult,"region":cat["region"],"uid_field":uid_field,"extra_delivery":dumps(extra),
                "enabled":int(bool(data.get("enabled"))),"mode":mode,"sale_price":cents(data.get("sale_price",0)),
                "min_profit":cents(data.get("min_profit",0)),"max_cost":cents(data.get("max_cost",0)),"auto_price":int(bool(data.get("auto_price"))),
                "markup":float(data.get("markup",15)),"manage_active":int(bool(data.get("manage_active"))),"updated":time.time(),"verified":0,
                "last_price":cat["price"],"available":int(cat["price"]>0 and cat["currency"]=="RUB" and cat["delivery_type"]=="id_only")}
        if not 0<=values["markup"]<=1000 or any(values[k]<0 for k in ("sale_price","min_profit","max_cost")):raise BusinessError("Некорректные финансовые ограничения")
        if values["sale_price"]<=0:raise BusinessError("Укажите положительную цену продажи за единицу лота")
        with self.db.tx() as c:
            if pid:
                old=c.execute("SELECT * FROM products WHERE id=? AND mode=?",(pid,mode)).fetchone()
                if not old:raise BusinessError("Товар не найден")
                if old["bot_hidden"] and old["fp_lot_id"]!=values["fp_lot_id"]:raise BusinessError("Сначала восстановите скрытое объявление, затем меняйте его ID")
                c.execute("UPDATE products SET "+",".join(k+"=?" for k in values)+" WHERE id=?",(*values.values(),pid))
            else:
                pid=c.execute("INSERT INTO products("+",".join(values)+") VALUES("+",".join("?" for _ in values)+")",tuple(values.values())).lastrowid
            self.db.audit(actor,"product.saved",pid,conn=c)
        return pid

    @locked
    def verify_product(self,pid):
        p=self.db.one("SELECT * FROM products WHERE id=?",(pid,))
        if not p:raise BusinessError("Товар не найден")
        lot=self.fp(p["mode"]).lot(p["fp_lot_id"])
        if lot["subcategory"]!=p["fp_subcategory"] or p["marker"] not in lot["text"]:raise BusinessError("Метка или подраздел объявления не совпали")
        if lot["currency"]!="RUB": raise BusinessError("Валюта объявления не подтверждена как RUB")
        self.db.execute("UPDATE products SET verified=1 WHERE id=?",(pid,))
        return "Метка, подраздел, право редактирования и валюта подтверждены"

    @locked
    def import_order(self,d,mode=None,baseline=False):
        mode=mode or self.dataset()
        existing=self.db.one("SELECT * FROM orders WHERE id=?",(d["id"],))
        if existing:
            self.refresh_status(d,existing)
            return d["id"]
        if d.get("section_type")!="lot" or d["status"]!="paid":return None
        products=self.db.rows("SELECT * FROM products WHERE mode=? AND archived=0 AND fp_subcategory=?",(mode,d["subcategory"]))
        matches=[p for p in products if p["marker"] in d.get("description","")]
        if len(matches)!=1:
            if len(matches)>1:self.alert("manual","Неоднозначные метки товара","Заказ "+d["id"]+" не импортирован",key="mapping:"+d["id"])
            return None
        p=matches[0]
        quantity=positive_int(d["quantity"],10000)
        units=quantity*p["multiplier"];uc=units*p["sku_uc"]
        reason=""
        if baseline:reason="Заказ существовал до подключения/перезапуска. Подтвердите, что UC ещё не выдавались."
        if not p["enabled"] or not p["verified"]:reason="Товар не включён или его объявление не проверено"
        if quantity>self.db.setting("max_quantity") or units>10000:reason="Количество превышает лимит"
        if d["currency"]!="RUB" or d["revenue"]<=0:reason="Сумма или валюта заказа не поддерживается"
        state="manual" if reason else "awaiting_uid"
        if self.mode()=="observe" and mode=="live":state="observed";reason="Режим наблюдения: никакой выдачи или автоответов"
        now=time.time()
        baseline_messages=[]
        if mode=="live":
            # Messages already present BEFORE our first request for this order are history,
            # not a new instruction to top up. This prevents an old UID from being reused.
            baseline_messages=self.fp("live").messages({d["chat_id"]:-1})
        fee=0 if self.db.setting("funpay_sum_is_net") else int(Decimal(d["revenue"])*Decimal(str(self.db.setting("funpay_fee_percent")))/100)
        with self.db.tx() as c:
            c.execute("""INSERT INTO orders(id,mode,product_id,buyer_id,buyer,chat_id,quantity,uc,sku_units,snapshot,state,fp_status,revenue,currency,fee,hold_reason,created,updated)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(d["id"],mode,p["id"],d["buyer_id"],d["buyer"],d["chat_id"],quantity,uc,units,dumps(p),state,d["status"],d["revenue"],d["currency"],fee,reason,now,now))
            c.execute("INSERT OR IGNORE INTO chat_controls(chat_id) VALUES(?)",(d["chat_id"],))
            for message in baseline_messages:
                mid=str(message["id"])
                c.execute("INSERT OR IGNORE INTO seen_messages VALUES(?,?,?)",(d["chat_id"],mid,now))
                c.execute("INSERT OR IGNORE INTO messages(external_id,chat_id,author,text,direction,created) VALUES(?,?,?,?,?,?)",(mid,d["chat_id"],str(message["author"]),message["text"],"history",now))
                if mid.isdigit():c.execute("UPDATE chat_controls SET last_seen=MAX(last_seen,?) WHERE chat_id=?",(int(mid),d["chat_id"]))
        if state=="awaiting_uid":self.queue(d["id"],"request_uid")
        elif state=="manual":self.alert("manual","Заказ ожидает проверки",reason,d["id"])
        self.db.audit("system","order.imported",d["id"],reason)
        return d["id"]

    def refresh_status(self,d,o=None):
        o=o or self.db.one("SELECT * FROM orders WHERE id=?",(d["id"],))
        if not o:return
        if (d["quantity"],d["buyer_id"],d["chat_id"],d["currency"],d["revenue"]) != (o["quantity"],o["buyer_id"],o["chat_id"],o["currency"],o["revenue"]):
            self.problem(o["id"],"manual","Изменились ключевые данные заказа FunPay",pause=True);raise BusinessError("Заказ FunPay изменился")
        self.db.execute("UPDATE orders SET fp_status=? WHERE id=?",(d["status"],o["id"]))
        if d["status"] in ("refunded","partially_refunded") and o["delivered"]>0:
            self.alert("manual","Возврат при уже выданных UC","Сверьте расчёты и фактическую сумму возврата",o["id"],key="refund:"+o["id"])
        if d["status"]!="paid" and o["state"]!="completed":
            has_batch=self.db.one("SELECT id FROM batches WHERE order_id=? AND state NOT IN('rejected','balance','retry')",(o["id"],))
            if has_batch:self.alert("manual","Статус FunPay изменился во время выдачи",d["status"],o["id"])
            else:self.db.execute("UPDATE orders SET state='cancelled',updated=? WHERE id=?",(time.time(),o["id"]))

    @locked
    def input_message(self,m):
        now=time.time();chat=m["chat_id"];text=m["text"].strip();mid=str(m["id"])
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM seen_messages WHERE chat_id=? AND message_id=?",(chat,mid)).fetchone():return
            c.execute("INSERT INTO seen_messages VALUES(?,?,?)",(chat,mid,now))
            c.execute("INSERT OR IGNORE INTO messages(external_id,chat_id,author,text,direction,created) VALUES(?,?,?,?,?,?)",(mid,chat,str(m["author"]),text,"in",now))
            if mid.isdigit():c.execute("UPDATE chat_controls SET last_seen=MAX(last_seen,?) WHERE chat_id=?",(int(mid),chat))
        orders=self.db.rows("SELECT * FROM orders WHERE chat_id=? AND buyer_id=? AND mode=? AND state NOT IN('completed','cancelled','observed') ORDER BY created",(chat,m["author"],self.dataset()))
        if not orders:return
        control=self.db.one("SELECT * FROM chat_controls WHERE chat_id=?",(chat,))
        if control and control["manual"]:return
        if self.mode()=="observe":return
        help_words=[x.strip().lower() for x in self.db.setting("help_words").split(",") if x.strip()]
        words=set(re.findall(r"[\w]+",text.lower()))
        if self.db.setting("pause_chat_on_help") and any(x in words for x in help_words):
            self.queue(orders[0]["id"],"help",mid,force=True)
            self.db.execute("UPDATE chat_controls SET manual=1 WHERE chat_id=?",(chat,))
            self.alert("manual","Покупатель вызвал оператора",text,orders[0]["id"]);return
        selected=None
        match=re.match(r"^#([A-Za-z0-9_-]{1,64})\s+(.+)$",text,re.S)
        if match:
            selected=next((o for o in orders if o["id"].upper()==match[1].upper()),None);text=match[2].strip()
            if not selected:return
        elif len(orders)==1:selected=orders[0]
        else:
            # A confirmation nonce uniquely identifies an order without an extra selector.
            selected=next((o for o in orders if o["confirm_code"] and text.upper()=="ПОДТВЕРЖДАЮ "+o["confirm_code"]),None)
            if not selected:
                msg=render_template(self.db.setting("tpl_selection"),orders=", ".join("#"+o["id"] for o in orders))
                self.queue_text(orders[0],msg,"selection:"+chat+":"+mid);return
        o=selected
        if o["state"] not in ("awaiting_uid","awaiting_confirmation","ready","waiting_balance"):return
        if self.db.one("SELECT id FROM batches WHERE order_id=?",(o["id"],)):return
        if o["state"]=="awaiting_confirmation" and text.upper()=="ПОДТВЕРЖДАЮ "+str(o["confirm_code"]):
            self.db.execute("UPDATE orders SET state='ready',confirmed=1,updated=? WHERE id=?",(now,o["id"]));return
        try:uid=validate_uid(text)
        except BusinessError:
            self.queue(o["id"],"invalid_uid",mid);return
        code=secrets.token_hex(3).upper()
        confirm=self.db.setting("confirm_uid")
        self.db.execute("UPDATE orders SET uid=?,confirm_code=?,confirmed=?,state=?,updated=? WHERE id=?",(uid,code,int(not confirm),"awaiting_confirmation" if confirm else "ready",now,o["id"]))
        if confirm:self.queue(o["id"],"confirm_uid",code)

    def balance(self,mode=None):
        mode=mode or self.dataset()
        total=self.db.one("SELECT COALESCE(SUM(delta),0) n FROM ledger WHERE mode=?",(mode,))["n"]
        reserved=self.db.one("SELECT COALESCE(SUM(b.quote),0) n FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode=? AND b.state IN('sending','unknown','retry')",(mode,))["n"]
        return total-reserved

    @locked
    def set_balance(self,value,actor="owner",mode=None):
        mode=mode or self.dataset();value=cents(value)
        if value<0:raise BusinessError("Отрицательный баланс не допускается")
        if self.db.one("SELECT b.id FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode=? AND b.state IN('sending','unknown','accepted','processing')",(mode,)):
            raise BusinessError("Нельзя пересверять остаток при незавершённых закупках; сначала проверьте их результат")
        with self.db.tx() as c:
            total=c.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE mode=?",(mode,)).fetchone()[0]
            c.execute("INSERT INTO ledger(mode,ref,delta,reason,created) VALUES(?,?,?,?,?)",(mode,"balance:"+uuid.uuid4().hex,value-total,"Сверка владельцем: это НЕ перевод денег",time.time()))
            self.db.set("balance_verified:"+mode,time.time(),c)
            self.db.audit(actor,"balance.reconciled",mode,str(value),c)
        self.resolve_alert("balance:system")
        if value>=cents(self.db.setting("balance_low_rub")):self.resolve_alert("balance:low")
        if self.db.setting("auto_resume_balance"):
            self.db.execute("UPDATE orders SET state='ready',updated=? WHERE mode=? AND state='waiting_balance'",(time.time(),mode))
            self.db.execute("UPDATE batches SET state='retry',next_check=0 WHERE state='balance' AND order_id IN(SELECT id FROM orders WHERE mode=?)",(mode,))
            if self.db.setting("pause_reason")=="balance":self.db.set("paused",False);self.db.set("pause_reason","")

    def live_ready(self):
        reasons=[]
        if not self.config.enable_live:reasons.append("Railway ENABLE_LIVE_PURCHASES=true не включён")
        if not self.config.public_url:reasons.append("PUBLIC_URL не задан")
        for k in ("gamecore_key","funpay_key","funpay_user_agent","telegram_token"):
            if not self.vault.get(k):reasons.append("Не задан секрет: "+k)
        if not self.db.setting("alert_chat_ids"):reasons.append("Не настроены получатели Telegram")
        if self.db.one("SELECT id FROM users WHERE active=1 AND role IN('owner','operator') AND totp_secret IS NULL"):
            reasons.append("Включите 2FA всем активным владельцам и операторам")
        for k in ("fees_confirmed","dynamic_price_accepted","provider_terms_confirmed"):
            if not self.db.setting(k):reasons.append(FIELDS[k][2])
        if not self.db.one("SELECT id FROM products WHERE mode='live' AND enabled=1 AND verified=1 AND available=1 AND archived=0"):
            reasons.append("Нет проверенного доступного live-товара")
        if not self.db.setting("balance_verified:live"):reasons.append("Остаток GameCore не сверялся")
        if self.db.setting("recovery_required",False):reasons.append("Восстановлена резервная копия: нужна ручная сверка")
        return reasons

    def purchase_allowed(self,mode):
        return not self.db.setting("paused") and (mode=="demo" and self.mode()=="demo" or mode=="live" and self.mode()=="live" and self.config.enable_live and self.db.setting("live_armed") and not self.db.setting("recovery_required",False))

    def validate_delivery(self,p,sku):
        if sku["id"]!=p["sku_id"] or sku["uc"]!=p["sku_uc"] or sku["region"]!=p["region"] or sku["delivery_type"]!="id_only" or sku["currency"]!="RUB" or sku["price"]<=0 or not sku["in_stock"]:
            raise BusinessError("SKU, номинал, регион, цена или наличие изменились")
        schema=sku["payload"].get("deliveryDataSchema",[])
        allowed={x.get("id") for x in schema}
        extra=json.loads(p["extra_delivery"])
        if p["uid_field"] not in allowed or set(extra)-allowed:raise BusinessError("Изменилась схема deliveryData")
        for x in schema:
            if x.get("required") and x["id"]!=p["uid_field"] and not extra.get(x["id"]):raise BusinessError("Не хватает поля "+x["id"])
        return extra

    @locked
    def prepare(self,oid):
        o=self.db.one("SELECT * FROM orders WHERE id=?",(oid,))
        if not o or o["state"]!="ready" or not self.purchase_allowed(o["mode"]):return
        if self.db.one("SELECT id FROM batches WHERE order_id=?",(oid,)):return
        control=self.db.one("SELECT manual FROM chat_controls WHERE chat_id=?",(o["chat_id"],))
        if control and control["manual"]:return
        if not o["confirmed"]:raise BusinessError("UID не подтверждён")
        validate_uid(o["uid"])
        p=json.loads(o["snapshot"])
        if not self.db.one("SELECT id FROM products WHERE id=? AND enabled=1 AND archived=0 AND verified=1",(o["product_id"],)):
            self.problem(oid,"manual","Товар отключён или требует проверки");return
        try:
            d=self.fp(o["mode"]).order(oid);self.refresh_status(d,o)
            if d["status"]!="paid":return
            if p["marker"] not in d["description"] or p["fp_subcategory"]!=d["subcategory"]:raise BusinessError("Метка оплаченного заказа не совпала")
            provider=self.provider(o["mode"])
            # Catalog membership is rechecked, not just product(id), because only the list enforces the allowlist.
            listing=provider.catalog()
            if not any(x["id"]==p["sku_id"] for x in listing):raise BusinessError("SKU отсутствует в доступном каталоге")
            sku=provider.product(p["sku_id"]);extra=self.validate_delivery(p,sku)
        except (BusinessError,ProviderError,FunPayError) as e:
            self.problem(oid,"price",str(e));return
        quote=sku["price"]*o["sku_units"]
        reserve=int((Decimal(quote)*(1+Decimal(str(self.db.setting("price_buffer_percent")))/100)).to_integral_value(rounding=ROUND_CEILING))
        effective=int(Decimal(quote)*(1+Decimal(str(self.db.setting("provider_fee_percent")))/100))
        if reserve>cents(self.db.setting("max_order_rub")) or (p["max_cost"] and quote>p["max_cost"]*o["quantity"]):
            self.problem(oid,"price","Закупка превысит лимит заказа");return
        if o["revenue"]-o["fee"]-effective<p["min_profit"]*o["quantity"]:
            self.problem(oid,"price","Прибыль ниже установленного минимума");return
        start=day_start(time.time(),self.db.setting("timezone"))
        spent=self.db.one("SELECT COALESCE(SUM(CASE WHEN b.actual_cost>0 THEN b.actual_cost ELSE b.quote END),0) n FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode=? AND b.created>=? AND b.state NOT IN('balance','rejected')",(o["mode"],start))["n"]
        if spent+reserve>cents(self.db.setting("daily_limit_rub")):
            self.problem(oid,"price","Достигнут суточный лимит закупок");return
        if self.db.setting("balance_mode")=="api" and o["mode"]=="live":
            try:bal=self.provider("live").balance(self.db.setting("balance_path"),self.db.setting("balance_json_field"))
            except (ProviderError,BusinessError) as e:self.problem(oid,"balance",str(e));return
            self.db.runtime("balance_api",{"amount":bal,"at":time.time()})
        else:bal=self.balance(o["mode"])
        if bal<reserve:
            self.wait_balance(oid);return
        key=uuid.uuid4().hex;extra[p["uid_field"]]=o["uid"]
        install=self.db.one("SELECT value FROM meta WHERE key='installation_id'")["value"]
        payload={"items":[{"productId":p["sku_id"],"quantity":o["sku_units"],"deliveryData":extra}],"externalOrderId":f"auc-{install}-{key}"}
        if o["mode"]=="live" and self.config.public_url and self.vault.get("webhook_secret"):
            payload["callbackUrl"]=self.config.public_url+"/webhooks/gamecore"
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM batches WHERE order_id=?",(oid,)).fetchone():return
            bid=c.execute("INSERT INTO batches(order_id,key,units,uc_per_unit,payload,quote,state,created) VALUES(?,?,?,?,?,?,?,?)",(oid,key,o["sku_units"],p["sku_uc"],dumps(payload),reserve,"retry",time.time())).lastrowid
        self.send_batch(bid)

    def wait_balance(self,oid):
        self.db.execute("UPDATE orders SET state='waiting_balance',hold_reason='balance',updated=? WHERE id=?",(time.time(),oid))
        self.alert("balance","Не хватает баланса GameCore","Пополните сайт, затем обновите остаток в панели",oid,key="balance:system")
        self.pause("balance");self.queue(oid,"waiting_balance")

    @locked
    def send_batch(self,bid,recovery=False):
        b=self.db.one("SELECT * FROM batches WHERE id=?",(bid,));o=self.db.one("SELECT * FROM orders WHERE id=?",(b["order_id"],))
        if b["state"] not in ("retry","unknown") or not self.purchase_allowed(o["mode"]):return
        if b["state"]=="unknown" and not recovery:return
        if b["first_sent"] and time.time()-b["first_sent"]>23*3600:
            self.problem(o["id"],"unknown","Истекло безопасное окно повтора ключа. Новый запрос запрещён.","unknown",True);return
        if self.db.one("SELECT manual FROM chat_controls WHERE chat_id=?",(o["chat_id"],))["manual"]:return
        d=self.fp(o["mode"]).order(o["id"]);self.refresh_status(d,o)
        if d["status"]!="paid":return
        if not self.purchase_allowed(o["mode"]):return
        if b["state"]=="retry" and b["attempts"]>0:
            # A previous 402/429 created no confirmed order. Refresh price and limits,
            # but keep precisely the same request body and idempotency key.
            p=json.loads(o["snapshot"])
            try:
                listing=self.provider(o["mode"]).catalog()
                if not any(x["id"]==p["sku_id"] for x in listing):raise BusinessError("SKU исчез из каталога")
                sku=self.provider(o["mode"]).product(p["sku_id"]);self.validate_delivery(p,sku)
                quote=sku["price"]*b["units"]
                reserve=int((Decimal(quote)*(1+Decimal(str(self.db.setting("price_buffer_percent")))/100)).to_integral_value(rounding=ROUND_CEILING))
                effective=int(Decimal(quote)*(1+Decimal(str(self.db.setting("provider_fee_percent")))/100))
                if reserve>cents(self.db.setting("max_order_rub")) or (p["max_cost"] and quote>p["max_cost"]*o["quantity"]):raise BusinessError("Цена повторной попытки превысила лимит")
                if o["revenue"]-o["fee"]-effective<p["min_profit"]*o["quantity"]:raise BusinessError("Изменилась прибыль при повторной попытке")
                start=day_start(time.time(),self.db.setting("timezone"))
                spent=self.db.one("SELECT COALESCE(SUM(CASE WHEN b.actual_cost>0 THEN b.actual_cost ELSE b.quote END),0) n FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode=? AND b.created>=? AND b.id!=? AND b.state NOT IN('balance','rejected')",(o["mode"],start,bid))["n"]
                if spent+reserve>cents(self.db.setting("daily_limit_rub")):raise BusinessError("Суточный лимит повторной попытки превышен")
                if self.db.setting("balance_mode")=="api" and o["mode"]=="live":
                    bal=self.provider("live").balance(self.db.setting("balance_path"),self.db.setting("balance_json_field"))
                else:bal=self.balance(o["mode"])+b["quote"]
                if bal<reserve:
                    self.db.execute("UPDATE batches SET state='balance' WHERE id=?",(bid,));self.wait_balance(o["id"]);return
                self.db.execute("UPDATE batches SET quote=? WHERE id=?",(reserve,bid))
            except (BusinessError,ProviderError) as exc:
                self.db.execute("UPDATE batches SET state='rejected',last_error=? WHERE id=?",(str(exc),bid))
                self.problem(o["id"],"price",str(exc));return
        now=time.time()
        self.db.execute("UPDATE batches SET state='sending',first_sent=COALESCE(first_sent,?),attempts=attempts+1 WHERE id=?",(now,bid))
        try:result=self.provider(o["mode"]).create(json.loads(b["payload"]),b["key"])
        except ProviderError as e:
            state="unknown" if e.kind in ("unknown","invariant") else "balance" if e.kind=="balance" else "retry" if e.kind=="retry" else "rejected"
            self.db.execute("UPDATE batches SET state=?,last_error=?,next_check=? WHERE id=?",(state,str(e),now+e.retry_after,bid))
            if state=="balance":self.wait_balance(o["id"])
            elif state=="unknown":self.problem(o["id"],"unknown",str(e),"unknown",True)
            elif state=="rejected":self.problem(o["id"],"failed",str(e),"failed")
            return
        except Exception:
            # An unexpected client/parsing failure after POST is ALWAYS an unknown monetary outcome.
            self.db.execute("UPDATE batches SET state='unknown',last_error='Неизвестный ответ после отправки' WHERE id=?",(bid,))
            self.problem(o["id"],"unknown","Ответ после отправки не удалось проверить","unknown",True);return
        try:self.accept_batch(bid,result)
        except Exception:
            self.db.execute("UPDATE batches SET state='unknown',last_error='Ответ принят, но локальная сверка не завершена',next_check=0 WHERE id=?",(bid,))
            self.problem(o["id"],"unknown","Списание требует сверки: локальная запись ответа не подтверждена","unknown",True)

    def accept_batch(self,bid,result):
        b=self.db.one("SELECT * FROM batches WHERE id=?",(bid,));o=self.db.one("SELECT * FROM orders WHERE id=?",(b["order_id"],))
        total=cents(result["totalAmount"])
        with self.db.tx() as c:
            # INSERT checks uniqueness of every supplier code. Any mismatch rolls back the entire acceptance.
            for sub in result["orders"]:
                existing=c.execute("SELECT batch_id FROM provider_orders WHERE code=?",(sub["code"],)).fetchone()
                if existing and existing["batch_id"]!=bid:raise BusinessError("Код GameCore уже связан с другой закупкой")
                c.execute("INSERT OR IGNORE INTO provider_orders(code,batch_id,state,total,updated) VALUES(?,?,'processing',?,?)",(sub["code"],bid,cents(sub.get("total",sub.get("totalAmount",0))),time.time()))
            c.execute("UPDATE batches SET state='accepted',actual_cost=?,net_cost=?,next_check=0 WHERE id=?",(total,total,bid))
            c.execute("INSERT OR IGNORE INTO ledger(mode,ref,delta,reason,created) VALUES(?,?,?,?,?)",(o["mode"],"purchase:"+b["key"],-total,"GameCore: принято к исполнению",time.time()))
            c.execute("UPDATE orders SET state='processing',updated=? WHERE id=?",(time.time(),o["id"]))
        self.queue(o["id"],"processing")
        if total>b["quote"]:self.alert("price","Цена при создании превысила резерв","Проверьте тариф и наценку",o["id"])
        if self.balance(o["mode"])<cents(self.db.setting("balance_low_rub")):
            self.alert("balance","Низкий расчётный баланс GameCore","Сверьте остаток на сайте; это не подтверждённый API-остаток",key="balance:low")

    @locked
    def recover_batch(self,bid):
        b=self.db.one("SELECT * FROM batches WHERE id=?",(bid,));o=self.db.one("SELECT * FROM orders WHERE id=?",(b["order_id"],))
        if b["state"] not in ("unknown","sending"):return
        found=self.provider(o["mode"]).recover(json.loads(b["payload"])["externalOrderId"])
        if found:
            # Validate every detail; externalOrderId is not itself a supplier uniqueness constraint.
            details=[self.provider(o["mode"]).order(x["code"]) for x in found]
            units=sum(positive_int(i["amount"],10000) for x in details for i in x.get("items",[]))
            if units!=b["units"]:raise BusinessError("Сверка количества GameCore не сошлась; требуется ручное расследование")
            result={"totalAmount":sum(Decimal(str(x["totalAmount"])) for x in details),"orders":[{"code":x["code"],"total":x["totalAmount"]} for x in details]}
            self.accept_batch(bid,result);self.poll_batch(bid);return
        self.db.execute("UPDATE batches SET next_check=? WHERE id=?",(time.time()+120,bid))
        self.alert("unknown","Операция пока не найдена","Автоматическая новая закупка запрещена. Сверьте externalOrderId с GameCore.",o["id"])

    @locked
    def poll_batch(self,bid):
        b=self.db.one("SELECT * FROM batches WHERE id=?",(bid,));o=self.db.one("SELECT * FROM orders WHERE id=?",(b["order_id"],))
        if b["state"] not in ("accepted","processing"):return
        codes=self.db.rows("SELECT * FROM provider_orders WHERE batch_id=?",(bid,))
        if not codes:raise BusinessError("У принятой закупки нет номеров поставщика")
        states=[];delivered=0;net=0;seen_units=0
        for sub in codes:
            d=self.provider(o["mode"]).order(sub["code"])
            if not d or d.get("code")!=sub["code"] or d.get("externalOrderId")!=json.loads(b["payload"])["externalOrderId"]:
                raise BusinessError("Ответ операции GameCore не прошёл сопоставление")
            items=d.get("items")
            if not isinstance(items,list) or not items:raise BusinessError("В ответе GameCore отсутствуют позиции")
            counts=[positive_int(i.get("amount"),10000) for i in items]
            count=sum(counts);done=sum(n for i,n in zip(items,counts) if i.get("status")=="completed")
            final=all(i.get("status") in ("completed","failed") for i in items) and d.get("status") in ("completed","failed","cancelled","refunded")
            state="completed" if final and done==count else "failed" if final else "processing"
            cost=cents(d.get("totalAmount",sub["total"]/100))
            if state=="completed":net_sub=cost
            elif final and done==0:net_sub=0
            elif final:
                all_prices=sum(cents(i.get("price"))*n for i,n in zip(items,counts))
                if all_prices!=cost:raise BusinessError("Неясная стоимость частичной выдачи: сумма позиций не совпала")
                net_sub=sum(cents(i.get("price"))*n for i,n in zip(items,counts) if i.get("status")=="completed")
            else:net_sub=cost
            if done<sub["delivered_units"]:raise BusinessError("GameCore уменьшил ранее подтверждённую выдачу")
            self.db.execute("UPDATE provider_orders SET state=?,delivered_units=?,net_cost=?,payload=?,updated=? WHERE code=?",(state,done,net_sub,dumps(d),time.time(),sub["code"]))
            states.append(state);delivered+=done;net+=net_sub;seen_units+=count
        if seen_units!=b["units"] or delivered>b["units"]:raise BusinessError("Количество позиций поставщика не совпало с закупкой")
        terminal=all(s in ("completed","failed") for s in states)
        bstate="completed" if terminal and delivered==b["units"] else "partial" if terminal and delivered else "failed" if terminal else "processing"
        now=time.time()
        with self.db.tx() as c:
            c.execute("UPDATE batches SET state=?,delivered_units=?,net_cost=?,next_check=? WHERE id=?",(bstate,delivered,net,now+self.db.setting("provider_poll_seconds"),bid))
            uc=delivered*b["uc_per_unit"]
            c.execute("UPDATE orders SET delivered=?,state=?,updated=? WHERE id=?",(uc,bstate,now,o["id"]))
            if terminal:
                c.execute("INSERT OR IGNORE INTO ledger(mode,ref,delta,reason,created) VALUES(?,?,?,?,?)",(o["mode"],"refund:"+b["key"],b["actual_cost"]-net,"Расчёт возврата по финальным позициям; сверяйте с балансом поставщика",now))
        if bstate=="completed":
            if o["fp_status"] in ("refunded","partially_refunded"):
                self.alert("manual","UC выданы, но оплата возвращена/изменена","Сверьте платёж на FunPay",o["id"],key="refund:"+o["id"])
            else:self.queue(o["id"],"completed")
            for kind in ("unknown","partial","failed","manual"):self.resolve_alert(kind+":"+o["id"])
        elif terminal:self.problem(o["id"],bstate,f"Подтверждено {delivered*b['uc_per_unit']} из {o['uc']} UC. Повтор не выполняется автоматически.",bstate,True)
        elif now-b["created"]>self.db.setting("processing_timeout_seconds"):
            self.alert("manual","Пополнение выполняется дольше обычного","Ожидание не является основанием для повторной закупки",o["id"])

    @locked
    def adopt(self,oid,actor):
        o=self.db.one("SELECT * FROM orders WHERE id=?",(oid,))
        if not o or o["state"] not in ("manual","observed"):raise BusinessError("Этот заказ нельзя принять таким действием")
        if self.db.one("SELECT id FROM batches WHERE order_id=?",(oid,)):raise BusinessError("Закупка уже существует. Сначала сверка у поставщика; повтор запрещён")
        if o["quantity"]>self.db.setting("max_quantity") or o["sku_units"]>10000:raise BusinessError("Превышен лимит количества")
        if o["mode"]=="live" and self.mode()!="live":raise BusinessError("Сначала включите live")
        d=self.fp(o["mode"]).order(oid);self.refresh_status(d,o)
        if d["status"]!="paid":raise BusinessError("Заказ не оплачен или уже закрыт")
        p=self.db.one("SELECT * FROM products WHERE id=? AND enabled=1 AND verified=1 AND archived=0",(o["product_id"],))
        if not p:raise BusinessError("Сначала включите и проверьте товар")
        # Keep the purchased recipe; do not retroactively replace it with edited settings.
        self.db.execute("UPDATE orders SET state='awaiting_uid',uid=NULL,confirmed=0,hold_reason='',updated=? WHERE id=?",(time.time(),oid))
        self.queue(oid,"request_uid","adopt:"+uuid.uuid4().hex)
        self.db.audit(actor,"order.adopted_not_previously_delivered",oid)

    @locked
    def seed_demo(self,quantity=3,uc=60):
        if self.mode()!="demo":raise BusinessError("Демонстрация доступна только в demo")
        self.sync_catalog("demo")
        pid=self.db.one("SELECT id FROM products WHERE marker=?",(f"[AUC:DEMO{uc}]",))
        if not pid:
            n=self.save_product({"name":f"ДЕМО {uc} UC","marker":f"[AUC:DEMO{uc}]","fp_lot_id":uc,"fp_subcategory":1,"fp_category":1,"sku_id":uc,"sku_uc":uc,"multiplier":1,"uid_field":"gameUserId","sale_price":uc*2,"enabled":True,"manage_active":True})
        else:n=pid["id"]
        self.verify_product(n)
        oid="DEMO-"+secrets.token_hex(4).upper()
        d={"id":oid,"buyer_id":2,"buyer":"Тестовый покупатель","chat_id":"users-1-2","quantity":positive_int(quantity),"status":"paid","revenue":uc*200*int(quantity),"currency":"RUB","subcategory":1,"section_type":"lot","description":f"[AUC:DEMO{uc}]"}
        self.db.set("demo-fp:"+oid,d)
        if not self.db.setting("balance_verified:demo"):self.set_balance(100000,"demo","demo")
        self.import_order(d,"demo");return oid

    @locked
    def manual_resolution(self,oid,action,cost,refund,note,actor):
        o=self.db.one("SELECT * FROM orders WHERE id=?",(oid,))
        if not o:raise BusinessError("Заказ не найден")
        if self.db.one("SELECT id FROM batches WHERE order_id=? AND state IN('sending','unknown','accepted','processing','retry','balance')",(oid,)):
            raise BusinessError("Есть незавершённая/неизвестная попытка. Ручное завершение запрещено до сверки её результата.")
        cost,refund=cents(cost),cents(refund)
        if cost<0 or not 0<=refund<=o["revenue"] or len(note.strip())<10:raise BusinessError("Укажите корректные суммы и пояснение со ссылкой/номером подтверждения (от 10 символов)")
        if action not in ("finance","manual_complete","manual_refund"):raise BusinessError("Неизвестный результат")
        old=self.db.one("SELECT * FROM order_finance WHERE order_id=?",(oid,))
        prior_cost=old["manual_cost"] if old else 0
        with self.db.tx() as c:
            c.execute("INSERT INTO order_finance VALUES(?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET manual_cost=excluded.manual_cost,refund=excluded.refund,note=excluded.note,updated=excluded.updated",(oid,cost,refund,note.strip()[:3000],time.time()))
            if cost!=prior_cost:
                c.execute("INSERT INTO ledger(mode,ref,delta,reason,created) VALUES(?,?,?,?,?)",(o["mode"],"manual:"+uuid.uuid4().hex,prior_cost-cost,"Ручная дополнительная закупка для "+oid,time.time()))
            if action=="manual_complete":
                c.execute("UPDATE orders SET delivered=uc,state='completed',hold_reason='',updated=? WHERE id=?",(time.time(),oid))
            elif action=="manual_refund":
                if refund!=o["revenue"]:raise BusinessError("Для закрытия полным возвратом укажите полную сумму заказа")
                c.execute("UPDATE orders SET state='cancelled',hold_reason='Закрыт владельцем после фактического возврата',updated=? WHERE id=?",(time.time(),oid))
            self.db.audit(actor,action,oid,note,c)
        if action=="manual_complete":self.queue(oid,"completed","manual")

    @locked
    def reset_demo(self, actor):
        """Remove only simulated data; never offer a destructive reset for live orders."""
        if self.mode() != "demo":
            raise BusinessError("Очистка доступна только в DEMO")
        with self.db.tx() as c:
            count=c.execute("SELECT COUNT(*) FROM orders WHERE mode='demo'").fetchone()[0]
            # A chat may be shared with live history: keep it in that case.
            chats=[r[0] for r in c.execute("SELECT DISTINCT d.chat_id FROM orders d WHERE d.mode='demo' AND NOT EXISTS(SELECT 1 FROM orders l WHERE l.mode='live' AND l.chat_id=d.chat_id)")]
            c.execute("DELETE FROM alert_deliveries WHERE alert_id IN(SELECT id FROM alerts WHERE order_id IN(SELECT id FROM orders WHERE mode='demo'))")
            c.execute("DELETE FROM alerts WHERE order_id IN(SELECT id FROM orders WHERE mode='demo')")
            c.execute("DELETE FROM messages WHERE order_id IN(SELECT id FROM orders WHERE mode='demo')")
            c.execute("DELETE FROM outbox WHERE mode='demo'")
            c.execute("DELETE FROM provider_orders WHERE batch_id IN(SELECT b.id FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode='demo')")
            c.execute("DELETE FROM batches WHERE order_id IN(SELECT id FROM orders WHERE mode='demo')")
            c.execute("DELETE FROM order_finance WHERE order_id IN(SELECT id FROM orders WHERE mode='demo')")
            c.execute("DELETE FROM orders WHERE mode='demo'")
            for chat in chats:
                c.execute("DELETE FROM messages WHERE chat_id=?",(chat,))
                c.execute("DELETE FROM seen_messages WHERE chat_id=?",(chat,))
                c.execute("DELETE FROM chat_controls WHERE chat_id=?",(chat,))
            c.execute("DELETE FROM products WHERE mode='demo' AND NOT EXISTS(SELECT 1 FROM orders o WHERE o.product_id=products.id)")
            c.execute("DELETE FROM catalog WHERE mode='demo'")
            c.execute("DELETE FROM ledger WHERE mode='demo'")
            c.execute("DELETE FROM settings WHERE key LIKE 'demo-%' OR key='balance_verified:demo'")
            self.db.set("demo_scenario","success",c);self.db.set("paused",True,c)
            self.db.set("pause_reason","Демонстрация очищена; реальные данные сохранены",c)
            self.db.audit(actor,"demo.reset",detail=str(count),conn=c)
        return count
