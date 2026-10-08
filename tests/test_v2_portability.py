import dataclasses
import hashlib
import hmac
import io
import json
from pathlib import Path
import sqlite3
import zipfile
import pytest
from autoucbot.config import Config
from autoucbot.db import DB
from autoucbot.security import Vault
from autoucbot.engine import Engine
from autoucbot.worker import Worker,InstanceLock
from autoucbot.portability import create_export,import_export,signature,transfer_path
from autoucbot.utils import BusinessError
from test_fazer_orders import ready_fazer,finish,row


def target(config,tmp_path):return dataclasses.replace(config,data_dir=tmp_path/'destination',public_url='https://new.example.org')

def test_export_import_preserves_financial_state_and_secrets(e,config,tmp_path):
    a=ready_fazer(e,'uc',3,60);finish(e,a)
    b=ready_fazer(e);e.db.set('demo_scenario','unknown_after');e.prepare(b)
    e.vault.set('fazer_key','secret-key-not-in-manifest')
    e.db.set('svc_stars_reminder_seconds',123)
    archive=create_export(e,'owner')
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist())=={'manifest.json','manifest.hmac','database.sqlite3'}
        assert config.secret.encode() not in z.read('manifest.json')
        assert b'secret-key-not-in-manifest' not in z.read('database.sqlite3')
    t=target(config,tmp_path);assert import_export(t,archive)
    db=DB(t.data_dir);v=Vault(db,t.secret);other=Engine(db,v,t)
    assert v.get('fazer_key')=='secret-key-not-in-manifest'
    assert db.setting('svc_stars_reminder_seconds')==123
    assert row(other,a)['delivered']==180 and row(other,b)['state']=='unknown'
    assert db.setting('mode')=='observe' and db.setting('paused') is True
    assert db.setting('migration_frozen') is True and db.setting('recovery_required') is True
    assert not db.setting('live_armed') and not db.one('SELECT * FROM sessions')
    assert db.one('SELECT COUNT(*) n FROM fazer_parts')['n']==4
    assert not import_export(t,archive) # same file only once
    assert db.one('SELECT username FROM users')['username']=='owner'


def test_export_captures_env_secret_encrypted(e,monkeypatch,config,tmp_path):
    monkeypatch.setenv('FAZER_API_KEY','fazer-only-env-value')
    archive=create_export(e,'owner');monkeypatch.delenv('FAZER_API_KEY')
    t=target(config,tmp_path);import_export(t,archive)
    assert Vault(DB(t.data_dir),t.secret).get('fazer_key')=='fazer-only-env-value'


def test_wrong_secret_import_refused_before_writing(e,config,tmp_path):
    archive=create_export(e,'owner');t=dataclasses.replace(target(config,tmp_path),secret='wrong-secret-'*6)
    with pytest.raises(ValueError,match='APP_SECRET'):import_export(t,archive)
    assert not (t.data_dir/'autoucbot.sqlite3').exists()

@pytest.mark.parametrize('mutation',['db','manifest','signature','extra','traversal','duplicates'])
def test_modified_archive_refused(e,config,tmp_path,mutation):
    archive=create_export(e,'owner')
    with zipfile.ZipFile(archive) as z:payload={n:z.read(n) for n in z.namelist()}
    if mutation=='db':payload['database.sqlite3']=payload['database.sqlite3'][:-1]+bytes([payload['database.sqlite3'][-1]^1])
    elif mutation=='manifest':payload['manifest.json']+=b' '
    elif mutation=='signature':payload['manifest.hmac']=b'0'*64
    elif mutation=='extra':payload['extra.txt']=b'file'
    elif mutation=='traversal':payload['../../escape.txt']=b'file'
    corrupted=tmp_path/'bad.zip'
    with zipfile.ZipFile(corrupted,'w') as z:
        for n,b in payload.items():z.writestr(n,b)
        if mutation=='duplicates':
            with pytest.warns(UserWarning):z.writestr('manifest.json',payload['manifest.json'])
    with pytest.raises(ValueError):import_export(target(config,tmp_path),corrupted)
    assert not (tmp_path/'escape.txt').exists()


def test_source_freeze_stops_background_operations(e,monkeypatch):
    ready_fazer(e);create_export(e,'owner')
    def fail():raise AssertionError('A frozen worker scheduled work')
    w=Worker(e);monkeypatch.setattr(w,'tasks',fail);monkeypatch.setattr(w,'purchases',fail);monkeypatch.setattr(w,'funpay',fail)
    w.tick();assert not e.db.setting('demo-fazer-counter',0)


def test_import_refused_while_worker_owns_disk(e,config,tmp_path):
    archive=create_export(e,'owner');t=target(config,tmp_path);t.data_dir.mkdir()
    lock=InstanceLock(t.data_dir/'instance.lock');lock.acquire()
    try:
        with pytest.raises(RuntimeError):import_export(t,archive)
    finally:lock.release()
    assert not (t.data_dir/'autoucbot.sqlite3').exists()


def test_sending_is_unknown_after_migration_never_prepared(e,config,tmp_path):
    oid=ready_fazer(e);e.db.set('demo_scenario','pending');e.prepare(oid)
    e.db.execute("UPDATE fazer_parts SET state='sending'");e.db.execute("UPDATE batches SET state='sending'")
    t=target(config,tmp_path);import_export(t,create_export(e,'owner'));db=DB(t.data_dir)
    assert db.one('SELECT state FROM fazer_parts')['state']=='unknown'
    assert db.one('SELECT state FROM batches')['state']=='unknown'


def test_schema1_upgrade_keeps_legacy_orders_disarms_and_backs_up(e,config):
    oid=e.seed_demo();e.db.execute("UPDATE orders SET mode='live'");e.db.execute("UPDATE products SET mode='live',enabled=1,verified=1")
    e.db.set('mode','live');e.db.set('live_armed',True);e.db.set('paused',False)
    with e.db.tx() as c:
        c.execute('DROP INDEX orders_service')
        for t in ('fazer_parts','fazer_wallet','fazer_skus'):c.execute('DROP TABLE '+t)
        for t in ('products','orders'):
            c.execute('ALTER TABLE '+t+' DROP COLUMN service');c.execute('ALTER TABLE '+t+' DROP COLUMN supplier')
        c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    db=DB(config.data_dir)
    assert db.one("SELECT value FROM meta WHERE key='schema_version'")['value']=='2'
    assert db.one('SELECT supplier FROM orders')['supplier']=='gamecore'
    assert db.one('SELECT id FROM orders')['id']==oid
    assert db.one('SELECT enabled FROM products')['enabled']==0
    assert db.setting('mode')=='observe' and db.setting('paused') and not db.setting('live_armed')
    assert list((config.data_dir/'backups').glob('*.sqlite3'))
    assert Vault(db,config.secret).get('fazer_key')==''

@pytest.mark.parametrize('name',['../secret.zip','autoUCbot-transfer-20260101-000000-abcdefab.zip/../secret','/etc/passwd','bad.zip'])
def test_download_path_strict(e,name):
    with pytest.raises(BusinessError):transfer_path(e.db,name)
