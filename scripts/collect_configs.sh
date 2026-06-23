#!/bin/bash
# Собирает конфиги для Git + бэкап БД в Яндекс Диск

source /opt/smart-home/.env
REPO=/opt/smart-home-git

echo "Collecting configs..."

# Docker
cp /opt/smart-home/docker-compose.yml $REPO/

# Prometheus
cp /opt/smart-home/config/prometheus.yml $REPO/config/

# Nginx
cp /opt/smart-home/config/nginx/nginx.conf $REPO/config/nginx.conf

# Homer
cp /data/homer/config.yml $REPO/config/homer.yml

# Network
cp /etc/netplan/50-cloud-init.yaml $REPO/config/netplan.yaml

# DHCP
cp /etc/dhcp/dhcpd.conf $REPO/config/

# Home Assistant
cp /data/homeassistant/configuration.yaml $REPO/config/ha_configuration.yaml
cp /data/homeassistant/automations.yaml $REPO/config/ha_automations.yaml
cp /data/homeassistant/scripts.yaml $REPO/config/ha_scripts.yaml

# Systemd
cp /etc/systemd/system/portal.service $REPO/config/portal.service

# Grafana dashboards
echo "Exporting Grafana dashboards..."
mkdir -p $REPO/config/grafana_dashboards
curl -s http://admin:${GRAFANA_PASSWORD}@localhost:3000/api/search?type=dash-db | \
  python3 -c "import sys,json; [print(d['uid'],d['title']) for d in json.load(sys.stdin)]" | \
  while read uid title; do
    curl -s "http://admin:${GRAFANA_PASSWORD}@localhost:3000/api/dashboards/uid/$uid" | \
      python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['dashboard'],indent=2))" \
      > "$REPO/config/grafana_dashboards/${uid}.json"
    echo "  Exported: $title"
  done

# Portal
echo "Collecting portal files..."
mkdir -p $REPO/config/portal
cp /data/portal/portal.html $REPO/config/portal/
cp /data/portal/sw.js $REPO/config/portal/
cp /data/portal/backend/main.py $REPO/config/portal/
cp /data/portal/backend/requirements.txt $REPO/config/portal/
cp /data/portal/backend/google_auth_migration.py $REPO/config/portal/
cp /data/portal/backend/todo_schema.sql $REPO/config/portal/
cp /data/portal/backend/notes_schema.sql $REPO/config/portal/
cp /data/portal/backend/finance_schema.sql $REPO/config/portal/
cp /data/portal/backend/garage_schema.sql $REPO/config/portal/
cp /data/portal/backend/events_schema.sql $REPO/config/portal/
cp /data/portal/backend/cal_schema.sql $REPO/config/portal/
cp /data/portal/backend/auth2fa_schema.sql $REPO/config/portal/
cp /data/portal/backend/app_schema.sql $REPO/config/portal/

echo "Done!"

# Бэкап БД в Яндекс Диск
echo "Starting DB backup..."
bash /opt/smart-home-git/scripts/backup_db_cloud.sh
