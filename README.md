# Smart Home Core

Система умного дома на базе собственного сервера Core.  
Версия документации: **v0.8**  
Статус: в разработке

---

## Железо

| Компонент | Описание |
|-----------|----------|
| CPU | Intel Xeon X3440 (4 ядра / 8 потоков, 2.53GHz) |
| RAM | 8 GB |
| SSD | 60 GB — ОС + Docker |
| HDD | 500 GB (WD Blue) — данные, примонтирован на `/data` |
| Сетевая | Встроенная Realtek (консоль) + Intel 4-портовая PCI-E |
| ОС | Ubuntu Server 24.04 LTS |
| Пользователь | `shadmin` |
| Hostname | `core` |

---

## Сетевая схема

| Интерфейс | Подсеть | Назначение |
|-----------|---------|------------|
| `console` (встроенная) | 192.168.10.0/24 | Прямое подключение ноутбука/консоли |
| `intel0` | 192.168.11.0/24 | IoT сеть (hAP mini → устройства) |
| `intel1` | 192.168.12.0/24 | Резерв |
| `intel2` | 192.168.13.0/24 | Резерв |
| `intel3` | 192.168.14.0/24 | Резерв |
| `wlx...` (USB Wi-Fi) | — | Интернет через AmneziaWG VPN |

**Core имеет .254 на каждом интерфейсе.**

### IoT сеть
- Точка доступа: MikroTik mAP lite (`192.168.11.253`)
- SSID: `iot_shogo`, пароль в `.env`
- DHCP сервер: на Core (`isc-dhcp-server`, intel0)
- Диапазон: `192.168.11.1 — 192.168.11.200`

---

## Docker стек (сервисы)

| Сервис | Образ | Порт | Назначение |
|--------|-------|------|------------|
| homeassistant | ghcr.io/home-assistant/home-assistant:stable | 8123 | Центральная шина автоматизации |
| mosquitto | eclipse-mosquitto:latest | 1883, 9001 | MQTT брокер |
| zigbee2mqtt | koenkk/zigbee2mqtt:latest | 8080 | Поддержка Zigbee устройств |
| esphome | ghcr.io/esphome/esphome:latest | 6052 | Кастомные прошивки ESP |
| prometheus | prom/prometheus:latest | 9090 | Сбор метрик |
| node-exporter | prom/node-exporter:latest | 9100 | Метрики сервера + textfile collector |
| smartctl-exporter | prometheuscommunity/smartctl-exporter:latest | 9633 | SMART метрики дисков |
| grafana | grafana/grafana:latest | 3000 | Дашборды |
| nginx | nginx:latest | 8443, 8444 | Reverse proxy + TLS терминация |
| vaultwarden | vaultwarden/server:latest | 8081, 3012 | Менеджер паролей (Bitwarden совместимый) |
| homer | b4bz/homer:latest | 8888 | Стартовая страница |

**Запуск стека:**
```bash
cd /opt/smart-home
docker compose up -d
```

---

## База данных

**PostgreSQL 18** установлен локально на Core (не в Docker).

```bash
sudo systemctl status postgresql
sudo -u postgres psql
```

### Базы данных

| База | Пользователь | Назначение |
|------|-------------|------------|
| vaultwarden | vaultwarden | Менеджер паролей |
| grafana | grafana | Дашборды Grafana |
| homeassistant | homeassistant | История состояний HA |
| portal | portal | Личный портал |

### Подключение из Docker контейнеров

Контейнеры подключаются через `host.docker.internal` (172.17.0.1 / 172.18.0.1).
Разрешённые подсети в `pg_hba.conf`:
- `172.17.0.0/16`, `172.18.0.0/16` — Docker bridge
- `192.168.10.0/24`, `192.168.11.0/24` — локальные интерфейсы
- `100.64.0.0/10` — Tailscale

---

## Nginx — Reverse Proxy

Nginx терминирует TLS и проксирует сервисы. Сертификат от Tailscale.

```bash
# Получить/обновить сертификат (раз в год)
tailscale cert core.tail751bc9.ts.net
cp core.tail751bc9.ts.net.* /opt/smart-home/config/nginx/
docker compose restart nginx
```

### Маршрутизация

| URL | Сервис | Доступ |
|-----|--------|--------|
| `https://core.tail751bc9.ts.net/` | Home Assistant | Публично (Funnel) |
| `https://core.tail751bc9.ts.net/vault/` | Vaultwarden | Публично (Funnel) |
| `https://core.tail751bc9.ts.net/portal/` | Личный портал | Telegram Mini App + Tailscale |
| `https://core.tail751bc9.ts.net/api/` | Portal API | Telegram Mini App + Tailscale |
| `http://100.69.214.120:8888` | Homer | Только Tailscale |
| `http://100.69.214.120:3000` | Grafana | Только Tailscale |
| `http://100.69.214.120:9090` | Prometheus | Только Tailscale |

### Tailscale Funnel

```
Интернет → Tailscale Funnel :443 → nginx :8444 (HTTP) → сервисы
```

