import json
import logging
import socket as _socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import icalendar
import requests
import serial

import config

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/home/mjb/sms-notification-daemon/daemon.log"),
    ],
)
log = logging.getLogger(__name__)

NC_URL = config.NC_URL
USER = config.NC_USER
PASS = config.NC_PASS
ROOMS = config.NC_ROOMS

MODEM_DEVICE = config.MODEM_DEVICE
SMS_RECIPIENT = config.SMS_RECIPIENT

SIGNAL_SOCKET = config.SIGNAL_SOCKET
SIGNAL_ACCOUNT = config.SIGNAL_ACCOUNT

WHATSAPP_BINARY = config.WHATSAPP_BINARY

CALDAV_URL = config.CALDAV_URL
CALDAV_USER = config.CALDAV_USER
CALDAV_PASS = config.CALDAV_PASS
CALDAV_NOTIFY_MINUTES = config.CALDAV_NOTIFY_MINUTES

_headers = {
    "OCS-APIRequest": "true",
    "Accept": "application/json",
}

_sms_lock = threading.Lock()


def _truncate_ucs2(text: str, max_bytes: int = 140) -> str:
    """Kürzt Text so dass die UTF-16 BE Kodierung in max_bytes passt."""
    encoded = text.encode("utf-16-be")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[: max_bytes - 2]  # 2 Bytes für "…" reservieren
    # Nicht mitten in einem Surrogate-Paar schneiden (High Surrogate = D800–DBFF)
    if len(cut) >= 2 and 0xD800 <= int.from_bytes(cut[-2:], "big") <= 0xDBFF:
        cut = cut[:-2]
    return cut.decode("utf-16-be") + "…"


def _build_sms_pdu(recipient: str, text: str) -> tuple[str, int]:
    """Baut ein SMS-SUBMIT PDU mit UCS-2 Kodierung.
    Gibt (pdu_hex, tpdu_länge_in_oktetten) zurück."""
    smsc = "00"  # SMSC aus SIM verwenden
    fo = "11"    # MTI=SUBMIT, VPF=relativ
    mr = "00"    # Message Reference

    digits = recipient.lstrip("+")
    ton_npi = "91" if recipient.startswith("+") else "81"
    padded = digits if len(digits) % 2 == 0 else digits + "F"
    bcd = "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2))
    da = f"{len(digits):02X}{ton_npi}{bcd}"

    pid = "00"
    dcs = "08"   # UCS-2
    vp = "FF"    # maximale Gültigkeit (~63 Wochen, relativ)

    ud_bytes = text.encode("utf-16-be")
    udl = f"{len(ud_bytes):02X}"
    ud = ud_bytes.hex().upper()

    tpdu = fo + mr + da + pid + dcs + vp + udl + ud
    return smsc + tpdu, len(tpdu) // 2


def send_sms(text: str):
    text = _truncate_ucs2(text)
    pdu, tpdu_len = _build_sms_pdu(SMS_RECIPIENT, text)

    with _sms_lock:
        try:
            with serial.Serial(MODEM_DEVICE, baudrate=115200, timeout=5) as modem:
                def at(cmd: str, wait_for: str = "OK", timeout: float = 5.0) -> str:
                    modem.write((cmd + "\r").encode())
                    deadline = time.monotonic() + timeout
                    buf = ""
                    while time.monotonic() < deadline:
                        buf += modem.read(modem.in_waiting or 1).decode(errors="replace")
                        if wait_for in buf:
                            return buf
                    raise TimeoutError(f"AT command {cmd!r} timed out, got: {buf!r}")

                at("AT+CMGF=0")
                at(f"AT+CMGS={tpdu_len}", wait_for=">", timeout=5.0)
                modem.write((pdu + "\x1a").encode("ascii"))
                at("", wait_for="OK", timeout=15.0)
                log.info("SMS gesendet.")
        except Exception as e:
            log.error(f"SMS-Fehler: {e}")


# ----------------------------
# DND State
# ----------------------------

_dnd: dict[str, bool] = {"wa": False, "s": False, "nc": False, "cal": False}
_dnd_lock = threading.Lock()

_DND_LABELS = {"wa": "WA", "s": "Signal", "nc": "NC", "cal": "Cal"}

# Per-sender throttle for WA and Signal: sender -> last SMS time
_throttle: dict[str, datetime] = {}
_throttle_lock = threading.Lock()
_THROTTLE_MINUTES = 60


