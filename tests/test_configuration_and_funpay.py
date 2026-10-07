import json,time
from dataclasses import replace
from types import SimpleNamespace
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from autoucbot.adapters.funpay import FunPay,FunPayError
from autoucbot.config import FIELDS,TEMPLATES
from autoucbot.worker import Worker
from autoucbot.db import dumps


def csrf(r):return BeautifulSoup(r.text,'html.parser').find('input',{'name':'csrf'})['value']
@pytest.fixture
def client(app):
    with TestClient(app) as c:
        r=c.get('/login');r=c.post('/login',data={'csrf':csrf(r),'username':'owner','password':'Test-password-12!'})
        assert r.status_code==200;yield c

def setting_form(c,e):
    f={'csrf':csrf(c.get('/settings'))}
    for k,s in FIELDS.items():
        v=e.db.setting(k)
        if s[0]=='bool':
            if v:f[k]='on'
        else:f[k]=str(v)
    for k in TEMPLATES:f['tpl_'+k]=e.db.setting('tpl_'+k)
    return f

def test_all_settings_roundtrip(client,e):
    f=setting_form(client,e);f.pop('confirm_uid');f['alert_chat_ids']='123,-100987';f['timezone']='Europe/Kyiv'
    r=client.post('/settings',data=f);assert r.status_code==200
    assert e.db.setting('confirm_uid') is False and e.db.setting('alert_chat_ids')=='123,-100987'

@pytest.mark.parametrize('key,value',[('timezone','unknown/zone'),('alert_chat_ids','notnumber'),('balance_path','https://evil.test'),('stats_from','invalid'),('max_quantity','0'),('price_buffer_percent','nan'),('balance_mode','unsupported'),('tpl_confirm_uid','Нет ID и кода'),('tpl_selection','Ничего'),('tpl_completed','{buyer.__class__}')])
def test_invalid_settings_atomic(client,e,key,value):
    f=setting_form(client,e);f['max_quantity']='99';f[key]=value
    assert client.post('/settings',data=f).status_code==400
    assert e.db.setting('max_quantity')==100

def test_stats_partial_and_manual_cost(client,e,ready):
    e.db.set('demo_scenario','partial');e.prepare(ready);e.poll_batch(1)
    r=client.get('/stats');s=r.context['summary'];assert s['cost']==12000 and s['gross']==0 and s['profit']==-12000
    e.manual_resolution(ready,'manual_complete',60,0,'Вручную выдан остаток чек123','owner')
    s=client.get('/stats').context['summary'];assert s['cost']==18000 and s['gross']==36000 and s['profit']==18000

def test_stats_refund_after_delivery(client,e,ready):
    e.prepare(ready);e.poll_batch(1);e.db.execute("UPDATE orders SET fp_status='refunded'")
    s=client.get('/stats').context['summary'];assert s['gross']==0 and s['refunds']==36000 and s['cost']==18000 and s['profit']==-18000

def test_stats_invalid_dates(client):assert client.get('/stats?from=2026-10-10&to=2026-10-01').status_code==400

def test_partial_refund_counted(client,e,ready):
    e.prepare(ready);e.poll_batch(1);e.manual_resolution(ready,'finance',0,50,'Частичный возврат подтвержден001','owner')
    s=client.get('/stats').context['summary'];assert s['gross']==31000 and s['refunds']==5000

def test_alert_ack_does_not_deliver(client,e,ready):
    e.alert('manual','Problem','details',ready);a=e.db.one('SELECT id FROM alerts')
    r=client.post('/alerts/'+str(a['id']),data={'csrf':csrf(client.get('/alerts')),'action':'ack'})
    assert r.status_code==200 and e.db.one('SELECT state FROM orders')['state']=='ready'

def test_old_uid_not_reused_for_new_order(e,monkeypatch):
    old=e.seed_demo();d=e.db.setting('demo-fp:'+old);d['id']='NEW_LIVE'
    e.db.execute("UPDATE products SET mode='live'");e.db.set('mode','live')
    oldmsg={'id':'501','author':2,'chat_id':'users-1-2','text':'5123456789'}
    monkeypatch.setattr(e,'fp',lambda mode=None:SimpleNamespace(messages=lambda chats:[oldmsg]))
    e.import_order(d,'live');e.input_message(oldmsg)
    assert e.db.one("SELECT uid FROM orders WHERE id='NEW_LIVE'")['uid'] is None
    e.input_message({**oldmsg,'id':'502'})
    assert e.db.one("SELECT uid FROM orders WHERE id='NEW_LIVE'")['uid']=='5123456789'

class Response:
    def __init__(self,text='',data=None):self.text=text;self.data=data
    def json(self):return self.data
@pytest.fixture
def fp():
    f=FunPay('fake-key','Test-agent');f.user_id=1;f.csrf='csrf';f.connected_at=time.time();return f

def test_funpay_paginated_sales_whitelist(fp,monkeypatch):
    calls=[]
    responses=[Response('<a class="user-link-name">own</a><h1 class="page-header">Мои продажи</h1><a class="tc-item"><span class="tc-order">#AB1</span></a><input name="continue" value="cursor1">'),Response('<a class="tc-item"><span class="tc-order">#AB2</span></a>')]
    def req(method,path,**kw):calls.append((method,path,kw));return responses.pop(0)
    monkeypatch.setattr(fp,'request',req)
    assert fp.paid_ids(123)==['AB1','AB2'];assert all(c[2]['params']=={'state':'paid','section':'lot-123'} for c in calls)
    assert calls[1][0]=='POST' and calls[1][2]['data']['continue']=='cursor1'

