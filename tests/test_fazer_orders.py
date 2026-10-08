import asyncio
import concurrent.futures
import json
import time
import pytest
from autoucbot.engine import Engine
from autoucbot.worker import Worker
from autoucbot.db import DB
from autoucbot.security import Vault
from autoucbot.adapters.gamecore import ProviderError
from autoucbot.dispatcher import AsyncOrderDispatcher
from autoucbot.utils import BusinessError

def row(e,oid):return e.db.one('SELECT * FROM orders WHERE id=?',(oid,))
def message(e,oid,text,mid=None,prefix=False):
    o=row(e,oid);e.input_message({'id':str(mid or time.time_ns()),'chat_id':o['chat_id'],'author':o['buyer_id'],'text':('#'+oid+' ' if prefix else '')+text})
def ready_fazer(e,service='stars',quantity=1,denomination=50):
    oid=e.seed_fazer_demo(service,quantity,denomination)
    e.db.set('paused',False);e.db.set('svc_'+service+'_max_order_rub',1000000);e.db.set('svc_'+service+'_daily_limit_rub',10000000)
    e.db.set('demo-fazer-wallet',1000000000)
    message(e,oid,'@buyer_name' if service=='stars' else '5123456789',prefix=True)
    o=row(e,oid);message(e,oid,'ПОДТВЕРЖДАЮ '+o['confirm_code'])
    assert row(e,oid)['confirmed']==1
    return oid

def finish(e,oid):
    e.prepare(oid)
    for _ in range(100):
        b=e.db.one('SELECT * FROM batches WHERE order_id=?',(oid,))
        if not b or b['state'] in ('completed','failed','partial','rejected'):break
        e.poll_batch(b['id']);e.send_batch(b['id'])
    return row(e,oid)

@pytest.mark.parametrize('service,nominal,quantity',[(s,n,q) for s,ns in [('uc',[60]),('stars',[50,100,250])] for n in ns for q in [1,2,3,7,20]])
def test_full_quantities(e,service,nominal,quantity):
    oid=ready_fazer(e,service,quantity,nominal);out=finish(e,oid)
    assert (out['state'],out['delivered'])==('completed',nominal*quantity)
    assert e.db.one('SELECT COUNT(*) n FROM fazer_parts')['n']==(quantity if service=='uc' else 1)
    assert e.db.one('SELECT COUNT(*) n FROM batches')['n']==1

@pytest.mark.parametrize('service',['uc','stars'])
def test_repeated_events_do_not_purchase_twice(e,service):
    oid=ready_fazer(e,service,3,50 if service=='stars' else 60)
    for _ in range(10):e.import_order(e.db.setting('demo-fp:'+oid));e.prepare(oid)
    finish(e,oid)
    count=e.db.setting('demo-fazer-counter')
    for _ in range(10):e.prepare(oid);e.poll_batch(1);e.send_batch(1,recovery=True)
    assert e.db.setting('demo-fazer-counter')==count==(3 if service=='uc' else 1)
    assert e.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE '%:completed:%'")['n']==1

@pytest.mark.parametrize('service',['uc','stars'])
def test_concurrent_same_order(e,service):
    oid=ready_fazer(e,service,1,50 if service=='stars' else 60)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda _:e.prepare(oid),range(12)))
    assert e.db.setting('demo-fazer-counter')==1
    assert row(e,oid)['state']=='completed'

@pytest.mark.parametrize('service',['uc','stars'])
def test_two_identical_consecutive_payments_each_fulfilled(e,service):
    a=ready_fazer(e,service,1,50 if service=='stars' else 60)
    b=ready_fazer(e,service,1,50 if service=='stars' else 60)
    finish(e,a);finish(e,b)
    assert a!=b and row(e,a)['state']==row(e,b)['state']=='completed'
    assert row(e,a)['uid']==row(e,b)['uid']
    parts=e.db.rows('SELECT * FROM fazer_parts')
    assert len(parts)==2 and len({p['provider_id'] for p in parts})==2 and len({p['idem_key'] for p in parts})==2


def test_mixed_orders_require_selection_and_do_not_mix_recipient(e):
    a=e.seed_fazer_demo('uc',1,60);b=e.seed_fazer_demo('stars',1,50)
    message(e,a,'@buyer_name');assert row(e,a)['uid'] is None and row(e,b)['uid'] is None
    message(e,b,'@buyer_name',prefix=True);assert row(e,b)['uid']=='@buyer_name' and row(e,a)['uid'] is None
    message(e,a,'5123456789',prefix=True);assert row(e,a)['uid']=='5123456789'

@pytest.mark.parametrize('scenario',['unknown_before','unknown_after'])
@pytest.mark.parametrize('service',['uc','stars'])
def test_unknown_never_reposts_even_after_week(e,scenario,service):
    oid=ready_fazer(e,service,1,50 if service=='stars' else 60)
    e.db.set('demo_scenario',scenario);e.prepare(oid)
    assert row(e,oid)['state']=='unknown'
    e.db.execute('UPDATE fazer_parts SET first_sent=?',(time.time()-8*86400,));e.db.set('paused',False);e.db.set('svc_'+service+'_paused',False)
    for _ in range(5):e.prepare(oid);e.send_batch(1,True);e.recover_batch(1)
    p=e.db.one('SELECT * FROM fazer_parts');assert p['attempts']==1 and p['state']=='unknown'
    assert e.db.setting('demo-fazer-counter',0)==(1 if scenario=='unknown_after' else 0)


