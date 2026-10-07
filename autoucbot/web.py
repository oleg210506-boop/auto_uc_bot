from __future__ import annotations
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime,timedelta
from pathlib import Path
from urllib.parse import parse_qs,urlsplit,quote
from zoneinfo import ZoneInfo
from fastapi import FastAPI,Request
from fastapi.responses import HTMLResponse,RedirectResponse,PlainTextResponse,FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from . import __version__
from .config import Config,FIELDS,TEMPLATES,SECRET_ENV
from .db import DB,dumps
from .engine import Engine
from .worker import Worker
from .security import Vault,password_hash,verify_password,token_hash,new_totp_secret,verify_totp,provisioning_uri,webhook_valid
from .utils import BusinessError,money,cents,csv_safe,validate_template,positive_int,day_start

ROOT=Path(__file__).parent
STATE_NAMES={"awaiting_uid":"Ожидает UID","awaiting_confirmation":"Ожидает подтверждения","ready":"Готов к закупке","waiting_balance":"Ожидает баланса",
 "processing":"Выполняется","unknown":"Результат неизвестен","partial":"Частично выдан","failed":"Ошибка","completed":"Выдан","cancelled":"Отменён/закрыт","manual":"Нужен оператор","observed":"Наблюдение"}
NAV=[("/","Обзор"),("/orders","Заказы"),("/products","Товары"),("/catalog","Каталог GameCore"),("/customers","Покупатели"),("/stats","Статистика"),("/alerts","Тревоги"),("/connections","Подключения"),("/settings","Настройки"),("/team","Команда"),("/backups","Резервные копии"),("/audit","Журнал"),("/account","Мой доступ")]


