#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Bitte als root ausführen: sudo ./install.sh"
    exit 1
fi

echo "Baue WhatsApp-Listener..."
(cd whatsapp && go build -o whatsapp-listener .)

echo "Kopiere Daemon nach /usr/local/bin..."
install -m 755 main.py /usr/local/bin/sms-notification-daemon

echo "Erstelle Konfigurations-Verzeichnis..."
install -d -m 750 /etc/sms-notification-daemon
if [ -f config.py ]; then
    install -m 640 config.py /etc/sms-notification-daemon/config.py
elif [ ! -f /etc/sms-notification-daemon/config.py ]; then
    install -m 640 config.example.py /etc/sms-notification-daemon/config.py
    echo "HINWEIS: /etc/sms-notification-daemon/config.py wurde angelegt – bitte vor dem Start anpassen!"
fi

echo "Erstelle State-Verzeichnis..."
install -d -m 755 -o mjb /var/lib/sms-notification-daemon

echo "Erstelle Log-Datei..."
touch /var/log/sms-notification-daemon.log
chmod 640 /var/log/sms-notification-daemon.log

echo "Installiere systemd Service..."
install -m 644 sms-notification-daemon.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable sms-notification-daemon
systemctl restart sms-notification-daemon

echo ""
echo "Fertig. Status:"
systemctl status sms-notification-daemon --no-pager