def test_unknown_after_spend_manual_exact_binding_no_new_purchase(e):
    oid=ready_fazer(e);e.db.set('demo_scenario','unknown_after');e.prepare(oid)
    p=e.db.one('SELECT * FROM fazer_parts');code=e.db.setting('demo-fazer-key:'+p['idem_key'])
    e.bind_fazer_order(p['id'],code,'Проверено в кабинете: покупатель, сумма, время и номер совпадают','owner')
    assert row(e,oid)['state']=='completed' and e.db.setting('demo-fazer-counter')==1


def test_wrong_binding_recipient_refused(e):
    oid=ready_fazer(e);e.db.set('demo_scenario','unknown_after');e.prepare(oid)
    p=e.db.one('SELECT * FROM fazer_parts');code=e.db.setting('demo-fazer-key:'+p['idem_key'])
    d=e.db.setting('demo-fazer-order:'+code);d['telegram_username']='@wrong_user';e.db.set('demo-fazer-order:'+code,d)
    with pytest.raises(BusinessError):e.bind_fazer_order(p['id'],code,'Проверено вручную, подробное подтверждение','owner')
    assert row(e,oid)['delivered']==0 and e.db.one('SELECT provider_id FROM fazer_parts')['provider_id'] is None


def test_same_provider_id_cannot_pay_two_orders(e,monkeypatch):
    a=ready_fazer(e);finish(e,a);first=e.db.one('SELECT * FROM fazer_parts');response=json.loads(first['response'])
    b=ready_fazer(e);monkeypatch.setattr(e.demo_fazer,'buy_item',lambda *args:response)
    e.prepare(b);assert row(e,b)['state']=='unknown' and row(e,b)['delivered']==0


def test_pending_over_a_minute_not_repeated(e):
    oid=ready_fazer(e);e.db.set('demo_scenario','pending');e.prepare(oid)
    e.db.execute('UPDATE batches SET created=?',(time.time()-3600,))
    for _ in range(5):e.poll_batch(1);e.send_batch(1)
    assert e.db.setting('demo-fazer-counter')==1 and row(e,oid)['state']=='processing'


def test_uc_partial_does_not_repeat_or_buy_remaining(e):
    oid=ready_fazer(e,'uc',3,60);e.db.set('demo_scenario','partial');out=finish(e,oid)
    assert (out['state'],out['delivered'])==('partial',60)
    assert e.db.setting('demo-fazer-counter')==2
    assert [p['state'] for p in e.db.rows('SELECT state FROM fazer_parts ORDER BY ordinal')]==['completed','failed','cancelled']

@pytest.mark.parametrize('n',[1,49,10001])
def test_min_max_stars_enforced_before_purchase(e,n):
    oid=ready_fazer(e);e.db.execute('UPDATE orders SET uc=? WHERE id=?',(n,oid));e.prepare(oid)
    assert not e.db.one('SELECT id FROM fazer_parts') and not e.db.setting('demo-fazer-counter',0)
    assert row(e,oid)['state']=='manual'


def test_api_stars_limit_tightens_local_limit(e,monkeypatch):
    oid=ready_fazer(e,'stars',3,50);monkeypatch.setattr(e.demo_fazer,'stars_quote',lambda:{'price_per_star':'0.015','min_amount':50,'max_amount':100})
    e.prepare(oid);assert not e.db.one('SELECT id FROM fazer_parts')


def test_service_confirmation_optional_independent(e):
    a=e.seed_fazer_demo('uc',1,60);b=e.seed_fazer_demo('stars',1,50)
    e.db.set('svc_stars_confirm_uid',False)
    message(e,b,'@buyer_name',prefix=True);message(e,a,'5123456789',prefix=True)
    assert row(e,b)['state']=='ready' and row(e,a)['state']=='awaiting_confirmation'


def test_changed_username_invalidates_confirmation(e):
    oid=e.seed_fazer_demo('stars',1,50);message(e,oid,'@buyer_name');old=row(e,oid)['confirm_code']
    message(e,oid,'@other_name');message(e,oid,'ПОДТВЕРЖДАЮ '+old)
    assert not row(e,oid)['confirmed'] and row(e,oid)['uid']=='@other_name'

@pytest.mark.parametrize('text',['5123456789','@имя','https://t.me/user','@','user name','+79123456789'])
def test_bad_stars_recipient_no_purchase(e,text):
    oid=e.seed_fazer_demo('stars',1,50);e.db.set('paused',False);e.db.set('svc_stars_confirm_uid',False)
    message(e,oid,text);e.prepare(oid)
    assert row(e,oid)['uid'] is None and not e.db.one('SELECT id FROM fazer_parts')


