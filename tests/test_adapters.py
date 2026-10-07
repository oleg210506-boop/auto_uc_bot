import json
import requests
import pytest
from autoucbot.adapters.gamecore import GameCore,ProviderError
from autoucbot.adapters.funpay import FunPay,FunPayError
from autoucbot.utils import BusinessError

class Response:
    def __init__(self,status=200,data=None,text=None,headers=None):
        self.status_code=status;self.data=data;self.headers=headers or {};self.text=text if text is not None else json.dumps(data or {});self.content=self.text.encode()
    def json(self):
        if self.data is None:raise ValueError()
        return self.data
class Session:
    def __init__(self,responses):self.responses=list(responses);self.headers={};self.cookies=requests.cookies.RequestsCookieJar();self.calls=[]
    def request(self,*a,**kw):
        self.calls.append((a,kw));r=self.responses.pop(0)
        if isinstance(r,Exception):raise r
        return r
    def close(self):pass

@pytest.mark.parametrize('status,error,kind',[(401,'x','auth'),(403,'x','auth'),(402,'Insufficient balance','balance'),(402,'Exceeds credit limit','balance'),(402,'Concurrent balance modification','retry'),(402,'weird','rejected'),(409,'idempotency_key_reused_with_different_body','invariant'),(409,'Duplicate request in progress','unknown'),(429,'Rate limit','retry'),(500,'x','unknown'),(302,'x','unknown'),(422,'invalid','rejected')])
def test_gamecore_error_classification(status,error,kind):
    s=Session([Response(status,{'success':False,'error':error})]);g=GameCore('key',session=s)
    with pytest.raises(ProviderError) as ex:g.create({'items':[]},'key')
    assert ex.value.kind==kind and len(s.calls)==1

def test_gamecore_no_automatic_post_retry():
    g=GameCore('key',session=Session([requests.Timeout()]))
    with pytest.raises(ProviderError) as ex:g.create({},'stable')
    assert ex.value.kind=='unknown'

def test_gamecore_create_headers_and_multiple_codes():
    d={'orders':[{'code':'A-1','total':60},{'code':'A-2','total':120}],'totalAmount':180}
    s=Session([Response(data={'success':True,'data':d})]);g=GameCore('secret',session=s)
    assert len(g.create({'items':[]},'stable-key')['orders'])==2
    assert s.headers['X-Api-Key']=='secret'
    assert s.calls[0][1]['headers']['X-Idempotency-Key']=='stable-key'
    assert s.calls[0][1]['allow_redirects'] is False

@pytest.mark.parametrize('body',[{}, {'totalAmount':1,'orders':[]},{'totalAmount':1,'orders':[{'code':'x/../../foo'}]},{'totalAmount':0,'orders':[{'code':'a'}]}])
def test_malformed_success_is_unknown(body):
    g=GameCore('k',session=Session([Response(data={'success':True,'data':body})]))
    with pytest.raises(ProviderError) as ex:g.create({},'key')
    assert ex.value.kind=='unknown'

@pytest.mark.parametrize('path',['https://evil.test','//evil.test','/b2b/../secrets','/b2b/\\evil','/health'])
def test_provider_no_ssrf_path(path):
    g=GameCore('key',session=Session([]))
    with pytest.raises(BusinessError):g.request('GET',path)

def test_catalog_exact_contract():
    p={'id':34521,'name':'PUBG 660','wholesalePrice':'1064.50','currency':'RUB','region':'global','deliveryType':'id_only','amountType':{'type':'fixed','value':660},'inStock':True,'deliveryDataSchema':[{'id':'gameUserId','required':True}]}
    s=Session([Response(data={'success':True,'data':[p]})]);g=GameCore('key',session=s)
    result=g.catalog()[0]
    assert result['price']==106450 and result['uc']==660
    assert s.calls[0][0][1].endswith('/b2b/catalog/games/pubg-mobile/products?deliveryType=id_only')

def test_balance_only_configured_contract():
    g=GameCore('key',session=Session([Response(data={'success':True,'data':{'balance':'123.45'}})]))
    assert g.balance('/b2b/support-confirmed-example','data.balance')==12345

