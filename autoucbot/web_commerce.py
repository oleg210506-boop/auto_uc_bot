"""Additional routes; authentication, CSRF and headers are owned by web.py."""
from __future__ import annotations
import json
import re
import time
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from .adapters.fazer import webhook_signature, order_id, to_micros
from .adapters.gamecore import ProviderError
from starlette.concurrency import run_in_threadpool
from .commerce_config import SERVICE_FIELDS, SERVICE_NAMES, default_templates
from .config import TEMPLATES
from .db import dumps
from .portability import create_export, transfer_path
from .utils import BusinessError, validate_template


def install(app, db, engine, worker, vault, config, page, redirect, owner, actor):
    def service_id(value):
        if value not in SERVICE_NAMES:raise BusinessError('Направление не найдено')
        return value

    @app.get('/services/{service}')
    async def service_page(request:Request,service:str):
        owner(request);service_id(service)
        fields={k:v for k,v in SERVICE_FIELDS.items() if not (service=='uc' and k in ('min_units','max_units')) and not (service=='stars' and k.startswith('validat'))}
        return page(request,'service.html',service=service,service_name=SERVICE_NAMES[service],fields=fields,
                    values={k:engine.service_rule(service,k) for k in fields},templates=TEMPLATES,
                    tpl_values={k:db.setting('tpl_'+service+'_'+k) for k in TEMPLATES},
                    service_paused=db.setting('svc_'+service+'_paused',False),
                    service_reason=db.setting('svc_'+service+'_pause_reason',''),validation_games=db.setting('fazer_validation_games',[]))

    @app.post('/services/{service}')
    async def service_save(request:Request,service:str):
        owner(request);service_id(service);f=await request.form();values={}
        for key,spec in SERVICE_FIELDS.items():
            if service=='uc' and key in ('min_units','max_units'):continue
            if service=='stars' and key.startswith('validat'):continue
            kind=spec[0]
            if kind=='bool':value=key in f
            elif kind in ('int','number','money'):
                value=int(str(f.get(key,''))) if kind=='int' else float(str(f.get(key,'')))
                if not spec[4]<=value<=spec[5]:raise BusinessError(spec[2]+': вне диапазона')
            else:value=str(f.get(key,''))[:2000]
            values[key]=value
        if service=='stars' and values['min_units']>values['max_units']:raise BusinessError('Минимум Stars больше максимума')
        if values['alert_chat_ids'] and any(not re.fullmatch(r'-?[0-9]{1,20}',s.strip()) for s in values['alert_chat_ids'].split(',')):
            raise BusinessError('Некорректные chat_id')
        texts={k:validate_template(str(f.get('tpl_'+k,''))) for k in TEMPLATES}
        if '{code}' not in texts['confirm_uid'] or not any(x in texts['confirm_uid'] for x in ('{uid}','{recipient}')):
            raise BusinessError('В подтверждении обязательны {recipient} (или {uid}) и {code}')
        if '{orders}' not in texts['selection']:raise BusinessError('В выборе заказа нужны {orders}')
        with engine.lock,db.tx() as c:
            for key,value in values.items():db.set('svc_'+service+'_'+key,value,c)
            for key,value in texts.items():db.set('tpl_'+service+'_'+key,value,c)
            db.audit(actor(request),'service.settings',service,conn=c)
        return redirect('/services/'+service,'Настройки направления сохранены; правила второго направления не изменены.')

    @app.post('/services/{service}/control')
    async def service_control(request:Request,service:str):
        owner(request);service_id(service);f=await request.form()
        with engine.lock:
            if f.get('action')=='pause':engine.set_direction_pause(service,'Пауза владельцем')
            elif f.get('action')=='resume':
                if db.one("SELECT id FROM orders WHERE mode=? AND service=? AND supplier='fazer' AND state IN('unknown','partial')",(engine.dataset(),service)):
                    raise BusinessError('Сначала сверьте неизвестные/частичные выдачи. Возобновление не должно скрывать проблему.')
                db.set('svc_'+service+'_paused',False);db.set('svc_'+service+'_pause_reason','')
            else:raise BusinessError('Неизвестное действие')
            db.audit(actor(request),'service.control',service,str(f.get('action')))
        return redirect('/services/'+service,'Состояние направления изменено. Общая пауза остаётся отдельной.')

    @app.post('/catalog/nominal')
    async def nominal(request:Request):
        owner(request);f=await request.form()
        if f.get('ack')!='НОМИНАЛ ПРОВЕРЕН':raise BusinessError('Подтвердите НОМИНАЛ ПРОВЕРЕН после сверки товара в кабинете')
        engine.confirm_sku_units(int(f.get('id')),int(f.get('units')),actor(request))
        return redirect('/catalog','Номинал сохранён. Связанные товары требуют повторной настройки и проверки.')

    @app.post('/fazer/parts/{pid}')
    async def part_action(request:Request,pid:int):
        owner(request);f=await request.form()
        part=db.one('SELECT p.*,b.order_id FROM fazer_parts p JOIN batches b ON b.id=p.batch_id WHERE p.id=?',(pid,))
        if not part:raise BusinessError('Часть заказа не найдена')
        if f.get('ack')!='СВЕРЕНО В КАБИНЕТЕ':raise BusinessError('Введите СВЕРЕНО В КАБИНЕТЕ после фактической проверки')
        if f.get('action')=='bind':
            tid=worker.enqueue('fazer_bind',{'id':pid,'code':str(f.get('code','')),'proof':str(f.get('proof','')),'actor':actor(request)})
            return redirect('/connections','Сверка #'+str(tid)+' добавлена. Новое пополнение не отправляется.')
        if f.get('action')=='close':engine.close_unsent_fazer(pid,str(f.get('proof','')),actor(request))
        elif f.get('action')=='cost':
            engine.verify_fazer_cost(pid,to_micros(f.get('amount')),str(f.get('proof','')),actor(request))
        else:raise BusinessError('Неизвестное действие')
        return redirect('/orders/'+part['order_id'],'Результат сверки записан. Покупка не выполнялась.')

    @app.get('/migration')
    async def migration(request:Request):
        owner(request)
        folder=db.data_dir/'transfers'
        files=[{'name':p.name,'size':p.stat().st_size,'created':p.stat().st_mtime} for p in sorted(folder.glob('*.zip'),reverse=True)] if folder.exists() else []
        return page(request,'migration.html',frozen=db.setting('migration_frozen',False),files=files,
                    public_url=config.public_url,source=db.setting('migration_source',''),recovery=db.setting('recovery_required',False))

    @app.post('/migration/export')
    async def migrate_export(request:Request):
        owner(request);f=await request.form()
        if f.get('ack')!='ЗАМОРОЗИТЬ ДЛЯ ПЕРЕНОСА':raise BusinessError('Введите ЗАМОРОЗИТЬ ДЛЯ ПЕРЕНОСА')
        path=await run_in_threadpool(create_export,engine,actor(request))
        return redirect('/migration','Создан '+path.name+'. Скачайте архив и остановите старый сервер.')

    @app.get('/migration/download/{name}')
    async def migrate_download(request:Request,name:str):
        owner(request);path=transfer_path(db,name)
        return FileResponse(path,filename=path.name,media_type='application/zip')

    @app.post('/migration/resume')
    async def migrate_resume(request:Request):
        owner(request);f=await request.form()
        if f.get('ack')!='РАБОТАЕТ ТОЛЬКО ОДНА КОПИЯ':raise BusinessError('Подтвердите РАБОТАЕТ ТОЛЬКО ОДНА КОПИЯ после остановки старого сервера (либо отмены переноса)')
        with engine.lock:
            db.set('migration_frozen',False);db.set('mode','observe');db.set('live_armed',False);db.set('paused',True)
            db.set('pause_reason','Миграция: проверьте оплаты и историю перед включением live')
            worker.fp_baselined.clear()
            db.audit(actor(request),'migration.unfreeze',detail='Владелец подтвердил единственный активный экземпляр; observe')
        return redirect('/','Заморозка снята. Включено наблюдение, закупки остаются выключенными.')

    @app.post('/webhooks/fazer')
    async def fazer_webhook(request:Request):
        raw=await request.body()
        if len(raw)>1048576:return PlainTextResponse('Too large',413)
        if not webhook_signature(raw,request.headers.get('X-Webhook-Signature',''),vault.get('fazer_webhook_secret')):
            return PlainTextResponse('Invalid signature',401)
        try:
            data=json.loads(raw);eid=data['event_id']
            if not isinstance(eid,str) or not 1<=len(eid)<=200 or not isinstance(data.get('data'),dict):raise ValueError()
            code=order_id(data['data']['order_id'])
        except (ValueError,KeyError,TypeError,ProviderError):return PlainTextResponse('Invalid event',400)
        # At-least-once webhook delivery only wakes authenticated GET polling.
        # It cannot bind a lost response or independently declare fulfillment.
        with db.tx() as c:
            key='fazer:'+eid
            if c.execute('SELECT 1 FROM webhooks WHERE event_id=?',(key,)).fetchone():return JSONResponse({'ok':True,'duplicate':True})
            c.execute('INSERT INTO webhooks VALUES(?,?,?)',(key,dumps(data),time.time()))
            c.execute("UPDATE batches SET next_check=0 WHERE id IN(SELECT batch_id FROM fazer_parts WHERE mode='live' AND provider_id=?)",(code,))
        return JSONResponse({'ok':True})
