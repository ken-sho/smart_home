# Smart Home Core

Система умного дома на базе собственного сервера Core.  
Версия документации: **v0.5**  
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
| node-exporter | prom/node-exporter:latest | 9100 | Метрики сервера |
| smartctl-exporter | prometheuscommunity/smartctl-exporter:latest | 9633 | SMART метрики дисков |
| grafana | grafana/grafana:latest | 3000 | Дашборды |

**Запуск стека:**
```bash
cd /opt/smart-home
docker compose up -d
```

---

## Структура директорий

```
/opt/smart-home/          # Docker Compose проект
  docker-compose.yml
  config/
    prometheus.yml

/opt/smart-home-git/      # Git репозиторий
  README.md
  docker-compose.yml
  config/
  scripts/
    collect_configs.sh    # копирует конфиги в репозиторий
    git_push.sh           # git pull + commit + push (запускается крон в 3:00)

/data/                    # HDD 500GB (UUID: 0d812c59-9f59-4129-9808-36abc88ad3ec)
  homeassistant/          # Конфиги Home Assistant
  mosquitto/              # Данные MQTT
  zigbee2mqtt/            # Данные Zigbee2MQTT
  esphome/                # Прошивки ESPHome
  prometheus/             # Данные Prometheus
  grafana/                # Данные Grafana
  backups/                # Резервные копии
```

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
```

```yaml
network:
  version: 2
  ethernets:
    console:
      match:
        macaddress: "1c:6f:65:94:af:93"
      set-name: console
      addresses:
        - "192.168.10.254/24"
    intel0:
      match:
        macaddress: "90:e2:ba:0b:ab:98"
      set-name: intel0
      addresses:
        - "192.168.11.254/24"
    intel1:
      match:
        macaddress: "90:e2:ba:0b:ab:99"
      set-name: intel1
      addresses:
        - "192.168.12.254/24"
    intel2:
      match:
        macaddress: "90:e2:ba:0b:ab:9a"
      set-name: intel2
      addresses:
        - "192.168.13.254/24"
    intel3:
      match:
        macaddress: "90:e2:ba:0b:ab:9b"
      set-name: intel3
      addresses:
        - "192.168.14.254/24"
```

```bash
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

### Шаг 5 — Клонирование репозитория

```bash
git clone https://github.com/ken-sho/smart_home.git /opt/smart-home-git
```

### Шаг 6 — Восстановление конфигов

```bash
mkdir -p /opt/smart-home/config
cp /opt/smart-home-git/docker-compose.yml /opt/smart-home/
cp /opt/smart-home-git/config/prometheus.yml /opt/smart-home/config/
cp /opt/smart-home-git/config/netplan.yaml /etc/netplan/50-cloud-init.yaml
cp /opt/smart-home-git/config/dhcpd.conf /etc/dhcp/dhcpd.conf
cp /opt/smart-home-git/config/ha_configuration.yaml /data/homeassistant/configuration.yaml
cp /opt/smart-home-git/config/ha_automations.yaml /data/homeassistant/automations.yaml
```

### Шаг 7 — Запуск стека

```bash
mkdir -p /data/{homeassistant,mosquitto/config,mosquitto/data,mosquitto/log,zigbee2mqtt,esphome,prometheus,grafana,backups}
chown -R 65534:65534 /data/prometheus
chown -R 472:472 /data/grafana
chown -R 1883:1883 /data/mosquitto
cd /opt/smart-home
docker compose up -d
```

### Шаг 8 — DHCP сервер

```bash
sudo apt install isc-dhcp-server -y
sudo cp /opt/smart-home-git/config/dhcpd.conf /etc/dhcp/dhcpd.conf
sudo sed -i 's/INTERFACESv4=""/INTERFACESv4="intel0"/' /etc/default/isc-dhcp-server
sudo systemctl restart isc-dhcp-server
```

### Шаг 9 — AmneziaWG VPN

