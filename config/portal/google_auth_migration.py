"""
Парсер формата экспорта Google Authenticator (otpauth-migration://).

Google Authenticator при экспорте кодирует пачку аккаунтов в один QR:

    otpauth-migration://offline?data=<base64( protobuf MigrationPayload )>

Здесь — самодостаточный разбор protobuf wire-формата без внешней
зависимости (protobuf-runtime не требуется). Опирается на открытое
описание формата:
  https://github.com/brookst/otpauth_migrate
  https://alexbakker.me/post/parsing-google-auth-export-qr-code.html

.proto (для справки):
    message MigrationPayload {
      message OtpParameters {
        bytes  secret    = 1;   // «сырой» секрет (НЕ base32)
        string name      = 2;   // имя аккаунта (часто "issuer:account")
        string issuer    = 3;
        Algorithm algorithm = 4; // 1=SHA1 2=SHA256 3=SHA512 4=MD5
        DigitCount digits   = 5; // 1=SIX 2=EIGHT
        OtpType type        = 6; // 1=HOTP 2=TOTP
        int64  counter   = 7;
      }
      repeated OtpParameters otp_parameters = 1;
      int32 version = 2; ...
    }
"""

import base64
import binascii
from urllib.parse import urlparse, parse_qs, unquote

_ALGO = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512", 4: "MD5"}
_DIGITS = {0: 6, 1: 6, 2: 8}


# ── protobuf wire-format (минимальный ридер) ──────────────────
def _read_varint(buf: bytes, i: int):
    """Возвращает (значение, новый_индекс). Бросает ValueError при обрыве."""
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("protobuf: неожиданный конец varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("protobuf: слишком длинный varint")


def _iter_fields(buf: bytes):
    """Итерирует (номер_поля, wire_type, значение) по сообщению protobuf.
       Для wire 0 значение — int (varint), для wire 2 — bytes."""
    i = 0
    n = len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field = key >> 3
        wt = key & 0x07
        if wt == 0:                       # varint
            val, i = _read_varint(buf, i)
            yield field, wt, val
        elif wt == 2:                     # length-delimited (bytes/string/msg)
            ln, i = _read_varint(buf, i)
            if i + ln > n:
                raise ValueError("protobuf: длина выходит за буфер")
            yield field, wt, buf[i:i + ln]
            i += ln
        elif wt == 5:                     # 32-bit
            yield field, wt, buf[i:i + 4]
            i += 4
        elif wt == 1:                     # 64-bit
            yield field, wt, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"protobuf: неподдерживаемый wire type {wt}")


def _parse_otp_parameters(buf: bytes) -> dict | None:
    """Разбирает одно сообщение OtpParameters → dict или None (если не TOTP)."""
    secret = b""
    name = ""
    issuer = ""
    algorithm = "SHA1"
    digits = 6
    otp_type = 2                          # по умолчанию TOTP
    for field, wt, val in _iter_fields(buf):
        if field == 1 and wt == 2:
            secret = val
        elif field == 2 and wt == 2:
            name = val.decode("utf-8", "replace")
        elif field == 3 and wt == 2:
            issuer = val.decode("utf-8", "replace")
        elif field == 4 and wt == 0:
            algorithm = _ALGO.get(val, "SHA1")
        elif field == 5 and wt == 0:
            digits = _DIGITS.get(val, 6)
        elif field == 6 and wt == 0:
            otp_type = val
    if not secret:
        return None
    if otp_type == 1:                     # HOTP — портал работает только с TOTP
        return None

    # name часто имеет вид "Issuer:account" — растащим, если issuer пуст
    acc = name
    if not issuer and ":" in name:
        issuer, _, acc = name.partition(":")
    issuer = issuer.strip()
    acc = acc.strip()

    b32 = base64.b32encode(secret).decode("ascii").rstrip("=")
    return {
        "issuer": issuer or acc or "Без названия",
        "account": acc,
        "secret": b32,
        "algorithm": algorithm,
        "digits": digits,
    }


def parse_google_migration(uri: str) -> list[dict]:
    """
    uri: 'otpauth-migration://offline?data=XXXXX'
    Возвращает: [{ secret(base32), issuer, account, algorithm, digits }, …]

    Бросает ValueError с понятным текстом, если это не migration-URI или
    payload битый.
    """
    uri = (uri or "").strip()
    if not uri.startswith("otpauth-migration://"):
        raise ValueError("Это не ссылка экспорта Google Authenticator")

    parsed = urlparse(uri)
    qs = parse_qs(parsed.query)
    data_vals = qs.get("data")
    if not data_vals:
        raise ValueError("В ссылке нет параметра data")

    data = unquote(data_vals[0])
    # base64 (стандартный алфавит, с возможным дополнением до кратности 4)
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("Не удалось декодировать data (base64)")

    out: list[dict] = []
    for field, wt, val in _iter_fields(raw):
        if field == 1 and wt == 2:        # repeated OtpParameters
            acc = _parse_otp_parameters(val)
            if acc:
                out.append(acc)
    if not out:
        raise ValueError("В QR не найдено ни одного TOTP-аккаунта")
    return out
