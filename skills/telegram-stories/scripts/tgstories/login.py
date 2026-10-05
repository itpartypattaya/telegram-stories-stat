"""Separate Telegram session for the stories service.

Why a separate login: see README "Why a separate session". In short — the
service is the only part that may write to people on its own; its own session
can be terminated from the phone (Settings → Devices → "Hermes Stories")
without touching any other session, and two long-running programs never share
one auth key.

The one-time code and the cloud password are typed by the owner in a terminal.
A cloud password typed into a chat with an agent would stay in the agent's
history and reach the model provider, so the agent-driven two-step mode refuses
to take it.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys

from . import config, db, tg

PENDING = "login-pending.json"


def _digits(code: str) -> str:
    return re.sub(r"\D", "", code or "")


def _ensure_api_credentials(interactive: bool) -> None:
    api_id, _ = config.api_credentials()
    if api_id:
        return
    if not interactive:
        raise SystemExit("no STORIES_API_ID / STORIES_API_HASH — get them at https://my.telegram.org "
                         "(API development tools) and run `stories.py login` in a terminal")
    print("Telegram API credentials are needed once (https://my.telegram.org → API development tools).")
    api_id = input("api_id: ").strip()
    api_hash = input("api_hash: ").strip()
    if not api_id.isdigit() or not api_hash:
        raise SystemExit("api_id must be a number and api_hash must not be empty")
    config.write_env_value("STORIES_API_ID", api_id)
    config.write_env_value("STORIES_API_HASH", api_hash)


async def _finish(client, cfg: dict) -> int:
    me = await client.get_me()
    session_env = cfg.get("session_env") or "STORIES_SESSION_STRING"
    config.write_env_value(session_env, client.session.save())
    con = db.connect()
    db.set_meta(con, "owner_id", me.id)
    db.set_meta(con, "owner_premium", int(bool(getattr(me, "premium", False))))
    db.set_meta(con, "login_at", db.now())
    con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET title=excluded.title, username=excluded.username",
                (me.id, "self", " ".join(x for x in (me.first_name, me.last_name) if x), me.username, db.now()))
    name = " ".join(x for x in (me.first_name, me.last_name) if x)
    print(f"Logged in as {name}" + (f" (@{me.username})" if me.username else "") +
          f". Premium: {'yes' if getattr(me, 'premium', False) else 'no'}.")
    print(f"The session is saved as {session_env} in {config.env_file()} (mode 600).")
    print(f"In Telegram it is listed as \"{tg.DEVICE_MODEL}\" in Settings → Devices; terminate it there to "
          "cut the service off instantly.")
    if not getattr(me, "premium", False):
        print("Note: without Telegram Premium, viewer lists disappear 24 h after a story expires — "
              "history can't be imported, only collected live from now on.")
    return 0


def describe_sent(sent) -> str:
    """Where Telegram actually delivered the code (it is not always the app)."""
    kind = type(getattr(sent, "type", None)).__name__
    t = getattr(sent, "type", None)
    where = {
        "SentCodeTypeApp": "to the Telegram app — the chat named \"Telegram\" (with the blue check) on your phone",
        "SentCodeTypeSms": "by SMS",
        "SentCodeTypeSmsWord": "by SMS (a word, type it as is)",
        "SentCodeTypeSmsPhrase": "by SMS (a phrase, type it as is)",
        "SentCodeTypeFirebaseSms": "by SMS",
        "SentCodeTypeCall": "by a phone call (the code is spoken)",
        "SentCodeTypeFlashCall": "as a missed call — the code is the last digits of the calling number",
        "SentCodeTypeMissedCall": "as a missed call — the code is the last digits of the calling number",
        "SentCodeTypeFragmentSms": "via Fragment (fragment.com) — the anonymous number's inbox",
        "SentCodeTypeEmailCode": "by e-mail" + (f" to {getattr(t, 'email_pattern', '')}" if getattr(t, "email_pattern",
                                                                                                  None) else ""),
        "SentCodeTypeSetUpEmailRequired": "nowhere yet: Telegram asks to set up a login e-mail first "
                                          "(do it in the Telegram app, then retry)",
    }.get(kind, f"({kind})")
    nxt = type(getattr(sent, "next_type", None)).__name__
    extra = ""
    if nxt and nxt != "NoneType":
        extra = " If it does not arrive, type `resend` to get it " + {
            "CodeTypeSms": "by SMS", "CodeTypeCall": "by a call", "CodeTypeFlashCall": "as a missed call",
            "CodeTypeMissedCall": "as a missed call", "CodeTypeFragmentSms": "via Fragment"}.get(nxt, "another way") + "."
    return f"Telegram sent the login code {where}.{extra}"


async def interactive(cfg: dict, phone: str | None = None) -> int:
    from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
    if not sys.stdin.isatty():
        raise SystemExit("interactive login needs a terminal (run it over ssh -t, or in a local terminal)")
    import logging
    logging.getLogger("telethon").setLevel(logging.ERROR)  # data-centre switch chatter only confuses people
    _ensure_api_credentials(True)
    client = tg.new_client()
    await client.connect()
    try:
        phone = (phone or input("Phone number in international format (+...): ")).strip()
        if phone and not phone.startswith("+"):
            phone = "+" + _digits(phone)
        sent = await client.send_code_request(phone)
        print(describe_sent(sent))
        for attempt in range(6):
            raw = input("Code (or `resend`): ").strip()
            # a terminal without a UTF-8 locale hands over undecodable bytes as surrogates
            raw = raw.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
            if raw.lower() in ("resend", "r", "again", "куыутв", "к"):   # also with a Russian keyboard layout
                from telethon.tl.functions.auth import ResendCodeRequest
                sent = await client(ResendCodeRequest(phone_number=phone, phone_code_hash=sent.phone_code_hash))
                print(describe_sent(sent))
                continue
            if not _digits(raw) and not raw.isascii():
                print("Type the digits of the code, or `resend`.")
                continue
            code = _digits(raw) or raw   # SMS "word"/"phrase" codes are typed as is
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                break
            except SessionPasswordNeededError:
                for _ in range(3):
                    pw = getpass.getpass("Cloud password (two-step verification, input hidden): ")
                    try:
                        await client.sign_in(password=pw)
                        break
                    except Exception as exc:  # noqa: BLE001 — PasswordHashInvalidError and friends
                        print(f"Password not accepted: {type(exc).__name__}")
                else:
                    raise SystemExit("cloud password rejected three times")
                break
            except PhoneCodeInvalidError:
                print("Wrong code, try again.")
            except PhoneCodeExpiredError:
                raise SystemExit("the code expired — run login again (do not send the code through any chat)")
        else:
            raise SystemExit("too many wrong codes")
        if not await client.is_user_authorized():
            raise SystemExit("login did not complete")
        return await _finish(client, cfg)
    finally:
        await client.disconnect()


def _print_qr(url: str) -> None:
    try:
        import qrcode  # optional; install.py --install-deps adds it
    except ImportError:
        print("(install the `qrcode` package to draw the QR here — or turn this link into a QR code)")
        print(url)
        return
    code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(url)
    code.make(fit=True)
    code.print_ascii(invert=True)


async def qr(cfg: dict) -> int:
    """Log in by scanning a QR code with the phone — no login code needed."""
    import logging
    from telethon.errors import SessionPasswordNeededError
    logging.getLogger("telethon").setLevel(logging.ERROR)
    _ensure_api_credentials(sys.stdin.isatty())
    client = tg.new_client()
    await client.connect()
    try:
        q = await client.qr_login()
        print("On your phone: Telegram → Settings → Devices → Link Desktop Device, and scan this code.")
        for _ in range(8):  # the token rotates about every 30 s
            _print_qr(q.url)
            try:
                await q.wait(timeout=max(10, int((q.expires.timestamp() - __import__("time").time())) - 1))
                break
            except SessionPasswordNeededError:
                if not sys.stdin.isatty():
                    raise SystemExit("two-step verification is on — run `stories.py login --qr` in a terminal")
                for _ in range(3):
                    try:
                        await client.sign_in(password=getpass.getpass("Cloud password (input hidden): "))
                        break
                    except Exception as exc:  # noqa: BLE001
                        print(f"Password not accepted: {type(exc).__name__}")
                else:
                    raise SystemExit("cloud password rejected three times")
                break
            except Exception as exc:  # noqa: BLE001 — asyncio.TimeoutError: token expired
                if type(exc).__name__ not in ("TimeoutError", "CancelledError"):
                    raise
                await q.recreate()
                print("\nThe code expired — here is a fresh one:")
        else:
            raise SystemExit("not scanned in time — run login --qr again")
        if not await client.is_user_authorized():
            raise SystemExit("login did not complete")
        return await _finish(client, cfg)
    finally:
        await client.disconnect()


async def start(cfg: dict, phone: str) -> int:
    """Two-step mode for agents, step 1: ask Telegram to send a code."""
    _ensure_api_credentials(False)
    client = tg.new_client()
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        print(describe_sent(sent))
        path = config.data_dir() / PENDING
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"phone": phone, "hash": sent.phone_code_hash, "session": client.session.save()}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        print("Next: `stories.py login finish --code <digits>`. "
              "Type the code with spaces or dashes (1 2 3 4 5) if it has to pass through a chat.")
        return 0
    finally:
        await client.disconnect()


async def finish(cfg: dict, code: str) -> int:
    """Two-step mode, step 2. Refuses to take a cloud password outside a terminal."""
    from telethon.errors import SessionPasswordNeededError
    path = config.data_dir() / PENDING
    if not path.exists():
        raise SystemExit("no pending login — run `stories.py login start --phone …` first")
    pending = json.loads(path.read_text(encoding="utf-8"))
    client = tg.new_client(pending["session"])
    await client.connect()
    try:
        try:
            await client.sign_in(phone=pending["phone"], code=_digits(code), phone_code_hash=pending["hash"])
        except SessionPasswordNeededError:
            if not sys.stdin.isatty():
                raise SystemExit("two-step verification is on: the cloud password must be typed in a terminal "
                                 "(`stories.py login`), never in a chat")
            await client.sign_in(password=getpass.getpass("Cloud password (input hidden): "))
        path.unlink()
        return await _finish(client, cfg)
    finally:
        await client.disconnect()
