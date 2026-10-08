import json
import time
import pytest
from autoucbot.worker import Worker
from autoucbot.adapters.gamecore import ProviderError
from autoucbot.utils import BusinessError
from test_fazer_orders import ready_fazer,finish,row,message


def test_second_live_order_does_not_swallow_first_orders_unread_reply(e,monkeypatch):
    seed=e.seed_fazer_demo('stars');data=e.db.setting('demo-fp:'+seed)
    pid=row(e,seed)['product_id'];e.db.execute('DELETE FROM outbox');e.db.execute('DELETE FROM orders');e.db.execute('DELETE FROM chat_controls')
    e.db.execute("UPDATE products SET mode='live' WHERE id=?",(pid,));e.db.set('mode','live')
    history=[{'id':'10','chat_id':data['chat_id'],'author':2,'text':'@old_username'}]
    class FP:
        def messages(self,*a):return list(history)
    monkeypatch.setattr(e,'fp',lambda mode:FP())
    a={**data,'id':'FIRST100'};e.import_order(a,'live');assert row(e,a['id'])['last_input']==10
    reply={'id':'11','chat_id':data['chat_id'],'author':2,'text':'@current_user'};history.append(reply)
    b={**data,'id':'SECOND100'};e.import_order(b,'live');assert row(e,b['id'])['last_input']==11
    assert not e.db.one('SELECT * FROM seen_messages WHERE message_id=?',('11',))
    e.input_message(reply)
    assert row(e,a['id'])['uid']=='@current_user' and row(e,b['id'])['uid'] is None
    code=row(e,a['id'])['confirm_code']
    e.input_message({**reply,'id':'12','text':'ПОДТВЕРЖДАЮ '+code})
    assert row(e,a['id'])['confirmed']==1 and row(e,b['id'])['confirmed']==0
    e.input_message({**reply,'id':'13','text':'#SECOND100 @second_user'})
    assert row(e,b['id'])['uid']=='@second_user'


def test_uid_validation_rejects_missing_player_without_buying(e,monkeypatch):
    oid=e.seed_fazer_demo('uc',1,60);e.db.set('paused',False)
    e.db.set('svc_uc_validate_recipient',True);e.db.set('svc_uc_validation_category','validation-pubg')
    monkeypatch.setattr(e.demo_fazer,'validation_games',lambda:[{'category_id':'validation-pubg','fields':[{'key':'player_id'}]}])
    monkeypatch.setattr(e.demo_fazer,'validate_player',lambda *a:{'valid':False})
    message(e,oid,'5123456789');e.prepare(oid)
    assert row(e,oid)['uid'] is None and not e.db.setting('demo-fazer-counter',0)


def test_uid_validation_nickname_in_confirmation(e,monkeypatch):
    oid=e.seed_fazer_demo('uc',1,60)
    e.db.set('svc_uc_validate_recipient',True);e.db.set('svc_uc_validation_category','validation-pubg')
    monkeypatch.setattr(e.demo_fazer,'validation_games',lambda:[{'category_id':'validation-pubg','fields':[{'key':'player_id'}]}])
    monkeypatch.setattr(e.demo_fazer,'validate_player',lambda *a:{'valid':True,'player_name':'VerifiedNick'})
    message(e,oid,'5123456789')
    assert 'VerifiedNick' in e.db.one('SELECT text FROM outbox ORDER BY id DESC LIMIT 1')['text']


def test_definitive_rate_limit_retries_same_uc_intent(e,monkeypatch):
    oid=ready_fazer(e,'uc',1,60);real=e.demo_fazer.buy_item;calls=[]
    def buy(service,payload,key):
        calls.append(key)
        if len(calls)==1:raise ProviderError('retry','Slow down',retry_after=1)
        return real(service,payload,key)
    monkeypatch.setattr(e.demo_fazer,'buy_item',buy);e.prepare(oid)
    part=e.db.one('SELECT * FROM fazer_parts');assert part['state']=='rate_limited'
    e.db.execute('UPDATE fazer_parts SET next_check=0');e.send_batch(part['batch_id'])
    assert calls==[part['idem_key'],part['idem_key']] and row(e,oid)['state']=='completed'


def test_inactive_subscription_blocks_new_orders(e,monkeypatch):
    oid=ready_fazer(e);e.db.set('fazer_profile:demo',{})
    monkeypatch.setattr(e.demo_fazer,'account',lambda:{'login':'demo','subscriptionActive':False})
    e.prepare(oid)
    assert not e.db.setting('demo-fazer-counter',0) and row(e,oid)['state']=='manual'


def test_two_recipients_are_not_cross_fulfilled(e):
    a=ready_fazer(e);b=ready_fazer(e)
    message(e,b,'@second_recipient',prefix=True);message(e,b,'ПОДТВЕРЖДАЮ '+row(e,b)['confirm_code'])
    finish(e,a);finish(e,b)
    recipients={p['telegram_username'] for p in [json.loads(r['body']) for r in e.db.rows('SELECT body FROM fazer_parts')]}
    assert recipients=={'@buyer_name','@second_recipient'}


def test_price_changed_beyond_max_does_not_buy(e,monkeypatch):
    oid=ready_fazer(e);real=e.demo_fazer.stars_quote
    monkeypatch.setattr(e.demo_fazer,'stars_quote',lambda:{**real(),'price_per_star':'1000.00'})
    e.prepare(oid);assert not e.db.setting('demo-fazer-counter',0) and row(e,oid)['state']=='manual'


def test_forced_manual_chat_blocks_purchase(e):
    oid=ready_fazer(e);e.db.execute('UPDATE chat_controls SET manual=1');e.prepare(oid)
    assert not e.db.setting('demo-fazer-counter',0)


def test_other_buyer_message_never_changes_recipient(e):
    oid=e.seed_fazer_demo('stars');e.input_message({'id':'11','chat_id':row(e,oid)['chat_id'],'author':999,'text':'@impostor_name'})
    assert row(e,oid)['uid'] is None
