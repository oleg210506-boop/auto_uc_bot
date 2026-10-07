import hashlib
import hmac
import json
import time
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from autoucbot.security import password_hash,verify_password,totp_at,verify_totp,new_totp_secret,webhook_valid,token_hash
from autoucbot.utils import BusinessError

def csrf(response):
    return BeautifulSoup(response.text,'html.parser').find('input',{'name':'csrf'})['value']
def login(client,user='owner',password='Test-password-12!'):
    r=client.get('/login');return client.post('/login',data={'csrf':csrf(r),'username':user,'password':password})
@pytest.fixture
def client(app):
    with TestClient(app) as c:
        assert login(c).status_code==200
        yield c

@pytest.mark.parametrize('path',['/','/orders','/products','/products/edit/0','/catalog','/customers','/stats','/alerts','/connections','/settings','/team','/backups','/audit','/account'])
def test_all_pages_render(client,path):
    r=client.get(path);assert r.status_code==200 and 'autoUCbot' in r.text
    assert r.headers['x-frame-options']=='DENY'

def test_authentication_required(app):
    with TestClient(app) as c:
        assert str(c.get('/orders').url).endswith('/login')

def test_bad_password(app):
    with TestClient(app) as c:
        assert login(c,password='wrong').status_code==400

def test_login_throttle(app):
    with TestClient(app) as c:
        for _ in range(5):login(c,password='wrong')
        r=login(c);assert r.status_code==400 and '15 минут' in r.text

def test_csrf_missing(client):assert client.post('/control',data={'action':'demo'}).status_code==403

def test_csrf_cross_origin(client):
    r=client.get('/');assert client.post('/control',data={'csrf':csrf(r),'action':'demo'},headers={'Origin':'https://evil.test'}).status_code==403

def test_live_requires_env_and_credentials(client):
    r=client.post('/control',data={'csrf':csrf(client.get('/')),'action':'live','ack':'РАЗРЕШАЮ ЗАКУПКИ'})
    assert r.status_code==400 and 'ENABLE_LIVE' in r.text

def test_demo_order_ui_flow(client,app):
    token=csrf(client.get('/'))
    r=client.post('/demo',data={'csrf':token,'quantity':'3','scenario':'success'})
    assert r.status_code==200 and '180' in r.text
    o=app.state.db.one('SELECT * FROM orders')
    assert client.post('/orders/'+o['id']+'/action',data={'csrf':token,'action':'demo_message','text':'5123456789'}).status_code==200
    o=app.state.db.one('SELECT * FROM orders')
    assert o['state']=='awaiting_confirmation'
    client.post('/orders/'+o['id']+'/action',data={'csrf':token,'action':'demo_message','text':'ПОДТВЕРЖДАЮ '+o['confirm_code']})
    assert app.state.db.one('SELECT confirmed FROM orders')['confirmed']==1

def test_operator_access_limits(client,app):
    token=csrf(client.get('/team'))
    r=client.post('/team',data={'csrf':token,'action':'create','username':'operator','password':'Operator-pass-001','role':'operator'});assert r.status_code==200
    with TestClient(app) as c:
        assert login(c,'operator','Operator-pass-001').status_code==200
        assert c.get('/orders').status_code==200
        assert c.get('/settings').status_code==403
        assert c.get('/connections').status_code==403
        assert c.get('/team').status_code==403
        assert c.get('/backups').status_code==403
        assert c.post('/control',data={'csrf':csrf(c.get('/')),'action':'resume'}).status_code==403

def test_two_sessions_independent(client,app):
    token=csrf(client.get('/team'))
    client.post('/team',data={'csrf':token,'action':'create','username':'second','password':'Second-user-123!','role':'owner'})
    with TestClient(app) as second:
        assert login(second,'second','Second-user-123!').status_code==200
        assert 'second' in second.get('/account').text
        assert 'owner' in client.get('/account').text
        assert app.state.db.one('SELECT COUNT(*) n FROM sessions')['n']==2

def test_secrets_are_encrypted_and_not_echoed(client,app):
    token=csrf(client.get('/connections'));secret='gc_live_example_super_secret'
    r=client.post('/connections/secrets',data={'csrf':token,'gamecore_key':secret})
    assert r.status_code==200 and secret not in r.text
    assert secret not in app.state.db.one("SELECT value FROM secrets WHERE key='gamecore_key'")['value']
    assert app.state.vault.get('gamecore_key')==secret

