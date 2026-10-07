from __future__ import annotations
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from urllib.parse import quote
from cryptography.fernet import Fernet
from .config import SECRET_ENV

class Vault:
    def __init__(self, db, master):
        self.db = db
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256((master + ":vault:v1").encode()).digest()))
        check = self.db.one("SELECT value FROM meta WHERE key='vault_check'")
        if check:
            try:
                if self.decrypt(check["value"]) != "autoUCbot-vault-v1": raise ValueError()
            except Exception:
                raise ValueError("APP_SECRET не соответствует этой базе. Верните первоначальный APP_SECRET; не удаляйте базу.") from None
        else:
            self.db.execute("INSERT INTO meta VALUES('vault_check',?)", (self.encrypt("autoUCbot-vault-v1"),))
    def encrypt(self, value): return self.fernet.encrypt(value.encode()).decode()
    def decrypt(self, value): return self.fernet.decrypt(value.encode()).decode()
    def get(self, key):
        env = os.getenv(SECRET_ENV.get(key, "_UNUSED_"))
        if env: return env
        row = self.db.one("SELECT value FROM secrets WHERE key=?", (key,))
        return self.decrypt(row["value"]) if row else ""
    def set(self, key, value):
        if key not in SECRET_ENV: raise ValueError("Неизвестный секрет")
        if os.getenv(SECRET_ENV[key]): raise ValueError("Это значение задано в Railway Variables; измените его там.")
        if value: self.db.execute("INSERT INTO secrets VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, self.encrypt(value)))
        else: self.db.execute("DELETE FROM secrets WHERE key=?", (key,))


def validate_password(password: str):
    if not 12 <= len(password) <= 128: raise ValueError("Пароль: от 12 до 128 символов.")
    if password.lower() in {"password12345", "123456789012", "qwerty123456"}: raise ValueError("Слишком простой пароль.")

def password_hash(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    out = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + out.hex()

def verify_password(password, hashed):
    if len(password) > 128: return False
    try:
        algo, salt, digest = hashed.split("$")
        if algo != "scrypt": return False
        out = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(out.hex(), digest)
    except (ValueError, TypeError): return False

def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()

def new_totp_secret(): return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

def totp_at(secret, timestamp=None, digits=6):
    counter = int(time.time() if timestamp is None else timestamp) // 30
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset:offset+4])[0] & 0x7fffffff
    return str(value % (10 ** digits)).zfill(digits)

def verify_totp(secret, code, last_step=-1, now=None):
    if not re.fullmatch(r"[0-9]{6}", code or ""): return None
    now = time.time() if now is None else now
    step = int(now) // 30
    for candidate in (step, step-1, step+1):
        if candidate > last_step and candidate >= 0 and hmac.compare_digest(totp_at(secret, candidate*30), code):
            return candidate
    return None

def provisioning_uri(secret, username):
    return f"otpauth://totp/{quote('autoUCbot:'+username)}?secret={secret}&issuer=autoUCbot&algorithm=SHA1&digits=6&period=30"

def webhook_valid(body: bytes, headers, secret: str, now=None):
    if not secret: return False
    try:
        stamp = int(headers.get("x-webhook-timestamp", ""))
    except (ValueError, TypeError): return False
    if abs((time.time() if now is None else now)-stamp) > 300: return False
    expected = "sha256=" + hmac.new(secret.encode(), str(stamp).encode()+b"."+body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers.get("x-webhook-signature", ""))