def _check_throttle(sender: str) -> bool:
    """Return True if an SMS should be sent (not throttled). Updates the throttle."""
    now = datetime.now(timezone.utc)
    with _throttle_lock:
        last = _throttle.get(sender)
        if last is not None and (now - last).total_seconds() < _THROTTLE_MINUTES * 60:
            return False
        _throttle[sender] = now
        return True


def _modem_at(modem, cmd: str, wait_for: str = "OK", timeout: float = 5.0) -> str:
    modem.write((cmd + "\r").encode())
    deadline = time.monotonic() + timeout
    buf = ""
    while time.monotonic() < deadline:
        buf += modem.read(modem.in_waiting or 1).decode(errors="replace")
        if wait_for in buf:
            return buf
    raise TimeoutError(f"AT timed out {cmd!r}: {buf!r}")


def _watch_sms_commands():
    """Poll modem for incoming SMS commands every 60 s."""
    log.info("[sms-cmd] Watcher gestartet.")
    while True:
        time.sleep(60)

        # Step 1: read all messages (lock → open → read → close → release)
        messages = []  # list of (index, body)
        log.debug("[sms-cmd] Polling Modem...")
        try:
            with _sms_lock:
                with serial.Serial(MODEM_DEVICE, baudrate=115200, timeout=5) as modem:
                    _modem_at(modem, 'AT+CPMS="SM","SM","SM"')
                    _modem_at(modem, "AT+CMGF=1")
                    resp = _modem_at(modem, 'AT+CMGL="ALL"', timeout=10.0)
                    log.debug(f"[sms-cmd] CMGL response: {resp!r}")
                    _modem_at(modem, "AT+CMGF=0")

            lines = resp.splitlines()
            i = 0
            while i < len(lines):
                if lines[i].startswith("+CMGL:"):
                    try:
                        idx = int(lines[i].split(":")[1].split(",")[0].strip())
                    except (ValueError, IndexError):
                        idx = None
                    i += 1
                    while i < len(lines) and not lines[i].strip():
                        i += 1
                    if i < len(lines) and lines[i].strip():
                        messages.append((idx, lines[i].strip()))
                i += 1
        except Exception as e:
            log.error(f"[sms-cmd] Lesefehler: {e}")
            continue

        # Step 2: handle commands (send_sms acquires lock freely)
        for _, body in messages:
            _handle_sms_command(body)

        # Step 3: delete processed messages by index
        indices = [idx for idx, _ in messages if idx is not None]
        if indices:
            try:
                with _sms_lock:
                    with serial.Serial(MODEM_DEVICE, baudrate=115200, timeout=5) as modem:
                        for idx in indices:
                            try:
                                _modem_at(modem, f"AT+CMGD={idx}")
                            except Exception:
                                pass
            except Exception as e:
                log.error(f"[sms-cmd] Löschfehler: {e}")


def _handle_sms_command(body: str):
    text = body.strip().lower()
    log.info(f"[sms-cmd] Empfangen: {body!r}")

    # dnd <channel> on|off
    parts = text.split()
    if len(parts) == 3 and parts[0] == "dnd" and parts[1] in _dnd and parts[2] in ("on", "off"):
        channel = parts[1]
        value = parts[2] == "on"
        with _dnd_lock:
            _dnd[channel] = value
        state = "an" if value else "aus"
        log.info(f"[sms-cmd] DND {channel} → {state}")
        send_sms(f"DND {_DND_LABELS[channel]}: {state}")
        return

    if text == "status dnd":
        with _dnd_lock:
            parts = [f"{_DND_LABELS[k]}={'an' if v else 'aus'}" for k, v in _dnd.items()]
        send_sms("DND: " + ", ".join(parts))
        return

    log.debug(f"[sms-cmd] Unbekannter Befehl: {body!r}")


# ----------------------------
# Signal Incoming Watcher
# ----------------------------