def test_user_xss_escaped(client,app):
    oid=app.state.engine.seed_demo();app.state.db.execute('UPDATE orders SET buyer=?',('<script>alert(1)</script>',))
    assert '<script>alert(1)</script>' not in client.get('/orders').text
    assert '&lt;script&gt;' in client.get('/orders').text

def test_totp_setup_and_one_time_recovery(client,app):
    token=csrf(client.get('/account'))
    assert client.post('/account',data={'csrf':token,'action':'totp_start','password':'Test-password-12!'}).status_code==200
    assert client.get('/account/qr.png').headers['content-type']=='image/png'
    u=app.state.db.one("SELECT * FROM users WHERE username='owner'")
    sec=app.state.vault.decrypt(u['totp_pending']);code=totp_at(sec)
    r=client.post('/account',data={'csrf':token,'action':'totp_finish','password':'Test-password-12!','otp':code})
    assert r.status_code==200 and 'резервные' in r.text.lower()
    codes=BeautifulSoup(r.text,'html.parser').find('pre').get_text().split()
    assert len(codes)==8
    with TestClient(app) as other:
        r=other.get('/login');r=other.post('/login',data={'csrf':csrf(r),'username':'owner','password':'Test-password-12!','otp':codes[0]})
        assert r.status_code==200
    assert app.state.db.one('SELECT COUNT(*) n FROM recovery_codes')['n']==7
    with TestClient(app) as other:
        r=other.get('/login');r=other.post('/login',data={'csrf':csrf(r),'username':'owner','password':'Test-password-12!','otp':codes[0]})
        assert r.status_code==400

def test_password_change_invalidates_sessions(client,app):
    token=csrf(client.get('/account'))
    client.post('/account',data={'csrf':token,'action':'password','password':'Test-password-12!','new_password':'New-strong-password001!'})
    assert app.state.db.one('SELECT COUNT(*) n FROM sessions')['n']==0
    assert login(client,password='New-strong-password001!').status_code==200

def test_csv_export_safe(client,app):
    app.state.engine.seed_demo();app.state.db.execute("UPDATE orders SET buyer='=HYPERLINK(1)'")
    r=client.get('/export.csv');assert r.status_code==200 and "'=HYPERLINK(1)" in r.text

def test_webhook_valid_and_deduplicated(app):
    secret='a1'*32;app.state.vault.set('webhook_secret',secret)
    raw=json.dumps({'event_id':'event-1','event_type':'order.completed','data':{'orderCode':'unknown-code'}}).encode();stamp=str(int(time.time()))
    sig='sha256='+hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    headers={'Content-Type':'application/json','X-Webhook-Timestamp':stamp,'X-Webhook-Signature':sig,'X-Idempotency-Key':'event-1'}
    with TestClient(app) as c:
        assert c.post('/webhooks/gamecore',content=raw,headers=headers).status_code==200
        assert c.post('/webhooks/gamecore',content=raw,headers=headers).json()['duplicate'] is True
    assert app.state.db.one('SELECT COUNT(*) n FROM webhooks')['n']==1
    assert not app.state.db.one('SELECT * FROM orders')

def test_webhook_invalid_rejected(app):
    with TestClient(app) as c:assert c.post('/webhooks/gamecore',json={}).status_code==401

def test_backup_download(client,app):
    path=app.state.db.backup();r=client.get('/backups/'+path.name)
    assert r.status_code==200 and r.content.startswith(b'SQLite format 3')

@pytest.mark.parametrize('password',['short','123456789012','password12345','x'*129])
def test_weak_password_rejected(password):
    with pytest.raises(ValueError):password_hash(password)

def test_password_hash():
    h=password_hash('Good-Password123!');assert verify_password('Good-Password123!',h);assert not verify_password('wrong',h)

def test_totp_rfc_vector():
    import base64
    secret=base64.b32encode(b'12345678901234567890').decode().rstrip('=')
    assert totp_at(secret,59,digits=8)=='94287082'

def test_totp_no_replay():
    s=new_totp_secret();now=2000000000;c=totp_at(s,now);step=verify_totp(s,c,now=now)
    assert step is not None and verify_totp(s,c,last_step=step,now=now) is None

def test_webhook_timestamp_window():
    secret='text-key';body=b'{}';stamp='1000';sig='sha256='+hmac.new(secret.encode(),b'1000.{}',hashlib.sha256).hexdigest();headers={'x-webhook-timestamp':stamp,'x-webhook-signature':sig}
    assert webhook_valid(body,headers,secret,now=1000)
    assert not webhook_valid(body,headers,secret,now=1301)
