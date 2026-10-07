import concurrent.futures
import json
import time
import pytest
from autoucbot.utils import BusinessError,cents,validate_uid,validate_template,positive_int,day_start,csv_safe
from autoucbot.db import DB
from autoucbot.security import Vault
from autoucbot.engine import Engine

@pytest.mark.parametrize('quantity',[1,2,3,10,25,100])
def test_quantity(e,quantity):
    oid=e.seed_demo(quantity);o=e.db.one('SELECT * FROM orders WHERE id=?',(oid,))
    assert o['uc']==quantity*60 and o['sku_units']==quantity

@pytest.mark.parametrize('quantity',[1,3,7])
def test_composite_180(e,quantity):
    e.sync_catalog();pid=e.save_product({'name':'180 UC','marker':'[AUC:TEST180]','fp_lot_id':180,'fp_subcategory':1,'fp_category':1,'sku_id':60,'sku_uc':60,'multiplier':3,'uid_field':'gameUserId','sale_price':360,'enabled':True})
    e.verify_product(pid)
    d={'id':'C180','buyer_id':2,'buyer':'demo','chat_id':'users-1-2','quantity':quantity,'status':'paid','revenue':36000*quantity,'currency':'RUB','subcategory':1,'section_type':'lot','description':'[AUC:TEST180]'}
    e.import_order(d)
    o=e.db.one('SELECT * FROM orders WHERE id=?',('C180',));assert o['uc']==180*quantity and o['sku_units']==3*quantity

def test_happy_path(e,ready):
    e.prepare(ready);e.poll_batch(1)
    o=e.db.one('SELECT * FROM orders WHERE id=?',(ready,))
    assert (o['state'],o['delivered'])==('completed',180)
    assert e.db.one('SELECT COUNT(*) n FROM batches')['n']==1
    assert e.balance()==10000000-18000
    assert e.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE '%:completed:%'")['n']==1

def test_repeated_prepare_and_poll_no_duplicate(e,ready):
    for _ in range(10):e.prepare(ready)
    for _ in range(10):e.poll_batch(1)
    assert e.db.one('SELECT COUNT(*) n FROM batches')['n']==1
    assert e.db.one("SELECT COUNT(*) n FROM ledger WHERE ref LIKE 'purchase:%'")['n']==1

def test_concurrent_prepare(e,ready):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda _:e.prepare(ready),range(20)))
    assert e.db.one('SELECT COUNT(*) n FROM batches')['n']==1

@pytest.mark.parametrize('scenario,expected,uc',[('success','completed',180),('split','completed',180),('partial','partial',120),('failed','failed',0),('pending','processing',0)])
def test_provider_outcomes(e,ready,scenario,expected,uc):
    e.db.set('demo_scenario',scenario);e.prepare(ready);e.poll_batch(1)
    o=e.db.one('SELECT * FROM orders WHERE id=?',(ready,));assert o['state']==expected and o['delivered']==uc
    if scenario=='partial':assert e.balance()==10000000-12000
    if scenario=='failed':assert e.balance()==10000000

def test_unknown_after_create_recovered_without_post(e,ready):
    e.db.set('demo_scenario','unknown_after');e.prepare(ready)
    assert e.db.one('SELECT state FROM orders')['state']=='unknown'
    e.recover_batch(1)
    assert e.db.one('SELECT state FROM orders')['state']=='completed'
    assert e.db.one('SELECT attempts FROM batches')['attempts']==1

def test_unknown_before_create_no_blind_retry(e,ready):
    e.db.set('demo_scenario','unknown_before');e.prepare(ready)
    e.recover_batch(1);e.db.set('paused',False);e.send_batch(1)
    assert e.db.one('SELECT attempts FROM batches')['attempts']==1
    assert e.db.one('SELECT state FROM orders')['state']=='unknown'

def test_idempotency_window(e,ready):
    e.db.set('demo_scenario','unknown_before');e.prepare(ready)
    e.db.set('paused',False);e.db.execute('UPDATE batches SET first_sent=?',(time.time()-24*3600,))
    e.send_batch(1,recovery=True)
    assert e.db.one('SELECT attempts FROM batches')['attempts']==1

def test_manual_insufficient_balance(e,ready):
    e.set_balance(1);e.prepare(ready)
    assert e.db.one('SELECT state FROM orders')['state']=='waiting_balance'
    assert not e.db.one('SELECT id FROM batches')
    assert e.db.setting('paused')
    e.set_balance(1000);e.prepare(ready);assert e.db.one('SELECT state FROM batches')['state']=='accepted'