```bash
tailscale funnel --bg 8444
```

---

## Личный портал

FastAPI + PostgreSQL + Telegram Mini App. Подробная документация в `config/portal/README.md`.

**Запуск:**
```bash
systemctl status portal
systemctl restart portal
journalctl -u portal -f
```

**Доступ:**
- Telegram: `t.me/ken_sho_portal_bot/portal`
- Tailscale: `http://100.69.214.120:7000`

---

## Структура директорий

```
/opt/smart-home/          # Docker Compose проект
  docker-compose.yml
  .env                    # Секреты (НЕ в Git)
  config/
    prometheus.yml
    nginx/
      nginx.conf
      core.tail751bc9.ts.net.crt
      core.tail751bc9.ts.net.key

/opt/smart-home-git/      # Git репозиторий
  README.md
  docker-compose.yml
  config/
    prometheus.yml
    nginx.conf
    homer.yml
    netplan.yaml
    dhcpd.conf
    ha_configuration.yaml
    portal.service
    grafana_dashboards/
    portal/
      README.md           # Документация портала
      main.py
      requirements.txt
      *_schema.sql
  scripts/
    collect_configs.sh
    git_push.sh
    check_connectivity.sh

/data/                    # HDD 500GB (UUID: 0d812c59-9f59-4129-9808-36abc88ad3ec)
  homeassistant/
  mosquitto/
  zigbee2mqtt/
  esphome/
  prometheus/
  grafana/
  vaultwarden/
  homer/
  portal/
    portal.html
    backend/
  backups/

/var/lib/node-exporter/textfile/
  connectivity.prom
```

---

## Мониторинг

Скрипт `/usr/local/bin/check_connectivity.sh` — крон каждую минуту:

| Метрика | Описание |
|---------|----------|
| `tailscale_up` | Статус Tailscale (1=Running, 0=не работает) |
| `telegram_reachable` | Доступность Telegram через AWG (1=OK, 0=недоступен) |

---

## Восстановление с нуля (DR)

> Цель: полное восстановление за 60 минут

### Шаг 1 — Установка Ubuntu Server

1. Скачать Ubuntu Server 24.04 LTS
2. Записать на флешку через Balena Etcher
3. Установить: язык English, OpenSSH server, без LVM
4. Hostname: `core`, пользователь: `shadmin`

### Шаг 2 — Настройка сети

```bash
sudo nano /etc/netplan/50-cloud-init.yaml
# Скопировать содержимое из config/netplan.yaml
sudo netplan apply
```

### Шаг 3 — Монтирование HDD

```bash
sudo mkdir -p /data
echo 'UUID=0d812c59-9f59-4129-9808-36abc88ad3ec /data ext4 defaults 0 2' | sudo tee -a /etc/fstab
sudo mount -a
```

### Шаг 4 — Установка Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker shadmin
```

### Шаг 5 — Установка PostgreSQL 18

```bash
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list'
sudo apt update && sudo apt install -y postgresql-18
```

Настроить `pg_hba.conf` и `postgresql.conf` (добавить подсети 172.17/18, 192.168.10/11, 100.64/10).

Восстановить данные из зашифрованного бэкапа:
```bash
gpg --decrypt --passphrase "$POSTGRES_BACKUP_PASS" --batch postgres_full.sql.gpg | sudo -u postgres psql
```

### Шаг 6 — Клонирование репозитория

```bash
git clone https://github.com/ken-sho/smart_home.git /opt/smart-home-git
```

### Шаг 7 — Восстановление конфигов

```bash
mkdir -p /opt/smart-home/config/nginx
cp /opt/smart-home-git/docker-compose.yml /opt/smart-home/
cp /opt/smart-home-git/config/prometheus.yml /opt/smart-home/config/
cp /opt/smart-home-git/config/nginx.conf /opt/smart-home/config/nginx/
cp /opt/smart-home-git/config/netplan.yaml /etc/netplan/50-cloud-init.yaml
cp /opt/smart-home-git/config/dhcpd.conf /etc/dhcp/dhcpd.conf
cp /opt/smart-home-git/config/ha_configuration.yaml /data/homeassistant/configuration.yaml
cp /opt/smart-home-git/config/ha_automations.yaml /data/homeassistant/automations.yaml
cp /opt/smart-home-git/config/homer.yml /data/homer/config.yml
```

Восстановить `.env` из защищённого хранилища. Получить TLS сертификат:
```bash
tailscale cert core.tail751bc9.ts.net
cp core.tail751bc9.ts.net.* /opt/smart-home/config/nginx/
```

### Шаг 8 — Запуск стека

```bash
mkdir -p /data/{homeassistant,mosquitto/config,mosquitto/data,mosquitto/log,zigbee2mqtt,esphome,prometheus,grafana,vaultwarden,homer,portal/backend,backups}
mkdir -p /var/lib/node-exporter/textfile
chmod 755 /var/lib/node-exporter/textfile
chown -R 65534:65534 /data/prometheus
chown -R 472:472 /data/grafana
chown -R 1883:1883 /data/mosquitto
cd /opt/smart-home && docker compose up -d
```

### Шаг 9 — Портал

```bash
sudo apt install python3-venv python3-pip -y
cd /data/portal/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r /opt/smart-home-git/config/portal/requirements.txt
cp /opt/smart-home-git/config/portal.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable portal && systemctl start portal
```

Подробнее — `config/portal/README.md`.

### Шаг 10 — DHCP сервер

```bash
sudo apt install isc-dhcp-server -y
sudo cp /opt/smart-home-git/config/dhcpd.conf /etc/dhcp/dhcpd.conf
sudo sed -i 's/INTERFACESv4=""/INTERFACESv4="intel0"/' /etc/default/isc-dhcp-server
sudo systemctl restart isc-dhcp-server
```

### Шаг 11 — AmneziaWG VPN

```bash
# Установить AmneziaWG согласно документации amnezia.org
# ВАЖНО: конфиг генерируется в приложении Amnezia на телефоне.
# При переустановке приложения конфиг пересоздаётся с новыми ключами —
# старый wg0.conf становится недействительным.
sudo cp wg0.conf /etc/amnezia/amneziawg/wg0.conf
sudo systemctl enable awg-quick@wg0
sudo systemctl start awg-quick@wg0
curl -s --max-time 5 https://api.telegram.org && echo OK
```

### Шаг 12 — Tailscale + Funnel

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --netfilter-mode=off
tailscale funnel --bg 8444

cat > /etc/networkd-dispatcher/routable.d/50-tailscale-routes.sh << 'ROUTE'
#!/bin/bash
ip route add 100.64.0.0/10 dev tailscale0 2>/dev/null || true
ROUTE
chmod +x /etc/networkd-dispatcher/routable.d/50-tailscale-routes.sh
ip route add 100.64.0.0/10 dev tailscale0 2>/dev/null || true
```

