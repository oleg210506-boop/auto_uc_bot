from __future__ import annotations
import re
import string
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

class BusinessError(ValueError): pass

def cents(value) -> int:
    try:
        d = Decimal(str(value).replace(",", "."))
        if not d.is_finite() or abs(d) > Decimal("1000000000"): raise ValueError()
        return int((d*100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError): raise BusinessError("Некорректная сумма") from None

def money(value):
    if value is None: return "—"
    return f"{Decimal(int(value))/100:,.2f}".replace(",", " ") + " ₽"

def positive_int(value, maximum=10000):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)): raise BusinessError("Требуется целое положительное число")
    n = int(value)
    if n < 1 or n > maximum: raise BusinessError(f"Число должно быть от 1 до {maximum}")
    return n

def validate_uid(uid):
    uid = str(uid).strip()
    if not re.fullmatch(r"[0-9]{6,15}", uid): raise BusinessError("UID должен содержать от 6 до 15 цифр")
    return uid

ALLOWED_VARS = {"order_id", "uc", "quantity", "uid", "code", "orders", "buyer"}
def validate_template(text):
    if not text or len(text) > 3000: raise BusinessError("Текст сообщения должен содержать 1–3000 символов")
    try:
        for _, key, spec, conversion in string.Formatter().parse(text):
            if key is not None and (key not in ALLOWED_VARS or spec or conversion):
                raise BusinessError("Неизвестная переменная шаблона: " + str(key))
    except ValueError as e: raise BusinessError(str(e)) from None
    return text

def render_template(text, **values):
    validate_template(text)
    return text.format_map({key: str(values.get(key, "")) for key in ALLOWED_VARS})

def day_start(now, timezone):
    dt = datetime.fromtimestamp(now, ZoneInfo(timezone))
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

def csv_safe(value):
    s = "" if value is None else str(value)
    return "'"+s if s.lstrip().startswith(("=", "+", "-", "@")) or s.startswith(("\t", "\r", "\n")) else s

def status_name(obj):
    return str(getattr(obj, "name", obj)).split(".")[-1].lower()
