"""Persistent local simulator. It never opens a network connection."""
import json
import time
from .gamecore import ProviderError

class DemoGameCore:
    def __init__(self, db): self.db=db
    def catalog(self):
        return [self.product(i) for i in (60,325,660,1800,3850,8100)]
    def product(self, i):
        i=int(i)
        # Deliberately synthetic prices; never a commercial offer.
        return {"id":i,"name":f"ДЕМО — {i} UC (условная цена)","price":i*100,"currency":"RUB","region":"global",
                "uc":i,"delivery_type":"id_only","in_stock":True,
                "payload":{"id":i,"deliveryDataSchema":[{"id":"gameUserId","required":True,"type":"text"}],"amountType":{"type":"fixed","value":i}}}
    def create(self,payload,key):
        prior=self.db.setting("demo-batch:"+key)
        if prior:
            if prior["payload"] != payload: raise ProviderError("invariant","Другое тело запроса")
            return prior["response"]
        scenario=self.db.setting("demo_scenario","success")
        if scenario=="balance": raise ProviderError("balance","Демонстрация нехватки баланса")
        if scenario=="unknown_before": raise ProviderError("unknown","Демонстрация потерянного ответа до создания")
        item=payload["items"][0]; qty=item["quantity"]; cost=item["productId"]*qty
        codes=["demo-"+key[:12]]
        if scenario=="split" and qty>1: codes.append("demo-"+key[12:24])
        quantities=[qty] if len(codes)==1 else [1,qty-1]
        resp={"paymentCode":"demo-payment-"+key[:8],"totalAmount":cost,"orders":[]}
        for code,n in zip(codes,quantities):
            items=[]
            for j in range(n):
                state="failed" if scenario=="failed" or (scenario=="partial" and j==n-1) else "completed"
                if scenario=="pending": state="pending"
                items.append({"id":j+1,"productName":f"{item['productId']} UC","amount":1,"price":item["productId"],"status":state,"cdKeys":[]})
            status="processing" if scenario=="pending" else "failed" if any(x["status"]=="failed" for x in items) else "completed"
            obj={"code":code,"externalOrderId":payload["externalOrderId"],"totalAmount":item["productId"]*n,"status":status,"items":items}
            self.db.set("demo-order:"+code,obj)
            resp["orders"].append({"code":code,"total":obj["totalAmount"],"itemCount":n})
        self.db.set("demo-batch:"+key,{"response":resp,"payload":payload})
        if scenario=="unknown_after": raise ProviderError("unknown","Демонстрация: деньги списаны, ответ потерян")
        return resp
    def order(self,code): return self.db.setting("demo-order:"+code)
    def recover(self,external_id):
        result=[]
        for row in self.db.rows("SELECT value FROM settings WHERE key LIKE 'demo-order:%'"):
            d=json.loads(row["value"])
            if d["externalOrderId"]==external_id: result.append(d)
        return result
    def balance(self,*args): return 100000000
    def close(self): pass

class DemoFunPay:
    def __init__(self,db): self.db=db; self.user_id=1
    def connect(self): return {"user_id":1,"name":"ДЕМО — не FunPay"}
    def order(self,oid):
        d=self.db.setting("demo-fp:"+oid)
        if not d: raise ValueError("Демо-заказ не найден")
        return d
    def paid_ids(self,subcategory): return []
    def messages(self,chats): return []
    def send(self,chat,text): pass
    def lot(self,lot_id):
        p=self.db.one("SELECT * FROM products WHERE fp_lot_id=? AND mode='demo'",(lot_id,))
        return {"subcategory":p["fp_subcategory"],"text":p["marker"],"active":True,"currency":"RUB","price":p["sale_price"],"fields":{}}
    def change_lot(self,p,**kwargs): return {"active":kwargs.get("active",True)}
    def raise_lots(self,*args): return 3600
    def close(self): pass
