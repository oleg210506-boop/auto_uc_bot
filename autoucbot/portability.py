"""Authenticated offline migration; no secrets are exposed in plain text.

An exclusive engine lock freezes the source before the SQLite online-backup API
is used. Local flock does NOT fence a second host: the owner must stop the source
before unfreezing a restored target. Keeping that operational gate explicit is
safer than claiming a copied SQLite file has a distributed lock.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import zipfile
from . import __version__
from .config import SECRET_ENV
from .db import dumps
from .maintenance import sha256_file, restore_backup
from .utils import BusinessError

MAX_DATABASE = 2 * 1024**3
FILES = {'manifest.json', 'manifest.hmac', 'database.sqlite3'}


def signature(raw: bytes, secret: str) -> str:
    key = hashlib.sha256((secret + ':migration:v1').encode()).digest()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def create_export(engine, actor: str) -> Path:
    with engine.lock:
        db = engine.db
        # Never expose a source backup which continues to place purchases.
        db.set('migration_frozen', True)
        db.set('migration_source', engine.config.public_url)
        db.set('paused', True)
        db.set('live_armed', False)
        db.set('pause_reason', 'Миграция: источник заморожен. Остановите его перед запуском копии.')
        with db.tx() as c:
            for name in SECRET_ENV:
                value = engine.vault.get(name)
                if value:
                    c.execute('INSERT INTO secrets VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                              (name, engine.vault.encrypt(value)))
            db.audit(actor, 'migration.freeze', detail='Новые закупки и автоответы остановлены; создан переносимый снимок', conn=c)
        snapshot = db.backup(db.setting('backup_keep'))
        folder = db.data_dir / 'transfers'
        folder.mkdir(exist_ok=True)
        target = folder / ('autoUCbot-transfer-' + time.strftime('%Y%m%d-%H%M%S',time.gmtime()) + '-' + uuid.uuid4().hex[:8] + '.zip')
        manifest = {'format': 1, 'version': __version__, 'schema': 2,
                    'installation_id': db.one("SELECT value FROM meta WHERE key='installation_id'")['value'],
                    'created': time.time(), 'source_url': engine.config.public_url,
                    'database_sha256': sha256_file(snapshot), 'database_bytes': snapshot.stat().st_size,
                    'app_secret_included': False, 'source_frozen': True}
        raw = dumps(manifest).encode()
        partial = target.with_suffix('.partial')
        try:
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
                z.write(snapshot,'database.sqlite3')
                z.writestr('manifest.json',raw)
                z.writestr('manifest.hmac',signature(raw,engine.config.secret))
            os.chmod(partial,0o600)
            os.replace(partial,target)
        finally:
            partial.unlink(missing_ok=True)
        return target


def import_export(config, archive: str | Path) -> bool:
    """Import only on a stopped installation, with its ORIGINAL APP_SECRET."""
    archive = Path(archive)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names)!=3 or set(names)!=FILES:
            raise ValueError('Архив содержит неожиданные файлы. Нужен экспорт из панели, не ZIP исходников.')
        if z.getinfo('manifest.json').file_size>32768 or z.getinfo('manifest.hmac').file_size!=64:
            raise ValueError('Неверный манифест миграции')
        raw = z.read('manifest.json')
        supplied = z.read('manifest.hmac').decode('ascii')
        if not hmac.compare_digest(signature(raw,config.secret),supplied):
            raise ValueError('Неверный APP_SECRET или архив изменён. База не заменена.')
        manifest = json.loads(raw)
        if manifest.get('format')!=1 or manifest.get('schema')!=2 or manifest.get('source_frozen') is not True:
            raise ValueError('Версия/состояние архива не поддерживается')
        info = z.getinfo('database.sqlite3')
        if not 0<info.file_size<=MAX_DATABASE or info.file_size!=manifest.get('database_bytes'):
            raise ValueError('Недопустимый размер базы')
        root=config.data_dir.resolve();folder=root/'imports';folder.mkdir(parents=True,exist_ok=True)
        name='transfer-'+uuid.uuid4().hex+'.sqlite3';target=folder/name
        try:
            # No extractall: fixed output path, streaming copy, authenticated size/hash.
            with z.open('database.sqlite3') as source, target.open('xb') as out:
                shutil.copyfileobj(source,out,length=1024*1024)
            os.chmod(target,0o600)
            if sha256_file(target)!=manifest.get('database_sha256'):
                raise ValueError('Контрольная сумма базы не совпала. Импорт отменён.')
            return restore_backup(config,str(target.relative_to(root)))
        finally:
            target.unlink(missing_ok=True)


def transfer_path(db, name):
    if not re.fullmatch(r'autoUCbot-transfer-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\.zip',name):
        raise BusinessError('Неверное имя архива')
    path=db.data_dir/'transfers'/name
    if not path.is_file():raise BusinessError('Архив не найден')
    return path