def test_wallet_whole_order_checked_before_first_uc(e):
    oid=ready_fazer(e,'uc',3,60);e.db.set('demo-fazer-wallet',1300000);e.prepare(oid)
    assert row(e,oid)['state']=='waiting_balance' and not e.db.one('SELECT id FROM fazer_parts')


def test_actual_api_balance_error_can_resume_without_duplicate(e):
    oid=ready_fazer(e);e.db.set('demo_scenario','balance');e.prepare(oid)
    assert row(e,oid)['state']=='waiting_balance'
    e.db.set('demo_scenario','success');e.read_fazer_wallet('demo');e.send_batch(1)
    assert row(e,oid)['state']=='completed' and e.db.setting('demo-fazer-counter')==1


def test_shared_balance_reserves_unknown_against_other_service(e):
    a=ready_fazer(e,'stars',1,50);e.db.set('demo_scenario','unknown_before');e.prepare(a)
    b=ready_fazer(e,'uc',1,60);e.db.set('demo-fazer-wallet',1000000);e.db.set('demo_scenario','success');e.prepare(b)
    assert row(e,b)['state']=='waiting_balance' and e.db.setting('demo-fazer-counter',0)==0


def test_one_paused_direction_does_not_starve_other(e):
    a=ready_fazer(e,'stars',1,50);b=ready_fazer(e,'uc',1,60)
    e.db.set('svc_stars_paused',True);w=Worker(e);w.purchases()
    assert row(e,a)['state']=='ready' and row(e,b)['state']=='completed'

@pytest.mark.parametrize('service',['uc','stars'])
def test_disabled_direction(e,service):
    oid=ready_fazer(e,service,1,50 if service=='stars' else 60);e.db.set('svc_'+service+'_enabled',False);e.prepare(oid)
    assert not e.db.one('SELECT id FROM fazer_parts')


def test_refund_before_buy(e):
    oid=ready_fazer(e);d=e.db.setting('demo-fp:'+oid);d['status']='refunded';e.db.set('demo-fp:'+oid,d);e.prepare(oid)
    assert not e.db.one('SELECT id FROM fazer_parts') and row(e,oid)['state']=='cancelled'


def test_refund_between_uc_parts_stops_remaining(e):
    oid=ready_fazer(e,'uc',3,60);e.prepare(oid)
    d=e.db.setting('demo-fp:'+oid);d['status']='refunded';e.db.set('demo-fp:'+oid,d);e.send_batch(1)
    assert e.db.setting('demo-fazer-counter')==1 and row(e,oid)['delivered']==60
    assert row(e,oid)['state']=='partial'

@pytest.mark.parametrize('field,value',[('quantity',2),('revenue',999),('buyer_id',333),('currency','USD'),('chat_id','other')])
def test_modified_payment_never_buys(e,field,value):
    oid=ready_fazer(e);d=e.db.setting('demo-fp:'+oid);d[field]=value;e.db.set('demo-fp:'+oid,d);e.prepare(oid)
    assert not e.db.one('SELECT id FROM fazer_parts') and row(e,oid)['state']=='manual'


def test_unknown_persists_restart(e,config):
    oid=ready_fazer(e);e.db.set('demo_scenario','unknown_after');e.prepare(oid)
    db=DB(config.data_dir);replacement=Engine(db,Vault(db,config.secret),config)
    db.set('paused',False);db.set('svc_stars_paused',False);replacement.prepare(oid);replacement.send_batch(1,True)
    assert db.setting('demo-fazer-counter')==1 and row(replacement,oid)['state']=='unknown'


def test_missing_actual_charge_is_explicit_estimate(e,monkeypatch):
    oid=ready_fazer(e);base=e.demo_fazer.buy_item
    def create(*args):
        d=base(*args);d.pop('total_usd');return d
    monkeypatch.setattr(e.demo_fazer,'buy_item',create);e.prepare(oid)
    p=e.db.one('SELECT * FROM fazer_parts');assert p['cost_source']=='quote' and p['charged_micros'] is None
    e.verify_fazer_cost(p['id'],750000,'Проверен итоговый расход по операции в кабинете','owner')
    assert e.db.one('SELECT cost_source FROM fazer_parts')['cost_source']=='owner_verified'


def test_after_request_body_tamper_cannot_resend(e):
    oid=ready_fazer(e);e.db.set('demo_scenario','balance');e.prepare(oid);e.db.set('demo_scenario','success');e.read_fazer_wallet('demo')
    e.db.execute("UPDATE fazer_parts SET body=?",(json.dumps({'telegram_username':'@attacker','quantity':50}),));e.send_batch(1)
    assert not e.db.setting('demo-fazer-counter',0)


def test_async_dispatcher_duplicate_calls(e):
    oid=ready_fazer(e)
    async def run():
        d=AsyncOrderDispatcher(e);results=await asyncio.gather(*(d.buy_item(oid) for _ in range(8)))
        assert all(r['state']=='completed' for r in results)
        assert (await d.check_order_status(oid))['delivered']==50
        assert await d.get_balance()>0
    asyncio.run(run());assert e.db.setting('demo-fazer-counter')==1
