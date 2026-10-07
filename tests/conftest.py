import socket
import pytest
from pathlib import Path
from autoucbot.config import Config
from autoucbot.web import create_app
from autoucbot.db import DB
from autoucbot.security import Vault
from autoucbot.engine import Engine

@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def blocked(*args,**kwargs):raise AssertionError('Offline test attempted a network connection')
    monkeypatch.setattr(socket,'create_connection',blocked)

@pytest.fixture
def config(tmp_path):return Config(tmp_path,'test-only-secret-'*4,bootstrap_password='Test-password-12!',secure_cookie=False,start_worker=False)
@pytest.fixture
def app(config):return create_app(config)
@pytest.fixture
def e(app):return app.state.engine
@pytest.fixture
def ready(e):
    oid=e.seed_demo();e.db.set('paused',False)
    e.input_message({'id':'101','author':2,'chat_id':'users-1-2','text':'5123456789'})
    o=e.db.one('SELECT * FROM orders WHERE id=?',(oid,))
    e.input_message({'id':'102','author':2,'chat_id':'users-1-2','text':'ПОДТВЕРЖДАЮ '+o['confirm_code']})
    return oid
