import json
import time
from dataclasses import replace
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup
from autoucbot.config import Config
from autoucbot.db import DB
from autoucbot.engine import Engine
from autoucbot.security import Vault
from autoucbot.utils import BusinessError
from autoucbot.worker import Worker,InstanceLock
from autoucbot.maintenance import restore_backup,sha256_file


def test_worker_full_demo_cycle(e,ready):
    w=Worker(e);w.purchases();w.poll();w.outbox()
    assert e.db.one('SELECT state FROM orders')['state']=='completed'
    assert not e.db.one("SELECT id FROM outbox WHERE state!='sent'")

def test_single_worker_lock(e):
    a=InstanceLock(e.db.data_dir/'instance.lock');b=InstanceLock(a.path)
    a.acquire()
    try:
        with pytest.raises(RuntimeError):b.acquire()
    finally:a.release()
    b.acquire();b.release()

def test_start_recovers_uncertain_sends(e,ready,monkeypatch):
    e.prepare(ready);e.db.execute("UPDATE batches SET state='sending'")
    e.db.execute("UPDATE outbox SET state='sending'")
    w=Worker(e);monkeypatch.setattr(w,'run',lambda:None)
    w.start();w.stop()
    assert e.db.one('SELECT state FROM batches')['state']=='unknown'
    assert not e.db.one("SELECT id FROM outbox WHERE state!='uncertain'")
    e.recover_batch(1)
    assert e.db.one('SELECT state FROM orders')['state']=='completed'
    assert e.db.one('SELECT attempts FROM batches')['attempts']==1

def test_outbox_failure_no_duplicate(e,monkeypatch):
    e.seed_demo();w=Worker(e);calls=[]
    def fail(*a):calls.append(a);raise RuntimeError('private value must not be logged')
    monkeypatch.setattr(e.fp(),'send',fail)
    w.outbox();w.outbox()
    assert len(calls)==1
    assert e.db.one('SELECT state FROM outbox')['state']=='uncertain'
    assert 'private value' not in str(e.db.rows('SELECT * FROM alerts'))

def test_manual_chat_suppresses_queued_auto(e):
    e.seed_demo();e.db.execute('UPDATE chat_controls SET manual=1')
    Worker(e).outbox()
    assert e.db.one('SELECT state FROM outbox')['state']=='suppressed'

def test_observe_does_not_send_demo_or_live(e):
    e.seed_demo();e.db.set('mode','observe');Worker(e).outbox()
    assert e.db.one('SELECT state FROM outbox')['state']=='pending'

def test_bounded_reminders_and_uid_timeout(e):
    e.seed_demo();e.db.execute('UPDATE orders SET created=?',(time.time()-7200,));w=Worker(e)
    for _ in range(5):w.reminders();e.db.execute('UPDATE orders SET last_reminder=0')
    assert e.db.one('SELECT reminder_count FROM orders')['reminder_count']==2
    assert e.db.one("SELECT id FROM alerts WHERE key LIKE 'uid:%'")
    assert e.db.one('SELECT state FROM orders')['state']=='awaiting_uid'

@pytest.mark.parametrize('kind',['backup','catalog'])
def test_queued_maintenance_task(e,kind):
    w=Worker(e);tid=w.enqueue(kind);w.tasks()
    assert e.db.one('SELECT state FROM tasks WHERE id=?',(tid,))['state']=='done'

def test_invalid_task_safe_failure(e):
    w=Worker(e);w.enqueue('not_a_method');w.tasks()
    assert e.db.one('SELECT state FROM tasks')['state']=='error'

def test_telegram_alert_cooldown_per_recipient(e,monkeypatch):
    w=Worker(e);e.vault.set('telegram_token','fake-token-never-used');e.db.set('alert_chat_ids','123,456')
    e.alert('balance','Баланс','Низкий',key='balance:test');calls=[]
    monkeypatch.setattr(w,'telegram_send',lambda chat,text:calls.append((chat,text)))
    w.alerts();w.alerts();assert [x[0] for x in calls]==['123','456']
    e.db.execute('UPDATE alert_deliveries SET sent=0');w.alerts();assert len(calls)==4
    e.db.execute('UPDATE alerts SET acked=1');e.db.execute('UPDATE alert_deliveries SET sent=0');w.alerts();assert len(calls)==4

def test_telegram_kind_filter(e,monkeypatch):
    w=Worker(e);e.vault.set('telegram_token','fake-token');e.db.set('alert_chat_ids','1');e.db.set('alert_kinds','balance')
    e.alert('manual','Manual','Details');calls=[];monkeypatch.setattr(w,'telegram_send',lambda *x:calls.append(x));w.alerts();assert not calls

def test_retention_keeps_financial_and_dedupe_records(e,ready):
    e.db.execute('UPDATE messages SET created=0');e.db.execute('UPDATE seen_messages SET created=0')
    Worker(e).cleanup()
    assert not e.db.one('SELECT id FROM messages')
    assert e.db.one('SELECT * FROM seen_messages') and e.db.one('SELECT id FROM orders')

def test_auto_backup_rotates(e):
    e.db.set('backup_keep',2);w=Worker(e)
    for _ in range(4):e.db.set('last_backup',0);w.backups()
    assert len(list((e.db.data_dir/'backups').glob('*.sqlite3')))==2

def test_manual_resolution_ledger_idempotent(e,ready):
    e.db.execute("UPDATE orders SET state='manual'")
    before=e.balance()
    for _ in range(3):e.manual_resolution(ready,'manual_complete',150,0,'Вручную проверен чек 123456','owner')
    assert e.balance()==before-15000
    assert e.db.one('SELECT state,delivered FROM orders')=={'state':'completed','delivered':180}
    assert e.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE '%completed%'")['n']==1

