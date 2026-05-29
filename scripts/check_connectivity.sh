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

cat > "$TMP" << METRICS
# HELP tailscale_up Tailscale VPN status (1=online, 0=offline)
# TYPE tailscale_up gauge
tailscale_up $TS
# HELP telegram_reachable Telegram reachability via AWG VPN (1=ok, 0=fail)
# TYPE telegram_reachable gauge
telegram_reachable $TG
METRICS

mv "$TMP" "$TEXTFILE"
chmod 644 "$TEXTFILE"
