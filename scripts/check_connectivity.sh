#!/bin/bash
TEXTFILE=/var/lib/node-exporter/textfile/connectivity.prom
TMP=$(mktemp)

# Tailscale статус через BackendState
TS_STATE=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('BackendState',''))" 2>/dev/null)
if [ "$TS_STATE" = "Running" ]; then
    TS=1
else
    TS=0
fi

# Telegram доступность
if curl -s --max-time 5 https://api.telegram.org > /dev/null 2>&1; then
    TG=1
else
    TG=0
fi

# TLS сертификат nginx — срок действия
CERT_FILE=/opt/smart-home/config/nginx/core.tail751bc9.ts.net.crt
CERT_EXPIRY_TS=0
if [ -f "$CERT_FILE" ]; then
    CERT_ENDDATE=$(openssl x509 -enddate -noout -in "$CERT_FILE" 2>/dev/null | cut -d= -f2)
    [ -n "$CERT_ENDDATE" ] && CERT_EXPIRY_TS=$(date -d "$CERT_ENDDATE" +%s 2>/dev/null || echo 0)
fi

cat > "$TMP" << METRICS
# HELP tailscale_up Tailscale VPN status (1=online, 0=offline)
# TYPE tailscale_up gauge
tailscale_up $TS
# HELP telegram_reachable Telegram reachability via AWG VPN (1=ok, 0=fail)
# TYPE telegram_reachable gauge
telegram_reachable $TG
# HELP cert_expiry_timestamp_seconds TLS cert (nginx, core.tail751bc9.ts.net) expiry as unix timestamp
# TYPE cert_expiry_timestamp_seconds gauge
cert_expiry_timestamp_seconds{domain="core.tail751bc9.ts.net"} $CERT_EXPIRY_TS
METRICS

mv "$TMP" "$TEXTFILE"
chmod 644 "$TEXTFILE"
