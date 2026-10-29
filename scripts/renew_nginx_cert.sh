#!/bin/bash
# Обновляет TLS-сертификат nginx (порты 8443/8445) через tailscale cert.
# tailscale cert сам перевыпускает сертификат только когда старый близок к
# истечению, иначе отдаёт кэш — поэтому запускаем часто (cron), а nginx
# перечитываем, только если файл реально поменялся.
# Cron: /etc/cron.d/nginx-cert-renew

DOMAIN=core.tail751bc9.ts.net
DIR=/opt/smart-home/config/nginx
CRT=$DIR/$DOMAIN.crt
KEY=$DIR/$DOMAIN.key

before=$(sha256sum "$CRT" 2>/dev/null | cut -d' ' -f1)
tailscale cert --cert-file "$CRT" --key-file "$KEY" "$DOMAIN" || exit 1
chown shadmin:shadmin "$CRT" "$KEY"
after=$(sha256sum "$CRT" | cut -d' ' -f1)

if [ "$before" != "$after" ]; then
  docker exec nginx nginx -s reload
  echo "$(date -Is) renewed: $(openssl x509 -in "$CRT" -noout -enddate)"
fi
