#!/bin/bash
# Собирает конфиги для Git

REPO=/opt/smart-home-git

echo "Collecting configs..."

# Docker
cp /opt/smart-home/docker-compose.yml $REPO/

# Prometheus
cp /opt/smart-home/config/prometheus.yml $REPO/config/

# Network
cp /etc/netplan/50-cloud-init.yaml $REPO/config/netplan.yaml

# DHCP
cp /etc/dhcp/dhcpd.conf $REPO/config/

# Home Assistant
cp /data/homeassistant/configuration.yaml $REPO/config/ha_configuration.yaml
cp /data/homeassistant/automations.yaml $REPO/config/ha_automations.yaml
cp /data/homeassistant/scripts.yaml $REPO/config/ha_scripts.yaml

echo "Done! Run git add/commit/push manually or use backup.sh"
