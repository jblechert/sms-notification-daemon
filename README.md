# sms-notification-daemon

A lightweight daemon for Raspberry Pi that replaces push notifications on **de-Googled Android phones** (GrapheneOS, CalyxOS, DivestOS, …) with SMS — no Google Play Services, no internet connection required on the phone, no background battery drain from polling.

## The problem it solves

De-Googled phones have no Firebase Cloud Messaging. Apps that rely on it either can't receive push notifications at all, or need to keep a persistent network connection open — killing battery life. This daemon runs on a Pi at home and forwards relevant events to your phone via plain SMS, which every phone receives instantly with zero battery cost.

## What it monitors

| Source | SMS prefix | Description |
|---|---|---|
| Nextcloud Talk | `NC` | Incoming chat messages across configured rooms |
| Signal | `S` | Incoming Signal messages |
| WhatsApp | `WA` | Incoming WhatsApp messages |
| CalDAV calendar | — | Event reminders 15 min before (or 18:00 the evening before for all-day events) |

## Hardware

- **Raspberry Pi** (any model with USB)
- **Waveshare SIM7600E-H HAT** (SIMCOM SIM7600E-H) with a SIM card

  The modem appears as `/dev/ttyUSB2` on the Pi. SMS are sent in PDU/UCS-2 mode for full Unicode support.

## Architecture

```
Nextcloud Talk ──┐
Signal          ──┤──► sms-notification-daemon ──► GSM modem ──► SMS ──► your phone
WhatsApp        ──┤
CalDAV          ──┘

your phone ──► SMS command ──► modem ──► daemon parses & acts
```

The daemon is a single Python process with one thread per source. WhatsApp uses a compiled Go binary ([whatsmeow](https://github.com/tulir/whatsmeow)) as a subprocess. Signal uses the [signal-cli](https://github.com/AsamK/signal-cli) JSON-RPC socket.

## Features

- **DND mode** — silence any channel via SMS command
- **Rate limiting** — WhatsApp and Signal capped at one SMS per sender per hour
- **SMS command interface** — control the daemon by texting it

## SMS commands

Send these as SMS to the modem's SIM number:

| Command | Effect |
|---|---|
| `dnd wa on` / `dnd wa off` | Mute / unmute WhatsApp |
| `dnd s on` / `dnd s off` | Mute / unmute Signal |
| `dnd nc on` / `dnd nc off` | Mute / unmute Nextcloud Talk |
| `dnd cal on` / `dnd cal off` | Mute / unmute calendar reminders |
| `status dnd` | Reply with current DND state |

## Setup

### Dependencies

```bash
pip install requests pyserial icalendar
# signal-cli must be running as a systemd socket service
# Go 1.21+ required to build the WhatsApp listener
```

### Configuration

```bash
cp config.example.py config.py
# edit config.py with your credentials
```

### WhatsApp pairing (first run only)

```bash
./whatsapp/whatsapp-listener
# Scan the QR code printed to stderr with WhatsApp → Linked Devices
```

### Install as systemd service

```bash
sudo ./install.sh
```

This builds the WhatsApp listener, installs the daemon to `/usr/local/bin/`, and enables the systemd service.

## Signal setup

Requires [signal-cli](https://github.com/AsamK/signal-cli) running as a system socket service (`signal-cli-socket.service`). The user running the daemon must be in the `signal-cli` group.

## License

MIT
