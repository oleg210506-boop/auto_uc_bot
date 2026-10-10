"""Offline regression checks for the incident reported on 2026-10-09.

No calls to FunPay, FazerCards, or Telegram are made in these tests.
"""
import json
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
import pytest
import requests

from autoucbot.adapters.funpay import FunPay
from autoucbot.config import Config
from autoucbot.funpay_guard import FunPayTraffic, FunPayDeferredError, FunPayRateLimitedError, retry_after_seconds
from autoucbot.worker import Worker
from autoucbot.utils import BusinessError


class Response:
    def __init__(self, code, retry_after=None):
        self.status_code=code
        self.headers={'Retry-After':retry_after} if retry_after else {}
        self.content=b''
        self.text=''


def test_429_persists_cooldown_and_prevents_second_network_call(e):
    traffic=FunPayTraffic(e.db)
    ses=requests.Session();calls=[]
    def fake(*args, **kwargs):
        calls.append((args,kwargs))
        return Response(429,'120')
    ses.request=fake
    client=FunPay('fake-key','Agent',session=ses,traffic=traffic)
    with pytest.raises(FunPayRateLimitedError) as err:client.request('GET','/')
    assert err.value.wait_seconds>=900
    assert len(calls)==1
    with pytest.raises(FunPayDeferredError) as block:client.request('GET','/')
    assert block.value.reason=='cooldown' and len(calls)==1
    assert e.db.setting('funpay_429_strikes')==1
    incident=e.db.one("SELECT value FROM runtime WHERE key='funpay_guard'")
    assert incident is not None
    assert json.loads(incident['value'])['endpoint']=='/'
    assert FunPayTraffic(e.db).is_blocked()  # simulated container restart
    client.close()


def test_retry_after_date_and_longer_server_pause(e):
    date=format_datetime(datetime.now(timezone.utc)+timedelta(hours=2))
    assert 7150<=retry_after_seconds(date)<=7210
    with pytest.raises(FunPayRateLimitedError) as ex:
        FunPayTraffic(e.db).limited({'Retry-After':date})
    assert ex.value.wait_seconds>=7150


def test_invalid_retry_after_uses_default_backoff(e):
    with pytest.raises(FunPayRateLimitedError) as ex:
        FunPayTraffic(e.db).limited({'Retry-After':'?garbled'})
    assert ex.value.wait_seconds==900
    with pytest.raises(FunPayRateLimitedError) as ex:
        FunPayTraffic(e.db).limited({})
    assert ex.value.wait_seconds==1800


def test_proxy_is_only_on_funpay_and_not_in_message_or_logs(e):
    proxy='http://user:password@192.0.2.44:9876'
    client=FunPay('fake-key','Agent',proxy_url=proxy,traffic=FunPayTraffic(e.db))
    assert client.session.proxies['https']==proxy
    assert client.session.trust_env is False
    assert 'password' not in repr(client.traffic)
    assert 'FUNPAY_PROXY_URL' not in e.vault.get('fazer_key')
    client.close()


@pytest.mark.parametrize('url',[
    'socks5://username:password@192.0.2.2:80',
    'http://192.0.2.2',
    'file:///etc/passwd',
    'http://host.example:8000/extra',
    'http://host.example:8000\nHeader:foo',
])
def test_bad_proxy_url_is_rejected(url):
    with pytest.raises((ValueError, TypeError)):
        FunPay('fake-key','Agent',proxy_url=url)


def test_original_six_second_poll_is_migrated_safely(e):
    e.db.set('funpay_poll_seconds',6)
    Worker(e)
    assert e.db.setting('funpay_poll_seconds')==30
    assert e.db.one("SELECT action FROM audit WHERE action='funpay.poll.migrated'")


def test_all_funpay_alerts_silent_after_first_delivery(e,monkeypatch):
    e.vault.set('telegram_token','fake-telegram-token')
    e.db.set('alert_chat_ids','1234')
    e.db.set('alert_repeat_seconds',60)
    e.db.set('alert_kinds','funpay,manual')
    w=Worker(e);calls=[]
    monkeypatch.setattr(w,'telegram_send',lambda *a:calls.append(a))
    e.alert('funpay','Ошибка: funpay','FunPay HTTP 429',key='worker:funpay')
    w.alerts()
    assert len(calls)==1
    e.db.execute('UPDATE alert_deliveries SET sent=sent-9999')
    e.alert('funpay','Ошибка: funpay','FunPay HTTP 429',key='worker:funpay')
    w.alerts()
    assert len(calls)==1
    e.resolve_alert('worker:funpay')
    e.alert('funpay','Ошибка: funpay','FunPay HTTP 429 again',key='worker:funpay')
    w.alerts()
    assert len(calls)==2


