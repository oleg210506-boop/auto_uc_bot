"""Offline recovery. Never restore a database underneath a running worker."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from .config import Config
from .db import DB
from .security import Vault
from .worker import InstanceLock


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def restore_backup(config: Config, filename: str) -> bool:
    """Restore an owner's complete SQLite backup, once per content hash.

    Accept only files inside DATA_DIR. The caller must keep the original APP_SECRET.
    After restoration all financial writes are disabled until explicit reconciliation.
    Returns False for an already applied backup. This is not a merge or a rollback tool.
    """
    root=config.data_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    relative=Path(filename)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Имя копии должно быть относительным путём внутри DATA_DIR.')
    source=(root/relative).resolve()
    if not source.is_relative_to(root) or not source.is_file() or source.name=='autoucbot.sqlite3':
        raise ValueError('Копия не найдена или выбран рабочий файл базы.')
    digest=sha256_file(source)
    sidecar=Path(str(source)+'.sha256')
    if sidecar.exists() and sidecar.read_text().split()[0]!=digest:
        raise ValueError('SHA256 копии не совпадает. Рабочая база не изменена.')
    marker=root/('.restored-'+digest)
    if marker.exists():return False
    lock=InstanceLock(root/'instance.lock');lock.acquire()
    temp=root/('restore-'+uuid.uuid4().hex+'.sqlite3')
    try:
        # immutable is valid for complete backups; never use a live DB/WAL as the input.
        with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1',uri=True)) as src:
            if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Копия повреждена.')
            if src.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()!=('1',):
                raise ValueError('Версия базы не поддерживается этим выпуском.')
            check=src.execute("SELECT value FROM meta WHERE key='vault_check'").fetchone()
            if not check:raise ValueError('Копия не содержит контрольной записи шифрования.')
            # Validate the original master key without creating or modifying any database.
            import base64
            from cryptography.fernet import Fernet
            cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256((config.secret+':vault:v1').encode()).digest()))
            try:
                if cipher.decrypt(check[0].encode()).decode()!='autoUCbot-vault-v1':raise ValueError()
            except Exception:raise ValueError('Нужен первоначальный APP_SECRET из этой копии.') from None
            with contextlib.closing(sqlite3.connect(temp)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Нарушена целостность связей в копии.')
                for key,value in {'mode':'observe','paused':True,'live_armed':False,'recovery_required':True,
                                  'pause_reason':'Восстановление: сначала сверьте историю у поставщика и FunPay',
                                  'balance_verified:live':False}.items():
                    dst.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,ensure_ascii=False)))
                dst.execute('DELETE FROM sessions')
                dst.execute("UPDATE batches SET state='unknown',next_check=0 WHERE state='sending'")
                dst.execute("UPDATE outbox SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'cancelled' END WHERE state IN('sending','pending')")
                dst.execute("UPDATE tasks SET state='error',result='Восстановление базы: действие отменено' WHERE state IN('pending','running')")
                dst.execute("UPDATE orders SET state='manual',confirmed=0,hold_reason='Восстановление: нужна сверка истории' WHERE state NOT IN('completed','cancelled','failed','partial') AND NOT EXISTS(SELECT 1 FROM batches b WHERE b.order_id=orders.id)")
                dst.execute('INSERT INTO audit(actor,action,entity,detail,created) VALUES(?,?,?,?,?)',('recovery','database.restored',digest,'Автозакупки отключены, требуется сверка',time.time()))
                dst.commit();dst.execute('PRAGMA journal_mode=DELETE')
        current=root/'autoucbot.sqlite3'
        if current.exists():
            old=DB(root);Vault(old,config.secret)
            old.backup(20)
            with contextlib.closing(old.connect()) as c:
                result=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if result and result[0]!=0:raise ValueError('База занята. Остановите другой процесс.')
        for suffix in ('-wal','-shm'):
            Path(str(current)+suffix).unlink(missing_ok=True)
        os.chmod(temp,0o600);os.replace(temp,current)
        marker.write_text(str(time.time())+'\n');os.chmod(marker,0o600)
        return True
    finally:
        temp.unlink(missing_ok=True);lock.release()


def restore_from_env(config: Config):
    name=os.getenv('RESTORE_BACKUP_FILE','').strip()
    if name:
        changed=restore_backup(config,name)
        print('autoUCbot: backup restored; reconciliation required.' if changed else 'autoUCbot: backup already applied; remove RESTORE_BACKUP_FILE.',flush=True)