def test_api_insufficient_balance(e,ready):
    e.db.set('demo_scenario','balance');e.prepare(ready)
    assert e.db.one('SELECT state FROM batches')['state']=='balance'
    e.db.set('demo_scenario','success');e.set_balance(1000);e.send_batch(1)
    assert e.db.one('SELECT COUNT(*) n FROM batches')['n']==1
    assert e.db.one('SELECT state FROM batches')['state']=='accepted'

def test_cannot_rebaseline_inflight(e,ready):
    e.prepare(ready)
    with pytest.raises(BusinessError):e.set_balance(10000)

def test_unknown_balance_is_reserved(e,ready):
    e.db.set('demo_scenario','unknown_before');e.prepare(ready)
    assert e.balance()<10000000

def test_paused_no_purchase(e,ready):
    e.pause('manual');e.prepare(ready);assert not e.db.one('SELECT id FROM batches')

def test_foreign_product_ignored(e):
    oid=e.seed_demo();d=e.db.setting('demo-fp:'+oid);d['id']='OTHER';d['description']='Some PUBG account 660'
    assert e.import_order(d) is None
    assert e.db.one('SELECT COUNT(*) n FROM orders')['n']==1

@pytest.mark.parametrize('field,value',[('status','unpaid'),('section_type','chip'),('subcategory',555)])
def test_unpaid_and_wrong_section_ignored(e,field,value):
    oid=e.seed_demo();d=e.db.setting('demo-fp:'+oid);d['id']='OTHER';d[field]=value
    assert e.import_order(d) is None

def test_baseline_order_manual(e):
    oid=e.seed_demo();d=e.db.setting('demo-fp:'+oid);d['id']='OLD'
    e.import_order(d,baseline=True);assert e.db.one("SELECT state FROM orders WHERE id='OLD'")['state']=='manual'

def test_product_snapshot_immutable(e,ready):
    e.db.execute('UPDATE products SET multiplier=20')
    e.prepare(ready)
    assert e.db.one('SELECT units FROM batches')['units']==3

def test_product_archiving_preserves_history(e,ready):
    e.db.execute('UPDATE products SET archived=1,enabled=0')
    e.prepare(ready);assert not e.db.one('SELECT id FROM batches')
    assert e.db.one('SELECT id FROM orders')['id']==ready

def test_uid_confirmation(e):
    oid=e.seed_demo();e.db.set('paused',False)
    e.input_message({'id':'1','author':2,'chat_id':'users-1-2','text':'5123456789'})
    e.prepare(oid);assert not e.db.one('SELECT id FROM batches')

def test_uid_confirmation_optional(e):
    oid=e.seed_demo();e.db.set('paused',False);e.db.set('confirm_uid',False)
    e.input_message({'id':'1','author':2,'chat_id':'users-1-2','text':'5123456789'})
    e.prepare(oid);assert e.db.one('SELECT state FROM batches')['state']=='accepted'

def test_wrong_confirmation_cannot_buy(e):
    oid=e.seed_demo();e.db.set('paused',False)
    for i,text in enumerate(['5123456789','Подтверждаю','да','ПОДТВЕРЖДАЮ WRONG']):e.input_message({'id':str(i+1),'author':2,'chat_id':'users-1-2','text':text})
    e.prepare(oid);assert not e.db.one('SELECT id FROM batches')

def test_uid_change_invalidates_nonce(e):
    oid=e.seed_demo();e.input_message({'id':'1','author':2,'chat_id':'users-1-2','text':'5123456789'})
    old=e.db.one('SELECT confirm_code FROM orders')['confirm_code']
    e.input_message({'id':'2','author':2,'chat_id':'users-1-2','text':'5987654321'})
    e.input_message({'id':'3','author':2,'chat_id':'users-1-2','text':'ПОДТВЕРЖДАЮ '+old})
    o=e.db.one('SELECT * FROM orders');assert not o['confirmed'] and o['uid']=='5987654321'

@pytest.mark.parametrize('author',[0,1,3,999])
def test_uid_from_wrong_author_ignored(e,author):
    e.seed_demo();e.input_message({'id':'1','author':author,'chat_id':'users-1-2','text':'5123456789'})
    assert e.db.one('SELECT uid FROM orders')['uid'] is None