def test_funpay_repeated_sales_page_stops(fp,monkeypatch):
    r=Response('<a class="user-link-name">own</a><h1 class="page-header">My sales</h1><input name="continue" value="same">');monkeypatch.setattr(fp,'request',lambda *a,**k:r)
    with pytest.raises(FunPayError):fp.paid_ids(1)

def test_funpay_sales_unknown_html(fp,monkeypatch):
    monkeypatch.setattr(fp,'request',lambda *a,**k:Response('<html>Challenge</html>'))
    with pytest.raises(FunPayError):fp.paid_ids(1)

def test_funpay_raise_selected_nodes_and_wait(fp,monkeypatch):
    calls=[]
    def req(*a,**kw):calls.append(kw);return Response(data={'error':'wait','wait':7200})
    monkeypatch.setattr(fp,'request',req)
    assert fp.raise_lots(12,[5,9])==7200 and calls[0]['data']['node_ids[]']==[5,9]

def test_funpay_raise_requires_manual(fp,monkeypatch):
    monkeypatch.setattr(fp,'request',lambda *a,**kw:Response(data={'url':'https://funpay.com/challenge'}))
    with pytest.raises(FunPayError):fp.raise_lots(1,[2])

def test_funpay_no_nodes_no_request(fp,monkeypatch):
    monkeypatch.setattr(fp,'request',lambda *a,**kw:pytest.fail('Should not request'))
    assert fp.raise_lots(1,[])==3600

def test_funpay_change_lot_readback(fp,monkeypatch):
    calls=[]
    lot={'subcategory':2,'text':'[AUC:UC0060]','active':True,'currency':'RUB','price':10000,'fields':{'csrf_token':'old','active':'on','price':'100','other_setting':'unchanged'}}
    responses=[lot,{**lot,'active':False,'price':12000}]
    monkeypatch.setattr(fp,'lot',lambda n:responses.pop(0))
    def req(*a,**kw):calls.append(kw);return Response(data={'error':False})
    monkeypatch.setattr(fp,'request',req)
    fp.change_lot({'fp_lot_id':4,'fp_subcategory':2,'marker':'[AUC:UC0060]'},active=False,price=12000)
    d=calls[0]['data'];assert 'active' not in d and d['price']=='120.00' and d['other_setting']=='unchanged'

def test_funpay_change_lot_wrong_readback(fp,monkeypatch):
    lot={'subcategory':2,'text':'[AUC:UC0060]','active':True,'currency':'RUB','price':10000,'fields':{}}
    monkeypatch.setattr(fp,'lot',lambda n:lot);monkeypatch.setattr(fp,'request',lambda *a,**kw:Response(data={}))
    with pytest.raises(FunPayError):fp.change_lot({'fp_lot_id':4,'fp_subcategory':2,'marker':'[AUC:UC0060]'},active=False)

def test_funpay_false_runner_data_safe(fp,monkeypatch):
    monkeypatch.setattr(fp,'request',lambda *a,**kw:Response(data={'objects':[{'type':'chat_node','id':'users-1-2','data':False},{'type':'orders_counters','tag':'abc'}]}))
    assert fp.messages({'users-1-2':-1})==[] and fp.counter_epoch==1
    fp.messages({'users-1-2':-1});assert fp.counter_epoch==1

def test_funpay_send_requires_ack(fp,monkeypatch):
    monkeypatch.setattr(fp,'runner',lambda *a,**kw:([],{'objects':[]}))
    with pytest.raises(FunPayError):fp.send('users-1-2','Text')

def live_product(e):
    e.seed_demo();e.db.set('mode','live');e.db.set('paused',False);e.db.set('live_armed',True);e.db.execute("UPDATE products SET mode='live'")
    e.config=replace(e.config,enable_live=True)
    return e.db.one('SELECT * FROM products')

def test_worker_raise_and_hide_only_selected(e,monkeypatch):
    p=live_product(e);calls=[];active={p['fp_lot_id']:True}
    def change(product,active=None,price=None):
        calls.append(('change',product['fp_lot_id'],active,price))
        if active is not None:states[product['fp_lot_id']]=active
    states=active
    fake=SimpleNamespace(lot=lambda n:{'active':states[n]},change_lot=change,raise_lots=lambda game,nodes:(calls.append(('raise',game,nodes)) or 7200))
    monkeypatch.setattr(e,'fp',lambda mode=None:fake);e.db.set('auto_raise',True);w=Worker(e)
    w.lots();w.lots();assert [c for c in calls if c[0]=='raise']==[('raise',p['fp_category'],[p['fp_subcategory']])]
    e.pause('test');w.lots();assert not states[p['fp_lot_id']] and e.db.one('SELECT bot_hidden FROM products')['bot_hidden']==1
    e.db.set('paused',False);w.lots();assert states[p['fp_lot_id']] and e.db.one('SELECT bot_hidden FROM products')['bot_hidden']==0

def test_worker_never_reactivates_manually_hidden_lot(e,monkeypatch):
    live_product(e);e.db.set('paused',True);calls=[]
    monkeypatch.setattr(e,'fp',lambda mode=None:SimpleNamespace(lot=lambda n:{'active':False},change_lot=lambda *a,**kw:calls.append(kw)))
    w=Worker(e);w.lots();e.db.set('paused',False);w.lots();assert not calls

def test_worker_price_formula(e,monkeypatch):
    live_product(e);e.db.execute('UPDATE products SET auto_price=1,markup=20,min_profit=1000,last_price=6000');calls=[]
    monkeypatch.setattr(e,'fp',lambda mode=None:SimpleNamespace(change_lot=lambda p,**kw:calls.append(kw)))
    Worker(e).lots();assert calls==[{'price':8200}]
