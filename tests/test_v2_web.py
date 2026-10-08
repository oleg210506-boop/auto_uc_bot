import hashlib
import hmac
import json
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from autoucbot.worker import Worker
from test_web_security import login,csrf
from test_fazer_orders import ready_fazer,finish,row,message

@pytest.fixture
def client(app):
    with TestClient(app) as c:
        assert login(c).status_code==200
        yield c

def form(response):
    soup=BeautifulSoup(response.text,'html.parser');data={}
    for x in soup.select('input[name],textarea[name],select[name]'):
        if x.name=='textarea':v=x.get_text()
        elif x.name=='select':
            selected=x.select_one('option[selected]') or x.select_one('option');v=selected.get('value',selected.get_text())
        elif x.get('type')=='checkbox':
            if not x.has_attr('checked'):continue
            v=x.get('value','on')
        else:v=x.get('value','')
        data[x['name']]=v
    return data

@pytest.mark.parametrize('path',['/services/uc','/services/stars','/migration','/connections','/catalog','/settings','/stats','/customers','/orders'])
def test_new_pages_render_with_both_directions(client,app,path):
    e=app.state.engine;finish(e,ready_fazer(e,'uc',3,60));finish(e,ready_fazer(e))
    r=client.get(path);assert r.status_code==200 and '2.0.0-rc1' in r.text


def test_service_settings_do_not_modify_other_service(client,app):
    e=app.state.engine;before=e.service_rule('uc','confirm_uid')
    values=form(client.get('/services/stars'));values.pop('confirm_uid',None);values['reminder_seconds']='125';values['tpl_completed']='{amount} Stars для {recipient}'
    r=client.post('/services/stars',data=values);assert r.status_code==200
    assert e.service_rule('stars','confirm_uid') is False
    assert e.service_rule('stars','reminder_seconds')==125
    assert e.service_rule('uc','confirm_uid')==before
    assert e.db.setting('tpl_stars_completed')=='{amount} Stars для {recipient}'

@pytest.mark.parametrize('change',[{'min_units':'49'},{'max_units':'10001'},{'min_units':'200','max_units':'50'},{'tpl_confirm_uid':'Без кода'},{'tpl_selection':'Без выбора'},{'alert_chat_ids':'https://evil.test'}])
def test_invalid_service_settings_rejected_atomically(client,app,change):
    previous=app.state.db.setting('svc_stars_enabled')
    values=form(client.get('/services/stars'));values.update(change)
    assert client.post('/services/stars',data=values).status_code==400
    assert app.state.db.setting('svc_stars_enabled')==previous


def test_fazer_secret_not_displayed(client,app):
    token=csrf(client.get('/connections'));key='fz_test-value-private'
    r=client.post('/connections/secrets',data={'csrf':token,'fazer_key':key})
    assert r.status_code==200 and key not in r.text and app.state.vault.get('fazer_key')==key


def test_webhook_verified_but_never_trusts_success(client,app):
    e=app.state.engine;oid=ready_fazer(e);e.db.set('demo_scenario','pending');e.prepare(oid)
    part=e.db.one('SELECT * FROM fazer_parts');e.vault.set('fazer_webhook_secret','my-signature-secret')
    body=json.dumps({'event_id':'event-one','event':'order.completed','data':{'order_id':part['provider_id'],'status':'completed'}}).encode()
    sig='sha256='+hmac.new(b'my-signature-secret',body,hashlib.sha256).hexdigest()
    r=client.post('/webhooks/fazer',content=body,headers={'X-Webhook-Signature':sig});assert r.status_code==200
    assert row(e,oid)['state']=='processing' and row(e,oid)['delivered']==0
    assert client.post('/webhooks/fazer',content=body,headers={'X-Webhook-Signature':sig}).json()['duplicate']
    assert client.post('/webhooks/fazer',content=body,headers={'X-Webhook-Signature':sig+'x'}).status_code==401
    assert e.db.one('SELECT COUNT(*) n FROM webhooks')['n']==1


def test_export_freezes_writes_requires_explicit_single_instance_ack(client,app):
    token=csrf(client.get('/migration'))
    assert client.post('/migration/export',data={'csrf':token,'ack':'wrong'}).status_code==400
    r=client.post('/migration/export',data={'csrf':token,'ack':'ЗАМОРОЗИТЬ ДЛЯ ПЕРЕНОСА'});assert r.status_code==200
    assert app.state.db.setting('migration_frozen')
    assert client.post('/control',data={'csrf':token,'action':'demo'}).status_code==409
    path=list((app.state.db.data_dir/'transfers').glob('*.zip'))[0]
    assert client.get('/migration/download/'+path.name).content==path.read_bytes()
    assert client.post('/migration/resume',data={'csrf':token,'ack':'wrong'}).status_code==400
    assert client.post('/migration/resume',data={'csrf':token,'ack':'РАБОТАЕТ ТОЛЬКО ОДНА КОПИЯ'}).status_code==200
    assert not app.state.db.setting('migration_frozen') and app.state.db.setting('mode')=='observe'
    assert app.state.db.setting('paused') and not app.state.db.setting('live_armed')


def test_operator_cannot_export_or_change_direction(client,app):
    token=csrf(client.get('/team'));client.post('/team',data={'csrf':token,'action':'create','username':'operator','password':'Operator-password123','role':'operator'})
    with TestClient(app) as c:
        login(c,'operator','Operator-password123')
        assert c.get('/services/stars').status_code==403 and c.get('/migration').status_code==403
        assert c.post('/migration/export',data={'csrf':csrf(c.get('/')),'ack':'ЗАМОРОЗИТЬ ДЛЯ ПЕРЕНОСА'}).status_code==403


def test_background_catalog_does_not_erase_new_demo_skus(e):
    oid=ready_fazer(e);Worker(e).catalog();assert finish(e,oid)['state']=='completed'


def test_uc_composite_demo_quantity(e):
    oid=ready_fazer(e,'uc',3,180);assert finish(e,oid)['delivered']==540
    assert e.db.one('SELECT COUNT(*) n FROM fazer_parts')['n']==9


def test_small_deposit_does_not_resume_unfunded_whole_order(e):
    oid=ready_fazer(e);e.db.set('demo-fazer-wallet',0);e.prepare(oid)
    e.db.set('demo-fazer-wallet',1000);e.read_fazer_wallet('demo')
    assert row(e,oid)['state']=='waiting_balance' and e.db.setting('paused')


def test_stars_selection_text_is_separate(e):
    a=e.seed_fazer_demo('stars');b=e.seed_fazer_demo('stars')
    e.db.set('tpl_stars_selection','Выбор Stars: {orders}')
    message(e,a,'@buyer_name')
    out=e.db.one('SELECT text FROM outbox ORDER BY id DESC LIMIT 1')['text']
    assert out.startswith('Выбор Stars:') and a in out and b in out