def test_recovery_pagination():
    data=[{'code':str(i),'externalOrderId':'other'} for i in range(100)]
    s=Session([Response(data={'success':True,'data':{'orders':data,'pagination':{'total':101}}}),Response(data={'success':True,'data':{'orders':[{'code':'x','externalOrderId':'match'}],'pagination':{'total':101}}})])
    assert GameCore('k',session=s).recover('match')==[{'code':'x','externalOrderId':'match'}]

def order_data():
    return {'order_uid':'ABC123','seller':{'user_id':1,'name':'owner'},'buyer':{'user_id':2,'name':'buyer'},'amount':'360.00','currency':'RUB','status':'paid','chat':{'node_name':'users-1-2'},'section':{'local_id':123,'type_id':'lot'},'type_data':{'amount':'3','fields':{'summary':{'value':{'ru':'180 UC [AUC:PUBG180]','en':'180 UC [AUC:PUBG180]'},'name':'Summary','field_type_id':'x'},'desc':{'value':'description'}}}}

def test_funpay_structured_quantity():
    d=FunPay.normalize_order(order_data(),1)
    assert d['quantity']==3 and d['revenue']==36000
    assert '[AUC:PUBG180]' in d['description']
@pytest.mark.parametrize('value',[None,0,-1,1.5,'nan','unknown','1e100'])
def test_funpay_no_guessed_quantity(value):
    d=order_data();d['type_data']['amount']=value
    with pytest.raises(FunPayError):FunPay.normalize_order(d,1)

def test_funpay_wrong_seller():
    with pytest.raises(FunPayError):FunPay.normalize_order(order_data(),3)

def test_funpay_wrong_chat():
    d=order_data();d['chat']['node_name']='users-1-99'
    with pytest.raises(FunPayError):FunPay.normalize_order(d,1)

def test_funpay_buyer_field_marker_not_trusted():
    d=order_data();d['type_data']['fields']={'player':{'value':'[AUC:PUBG180]'}}
    assert FunPay.normalize_order(d,1)['description']==''

def test_funpay_connect():
    html="<html><body data-app-data='{"+'"userId":1,"csrf-token":"csrf"'+"}'><div class='user-link-name'>owner</div></body></html>"
    f=FunPay('cookie','agent',session=Session([Response(text=html)]))
    assert f.connect()=={'user_id':1,'name':'owner'}
@pytest.mark.parametrize('status',[302,403,429,503])
def test_funpay_does_not_bypass(status):
    f=FunPay('k','ua',session=Session([Response(status,text='captcha')]))
    with pytest.raises(FunPayError):f.connect()

def test_funpay_lot_form_parse():
    html='''<form class="form-offer-editor"><input name="offer_id" value="60"><input name="node_id" value="1"><input name="price" value="120"><input type="checkbox" name="active" checked><input type="checkbox" name="auto_delivery"><textarea name="fields[summary][ru]">60 UC [AUC:PUBG60]</textarea><span class="form-control-feedback">₽</span></form>'''
    f=FunPay('k','ua',session=Session([Response(text=html)]));f.user_id=1;f.connected_at=10**15
    lot=f.lot(60);assert lot['active'] and lot['currency']=='RUB' and 'auto_delivery' not in lot['fields']

def test_funpay_refuses_foreign_lot_change():
    html='''<form class="form-offer-editor"><input name="offer_id" value="60"><input name="node_id" value="999"><input name="price" value="120"><textarea name="fields[summary][ru]">Other</textarea></form>'''
    s=Session([Response(text=html)]);f=FunPay('k','ua',session=s);f.user_id=1;f.connected_at=10**15
    with pytest.raises(FunPayError):f.change_lot({'fp_lot_id':60,'fp_subcategory':1,'marker':'[AUC:PUBG60]'},active=False)
    assert len(s.calls)==1

def test_funpay_runner_only_requested_chats():
    d={'objects':[{'type':'chat_node','id':'users-1-2','data':{'node':{'name':'users-1-2'},'messages':[{'id':55,'author':2,'html':'<div class="chat-msg-text">5123456789</div>'}]}},{'type':'chat_node','id':'users-1-3','data':{'node':{'name':'users-1-3'},'messages':[{'id':66,'author':3,'html':'bad'}]}}]}
    f=FunPay('k','ua',session=Session([Response(data=d)]));f.user_id=1;f.connected_at=10**15;f.csrf='csrf'
    msgs=f.messages({'users-1-2':0});assert len(msgs)==1 and msgs[0]['text']=='5123456789'
