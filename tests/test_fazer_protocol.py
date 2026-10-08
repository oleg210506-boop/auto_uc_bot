"""Offline protocol contracts taken from FazerCards' public API reference."""
import asyncio
import hashlib
import hmac
import json
from decimal import Decimal
import httpx
import pytest
from autoucbot.adapters.fazer import (FazerClient,AsyncFazerClient,FazerCardsUCAdapter,FazerCardsStarsAdapter,
    parse_response,username,to_micros,usd_to_rub,buy_request,webhook_signature,order_id)
from autoucbot.adapters.gamecore import ProviderError
from autoucbot.utils import BusinessError
KEY='auc_persisted_0123456789abcdef'

@pytest.mark.parametrize('value,expected',[('0.015',15000),('2.25',2250000),('0.000001',1),(0,0),('0.1234567',123457)])
def test_decimal_currency(value,expected):assert to_micros(value)==expected
@pytest.mark.parametrize('value',['NaN','Infinity','-1',None,True,'abc',1e30])
def test_bad_currency(value):
    with pytest.raises(ProviderError):to_micros(value)

def test_multiply_before_rounding():
    assert to_micros(Decimal('0.0149999')*150)==2249985
    assert usd_to_rub(2249985,'99.99')==22498

@pytest.mark.parametrize('value',['@Test_User','test_user','  @test_user  '])
def test_recipient_normalized(value):assert username(value)=='@test_user'
@pytest.mark.parametrize('value',['','123456','@ab','https://t.me/user','t.me/user','@имя','user name','@user\nname','@@username','a'*33,'@user/test','@user?amount=10'])
def test_recipient_rejected(value):
    with pytest.raises(BusinessError):username(value)

@pytest.mark.parametrize('n',[50,51,100,150,9999,10000])
def test_stars_request(n):
    path,body,idem=buy_request('stars',{'telegram_username':'@Recipient','quantity':n},KEY)
    assert path=='/telegram/stars/buy' and body=={'telegram_username':'@recipient','quantity':n}
    assert idem is None # NOT documented as replay-safe at the provider.
@pytest.mark.parametrize('n',[0,-1,1,49,10001,'50.0',True,None])
def test_stars_limits(n):
    with pytest.raises((BusinessError,ValueError,TypeError)):
        buy_request('stars',{'telegram_username':'@recipient','quantity':n},KEY)

def test_uc_contract():
    path,body,idem=buy_request('uc',{'category_id':'c','offer_id':'o','fields':{'player_id':'5123456789'}},KEY)
    assert (path,idem)==('/topups/order',KEY) and body['fields']['player_id']=='5123456789'

@pytest.mark.parametrize('service,payload',[('uc',{}),('stars',{'quantity':50,'telegram_username':'@user','ton':'0.02'}),('ton',{})])
def test_unrecognized_purchase_schema(service,payload):
    with pytest.raises(BusinessError):buy_request(service,payload,KEY)

@pytest.mark.parametrize('status,payload,kind',[
 (200,{},'unknown'),(200,{'ok':False},'unknown'),(500,{'ok':False},'unknown'),(502,{'ok':False},'unknown'),
 (401,{'ok':False},'auth'),(403,{'ok':False},'auth'),(409,{'ok':False},'unknown'),
 (429,{'ok':False},'retry'),(402,{'ok':False,'code':'INSUFFICIENT_BALANCE'},'balance'),
 (400,{'ok':False,'code':'invalid_recipient'},'rejected'),
 (400,{'ok':False,'order':{'id':'ord-12'}},'unknown'),(302,{'ok':False},'unknown'),
 (429,{},'unknown'),(503,{'ok':False,'error':'insufficient funds'},'unknown')])
def test_post_errors_conservative(status,payload,kind):
    with pytest.raises(ProviderError) as exc:parse_response(httpx.Response(status,json=payload),True)
    assert exc.value.kind==kind

@pytest.mark.parametrize('content',[b'<html>Cloudflare</html>',b'null',b'[]',b'not-json'])
def test_unstructured_response_never_means_rejected(content):
    with pytest.raises(ProviderError) as exc:parse_response(httpx.Response(200,content=content),True)
    assert exc.value.kind=='unknown'

