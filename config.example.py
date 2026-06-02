# Copy this file to config.py and fill in your values.

# Nextcloud
NC_URL = "https://your.nextcloud.instance"
NC_USER = "user@example.com"
NC_PASS = "app-password"
NC_ROOMS = [
    "roomtoken1",
    "roomtoken2",
]

# CalDAV
CALDAV_URL = "http://your-caldav-server/user/calendar/"
CALDAV_USER = "username"
CALDAV_PASS = "password"
CALDAV_NOTIFY_MINUTES = 15

# Modem / SMS
MODEM_DEVICE = "/dev/ttyUSB2"
SMS_RECIPIENT = "+49..."

# Signal
SIGNAL_SOCKET = "/run/signal-cli/socket"
SIGNAL_ACCOUNT = "+49..."

# WhatsApp
WHATSAPP_BINARY = "/home/USER/sms-notification-daemon/whatsapp/whatsapp-listener"