def test_outbox_is_not_marked_uncertain_before_http_call(e,monkeypatch):
    oid=e.seed_fazer_demo('uc',denomination=60);e.db.set('mode','live')
    e.db.execute("UPDATE orders SET mode='live' WHERE id=?",(oid,))
    e.db.execute("UPDATE outbox SET mode='live' WHERE order_id=?",(oid,))
    monkeypatch.setattr(e,'fp',lambda *args: SimpleNamespace(send=lambda *a:(_ for _ in ()).throw(FunPayDeferredError(120,'local_budget'))))
    w=Worker(e);w.outbox()
    row=e.db.one('SELECT state,next_attempt FROM outbox WHERE order_id=?',(oid,))
    assert row['state']=='pending' and row['next_attempt']>time.time()


def test_unknown_funpay_send_does_not_retry(e,monkeypatch):
    oid=e.seed_fazer_demo('uc',denomination=60);e.db.set('mode','live')
    e.db.execute("UPDATE orders SET mode='live' WHERE id=?",(oid,))
    e.db.execute("UPDATE outbox SET mode='live' WHERE order_id=?",(oid,))
    calls=[]
    def send(*args):
        calls.append(args)
        raise FunPayRateLimitedError(900)
    monkeypatch.setattr(e,'fp',lambda *args: SimpleNamespace(send=send))
    w=Worker(e);w.outbox()
    assert len(calls)==1
    assert e.db.one('SELECT state FROM outbox WHERE order_id=?',(oid,))['state']=='uncertain'
    w.outbox()
    assert len(calls)==1


def test_manual_uid_recovery_requires_paid_order_and_no_batch(e,monkeypatch):
    oid=e.seed_fazer_demo('uc',denomination=60);o=e.db.one('SELECT * FROM orders WHERE id=?',(oid,))
    e.db.set('mode','live');e.db.execute("UPDATE orders SET mode='live' WHERE id=?",(oid,))
    # Offline stub stands in for authenticated, paid order read.
    fp=SimpleNamespace(order=lambda id:{
        'id':oid,'buyer_id':o['buyer_id'],'chat_id':o['chat_id'],
        'quantity':o['quantity'],'currency':o['currency'],'revenue':o['revenue'],
        'status':'paid'})
    monkeypatch.setattr(e,'fp',lambda mode=None:fp)
    assert 'подтверждение' in e.manually_confirm_recipient_from_chat(oid,'5123456789','owner').lower()
    updated=e.db.one('SELECT * FROM orders WHERE id=?',(oid,))
    assert updated['state']=='awaiting_confirmation' and updated['uid']=='5123456789'
    assert not e.db.one('SELECT id FROM batches WHERE order_id=?',(oid,))
    assert e.db.one("SELECT * FROM outbox WHERE dedupe LIKE ?",('%manual:%',))
    with pytest.raises(BusinessError):e.manually_confirm_recipient_from_chat(oid,'5123456789','owner')


def test_manual_uid_recovery_rejects_refund(e,monkeypatch):
    oid=e.seed_fazer_demo('uc',denomination=60);o=e.db.one('SELECT * FROM orders WHERE id=?',(oid,))
    e.db.set('mode','live');e.db.execute("UPDATE orders SET mode='live' WHERE id=?",(oid,))
    def order(_):return {'id':oid,'buyer_id':o['buyer_id'],'chat_id':o['chat_id'],'quantity':o['quantity'],
                         'currency':o['currency'],'revenue':o['revenue'],'status':'refunded'}
    monkeypatch.setattr(e,'fp',lambda mode=None:SimpleNamespace(order=order))
    with pytest.raises(BusinessError):e.manually_confirm_recipient_from_chat(oid,'5123456789','owner')
    assert e.db.one('SELECT uid FROM orders WHERE id=?',(oid,))['uid'] is None


def test_task_defer_not_starve_provider_tasks(e,monkeypatch):
    w=Worker(e);e.db.set('mode','live')
    a=w.enqueue('funpay_test')
    b=w.enqueue('backup')
    monkeypatch.setattr(e,'fp',lambda *args:SimpleNamespace(connect=lambda:(_ for _ in ()).throw(FunPayDeferredError(3600))))
    w.tasks()
    assert e.db.one('SELECT state FROM tasks WHERE id=?',(a,))['state']=='pending'
    assert e.db.one('SELECT updated FROM tasks WHERE id=?',(a,))['updated']>time.time()+3500
    w.tasks()
    assert e.db.one('SELECT state FROM tasks WHERE id=?',(b,))['state']=='done'


def test_sale_archive_is_bounded_to_three_pages():
    client=FunPay('fake-key','Agent')
    client.user_id=1;client.csrf='x';client.connected_at=time.time()
    calls=[]
    class Page:
        def __init__(self,content):self.text=content
    def request(method,path,**kw):
        calls.append((method,path))
        n=len(calls)
        return Page('<a class="user-link-name">me</a><h1 class="page-header">Мои продажи</h1>'+
                    '<a class="tc-item"><span class="tc-order">#ORDER'+str(n)+'</span></a>'+
                    '<input name="continue" value="cursor'+str(n)+'">')
    client.request=request
    assert client.paid_ids(123)==['ORDER1','ORDER2','ORDER3']
    assert len(calls)==3
    assert client.sales_page_limit_hit is True
    client.close()