def test_duplicate_message_ignored(e):
    e.seed_demo();m={'id':'1','author':2,'chat_id':'users-1-2','text':'5123456789'}
    e.input_message(m);first=e.db.one('SELECT confirm_code FROM orders')['confirm_code'];e.input_message(m)
    assert e.db.one('SELECT confirm_code FROM orders')['confirm_code']==first

def test_multiple_orders_require_selection(e):
    a=e.seed_demo();b=e.seed_demo()
    e.input_message({'id':'1','author':2,'chat_id':'users-1-2','text':'5123456789'})
    assert not e.db.one('SELECT uid FROM orders WHERE uid IS NOT NULL')
    e.input_message({'id':'2','author':2,'chat_id':'users-1-2','text':'#'+b+' 5123456789'})
    assert e.db.one('SELECT uid FROM orders WHERE id=?',(b,))['uid']=='5123456789'
    assert e.db.one('SELECT uid FROM orders WHERE id=?',(a,))['uid'] is None

def test_help_handover_blocks_purchase(e,ready):
    e.input_message({'id':'help','author':2,'chat_id':'users-1-2','text':'мне нужен оператор'})
    e.prepare(ready);assert not e.db.one('SELECT id FROM batches')
    assert e.db.one('SELECT manual FROM chat_controls')['manual']==1

def test_cancel_before_send(e,ready):
    d=e.db.setting('demo-fp:'+ready);d['status']='refunded';e.db.set('demo-fp:'+ready,d)
    e.prepare(ready);assert not e.db.one('SELECT id FROM batches')
    assert e.db.one('SELECT state FROM orders')['state']=='cancelled'

def test_cancel_inflight_still_checks_provider(e,ready):
    e.prepare(ready);d=e.db.setting('demo-fp:'+ready);d['status']='refunded';e.refresh_status(d)
    e.poll_batch(1);assert e.db.one('SELECT delivered FROM orders')['delivered']==180
    assert e.db.one("SELECT id FROM alerts WHERE kind='manual'")

@pytest.mark.parametrize('setting,value',[('max_order_rub',1),('daily_limit_rub',1)])
def test_financial_limits(e,ready,setting,value):
    e.db.set(setting,value);e.prepare(ready);assert not e.db.one('SELECT id FROM batches')
    assert e.db.one('SELECT state FROM orders')['state']=='manual'

def test_insufficient_margin(e,ready):
    snap=json.loads(e.db.one('SELECT snapshot FROM orders')['snapshot']);snap['min_profit']=999999
    e.db.execute('UPDATE orders SET snapshot=?',(json.dumps(snap),));e.prepare(ready)
    assert not e.db.one('SELECT id FROM batches')

def test_no_second_adoption_after_issue(e,ready):
    e.prepare(ready);e.db.execute("UPDATE orders SET state='manual'")
    with pytest.raises(BusinessError):e.adopt(ready,'owner')

def test_backup_integrity(e,ready):
    e.prepare(ready);e.poll_batch(1);path=e.db.backup()
    import sqlite3
    c=sqlite3.connect(path)
    try:assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert c.execute('SELECT delivered FROM orders').fetchone()[0]==180
    finally:c.close()
    assert path.with_suffix('.sha256').exists()

@pytest.mark.parametrize('value',['0','-1','1.2','abc','１２３',True,None])
def test_positive_int_invalid(value):
    with pytest.raises(BusinessError):positive_int(value)
@pytest.mark.parametrize('value',['123456','5123456789','123456789012345'])
def test_uid_valid(value):assert validate_uid(value)==value
@pytest.mark.parametrize('value',['12345','1234567890123456','+123456','123 456','１２３４５６','١٢٣٤٥٦','123456abc'])
def test_uid_invalid(value):
    with pytest.raises(BusinessError):validate_uid(value)
@pytest.mark.parametrize('value',['nan','inf','1e100',None,'abc'])
def test_money_invalid(value):
    with pytest.raises(BusinessError):cents(value)
@pytest.mark.parametrize('raw,expected',[('1.005',101),('1,23',123),('0',0),('12.99',1299)])
def test_money_decimal(raw,expected):assert cents(raw)==expected
@pytest.mark.parametrize('template',['{uid.__class__}','{buyer[0]}','{uc:9999}','{uid!r}','{unknown}','{'])
def test_unsafe_template(template):
    with pytest.raises(BusinessError):validate_template(template)
@pytest.mark.parametrize('text',['=HYPERLINK("bad")','+12','-1','@SUM()','\tformula','  =1'])
def test_csv_formula_escape(text):assert csv_safe(text).startswith("'")