def test_manual_resolution_correcting_cost_delta(e,ready):
    before=e.balance();e.manual_resolution(ready,'finance',150,0,'Подтверждение ручной закупки 001','owner')
    e.manual_resolution(ready,'finance',120,0,'Исправлен ошибочный расход 001','owner')
    assert e.balance()==before-12000

@pytest.mark.parametrize('state',['sending','unknown','accepted','processing','retry','balance'])
def test_manual_completion_forbidden_inflight(e,ready,state):
    e.prepare(ready);e.db.execute('UPDATE batches SET state=?',(state,))
    with pytest.raises(BusinessError):e.manual_resolution(ready,'manual_complete',150,0,'Подтверждение поставщика 001','owner')

def test_manual_refund_requires_full_amount_rollback(e,ready):
    before=e.balance()
    with pytest.raises(BusinessError):e.manual_resolution(ready,'manual_refund',150,1,'Возврат был проверен 001','owner')
    assert not e.db.one('SELECT * FROM order_finance') and e.balance()==before
    revenue=e.db.one('SELECT revenue FROM orders')['revenue']
    e.manual_resolution(ready,'manual_refund',0,revenue/100,'Полный возврат проверен 001','owner')
    assert e.db.one('SELECT state FROM orders')['state']=='cancelled'

def test_wrong_master_key_fails_closed(e):
    with pytest.raises(ValueError):Vault(e.db,'wrong-master-key'*3)

def test_same_key_reopens_database(e,ready,config):
    e.prepare(ready);db=DB(config.data_dir);other=Engine(db,Vault(db,config.secret),config)
    other.poll_batch(1);other.prepare(ready)
    assert db.one('SELECT COUNT(*) n FROM batches')['n']==1
    assert db.one('SELECT state FROM orders')['state']=='completed'

def test_restore_pauses_and_marks_history(e,ready,config):
    backup=e.db.backup();e.db.set('paused',False)
    assert restore_backup(config,str(backup.relative_to(config.data_dir)))
    db=DB(config.data_dir);assert db.setting('mode')=='observe' and db.setting('paused') and not db.setting('live_armed')
    assert db.setting('recovery_required') and db.one('SELECT state FROM orders')['state']=='manual'
    assert not db.one("SELECT id FROM outbox WHERE state='pending'")
    assert restore_backup(config,str(backup.relative_to(config.data_dir))) is False

def test_restore_uses_original_secret_only(e,config):
    backup=e.db.backup();before=sha256_file(e.db.path)
    with pytest.raises(ValueError):restore_backup(replace(config,secret='wrong-secret'*8),str(backup.relative_to(config.data_dir)))
    assert sha256_file(e.db.path)==before

@pytest.mark.parametrize('filename',['../other.sqlite3','/etc/passwd','autoucbot.sqlite3','missing.sqlite3'])
def test_restore_path_guards(e,config,filename):
    with pytest.raises(ValueError):restore_backup(config,filename)

def test_restore_checksum_rejected(e,config):
    backup=e.db.backup();Path(str(backup)+'.sha256').write_text('badchecksum')
    with pytest.raises(ValueError):restore_backup(config,str(backup.relative_to(config.data_dir)))

def test_restore_cannot_run_with_worker_lock(e,config):
    backup=e.db.backup();lock=InstanceLock(e.db.data_dir/'instance.lock');lock.acquire()
    try:
        with pytest.raises(RuntimeError):restore_backup(config,str(backup.relative_to(config.data_dir)))
    finally:lock.release()

@pytest.mark.parametrize('mount',['','/not-data'])
def test_railway_requires_real_volume(monkeypatch,mount):
    monkeypatch.setenv('APP_SECRET','secret'*10);monkeypatch.setenv('RAILWAY_ENVIRONMENT_ID','test-env');monkeypatch.setenv('DATA_DIR','/data')
    monkeypatch.setenv('RAILWAY_VOLUME_MOUNT_PATH',mount)
    with pytest.raises(ValueError):Config.from_env()

def test_railway_volume_config(monkeypatch):
    monkeypatch.setenv('APP_SECRET','secret'*10);monkeypatch.setenv('RAILWAY_ENVIRONMENT_ID','test-env');monkeypatch.setenv('DATA_DIR','/data');monkeypatch.setenv('RAILWAY_VOLUME_MOUNT_PATH','/data')
    assert Config.from_env().data_dir==Path('/data')

def test_reset_demo_after_unknown_keeps_users_settings(e,ready):
    e.db.set('demo_scenario','unknown_before');e.prepare(ready);e.db.set('confirm_uid',False)
    assert e.reset_demo('owner')==1
    assert not e.db.one('SELECT * FROM orders') and not e.db.one('SELECT * FROM batches')
    assert e.db.one('SELECT username FROM users')['username']=='owner'
    assert not e.db.setting('confirm_uid') and e.db.setting('paused')
    assert e.db.setting('demo_scenario')=='success'
    e.seed_demo();assert e.db.one('SELECT state FROM orders')['state']=='awaiting_uid'

def test_reset_demo_cannot_touch_live(e,ready):
    e.db.execute("UPDATE orders SET mode='live'");e.db.execute("UPDATE products SET mode='live'")
    assert e.reset_demo('owner')==0
    assert e.db.one('SELECT id FROM orders')['id']==ready and e.db.one('SELECT * FROM products')

def test_reset_live_forbidden(e):
    e.db.set('mode','live')
    with pytest.raises(BusinessError):e.reset_demo('owner')
