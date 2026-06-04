#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Bitte als root ausführen: sudo ./whatsapp-pair.sh"
    exit 1
fi

BINARY="/home/mjb/sms-notification-daemon/whatsapp/whatsapp-listener"

echo "Stoppe Daemon..."
systemctl stop sms-notification-daemon

echo "Starte WhatsApp-Listener, QR-Code erscheint gleich..."
echo ""

# Pipe stderr durch qrencode, stdout verwerfen
"$BINARY" 2>&1 1>/dev/null | while IFS= read -r line; do
    if [[ "$line" == QR\ Code* ]]; then
        code="${line#QR Code (scan with WhatsApp): }"
        qrencode -t ansiutf8 "$code"
        echo ""
        echo "QR-Code scannen: WhatsApp → Verknüpfte Geräte → Gerät hinzufügen"
        echo "(Code läuft in ~20 Sekunden ab)"
    else
        echo "$line"
    fi
done

echo ""
echo "Starte Daemon neu..."
systemctl start sms-notification-daemon
systemctl status sms-notification-daemon --no-pager