def _watch_signal():
    """Connect to signal-cli socket, subscribe for incoming messages, relay via SMS."""
    log.info("[signal] Watcher gestartet.")
    while True:
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.connect(SIGNAL_SOCKET)

            # Subscribe to incoming message notifications
            sub = json.dumps({"jsonrpc": "2.0", "method": "subscribeReceive",
                              "params": {"account": SIGNAL_ACCOUNT}, "id": 1}) + "\n"
            s.sendall(sub.encode())

            buf = ""
            while True:
                chunk = s.recv(4096).decode(errors="replace")
                if not chunk:
                    raise ConnectionError("Socket geschlossen")
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Ignore RPC responses (have "id"), only process notifications
                    if "id" in msg:
                        continue

                    envelope = (msg.get("params") or {}).get("envelope") or {}
                    data_msg = envelope.get("dataMessage") or {}
                    text = (data_msg.get("message") or "").strip()
                    if not text:
                        continue

                    sender = (envelope.get("sourceName")
                              or envelope.get("sourceNumber")
                              or "?")
                    log.info(f"[signal] Nachricht von {sender}: {text}")
                    with _dnd_lock:
                        if _dnd["s"]:
                            continue
                    if not _check_throttle(f"s:{sender}"):
                        log.debug(f"[signal] Gedrosselt: {sender}")
                        continue
                    send_sms(f"S {sender}: {text}")

        except Exception as e:
            log.error(f"[signal] Verbindungsfehler: {e}, reconnect in 30s...")
            time.sleep(30)


# ----------------------------
# WhatsApp Incoming Watcher
# ----------------------------