@pytest.mark.parametrize('service',['uc','stars'])
def test_real_transport_contract_no_hidden_retry(service):
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200,json={'ok':True,'order':{'id':'ord-12','status':'processing'}})
    f=FazerClient('not-a-real-key',httpx.Client(transport=httpx.MockTransport(handler)))
    body={'category_id':'cat','offer_id':'of','fields':{'player_id':'5123456789'}} if service=='uc' else {'telegram_username':'@User','quantity':50}
    out=f.buy_item(service,body,KEY)
    assert out['id']=='ord-12' and len(seen)==1
    assert seen[0].headers['X-API-Key']=='not-a-real-key'
    assert seen[0].headers.get('Idempotency-Key')==(KEY if service=='uc' else None)
    assert seen[0].url.host=='api.fzr.cards'
    f.close()

@pytest.mark.parametrize('service',['uc','stars'])
def test_timeout_no_transport_retries(service):
    calls=[]
    def handler(req):calls.append(req);raise httpx.ReadTimeout('lost')
    f=FazerClient('fake',httpx.Client(transport=httpx.MockTransport(handler)))
    payload={'category_id':'cat','offer_id':'of','fields':{'player_id':'5123456789'}} if service=='uc' else {'telegram_username':'@user','quantity':50}
    with pytest.raises(ProviderError) as exc:f.buy_item(service,payload,KEY)
    assert exc.value.kind=='unknown' and len(calls)==1


def test_redirect_never_forwards_key():
    seen=[]
    def handler(req):seen.append(req);return httpx.Response(302,headers={'Location':'https://evil.test'})
    f=FazerClient('fake',httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True))
    with pytest.raises(ProviderError):f.get_balance()
    assert len(seen)==1

@pytest.mark.parametrize('currency',['RUB','TON','USDT',None])
def test_balance_currency_must_be_usd(currency):
    f=FazerClient('fake',httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'balance':'100','currency':currency}))))
    with pytest.raises(ProviderError):f.get_balance()


def test_paginated_categories():
    seen=[]
    def handler(req):
        seen.append(str(req.url))
        cursor=req.url.params.get('cursor');return httpx.Response(200,json={'ok':True,'items':[{'category_id':'b' if cursor else 'a'}],'meta':{'has_more':not bool(cursor),'next_cursor':None if cursor else 'next'}})
    f=FazerClient('fake',httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(f.categories())==2 and len(seen)==2


def test_repeated_catalog_cursor_stops():
    f=FazerClient('fake',httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'ok':True,'items':[],'meta':{'has_more':True,'next_cursor':'again'}}))))
    with pytest.raises(ProviderError):f.categories()

@pytest.mark.parametrize('code',['ord_123','ORD-1','../../balance','ord-','http://evil','ord-1?a=2',123])
def test_invalid_provider_id(code):
    with pytest.raises(ProviderError):order_id(code)


def test_hmac_raw_bytes():
    body=b'{ "event_id": "123" }';secret='test-key';sig='sha256='+hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    assert webhook_signature(body,sig,secret)
    assert not webhook_signature(body+b' ',sig,secret)
    assert not webhook_signature(body,sig,'wrong')
    assert not webhook_signature(body,sig,'')

@pytest.mark.parametrize('service',['uc','stars'])
def test_async_adapters_contract(service):
    async def run():
        seen=[]
        async def handler(req):
            seen.append(req)
            data={'ok':True,'balance':'100.1234','currency':'USD'} if req.url.path.endswith('/balance') else {'ok':True,'order':{'id':'ord-123','status':'completed'}}
            return httpx.Response(200,json=data)
        f=AsyncFazerClient('fake',httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        a=FazerCardsUCAdapter(f) if service=='uc' else FazerCardsStarsAdapter(f)
        result=await a.buy_item('cat','offer',{'player_id':'5123456789'},KEY) if service=='uc' else await a.buy_item('@recipient',50,KEY)
        assert result['id']=='ord-123'
        assert (await a.check_order_status('ord-123'))['status']=='completed'
        assert await a.get_balance()==100123400
        assert len(seen)==3
        await f.aclose()
    asyncio.run(run())