```bash
# Установить AmneziaWG согласно документации amnezia.org
# Скопировать конфиг из защищённого хранилища
sudo cp wg0.conf /etc/amnezia/amneziawg/wg0.conf
sudo systemctl enable awg-quick@wg0
sudo systemctl start awg-quick@wg0
# Проверка
curl -s --max-time 5 https://api.telegram.org && echo OK
```

### Шаг 10 — Tailscale + Funnel

```bash
# Установка
curl -fsSL https://tailscale.com/install.sh | sh

# Авторизация (откроется ссылка — войти через gakto1981@gmail.com)
# Запускать без netfilter из-за конфликта с AmneziaWG
tailscale up --netfilter-mode=off

# Запуск Funnel для Home Assistant (публичный HTTPS)
tailscale funnel --bg 8123

# Маршрут для корректной работы через AmneziaWG (асимметричная маршрутизация)
cat > /etc/networkd-dispatcher/routable.d/50-tailscale-routes.sh << 'ROUTE'
#!/bin/bash
ip route add 100.64.0.0/10 dev tailscale0 2>/dev/null || true
ROUTE
chmod +x /etc/networkd-dispatcher/routable.d/50-tailscale-routes.sh
# Применить сразу без перезагрузки
ip route add 100.64.0.0/10 dev tailscale0 2>/dev/null || true
```

**Результат:**
- Tailscale IP Core: `100.69.214.120`
- Публичный URL HA: `https://core.tail751bc9.ts.net`

**Доступ к сервисам:**

| Сервис | URL |
|--------|-----|
| Home Assistant (публичный) | https://core.tail751bc9.ts.net |
| Home Assistant (локальный) | http://192.168.10.254:8123 |
| Grafana | http://100.69.214.120:3000 |
| Prometheus | http://100.69.214.120:9090 |

> Funnel (публичный интернет) — только для HA, нужен для Алисы.  
> Grafana и Prometheus — только внутри tailnet, без Funnel.

### Шаг 11 — Проверка

```bash
docker ps                                              # все контейнеры Up
ping 192.168.11.253                                    # микротик доступен
curl http://localhost:8123                             # Home Assistant
curl http://localhost:3000                             # Grafana
curl -s https://api.telegram.org && echo OK            # Telegram доступен
tailscale status                                       # Tailscale подключён
curl https://core.tail751bc9.ts.net && echo OK         # Funnel работает
```

---

## Резервное копирование

Автоматический бэкап запускается крон-джобом ежесуточно в 3:00.

Состоит из двух скриптов которые запускаются последовательно:

- `collect_configs.sh` — собирает актуальные конфиги в репозиторий
- `git_push.sh` — делает commit и push в GitHub

Для ручного запуска:

```bash
bash /opt/smart-home-git/scripts/collect_configs.sh
bash /opt/smart-home-git/scripts/git_push.sh
```

---

## Секреты (НЕ в Git)

Хранятся отдельно в зашифрованном виде:

- AmneziaWG конфиг (`/etc/amnezia/amneziawg/wg0.conf`)
- Telegram Bot токен
- Tuya API credentials
- HA Long-Lived Access Token
- Пароли Wi-Fi сетей
- Tailscale authkey

---

## Устройства IoT

| Устройство | Протокол | IP | Интеграция |
|------------|----------|----|------------|
| temp_sensor_2 | Wi-Fi Tuya | 192.168.11.10 | Tuya Cloud |
| MikroTik mAP lite | Ethernet | 192.168.11.253 | — |

---

## Changelog

| Версия | Дата | Изменения |
|--------|------|-----------|
| v0.1 | 2026-05 | Начальная архитектура |
| v0.2 | 2026-05 | Установка Ubuntu, Docker стек, IoT сеть |
| v0.3 | 2026-05 | Первое устройство (Tuya датчик), Grafana дашборд, Git |
| v0.4 | 2026-05 | Tailscale VPN, Funnel для HA, удалённый доступ к Grafana |
| v0.5 | 2026-05 | Yandex Smart Home (Алиса), исправление маршрутизации Tailscale + AmneziaWG |