def create_app(config=None):
    config=config or Config.from_env();db=DB(config.data_dir);vault=Vault(db,config.secret);engine=Engine(db,vault,config);worker=Worker(engine)
    if not db.one("SELECT id FROM users LIMIT 1"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}",config.bootstrap_user):raise ValueError("ADMIN_USERNAME: 3–40 латинских букв/цифр/_.-")
        db.execute("INSERT INTO users(username,password_hash,role,created) VALUES(?,?,'owner',?)",(config.bootstrap_user,password_hash(config.bootstrap_password),time.time()))
    @asynccontextmanager
    async def lifespan(app):
        if config.start_worker:worker.start()
        try:yield
        finally:
            if config.start_worker:worker.stop()
    app=FastAPI(title="autoUCbot",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.db=db;app.state.engine=engine;app.state.worker=worker;app.state.vault=vault
    templates=Jinja2Templates(directory=str(ROOT/"templates"))
    templates.env.filters["money"]=money
    templates.env.filters["stamp"]=lambda x:datetime.fromtimestamp(x,ZoneInfo(db.setting("timezone","Europe/Amsterdam"))).strftime("%d.%m.%Y %H:%M:%S") if x else "—"
    templates.env.filters["jsonpretty"]=lambda x:json.dumps(json.loads(x) if isinstance(x,str) else x,ensure_ascii=False,indent=2)
    templates.env.filters["state"]=lambda x:STATE_NAMES.get(x,x)
    app.mount("/static",StaticFiles(directory=str(ROOT/"static")),name="static")

    def page(request,name,**ctx):
        user=getattr(request.state,"user",None)
        return templates.TemplateResponse(request=request,name=name,context={"user":user,"csrf":getattr(request.state,"csrf",""),"nav":NAV,"mode":engine.mode(),"paused":db.setting("paused"),"version":__version__,"states":STATE_NAMES,"notice":request.query_params.get("notice",""),**ctx})
    def redirect(path,notice=""):
        return RedirectResponse(path+("&" if "?" in path else "?")+"notice="+quote(notice),status_code=303) if notice else RedirectResponse(path,status_code=303)
    def role(request,*roles):
        if request.state.user["role"] not in roles:raise PermissionError("Недостаточно прав")
    def owner(request):role(request,"owner")
    def actor(request):return request.state.user["username"]
    def require_order(oid):
        o=db.one("SELECT * FROM orders WHERE id=?",(oid,))
        if not o:raise BusinessError("Заказ не найден")
        return o
    def task_notice(tid):return f"Действие #{tid} добавлено. Результат будет в таблице действий; обновите страницу."

    @app.middleware("http")
    async def security(request,call_next):
        path=request.url.path
        if request.headers.get("content-length","").isdigit() and int(request.headers["content-length"])>1_048_576:
            return PlainTextResponse("Слишком большой запрос",413)
        request.state.user=None;request.state.csrf=""
        public=path in ("/login","/healthz","/webhooks/gamecore") or path.startswith("/static/")
        cookie=request.cookies.get("auc_session","")
        if cookie:
            row=db.one("SELECT u.*,s.csrf,s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1",(token_hash(cookie),time.time()))
            if row:request.state.user=row;request.state.csrf=row["csrf"]
        if not public and not request.state.user:return redirect("/login")
        if request.method=="POST" and path!="/webhooks/gamecore":
            body=await request.body()
            if len(body)>1_048_576:return PlainTextResponse("Слишком большой запрос",413)
            if not request.headers.get("content-type","").startswith("application/x-www-form-urlencoded"):
                return PlainTextResponse("Ожидается обычная HTML-форма",415)
            fields=parse_qs(body.decode("utf-8",errors="replace"),keep_blank_values=True)
            expected=request.cookies.get("auc_login_csrf","") if path=="/login" else request.state.csrf
            supplied=fields.get("csrf",[""])[0]
            if not expected or not secrets.compare_digest(expected,supplied):return PlainTextResponse("Ошибка CSRF. Обновите страницу и повторите.",403)
            origin=request.headers.get("origin")
            expected_origin=config.public_url or str(request.base_url).rstrip("/")
            if origin and origin!=expected_origin:return PlainTextResponse("Неверный Origin",403)
        try:response=await call_next(request)
        except PermissionError as exc:response=page(request,"error.html",title="Нет доступа",error=str(exc));response.status_code=403
        except (BusinessError,ValueError,sqlite3.IntegrityError) as exc:
            error="Конфликт данных: метка, ID объявления или имя пользователя уже используются." if isinstance(exc,sqlite3.IntegrityError) else str(exc)
            response=page(request,"error.html",title="Действие не выполнено",error=error);response.status_code=400
        except Exception as exc:
            db.audit("web","error",detail=type(exc).__name__)
            response=page(request,"error.html",title="Внутренняя ошибка",error="Действие не подтверждено. Проверьте журнал и состояние заказа; не повторяйте закупку вслепую.");response.status_code=500
        response.headers.update({"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer","Cache-Control":"no-store",
            "Content-Security-Policy":"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if config.secure_cookie:response.headers["Strict-Transport-Security"]="max-age=31536000"
        return response

    @app.get("/healthz")
    async def health():
        beat=db.one("SELECT updated FROM runtime WHERE key='worker'")
        ok=not config.start_worker or bool(beat and time.time()-beat["updated"]<300)
        return JSONResponse({"status":"ok" if ok else "starting_or_stalled"},status_code=200 if ok else 503)

    @app.get("/login",response_class=HTMLResponse)
    async def login_page(request:Request):
        csrf=secrets.token_urlsafe(32)
        r=page(request,"login.html",login_csrf=csrf,error="")
        r.set_cookie("auc_login_csrf",csrf,httponly=True,secure=config.secure_cookie,samesite="strict",max_age=900)
        return r
    @app.post("/login")
    async def login(request:Request):
        f=await request.form();name=str(f.get("username","")).strip();password=str(f.get("password",""));otp=str(f.get("otp","")).strip()
        key=token_hash("account:"+name.lower());now=time.time()
        attempt=db.one("SELECT * FROM login_attempts WHERE key=?",(key,))
        if attempt and attempt["count"]>=5 and attempt["until"]>now:raise BusinessError("Слишком много попыток. Повторите через 15 минут.")
        u=db.one("SELECT * FROM users WHERE username=? AND active=1",(name,))
        good=bool(u and verify_password(password,u["password_hash"]))
        step=None;recovery=False
        if good and u["totp_secret"]:
            step=verify_totp(vault.decrypt(u["totp_secret"]),otp,u["totp_last"])
            if step is None:recovery=bool(db.one("SELECT 1 FROM recovery_codes WHERE user_id=? AND code_hash=?",(u["id"],token_hash(otp))))
            good=step is not None or recovery
        if not good:
            db.execute("INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN until<? THEN 1 ELSE count+1 END,until=excluded.until",(key,now+900,now))
            raise BusinessError("Неверное имя, пароль или код 2FA")
        token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(32)
        with db.tx() as c:
            if step is not None:
                cur=c.execute("UPDATE users SET totp_last=? WHERE id=? AND totp_last<?",(step,u["id"],step))
                if cur.rowcount!=1:raise BusinessError("Этот код уже использован. Дождитесь следующего.")
            if recovery:
                cur=c.execute("DELETE FROM recovery_codes WHERE user_id=? AND code_hash=?",(u["id"],token_hash(otp)))
                if cur.rowcount!=1:raise BusinessError("Резервный код уже использован")
            c.execute("DELETE FROM login_attempts WHERE key=?",(key,))
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",(token_hash(token),u["id"],csrf,now+8*3600,now))
            db.audit(name,"login",conn=c)
        r=redirect("/");r.set_cookie("auc_session",token,httponly=True,secure=config.secure_cookie,samesite="strict",max_age=8*3600);r.delete_cookie("auc_login_csrf");return r
    @app.post("/logout")
    async def logout(request:Request):
        db.execute("DELETE FROM sessions WHERE token_hash=?",(request.state.user["token_hash"],))
        r=redirect("/login");r.delete_cookie("auc_session");return r

    @app.get("/",response_class=HTMLResponse)
    async def dashboard(request:Request):
        data=engine.dataset()
        counts=db.rows("SELECT state,COUNT(*) n FROM orders WHERE mode=? GROUP BY state",(data,))
        return page(request,"dashboard.html",counts=counts,balance=engine.balance(),balance_at=db.setting("balance_verified:"+data),
                    latest=db.rows("SELECT * FROM orders WHERE mode=? ORDER BY created DESC LIMIT 8",(data,)),
                    alerts=db.rows("SELECT * FROM alerts WHERE active=1 ORDER BY updated DESC LIMIT 5"),
                    runtime=db.rows("SELECT * FROM runtime WHERE key IN('worker','funpay','catalog:live','raise','balance_api') ORDER BY key"),
                    reasons=engine.live_ready(),pause_reason=db.setting("pause_reason",""),live_armed=db.setting("live_armed"))
    @app.post("/control")
    async def control(request:Request):
        owner(request);f=await request.form();action=f.get("action")
        with engine.lock:
            if action=="pause":engine.pause("Пауза владельцем "+actor(request))
            elif action=="resume":
                if engine.mode()=="live":
                    reasons=engine.live_ready()
                    if reasons:raise BusinessError("; ".join(reasons))
                    if not db.setting("live_armed"):raise BusinessError("Сначала включите live отдельной кнопкой")
                db.set("paused",False);db.set("pause_reason","")
            elif action in ("demo","observe","live"):
                if db.one("SELECT id FROM batches WHERE state IN('sending','unknown','accepted','processing')"):
                    raise BusinessError("Сначала дождитесь/проверьте незавершённые закупки. Пауза не мешает их проверке.")
                if action=="live":
                    if f.get("ack")!="РАЗРЕШАЮ ЗАКУПКИ":raise BusinessError("Введите РАЗРЕШАЮ ЗАКУПКИ")
                    reasons=engine.live_ready()
                    if reasons:raise BusinessError("; ".join(reasons))
                    db.set("live_armed",True)
                else:db.set("live_armed",False)
                db.set("mode",action);db.set("paused",True);db.set("pause_reason","Режим изменён: проверьте настройки перед запуском")
                worker.fp_baselined.clear()
            elif action=="recovery_clear":
                if f.get("ack")!="ИСТОРИЯ СВЕРЕНА":raise BusinessError("Введите ИСТОРИЯ СВЕРЕНА только после проверки FunPay и всех списаний GameCore после даты копии")
                db.set("recovery_required",False)
            else:raise BusinessError("Неизвестное действие")
            db.audit(actor(request),"control",action)
        return redirect("/","Настройка сохранена")

    def order_filters(request):
        mode=request.query_params.get("mode",engine.dataset());mode=mode if mode in ("demo","live") else engine.dataset()
        query=request.query_params.get("q","").strip()[:100];state=request.query_params.get("state","")
        where="mode=?";args=[mode]
        if query:where+=" AND (id LIKE ? OR uid LIKE ? OR buyer LIKE ?)";args.extend(["%"+query+"%"]*3)
        if state in STATE_NAMES:where+=" AND state=?";args.append(state)
        return where,args
    @app.get("/orders",response_class=HTMLResponse)
    async def orders(request:Request):
        where,args=order_filters(request);p=max(1,min(100000,int(request.query_params.get("page","1"))))
        rows=db.rows("SELECT * FROM orders WHERE "+where+" ORDER BY created DESC LIMIT 50 OFFSET ?",(*args,(p-1)*50))
        return page(request,"orders.html",orders=rows,page_number=p,total=db.one("SELECT COUNT(*) n FROM orders WHERE "+where,args)["n"])
    @app.get("/orders/{oid}",response_class=HTMLResponse)
    async def order_page(request:Request,oid:str):
        o=require_order(oid)
        return page(request,"order.html",order=o,batches=db.rows("SELECT * FROM batches WHERE order_id=?",(oid,)),
                    provider_orders=db.rows("SELECT p.* FROM provider_orders p JOIN batches b ON b.id=p.batch_id WHERE b.order_id=?",(oid,)),
                    messages=db.rows("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 100",(o["chat_id"],))[::-1],
                    control=db.one("SELECT * FROM chat_controls WHERE chat_id=?",(o["chat_id"],)),
                    finance=db.one("SELECT * FROM order_finance WHERE order_id=?",(oid,)) or {},tasks=db.rows("SELECT * FROM tasks ORDER BY id DESC LIMIT 8"),outbox=db.rows("SELECT * FROM outbox WHERE order_id=? ORDER BY id DESC LIMIT 20",(oid,)))
    @app.post("/orders/{oid}/action")
    async def order_action(request:Request,oid:str):
        role(request,"owner","operator");o=require_order(oid);f=await request.form();action=f.get("action")
        with engine.lock:
            if action=="check":tid=worker.enqueue("check_order",{"id":oid});notice=task_notice(tid)
            elif action=="adopt":
                owner(request)
                if f.get("ack")!="UC НЕ ВЫДАВАЛИСЬ":raise BusinessError("Подтвердите UC НЕ ВЫДАВАЛИСЬ после проверки истории")
                tid=worker.enqueue("adopt",{"id":oid,"actor":actor(request)});notice=task_notice(tid)
            elif action=="take":
                c=db.one("SELECT * FROM chat_controls WHERE chat_id=?",(o["chat_id"],))
                if c["owner_user_id"] not in (None,request.state.user["id"]) and request.state.user["role"]!="owner":raise BusinessError("Чат занят другим оператором")
                db.execute("UPDATE chat_controls SET manual=1,owner_user_id=? WHERE chat_id=?",(request.state.user["id"],o["chat_id"]));notice="Чат у вас. Автоответы и новые закупки в нём приостановлены."
            elif action=="release":
                c=db.one("SELECT * FROM chat_controls WHERE chat_id=?",(o["chat_id"],))
                if c["owner_user_id"] not in (None,request.state.user["id"]) and request.state.user["role"]!="owner":raise BusinessError("Чат занят другим оператором")
                db.execute("UPDATE chat_controls SET manual=0,owner_user_id=NULL WHERE chat_id=?",(o["chat_id"],));notice="Чат возвращён автоматике. Старые подавленные сообщения не переотправляются."
            elif action=="message":
                if o["mode"]=="live" and engine.mode()!="live":raise BusinessError("В наблюдении сообщения не отправляются")
                c=db.one("SELECT * FROM chat_controls WHERE chat_id=?",(o["chat_id"],))
                if not c["manual"] or c["owner_user_id"]!=request.state.user["id"]:raise BusinessError("Сначала возьмите чат себе")
                text=str(f.get("text","")).strip()
                if not 1<=len(text)<=3000:raise BusinessError("Сообщение: 1–3000 символов")
                engine.queue_text(o,text,"operator:"+secrets.token_hex(16),"manual");notice="Сообщение поставлено на отправку"
            elif action in ("finance","manual_complete","manual_refund"):
                owner(request)
                if f.get("ack")!="СВЕРЕНО ВРУЧНУЮ":raise BusinessError("Введите СВЕРЕНО ВРУЧНУЮ после проверки фактической выдачи/возврата")
                engine.manual_resolution(oid,action,f.get("manual_cost",0),f.get("refund",0),str(f.get("proof","")),actor(request))
                notice="Ручной результат записан. Эта кнопка не покупала UC и не переводила возврат на FunPay."
            elif action=="note":db.execute("UPDATE orders SET note=? WHERE id=?",(str(f.get("note",""))[:3000],oid));notice="Заметка сохранена"
            elif action=="demo_message":
                if o["mode"]!="demo" or engine.mode()!="demo":raise BusinessError("Это действие только для демо")
                engine.input_message({"id":str(time.time_ns()),"chat_id":o["chat_id"],"author":o["buyer_id"],"text":str(f.get("text",""))});notice="Демо-сообщение обработано"
            else:raise BusinessError("Неизвестное действие")
            db.audit(actor(request),"order."+str(action),oid)
        return redirect("/orders/"+oid,notice)
    @app.post("/demo")
    async def demo(request:Request):
        owner(request);f=await request.form();scenario=str(f.get("scenario","success"))
        if scenario not in ("success","balance","unknown_before","unknown_after","partial","failed","pending","split"):raise BusinessError("Неизвестный сценарий")
        db.set("demo_scenario",scenario)
        oid=engine.seed_demo(positive_int(f.get("quantity",3)),60)
        db.audit(actor(request),"demo.created",oid,scenario)
        return redirect("/orders/"+oid,"Это локальная демонстрация. Денег не списывает.")

    @app.post("/demo/reset")
    async def reset_demo(request:Request):
        owner(request);f=await request.form()
        if f.get("ack")!="ОЧИСТИТЬ ДЕМО":raise BusinessError("Введите ОЧИСТИТЬ ДЕМО. Это удалит только демонстрационные записи.")
        n=engine.reset_demo(actor(request))
        return redirect("/",f"Очищено демо-заказов: {n}. Live-история и доступы сохранены.")

    @app.get("/catalog",response_class=HTMLResponse)
    async def catalog_page(request:Request):
        mode=request.query_params.get("mode",engine.dataset());mode="live" if mode=="live" else "demo"
        return page(request,"catalog.html",catalog=db.rows("SELECT * FROM catalog WHERE mode=? ORDER BY uc,id",(mode,)),catalog_mode=mode,
                    tasks=db.rows("SELECT * FROM tasks WHERE kind='catalog' ORDER BY id DESC LIMIT 6"))
    @app.get("/products",response_class=HTMLResponse)
    async def products(request:Request):
        return page(request,"products.html",products=db.rows("SELECT * FROM products WHERE mode=? ORDER BY archived,id",(engine.dataset(),)))
    @app.get("/products/edit/{pid}",response_class=HTMLResponse)
    async def edit_product(request:Request,pid:int):
        owner(request)
        p=db.one("SELECT * FROM products WHERE id=? AND mode=?",(pid,engine.dataset())) if pid else None
        if pid and not p:raise BusinessError("Товар не найден")
        return page(request,"product_edit.html",product=p or {},catalog=db.rows("SELECT * FROM catalog WHERE mode=? ORDER BY uc",(engine.dataset(),)))
    @app.post("/products/save")
    async def save_product(request:Request):
        owner(request);f=dict(await request.form());pid=int(f.pop("id","0"));newid=engine.save_product(f,pid or None,actor(request));return redirect("/products",f"Товар #{newid} сохранён. Выполните проверку объявления перед выдачей.")
    @app.post("/products/{pid}/action")
    async def product_action(request:Request,pid:int):
        owner(request);f=await request.form()
        p=db.one("SELECT * FROM products WHERE id=?",(pid,))
        if not p:raise BusinessError("Товар не найден")
        if f.get("action")=="verify":return redirect("/connections",task_notice(worker.enqueue("verify_product",{"id":pid})))
        if f.get("action")=="unarchive":
            db.execute("UPDATE products SET archived=0,enabled=0,verified=0 WHERE id=?",(pid,));db.audit(actor(request),"product.unarchived",pid)
            return redirect("/products","Товар восстановлен выключенным. Настройте и проверьте его перед включением.")
        if f.get("action")=="archive":
            if p["bot_hidden"]:raise BusinessError("Сначала восстановите скрытый ботом лот либо вручную отключите его на FunPay и снимите отметку через проверку. Для остановки просто снимите «Включён».")
            if db.one("SELECT id FROM orders WHERE product_id=? AND state NOT IN('completed','cancelled','failed','partial')",(pid,)):raise BusinessError("У товара есть незавершённые заказы")
            db.execute("UPDATE products SET archived=1,enabled=0 WHERE id=?",(pid,));db.audit(actor(request),"product.archived",pid)
        else:raise BusinessError("Неизвестное действие")
        return redirect("/products","Товар архивирован. История сохранена.")

    @app.get("/connections",response_class=HTMLResponse)
    async def connections(request:Request):
        owner(request)
        return page(request,"connections.html",secret_status={k:bool(vault.get(k)) for k in SECRET_ENV},env_secrets={k:bool(os.getenv(v)) for k,v in SECRET_ENV.items()},
                    settings={k:db.setting(k) for k in FIELDS},balance=engine.balance(),tasks=db.rows("SELECT * FROM tasks ORDER BY id DESC LIMIT 20"),
                    webhook_url=config.public_url+"/webhooks/gamecore" if config.public_url else "Сначала задайте PUBLIC_URL")
    @app.post("/connections/secrets")
    async def save_secrets(request:Request):
        owner(request);f=await request.form()
        with engine.lock:
            for k in SECRET_ENV:
                value=str(f.get(k,"")).strip()
                if not value:continue
                if len(value)>2048 or "\n" in value or "\r" in value:raise BusinessError("Некорректное значение секрета")
                if k=="telegram_token" and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}",value):raise BusinessError("Некорректный токен Telegram")
                vault.set(k,value);db.audit(actor(request),"secret.changed",k)
            worker.fp_baselined.clear()
        return redirect("/connections","Ключи сохранены зашифрованно. Пустые поля оставили старые значения.")
    @app.post("/connections/balance")
    async def balance_save(request:Request):
        owner(request);f=await request.form();engine.set_balance(f.get("balance"),actor(request));return redirect("/connections","Расчётный остаток сверён. Сам сайт этой кнопкой не пополняется.")
    @app.post("/tasks")
    async def tasks(request:Request):
        owner(request);f=await request.form();kind=str(f.get("kind"))
        if kind not in ("catalog","funpay_test","telegram_test","telegram_recipients","balance_api","backup"):raise BusinessError("Действие не разрешено")
        payload={}
        if kind=="catalog":payload["mode"]="live" if f.get("mode")=="live" else engine.dataset()
        if kind=="telegram_test":payload["chat_id"]=str(f.get("chat_id",""))
        tid=worker.enqueue(kind,payload);db.audit(actor(request),"task.queued",tid,kind)
        return redirect("/connections",task_notice(tid))

    @app.get("/settings",response_class=HTMLResponse)
    async def settings(request:Request):
        owner(request)
        return page(request,"settings.html",fields=FIELDS,values={k:db.setting(k) for k in FIELDS},templates=TEMPLATES,tpl_values={k:db.setting("tpl_"+k) for k in TEMPLATES})
    @app.post("/settings")
    async def settings_save(request:Request):
        owner(request);f=await request.form();values={}
        for key,spec in FIELDS.items():
            kind=spec[0]
            if kind=="bool":v=key in f
            elif kind in ("int","number","money"):
                v=int(str(f.get(key,""))) if kind=="int" else float(str(f.get(key,"")))
                if not spec[4]<=v<=spec[5]:raise BusinessError(spec[2]+": значение вне диапазона")
            elif kind=="choice":
                v=str(f.get(key,""))
                if v not in spec[4]:raise BusinessError("Некорректный выбор")
            else:v=str(f.get(key,""))[:2000]
            values[key]=v
        try:ZoneInfo(values["timezone"])
        except Exception:raise BusinessError("Неизвестный часовой пояс") from None
        if values["alert_chat_ids"] and any(not re.fullmatch(r"-?[0-9]{1,20}",s.strip()) for s in values["alert_chat_ids"].split(",")):raise BusinessError("Некорректные chat_id Telegram")
        if values["balance_path"] and (not values["balance_path"].startswith("/b2b/") or any(x in values["balance_path"] for x in ("..","\\","@","://"))):raise BusinessError("Метод остатка должен быть относительным /b2b/…")
        if values["stats_from"]:datetime.strptime(values["stats_from"],"%Y-%m-%d")
        for key in TEMPLATES:values["tpl_"+key]=validate_template(str(f.get("tpl_"+key,"")))
        if "{uid}" not in values["tpl_confirm_uid"] or "{code}" not in values["tpl_confirm_uid"]:
            raise BusinessError("В сообщении подтверждения обязательны {uid} и {code}")
        if "{orders}" not in values["tpl_selection"]:raise BusinessError("В выборе заказа обязательна переменная {orders}")
        with engine.lock,db.tx() as c:
            for k,v in values.items():db.set(k,v,c)
            db.audit(actor(request),"settings.saved",conn=c)
        return redirect("/settings","Настройки сохранены")

    @app.get("/alerts",response_class=HTMLResponse)
    async def alerts(request:Request):return page(request,"alerts.html",alerts=db.rows("SELECT * FROM alerts ORDER BY active DESC,updated DESC LIMIT 200"))
    @app.post("/alerts/{aid}")
    async def alert_ack(request:Request,aid:int):
        role(request,"owner","operator");f=await request.form()
        if f.get("action")=="resolve":db.execute("UPDATE alerts SET active=0 WHERE id=?",(aid,))
        else:db.execute("UPDATE alerts SET acked=1 WHERE id=?",(aid,))
        db.audit(actor(request),"alert.ack",aid);return redirect("/alerts","Подтверждение сохранено. Оно не меняет статус заказа.")
    @app.get("/customers",response_class=HTMLResponse)
    async def customers(request:Request):
        q=request.query_params.get("q","")[:100]
        rows=db.rows("SELECT buyer_id,buyer,COUNT(*) orders,SUM(revenue) revenue,SUM(delivered) uc,MAX(created) last FROM orders WHERE mode=? AND buyer LIKE ? GROUP BY buyer_id ORDER BY last DESC LIMIT 200",(engine.dataset(),"%"+q+"%"))
        return page(request,"customers.html",customers=rows)

    def stats_range(request):
        tz=ZoneInfo(db.setting("timezone"));start=request.query_params.get("from") or db.setting("stats_from") or datetime.now(tz).strftime("%Y-%m-01")
        end=request.query_params.get("to") or datetime.now(tz).strftime("%Y-%m-%d")
        a=datetime.strptime(start,"%Y-%m-%d").replace(tzinfo=tz).timestamp();b=(datetime.strptime(end,"%Y-%m-%d").replace(tzinfo=tz)+timedelta(days=1)).timestamp()
        if b<=a:raise BusinessError("Неверный диапазон дат")
        return start,end,a,b
    @app.get("/stats",response_class=HTMLResponse)
    async def stats(request:Request):
        start,end,a,b=stats_range(request);mode=engine.dataset()
        sql="""WITH costs AS (SELECT order_id,SUM(net_cost) cost FROM batches GROUP BY order_id),
            detail AS (SELECT o.*,COALESCE(c.cost,0)+COALESCE(f.manual_cost,0) cost,
            CASE WHEN o.fp_status='refunded' THEN o.revenue ELSE COALESCE(f.refund,0) END refund
            FROM orders o LEFT JOIN costs c ON c.order_id=o.id LEFT JOIN order_finance f ON f.order_id=o.id
            WHERE o.mode=? AND o.created>=? AND o.created<?)
            SELECT COUNT(*) n,COALESCE(SUM(state='completed'),0) complete,COALESCE(SUM(delivered),0) uc,
            COALESCE(SUM(CASE WHEN state='completed' THEN revenue-refund ELSE 0 END),0) gross,
            COALESCE(SUM(CASE WHEN state IN('completed','cancelled','failed','partial') THEN cost ELSE 0 END),0) cost,
            COALESCE(SUM(CASE WHEN state='completed' AND refund<revenue THEN fee ELSE 0 END),0) fee,
            COALESCE(SUM(CASE WHEN state NOT IN('completed','cancelled','failed','partial') THEN cost ELSE 0 END),0) pending,
            COALESCE(SUM(refund),0) refunds FROM detail"""
        agg=db.one(sql,(mode,a,b))
        gross,fee,cost=agg["gross"],agg["fee"],agg["cost"]
        extra=int(cost*db.setting("provider_fee_percent")/100)
        expenses=db.rows("SELECT * FROM expenses WHERE created>=? AND created<? ORDER BY created DESC LIMIT 200",(a,b)) if mode=="live" else []
        overhead=db.one("SELECT COALESCE(SUM(amount),0) n FROM expenses WHERE created>=? AND created<?",(a,b))["n"] if mode=="live" else 0
        summary={"count":agg["n"],"complete":agg["complete"],"gross":gross,"cost":cost,"fee":fee+extra,"profit":gross-fee-cost-extra-overhead,"uc":agg["uc"],"average":int(gross/agg["complete"]) if agg["complete"] else 0,"expenses":overhead,"pending":agg["pending"],"refunds":agg["refunds"]}
        return page(request,"stats.html",summary=summary,start=start,end=end,expenses=expenses)
    @app.post("/stats/expense")
    async def expense(request:Request):
        owner(request);f=await request.form();amount=cents(f.get("amount"));description=str(f.get("description","")).strip()[:500]
        if amount<=0 or not description:raise BusinessError("Укажите положительную сумму и описание")
        db.execute("INSERT INTO expenses(amount,description,created,user_id) VALUES(?,?,?,?)",(amount,description,time.time(),request.state.user["id"]))
        db.audit(actor(request),"expense.added",detail=str(amount));return redirect("/stats","Расход добавлен для live-статистики")
    @app.get("/export.csv")
    async def export(request:Request):
        where,args=order_filters(request)
        # Stream in chunks to avoid materializing the entire history in RAM.
        from fastapi.responses import StreamingResponse
        def generate():
            out=io.StringIO();w=csv.writer(out,delimiter=";")
            yield "\ufeff".encode("utf-8")
            columns=["id","mode","buyer","uid","quantity","uc","delivered","state","revenue","fee","created"]
            w.writerow(columns);yield out.getvalue().encode("utf-8");out.seek(0);out.truncate()
            conn=db.connect()
            try:
                cur=conn.execute("SELECT "+",".join(columns)+" FROM orders WHERE "+where+" ORDER BY created DESC",args)
                while True:
                    rows=cur.fetchmany(100)
                    if not rows:break
                    for row in rows:w.writerow([csv_safe(v) for v in row])
                    yield out.getvalue().encode("utf-8");out.seek(0);out.truncate()
            finally:conn.close()
        return StreamingResponse(generate(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=autoucbot-orders.csv"})

    @app.get("/team",response_class=HTMLResponse)
    async def team(request:Request):
        owner(request);return page(request,"team.html",users=db.rows("SELECT id,username,role,active,totp_secret IS NOT NULL twofa FROM users ORDER BY id"))
    @app.post("/team")
    async def team_save(request:Request):
        owner(request);f=await request.form();action=f.get("action")
        with engine.lock:
            if action=="create":
                username=str(f.get("username",""));r=str(f.get("role","operator"))
                if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}",username) or r not in ("owner","operator","viewer"):raise BusinessError("Некорректное имя/роль")
                if engine.mode()=="live":engine.pause("Добавлен новый пользователь: настройте ему 2FA")
                db.execute("INSERT INTO users(username,password_hash,role,created) VALUES(?,?,?,?)",(username,password_hash(str(f.get("password",""))),r,time.time()))
                db.audit(actor(request),"user.created",username,r)
            elif action=="toggle":
                uid=int(f.get("id"));u=db.one("SELECT * FROM users WHERE id=?",(uid,))
                if not u or uid==request.state.user["id"]:raise BusinessError("Нельзя отключить себя")
                db.execute("UPDATE users SET active=1-active WHERE id=?",(uid,));db.execute("DELETE FROM sessions WHERE user_id=?",(uid,));db.audit(actor(request),"user.toggled",uid)
            else:raise BusinessError("Неизвестное действие")
        return redirect("/team","Команда обновлена")
    @app.get("/account",response_class=HTMLResponse)
    async def account(request:Request):
        u=request.state.user;pending=vault.decrypt(u["totp_pending"]) if u["totp_pending"] else None
        return page(request,"account.html",pending=pending)
    @app.post("/account")
    async def account_save(request:Request):
        f=await request.form();u=db.one("SELECT * FROM users WHERE id=?",(request.state.user["id"],));action=f.get("action")
        if not verify_password(str(f.get("password","")),u["password_hash"]):raise BusinessError("Введите текущий пароль")
        if action=="totp_start":
            if u["totp_secret"]:raise BusinessError("2FA уже включена")
            db.execute("UPDATE users SET totp_pending=? WHERE id=?",(vault.encrypt(new_totp_secret()),u["id"]))
            return redirect("/account","Добавьте ключ в приложение-аутентификатор и подтвердите кодом")
        if action=="totp_finish":
            if not u["totp_pending"] or u["totp_secret"]:raise BusinessError("Сначала создайте ключ")
            secret=vault.decrypt(u["totp_pending"]);step=verify_totp(secret,str(f.get("otp","")))
            if step is None:raise BusinessError("Неверный код 2FA")
            codes=[secrets.token_hex(6) for _ in range(8)]
            with db.tx() as c:
                c.execute("UPDATE users SET totp_secret=totp_pending,totp_pending=NULL,totp_last=? WHERE id=?",(step,u["id"]))
                for code in codes:c.execute("INSERT INTO recovery_codes VALUES(?,?)",(u["id"],token_hash(code)))
                db.audit(actor(request),"2fa.enabled",conn=c)
            return page(request,"recovery.html",codes=codes)
        if action=="password":
            new=password_hash(str(f.get("new_password","")))
            with db.tx() as c:
                c.execute("UPDATE users SET password_hash=? WHERE id=?",(new,u["id"]))
                c.execute("DELETE FROM sessions WHERE user_id=?",(u["id"],))
                db.audit(actor(request),"password.changed",conn=c)
            return redirect("/login")
        raise BusinessError("Неизвестное действие")
    @app.get("/account/qr.png")
    async def qr(request:Request):
        pending=request.state.user["totp_pending"]
        if not pending:raise BusinessError("Ключ не создан")
        import qrcode
        from fastapi.responses import Response
        img=qrcode.make(provisioning_uri(vault.decrypt(pending),actor(request)));out=io.BytesIO();img.save(out,format="PNG")
        return Response(out.getvalue(),media_type="image/png")

    @app.get("/backups",response_class=HTMLResponse)
    async def backups(request:Request):
        owner(request);folder=db.data_dir/"backups"
        files=sorted(folder.glob("*.sqlite3"),key=lambda x:x.stat().st_mtime,reverse=True) if folder.exists() else []
        return page(request,"backups.html",files=[{"name":x.name,"size":x.stat().st_size,"time":x.stat().st_mtime} for x in files],recovery_required=db.setting("recovery_required",False))
    @app.get("/backups/{name}")
    async def backup_file(request:Request,name:str):
        owner(request)
        if not re.fullmatch(r"autoucbot-[0-9]{8}-[0-9]{6}-[a-f0-9]{4}\.sqlite3",name):raise BusinessError("Недопустимое имя копии")
        path=db.data_dir/"backups"/name
        if not path.is_file():raise BusinessError("Копия не найдена")
        db.audit(actor(request),"backup.downloaded",name)
        return FileResponse(path,filename=name,media_type="application/octet-stream")
    @app.get("/audit",response_class=HTMLResponse)
    async def audit(request:Request):
        owner(request);return page(request,"audit.html",logs=db.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 300"))

    @app.post("/webhooks/gamecore")
    async def webhook(request:Request):
        body=await request.body()
        if len(body)>1_048_576:return PlainTextResponse("Too large",413)
        if not webhook_valid(body,request.headers,vault.get("webhook_secret")):return PlainTextResponse("Invalid signature",401)
        try:d=json.loads(body)
        except ValueError:return PlainTextResponse("Invalid JSON",400)
        eid=d.get("event_id")
        if not isinstance(eid,str) or not 1<=len(eid)<=200:return PlainTextResponse("Missing event id",400)
        if request.headers.get("x-idempotency-key") not in (None,eid):return PlainTextResponse("Event mismatch",400)
        with db.tx() as c:
            if c.execute("SELECT 1 FROM webhooks WHERE event_id=?",(eid,)).fetchone():return JSONResponse({"ok":True,"duplicate":True})
            c.execute("INSERT INTO webhooks VALUES(?,?,?)",(eid,dumps(d),time.time()))
            external=d.get("data",{}).get("externalOrderId")
            code=d.get("data",{}).get("orderCode")
            # Wake polling, but NEVER treat a webhook as independent proof of UC delivery.
            if code:c.execute("UPDATE batches SET next_check=0 WHERE id IN(SELECT batch_id FROM provider_orders WHERE code=?)",(code,))
            if external:
                for b in c.execute("SELECT id,payload FROM batches WHERE state='unknown'").fetchall():
                    if json.loads(b["payload"]).get("externalOrderId")==external:c.execute("UPDATE batches SET next_check=0 WHERE id=?",(b["id"],))
        return JSONResponse({"ok":True})
    return app