### Шаг 13 — Мониторинг подключения

```bash
cp /opt/smart-home-git/scripts/check_connectivity.sh /usr/local/bin/
chmod +x /usr/local/bin/check_connectivity.sh
echo "* * * * * root /usr/local/bin/check_connectivity.sh" > /etc/cron.d/connectivity-check
```

### Шаг 14 — Проверка

```bash
docker ps                                              # все контейнеры Up
ping 192.168.11.253                                    # микротик доступен
curl http://localhost:8123                             # Home Assistant
curl http://localhost:3000                             # Grafana
curl http://localhost:7000/api/health                  # Портал
curl -k https://localhost:8443                         # nginx
curl -s https://api.telegram.org && echo OK            # Telegram доступен
tailscale status                                       # Tailscale подключён
curl https://core.tail751bc9.ts.net && echo OK         # Funnel работает
```

---

## Резервное копирование

Автоматический бэкап — крон ежесуточно в 3:00:

- `collect_configs.sh` — собирает конфиги, схемы портала, дашборды Grafana
- `git_push.sh` — коммит и пуш в GitHub

```bash
bash /opt/smart-home-git/scripts/collect_configs.sh
bash /opt/smart-home-git/scripts/git_push.sh
```

PostgreSQL бэкапы — через Barman на NAS (планируется).

---

## Секреты (НЕ в Git)

**`/opt/smart-home/.env`:**
- `GRAFANA_PASSWORD`, `VW_DB_PASSWORD`, `VW_ADMIN_TOKEN`, `PORTAL_DB_PASSWORD`

**`/data/portal/backend/.env` (через systemd Environment=):**
- `DB_HOST/PORT/NAME/USER/PASS`, `PORTAL_HTML`

**Отдельно (защищённое хранилище):**
- AmneziaWG конфиг, Tailscale authkey, пароли Wi-Fi

**В БД `app.settings`:**
- `telegram_bot_token`, `telegram_owner_id`, `telegram_chat_id`

---

## Устройства IoT

Всё касательно IoT-устройств — в директории `devices/`.

---

## Changelog

| Версия | Дата | Изменения |
|--------|------|-----------|
| v0.1 | 2026-05 | Начальная архитектура |
| v0.2 | 2026-05 | Установка Ubuntu, Docker стек, IoT сеть |
| v0.3 | 2026-05 | Первое устройство (Tuya датчик), Grafana дашборд, Git |
| v0.4 | 2026-05 | Tailscale VPN, Funnel для HA, удалённый доступ к Grafana |
| v0.5 | 2026-05 | Yandex Smart Home (Алиса), исправление маршрутизации Tailscale + AmneziaWG |
| v0.6 | 2026-05 | Переустановка AWG, мониторинг Tailscale + Telegram в Grafana |
| v0.7 | 2026-06 | PostgreSQL 18, Vaultwarden, nginx reverse proxy, Homer, Grafana/HA → PostgreSQL |
| v0.8 | 2026-06 | Личный портал (FastAPI + PostgreSQL + Telegram Mini App), авторизация |