def _watch_whatsapp():
    """Spawn whatsapp-listener, read JSON lines, relay via SMS."""
    log.info("[whatsapp] Watcher gestartet.")
    while True:
        try:
            proc = subprocess.Popen(
                [WHATSAPP_BINARY],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            log.info(f"[whatsapp] Prozess gestartet (PID {proc.pid})")
            for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sender = evt.get("from", "?")
                text = evt.get("text", "").strip()
                if not text:
                    continue
                log.info(f"[whatsapp] Nachricht von {sender}: {text}")
                with _dnd_lock:
                    if _dnd["wa"]:
                        continue
                if not _check_throttle(f"wa:{sender}"):
                    log.debug(f"[whatsapp] Gedrosselt: {sender}")
                    continue
                send_sms(f"WA {sender}: {text}")

            proc.wait()
            stderr = proc.stderr.read().decode(errors="replace").strip()
            if stderr:
                log.error(f"[whatsapp] stderr: {stderr[-500:]}")
            log.warning(f"[whatsapp] Prozess beendet (rc={proc.returncode}), reconnect in 30s...")
        except Exception as e:
            log.error(f"[whatsapp] Fehler: {e}")
        time.sleep(30)


# ----------------------------
# Talk Chat Long-Polling
# ----------------------------


def _fetch_own_user_id() -> str:
    r = requests.get(
        f"{NC_URL}/ocs/v2.php/cloud/user",
        auth=(USER, PASS),
        headers=_headers,
        params={"format": "json"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"cloud/user HTTP {r.status_code}: {r.text[:200]}")
    uid = ((r.json().get("ocs") or {}).get("data") or {}).get("id")
    if not uid:
        raise RuntimeError("cloud/user: missing ocs.data.id")
    return str(uid)


def _get_last_message_id(token: str) -> int:
    """Fetch the ID of the newest message in the room (for bootstrapping)."""
    url = f"{NC_URL}/ocs/v2.php/apps/spreed/api/v1/chat/{token}"
    try:
        r = requests.get(
            url,
            auth=(USER, PASS),
            headers=_headers,
            params={"format": "json", "lookIntoFuture": 0, "limit": 1},
            timeout=30,
        )
        if r.status_code == 200:
            last_given = r.headers.get("X-Chat-Last-Given")
            if last_given:
                return int(last_given)
            messages = ((r.json().get("ocs") or {}).get("data") or [])
            if messages:
                return max(m.get("id", 0) for m in messages)
    except Exception as e:
        log.error(f"[{token}] Bootstrap-Fehler: {e}")
    return 0


def _watch_room(token: str, my_user_id: str):
    """Long-poll a single Talk room. Runs in its own thread."""
    log.info(f"[{token}] Watcher gestartet, initialisiere...")
    last_id = _get_last_message_id(token)
    last_processed_id = last_id
    log.info(f"[{token}] Starte ab Nachrichten-ID {last_id}")

    url = f"{NC_URL}/ocs/v2.php/apps/spreed/api/v1/chat/{token}"

    while True:
        try:
            r = requests.get(
                url,
                auth=(USER, PASS),
                headers=_headers,
                params={
                    "format": "json",
                    "lookIntoFuture": 1,
                    "lastKnownMessageId": last_id,
                    "limit": 200,
                    "timeout": 0,  # Kein Long-Polling: sofort antworten
                },
                timeout=30,
            )
        except requests.Timeout:
            time.sleep(60)
            continue
        except Exception as e:
            log.error(f"[{token}] Verbindungsfehler: {e}")
            time.sleep(60)
            continue

        # Keine neuen Nachrichten
        if r.status_code == 304:
            time.sleep(60)
            continue

        # Immer X-Chat-Last-Given übernehmen, auch bei Fehlern
        new_last = r.headers.get("X-Chat-Last-Given")
        if new_last:
            last_id = int(new_last)

        if r.status_code != 200:
            log.error(f"[{token}] HTTP {r.status_code}: {r.text[:500]}")
            time.sleep(5)
            continue

        try:
            messages = ((r.json().get("ocs") or {}).get("data") or [])
        except Exception:
            log.error(f"[{token}] Ungültige JSON-Antwort")
            continue

        for msg in messages:
            msg_id = msg.get("id", 0)
            if msg_id and msg_id <= last_processed_id:
                log.debug(f"[{token}] Überspringe bereits gesehene Nachricht ID {msg_id}")
                continue
            if msg_id:
                last_processed_id = max(last_processed_id, msg_id)

            # Systemnachrichten überspringen (Beitreten, Verlassen, Anruf etc.)
            if msg.get("systemMessage"):
                continue

            # Eigene Nachrichten ignorieren
            if str(msg.get("actorId", "")) == my_user_id:
                continue

            actor = msg.get("actorDisplayName") or msg.get("actorId") or "?"
            msg_type = msg.get("messageType", "?")
            text = msg.get("message", "").strip()
            log.debug(f"[{token}] MSG type={msg_type!r} actor={actor!r} text={text!r}")
            if not text:
                continue

            log.info(f"[{token}] NEUE NACHRICHT von {actor}: {text}")
            with _dnd_lock:
                if _dnd["nc"]:
                    continue
            send_sms(f"NC {actor}: {text}")

        time.sleep(60)


# ----------------------------
# CalDAV Calendar Watcher
# ----------------------------

_CALDAV_NS = {
    "D": "DAV:",
    "C": "urn:ietf:params:xml:ns:caldav",
}


def _fetch_calendar_events(start: datetime, end: datetime) -> list[icalendar.Calendar]:
    """Fetch VEVENT components from the CalDAV server within [start, end]."""
    fmt = "%Y%m%dT%H%M%SZ"
    report_body = f"""<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start.strftime(fmt)}" end="{end.strftime(fmt)}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""

    try:
        r = requests.request(
            "REPORT",
            CALDAV_URL,
            auth=(CALDAV_USER, CALDAV_PASS),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
            data=report_body.encode("utf-8"),
            timeout=30,
        )
    except Exception as e:
        log.error(f"[caldav] Verbindungsfehler: {e}")
        return []

    if r.status_code not in (200, 207):
        log.error(f"[caldav] HTTP {r.status_code}: {r.text[:300]}")
        return []

    calendars = []
    try:
        root = ET.fromstring(r.text)
        for cal_data in root.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
            if cal_data.text:
                try:
                    calendars.append(icalendar.Calendar.from_ical(cal_data.text))
                except Exception as e:
                    log.warning(f"[caldav] iCal parse error: {e}")
    except Exception as e:
        log.error(f"[caldav] XML parse error: {e}")
    return calendars


def _to_utc(dt) -> datetime | None:
    """Normalise a timed DTSTART value to a UTC-aware datetime. Returns None for date-only."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _watch_calendar():
    """Periodically check CalDAV for upcoming events and send SMS notifications."""
    log.info("[caldav] Kalender-Watcher gestartet.")
    # Maps (uid, notify_at_iso) -> datetime when we sent the SMS
    notified: dict[tuple, datetime] = {}
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo

    while True:
        now = datetime.now(timezone.utc)

        # Query next 36 h — wide enough to catch all-day events we notify for the evening before
        calendars = _fetch_calendar_events(now, now + timedelta(hours=36))

        for cal in calendars:
            for component in cal.walk():
                if component.name != "VEVENT":
                    continue

                uid = str(component.get("UID", ""))
                dtstart_raw = component.get("DTSTART")
                if dtstart_raw is None:
                    continue

                dt = dtstart_raw.dt
                is_allday = not isinstance(dt, datetime)

                if is_allday:
                    # All-day event: notify at 18:00 local time the evening before
                    notify_day = dt - timedelta(days=1)
                    notify_at = datetime(
                        notify_day.year, notify_day.month, notify_day.day,
                        18, 0, 0, tzinfo=local_tz,
                    ).astimezone(timezone.utc)
                else:
                    start_utc = _to_utc(dt)
                    if start_utc is None:
                        continue
                    notify_at = start_utc - timedelta(minutes=CALDAV_NOTIFY_MINUTES)

                key = (uid, notify_at.isoformat())

                # Send only within a 2-minute window after notify_at (handles poll jitter)
                delta = (now - notify_at).total_seconds()
                if not (0 <= delta < 120):
                    continue

                if key in notified:
                    continue

                summary = str(component.get("SUMMARY", "(kein Titel)")).strip()
                location = str(component.get("LOCATION", "")).strip()

                if is_allday:
                    date_str = dt.strftime("%d.%m.%Y")
                    sms = f"Termin morgen ({date_str}): {summary}"
                else:
                    local_time = notify_at.astimezone(local_tz).strftime("%H:%M")
                    sms = f"Termin in {CALDAV_NOTIFY_MINUTES} Min ({local_time}): {summary}"

                if location:
                    sms += f" @ {location}"

                log.info(f"[caldav] Benachrichtigung: {sms}")
                with _dnd_lock:
                    if _dnd["cal"]:
                        notified[key] = now  # mark as sent so it doesn't retry
                        continue
                send_sms(sms)
                notified[key] = now

        # Prune entries sent more than 2 hours ago
        notified = {k: v for k, v in notified.items() if (now - v).total_seconds() < 7200}

        time.sleep(60)


# ----------------------------
# Main
# ----------------------------


def main():
    log.info(f"Starte Daemon, ueberwache {len(ROOMS)} Raeume per Long-Polling...")

    try:
        my_user_id = _fetch_own_user_id()
        log.info(f"Eigene Nachrichten (@{my_user_id}) werden ignoriert.")
    except Exception as e:
        log.error(f"Konnte Nextcloud-Benutzer-ID nicht laden: {e}")
        raise SystemExit(1)

    threads = []
    for token in ROOMS:
        t = threading.Thread(target=_watch_room, args=(token, my_user_id), daemon=True, name=f"room-{token}")
        t.start()
        threads.append(t)
        time.sleep(60 / len(ROOMS))  # Threads gleichmaessig ueber 60s verteilen

    cal_thread = threading.Thread(target=_watch_calendar, daemon=True, name="caldav")
    cal_thread.start()

    sig_thread = threading.Thread(target=_watch_signal, daemon=True, name="signal")
    sig_thread.start()

    wa_thread = threading.Thread(target=_watch_whatsapp, daemon=True, name="whatsapp")
    wa_thread.start()

    cmd_thread = threading.Thread(target=_watch_sms_commands, daemon=True, name="sms-cmd")
    cmd_thread.start()

    # Hauptthread am Leben halten; abgestuerzte Worker-Threads neu starten
    while True:
        time.sleep(30)
        for i, (token, t) in enumerate(zip(ROOMS, threads)):
            if not t.is_alive():
                log.warning(f"[{token}] Thread abgestuerzt, starte neu...")
                new_t = threading.Thread(target=_watch_room, args=(token, my_user_id), daemon=True, name=f"room-{token}")
                new_t.start()
                threads[i] = new_t
        if not cal_thread.is_alive():
            log.warning("[caldav] Thread abgestuerzt, starte neu...")
            cal_thread = threading.Thread(target=_watch_calendar, daemon=True, name="caldav")
            cal_thread.start()
        if not sig_thread.is_alive():
            log.warning("[signal] Thread abgestuerzt, starte neu...")
            sig_thread = threading.Thread(target=_watch_signal, daemon=True, name="signal")
            sig_thread.start()
        if not wa_thread.is_alive():
            log.warning("[whatsapp] Thread abgestuerzt, starte neu...")
            wa_thread = threading.Thread(target=_watch_whatsapp, daemon=True, name="whatsapp")
            wa_thread.start()
        if not cmd_thread.is_alive():
            log.warning("[sms-cmd] Thread abgestuerzt, starte neu...")
            cmd_thread = threading.Thread(target=_watch_sms_commands, daemon=True, name="sms-cmd")
            cmd_thread.start()


if __name__ == "__main__":
    main()
