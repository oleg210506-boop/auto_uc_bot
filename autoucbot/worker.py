from __future__ import annotations
import fcntl
import json
import os
import threading
import time
import uuid
from decimal import Decimal,ROUND_CEILING
import requests
from .db import dumps
from .utils import BusinessError,cents
from .adapters.gamecore import ProviderError
from .adapters.funpay import FunPayError
from .funpay_guard import FunPayDeferredError, FunPayRateLimitedError

class InstanceLock:
    """Lifetime lock on the persistent volume: one active process per database."""
    def __init__(self,path):self.path=path;self.file=None
    def acquire(self):
        self.file=open(self.path,"a+")
        try:fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close();self.file=None
            raise RuntimeError("Другая копия autoUCbot уже использует этот диск. Оставьте 1 replica и 1 worker.") from None
    def release(self):
        if self.file:
            fcntl.flock(self.file,fcntl.LOCK_UN);self.file.close();self.file=None

class Worker:
    def __init__(self,engine):
        self.e=engine;self.db=engine.db
        self.stop_event=threading.Event();self.thread=None
        self.lock=InstanceLock(self.db.data_dir/"instance.lock")
        self.due={};self.startup=True;self.fp_baselined=set()
        self.tg=requests.Session()
        self.chat_cursor=0;self.sales_epoch=-1;self.sales_scan_at=0
        # Retain all customer-configured features, but fix the dangerous old 4–6s interval.
        if int(self.db.setting("funpay_poll_seconds", 30)) < 20:
            self.db.set("funpay_poll_seconds", 30)
            self.db.audit("system", "funpay.poll.migrated", detail="Интервал увеличен до 30 с, чтобы снизить риск HTTP 429")

    def start(self):
        self.lock.acquire()
        self.db.execute("UPDATE batches SET state='unknown',next_check=0 WHERE state='sending'")
        self.db.execute("UPDATE fazer_parts SET state='unknown',next_check=0 WHERE state='sending'")
        self.db.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
        self.db.execute("UPDATE tasks SET state='error',result='Перезапуск: действие нужно проверить; автоматически не повторено',updated=? WHERE state='running'",(time.time(),))
        self.thread=threading.Thread(target=self.run,name="autoucbot-worker",daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=40)
        if self.thread and self.thread.is_alive():
            # Do not release the lifetime lock while a purchase can still be executing.
            return
        self.lock.release();self.tg.close()

    def schedule(self,key,interval,fn):
        if self.db.setting("migration_frozen",False):return
        if time.time()<self.due.get(key,0):return
        self.due[key]=time.time()+interval
        try:fn()
        except FunPayDeferredError as exc:
            self.due[key] = time.time() + max(1, exc.wait_seconds)
            if isinstance(exc, FunPayRateLimitedError):
                self.e.alert("funpay", "FunPay: временное ограничение HTTP 429",
                             "FunPay просит прекратить запросы. Бот автоматически ждёт; не обновляйте ключ и не нажимайте проверку многократно.",
                             key="worker:funpay")
            if key == "funpay":
                self.db.runtime("funpay", {"ok": False, "status": "cooldown" if exc.reason != "local_budget" else "pacing",
                                                  "retry_after_seconds": exc.wait_seconds})
            return
        except Exception as exc:
            # Never log request headers, tokens, response bodies or buyer data to Railway logs.
            detail=str(exc) if isinstance(exc,(BusinessError,ProviderError,FunPayError)) else "Внутренняя ошибка: "+type(exc).__name__
            self.e.alert("manual" if key not in ("funpay","catalog") else "funpay" if key=="funpay" else "fazer",f"Ошибка: {key}",detail,key="worker:"+key)
            self.db.runtime(key,{"ok":False,"error":detail})
            if key=="catalog" and self.e.dataset()=="live":self.e.alert("fazer","Каталог FazerCards недоступен",detail,key="fazer:catalog")
            self.due[key]=time.time()+max(interval,30)

    def run(self):
        while not self.stop_event.is_set():
            try:self.tick()
            except Exception as exc:
                # A database outage cannot be recorded to that same database reliably.
                print("autoUCbot worker error:",type(exc).__name__,flush=True)
            self.stop_event.wait(1)

    def tick(self):
        self.db.runtime("worker",{"ok":True,"pid":os.getpid()})
        if self.db.setting("migration_frozen",False):return
        self.schedule("tasks",1,self.tasks)
        self.schedule("funpay",max(20,int(self.db.setting("funpay_poll_seconds"))),self.funpay)
        self.schedule("purchases",2,self.purchases)
        self.schedule("poll",5,self.poll)
        self.schedule("outbox",2,self.outbox)
        self.schedule("reminders",30,self.reminders)
        self.schedule("telegram",15,self.alerts)
        self.schedule("lots",60,self.lots)
        self.schedule("catalog",self.db.setting("catalog_seconds"),self.catalog)
        self.schedule("fazer_wallet",self.db.setting("fazer_balance_seconds"),self.fazer_wallet)
        self.schedule("backups",60,self.backups)
        self.schedule("cleanup",3600,self.cleanup)
        self.startup=False

    def catalog(self):
        if self.e.dataset()=="demo":
            # The legacy demo catalog must not erase the new Fazer demo SKUs.
            if self.db.one("SELECT id FROM products WHERE mode='demo' AND supplier='fazer'"):
                self.e.sync_fazer_catalog('demo')
            else:self.e.sync_catalog('demo')
        elif self.e.vault.get("fazer_key"):
            self.e.sync_catalog()

    def fazer_wallet(self):
        if self.e.dataset()=='live' and self.e.vault.get('fazer_key'):
            self.e.read_fazer_wallet('live')

    def enqueue(self,kind,payload=None):
        now=time.time()
        return self.db.execute("INSERT INTO tasks(kind,payload,created,updated) VALUES(?,?,?,?)",(kind,dumps(payload or {}),now,now))

    def tasks(self):
        task=self.db.one("SELECT * FROM tasks WHERE state='pending' AND updated<=? ORDER BY id LIMIT 1",(time.time(),))
        if not task:return
        self.db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?",(time.time(),task["id"]))
        try:
            with self.e.lock:
                if self.db.setting("migration_frozen",False):raise BusinessError("Установка заморожена")
                p=json.loads(task["payload"]);kind=task["kind"]
                if kind=="catalog":result=f"Загружено товаров: {self.e.sync_catalog(p.get('mode'))}"
                elif kind=="fazer_test":
                    profile=self.e.fazer_account('live',True)
                    balance=self.e.read_fazer_wallet('live')
                    plans=self.e.fazer('live').plans()
                    self.db.set('fazer_plans',plans)
                    result=dumps({'account':profile,'balance_usd':str(Decimal(balance)/1000000),'plans':plans})
                elif kind=="fazer_balance":result=str(Decimal(self.e.read_fazer_wallet(p.get('mode','live')))/1000000)+' USD'
                elif kind=="fazer_discover":result=dumps(self.e.discover_fazer())
                elif kind=="fazer_catalog":result='Товаров FazerCards: '+str(self.e.sync_fazer_catalog(p.get('mode','live')))
                elif kind=="fazer_validation":
                    result=dumps(self.e.fazer('live').validation_games());self.db.set('fazer_validation_games',json.loads(result))
                elif kind=="fazer_bind":
                    self.e.bind_fazer_order(p['id'],p['code'],p['proof'],p['actor']);result='Операция привязана после ручной сверки, новая покупка не отправлялась'
                elif kind=="funpay_test":
                    result=dumps(self.e.fp("live").connect());self.db.runtime("funpay",{"ok":True})
                elif kind=="verify_product":result=self.e.verify_product(int(p["id"]))
                elif kind=="check_order":
                    o=self.db.one("SELECT * FROM orders WHERE id=?",(p["id"],))
                    if not o:raise BusinessError("Заказ не найден")
                    self.e.refresh_status(self.e.fp(o["mode"]).order(o["id"]),o)
                    for b in self.db.rows("SELECT id,state FROM batches WHERE order_id=?",(o["id"],)):
                        if b["state"] in ("unknown","sending"):self.e.recover_batch(b["id"])
                        else:self.e.poll_batch(b["id"])
                    result="Проверка выполнена. Посмотрите состояние и тревоги заказа."
                elif kind=="adopt":self.e.adopt(p["id"],p["actor"]);result="Заказ принят под контроль"
                elif kind=="manual_uid":result=self.e.manually_confirm_recipient_from_chat(p["id"],p["uid"],p["actor"])
                elif kind=="backup":result=self.db.backup(self.db.setting("backup_keep")).name
                elif kind=="telegram_test":
                    self.telegram_send(p["chat_id"],"autoUCbot: тест срочных уведомлений. Покупок не выполнялось.");result="Сообщение отправлено"
                elif kind=="telegram_recipients":
                    result=dumps(self.telegram_recipients())
                elif kind=="balance_api":
                    bal=self.e.provider("live").balance(self.db.setting("balance_path"),self.db.setting("balance_json_field"))
                    self.db.runtime("balance_api",{"amount":bal,"at":time.time()})
                    if self.db.one("SELECT b.id FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode='live' AND b.state IN('sending','unknown','accepted','processing')"):
                        result=f"Остаток API: {bal/100:.2f} RUB. Пересверка книги после завершения операций."
                    else:self.e.set_balance(bal/100,"GameCore API","live");result=f"Подтверждён остаток API: {bal/100:.2f} RUB"
                else:raise BusinessError("Неизвестное действие")
            self.db.execute("UPDATE tasks SET state='done',result=?,updated=? WHERE id=?",(str(result)[:12000],time.time(),task["id"]))
        except FunPayDeferredError as exc:
            # No HTTP request was performed; defer, do not mark the task failed.
            self.db.execute("UPDATE tasks SET state='pending',result=?,updated=? WHERE id=?",
                            ("Ожидаем FunPay, не повторять вручную",time.time()+max(20, exc.wait_seconds),task["id"]))
            if isinstance(exc, FunPayRateLimitedError):
                self.e.alert("funpay", "FunPay: временное ограничение HTTP 429",
                             "Проверка отложена; сайт попросил подождать.", key="worker:funpay")
        except Exception as exc:
            msg=str(exc) if isinstance(exc,(BusinessError,ProviderError,FunPayError,ValueError)) else type(exc).__name__+": действие не выполнено"
            self.db.execute("UPDATE tasks SET state='error',result=?,updated=? WHERE id=?",(msg[:2000],time.time(),task["id"]))

    def funpay(self):
        mode=self.e.dataset()
        if mode=="demo":return
        if not self.e.vault.get("funpay_key"):return
        if self.e.funpay_traffic.is_blocked():
            self.db.runtime("funpay", {"ok":False,"status":"cooldown",
                                           "until":self.e.funpay_traffic.until()})
            return
        with self.e.lock:
            if self.db.setting("migration_frozen",False):return
            fp=self.e.fp("live");fp.ensure()
            products=self.db.rows("SELECT * FROM products WHERE mode='live' AND archived=0 AND verified=1")
            # Poll new orders on changed counters or at most once per 120 s. Live
            # chat is checked separately, so no need to hammer paid-sales HTML.
            epoch=getattr(fp,"counter_epoch",0)
            if epoch!=self.sales_epoch or time.time()-self.sales_scan_at>=120 or not self.fp_baselined:
                for subcat in sorted({p["fp_subcategory"] for p in products}):
                    ids=fp.paid_ids(subcat)
                    if getattr(fp, 'sales_page_limit_hit', False):
                        self.db.runtime('funpay_sales', {
                            'ok': True, 'subcategory': subcat,
                            'history_truncated': True,
                            'detail': 'Проверены только 3 последние страницы продаж: старую историю сверяйте вручную',
                        })
                    initial=subcat not in self.fp_baselined
                    for oid in ids:
                        existing=self.db.one("SELECT state FROM orders WHERE id=?",(oid,))
                        if existing:continue
                        d=fp.order(oid)
                        self.e.import_order(d,"live",baseline=initial)
                    self.fp_baselined.add(subcat)
                self.sales_epoch=epoch;self.sales_scan_at=time.time()
            # 1–2 status checks per pass; never 10 order requests every six seconds.
            # Recent closed orders are eventually rechecked for refunds.
            check_before=time.time()-120
            due=self.db.rows("""SELECT id FROM orders WHERE mode='live' AND updated<? AND
                (state NOT IN('completed','cancelled') OR updated<?)
                ORDER BY updated LIMIT 2""",(check_before,time.time()-1800))
            for o in due:
                self.e.refresh_status(fp.order(o["id"]))
                self.db.execute("UPDATE orders SET updated=? WHERE id=?",(time.time(),o["id"]))
            chats=self.db.rows("""SELECT c.* FROM chat_controls c WHERE EXISTS(SELECT 1 FROM orders o WHERE o.chat_id=c.chat_id AND o.mode='live'
                AND (o.state NOT IN('completed','cancelled') OR o.created>?)) ORDER BY c.chat_id""",(time.time()-86400,))
            subset=chats[self.chat_cursor:self.chat_cursor+4]
            self.chat_cursor=(self.chat_cursor+4) if self.chat_cursor+4<len(chats) else 0
            messages=fp.messages({c["chat_id"]:c["last_seen"] or -1 for c in subset})
            for m in messages:self.e.input_message(m)
        self.db.runtime("funpay",{"ok":True,"user_id":fp.user_id})
        self.e.resolve_alert("worker:funpay")

    def purchases(self):
        if self.e.funpay_traffic.is_blocked():return
        if not self.e.purchase_allowed(self.e.dataset()):return
        # Filter by direction before selecting the queue head; a paused Stars
        # purchase cannot starve unrelated UC orders (and vice versa).
        candidates=self.db.rows("SELECT b.id,o.service,o.supplier,o.mode FROM batches b JOIN orders o ON o.id=b.order_id WHERE o.mode=? AND b.state='retry' AND b.next_check<=? ORDER BY b.next_check,b.id",(self.e.dataset(),time.time()))
        for row in candidates:
            if row['supplier']=='fazer' and not self.e.direction_open(row['service']):continue
            if row['supplier']=='gamecore' and row['mode']=='live':continue
            self.db.execute('UPDATE batches SET next_check=? WHERE id=?',(time.time()+3,row['id']))
            self.e.send_batch(row['id']);return
        for row in self.db.rows("SELECT * FROM orders WHERE mode=? AND state='ready' ORDER BY created",(self.e.dataset(),)):
            if row['supplier']=='fazer' and not self.e.direction_open(row['service']):continue
            self.e.prepare(row['id']);return

    def poll(self):
        rows=self.db.rows("SELECT b.* FROM batches b JOIN orders o ON o.id=b.order_id WHERE b.state IN('accepted','processing','unknown') AND b.next_check<=? ORDER BY b.next_check LIMIT 5",(time.time(),))
        for b in rows:
            try:
                if b["state"]=="unknown":self.e.recover_batch(b["id"])
                else:self.e.poll_batch(b["id"])
            except Exception as exc:
                self.db.execute("UPDATE batches SET next_check=? WHERE id=?",(time.time()+60,b["id"]))
                message=str(exc) if isinstance(exc,(BusinessError,ProviderError)) else "Не удалось проверить результат: "+type(exc).__name__
                self.e.alert("unknown","Не удалось сверить пополнение",message,b["order_id"])
                if isinstance(exc,BusinessError):self.e.pause(message)
        if self.db.setting("balance_mode")=="api" and self.e.dataset()=="live" and self.e.vault.get("gamecore_key") and time.time()>self.due.get("api_balance",0):
            self.due["api_balance"]=time.time()+self.db.setting("balance_ttl_seconds")
            self.enqueue("balance_api")

    def outbox(self):
        # Observe never sends to FunPay. Cooldown must not turn pending into unknown.
        if self.e.mode()=="observe" or self.e.funpay_traffic.is_blocked():return
        rows=self.db.rows("SELECT * FROM outbox WHERE mode=? AND state='pending' AND next_attempt<=? ORDER BY id LIMIT 5",(self.e.dataset(),time.time()))
        for m in rows:
            manual=self.db.one("SELECT manual FROM chat_controls WHERE chat_id=?",(m["chat_id"],))
            if m["kind"]=="auto" and manual and manual["manual"]:
                self.db.execute("UPDATE outbox SET state='suppressed' WHERE id=?",(m["id"],));continue
            self.db.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?",(m["id"],))
            try:
                with self.e.lock:
                    if self.db.setting("migration_frozen",False):raise BusinessError("Установка заморожена")
                    self.e.fp(m["mode"]).send(m["chat_id"],m["text"])
                with self.db.tx() as c:
                    c.execute("UPDATE outbox SET state='sent' WHERE id=?",(m["id"],))
                    c.execute("INSERT INTO messages(chat_id,order_id,author,text,direction,created) VALUES(?,?,?,?,?,?)",(m["chat_id"],m["order_id"],"autoUCbot" if m["kind"]=="auto" else "operator",m["text"],"out",time.time()))
                    if m["kind"]=="auto" and (":request_uid:" in m["dedupe"] or ":confirm_uid:" in m["dedupe"]):
                        c.execute("UPDATE orders SET last_reminder=? WHERE id=?", (time.time(), m["order_id"]))
            except FunPayDeferredError as exc:
                if isinstance(exc, FunPayRateLimitedError):
                    # Even a rejected POST is treated conservatively as uncertain.
                    self.db.execute("UPDATE outbox SET state='uncertain',last_error=? WHERE id=?",
                                    ("429: необходимо проверить, было ли доставлено сообщение",m["id"]))
                    self.e.alert("funpay", "FunPay: временное ограничение HTTP 429",
                                 "Ожидаем восстановления; проверьте последнее сообщение вручную.", key="worker:funpay")
                else:
                    # The traffic gate rejected the call before any HTTP request.
                    self.db.execute("UPDATE outbox SET state='pending',next_attempt=? WHERE id=?",
                                    (time.time()+exc.wait_seconds,m["id"]))
                    return
            except Exception:
                self.db.execute("UPDATE outbox SET state='uncertain',last_error='Доставка сообщения не подтверждена; автоматического повтора нет' WHERE id=?",(m["id"],))
                self.e.alert("manual","Проверьте сообщение в чате FunPay","Сообщение могло отправиться. Проверьте чат перед ручным повтором.",m["order_id"],key="outbox:"+str(m["id"]))

    def reminders(self):
        if self.e.mode()=="observe" or self.e.funpay_traffic.is_blocked():return
        now=time.time()
        for o in self.db.rows("SELECT * FROM orders WHERE mode=? AND state IN('awaiting_uid','awaiting_confirmation')",(self.e.dataset(),)):
            if now-o["created"]>self.e.rule("uid_timeout_seconds",o):
                self.e.alert("manual","Покупатель долго не присылает получателя/подтверждение","Автоматический возврат не выполняется",o["id"],key="uid:"+o["id"])
            # A delayed/imported live order must not get several reminders before
            # its *first* UID request was actually sent to the customer.
            if o["mode"]=="live" and not o["last_reminder"]:
                sent=self.db.one("SELECT 1 FROM outbox WHERE order_id=? AND state='sent' AND "
                                 "(dedupe LIKE ? OR dedupe LIKE ?)",
                                 (o["id"],o["id"]+":request_uid:%",o["id"]+":confirm_uid:%"))
                if not sent:
                    continue
                self.db.execute("UPDATE orders SET last_reminder=? WHERE id=?",(now,o["id"]))
                continue
            if self.e.rule("reminders",o) and o["reminder_count"]<self.e.rule("reminder_limit",o) and now-max(o["last_reminder"],o["created"])>self.e.rule("reminder_seconds",o):
                self.e.queue(o["id"],"reminder",str(o["reminder_count"]))
                self.db.execute("UPDATE orders SET reminder_count=reminder_count+1,last_reminder=? WHERE id=?",(now,o["id"]))

    def telegram_request(self,method,payload):
        token=self.e.vault.get("telegram_token")
        if not token:raise BusinessError("Сначала задайте токен Telegram")
        try:r=self.tg.post("https://api.telegram.org/bot"+token+"/"+method,json=payload,timeout=(8,15),allow_redirects=False)
        except requests.RequestException:raise BusinessError("Telegram не ответил") from None
        try:d=r.json()
        except ValueError:raise BusinessError("Некорректный ответ Telegram") from None
        if not d.get("ok"):raise BusinessError("Telegram отклонил запрос. Проверьте токен/chat_id и нажмите Start; HTTP "+str(r.status_code))
        return d.get("result")
    def telegram_send(self,chat,text):
        if not str(chat).lstrip("-").isdigit():raise BusinessError("chat_id должен быть числом")
        return self.telegram_request("sendMessage",{"chat_id":str(chat),"text":text[:4000],"disable_web_page_preview":True})
    def telegram_recipients(self):
        updates=self.telegram_request("getUpdates",{"timeout":0,"limit":100,"allowed_updates":["message"]})
        chats={}
        for u in updates:
            chat=u.get("message",{}).get("chat",{})
            if "id" in chat:chats[str(chat["id"])]=chat.get("title") or chat.get("username") or chat.get("first_name","")
        return chats
    def alerts(self):
        if not self.e.vault.get("telegram_token"):return
        recipients=[s.strip() for s in self.db.setting("alert_chat_ids").split(",") if s.strip()]
        kinds={s.strip() for s in self.db.setting("alert_kinds").split(",")}
        now=time.time()
        for a in self.db.rows("SELECT * FROM alerts WHERE active=1 AND acked=0 ORDER BY created LIMIT 20"):
            if a["kind"] not in kinds and a['kind']!='fazer':continue
            targets=recipients
            row=self.db.one('SELECT * FROM orders WHERE id=?',(a['order_id'],)) if a['order_id'] else None
            if row and row['supplier']=='fazer':
                configured=self.e.service_rule(row['service'],'alert_chat_ids')
                if configured:targets=[x.strip() for x in configured.split(',') if x.strip()]
            for recipient in targets:
                last=self.db.one("SELECT sent FROM alert_deliveries WHERE alert_id=? AND chat_id=?",(a["id"],recipient))
                # A single rate-limit incident generates ONE message per person.
                # Other critical alerts keep their configured repeat interval.
                if last and (a["kind"]=="funpay" or now-last["sent"]<self.db.setting("alert_repeat_seconds")):continue
                link=(self.e.config.public_url+"/orders/"+a["order_id"]) if a["order_id"] and self.e.config.public_url else self.e.config.public_url
                self.telegram_send(recipient,"⚠️ autoUCbot\n"+a["title"]+"\n"+a["detail"]+"\n"+link)
                self.db.execute("INSERT INTO alert_deliveries VALUES(?,?,?) ON CONFLICT(alert_id,chat_id) DO UPDATE SET sent=excluded.sent",(a["id"],recipient,now))
                self.db.execute("UPDATE alerts SET last_sent=? WHERE id=?",(now,a["id"]))

    def lots(self):
        if self.e.mode()!="live" or not self.db.setting("live_armed") or not self.e.config.enable_live:return
        if self.e.funpay_traffic.is_blocked():return
        products=self.db.rows("SELECT * FROM products WHERE mode='live' AND verified=1 AND archived=0")
        with self.e.lock:
            if self.db.setting("migration_frozen",False):return
            fp=self.e.fp("live")
            for p in products:
                hide=self.db.setting("paused") or not p["enabled"] or not p["available"] or (p["supplier"]=="fazer" and not self.e.direction_open(p["service"]))
                if self.e.rule("auto_hide",p) and p["manage_active"]:
                    if hide and not p["bot_hidden"]:
                        lot=fp.lot(p["fp_lot_id"])
                        if lot["active"]:
                            fp.change_lot(p,active=False)
                            self.db.execute("UPDATE products SET bot_hidden=1 WHERE id=?",(p["id"],))
                    elif not hide and p["bot_hidden"]:
                        fp.change_lot(p,active=True)
                        self.db.execute("UPDATE products SET bot_hidden=0 WHERE id=?",(p["id"],))
                if p["auto_price"] and not hide and p["last_price"]:
                    cost=Decimal(p["last_price"]*p["multiplier"])
                    if p['supplier']=='fazer':
                        cat=self.db.one('SELECT payload FROM catalog WHERE mode=? AND id=?',(p['mode'],p['sku_id']))
                        if not cat:continue
                        meta=json.loads(cat['payload'])
                        cost=Decimal(meta['price_usd'])*Decimal(str(self.db.setting('fazer_usd_rub')))*100*p['multiplier']
                    cost*=1+Decimal(str(self.e.rule("provider_fee_percent",p)))/100
                    gross=(cost*(1+Decimal(str(p["markup"]))/100)+p["min_profit"])/(1-Decimal(str(self.e.rule("funpay_fee_percent",p)))/100)
                    price=int(gross.to_integral_value(rounding=ROUND_CEILING))
                    if price!=p["sale_price"]:
                        fp.change_lot(p,price=price)
                        self.db.execute("UPDATE products SET sale_price=? WHERE id=?",(price,p["id"]))
                        self.db.audit("system","lot.price",p["id"],str(price))
            if not self.db.setting("paused"):
                groups={}
                for p in products:
                    if p["enabled"] and p["available"] and self.e.rule("auto_raise",p) and (p["supplier"]!="fazer" or self.e.direction_open(p["service"])):groups.setdefault(p["fp_category"],set()).add(p["fp_subcategory"])
                for category,subcats in groups.items():
                    key="raise_next:"+str(category)
                    if time.time()<self.db.setting(key,0):continue
                    wait=fp.raise_lots(category,sorted(subcats))
                    interval=max(self.e.rule("raise_seconds",p) for p in products if p["fp_category"]==category and p["fp_subcategory"] in subcats)
                    self.db.set(key,time.time()+max(wait,interval))
                    self.db.runtime("raise",{"ok":True,"category":category,"subcategories":sorted(subcats)})
        self.e.resolve_alert("worker:lots")

    def backups(self):
        now=time.time()
        if now-self.db.setting("last_backup",0)>self.db.setting("backup_hours")*3600:
            self.db.backup(self.db.setting("backup_keep"));self.db.set("last_backup",now)
    def cleanup(self):
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires<?",(time.time(),))
            c.execute("DELETE FROM login_attempts WHERE until<?",(time.time()-86400,))
            c.execute("DELETE FROM messages WHERE created<?",(time.time()-self.db.setting("message_retention_days")*86400,))
            # Keep seen_messages, orders, batch keys and financial history indefinitely for deduplication.
