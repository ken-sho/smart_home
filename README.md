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

**ВАЖНО (урок от 24.08.2026):** сертификат `tailscale cert` живёт не год, а
около 1.5–3 месяцев (Let's Encrypt-подобный). 24.08.2026 он протух (истёк
11.08.2026), а 11.10.2026 — ещё раз: ручной `tailscale cert` 24.08 вернул
закэшированный старый сертификат (tailscale перевыпускает только ближе к
истечению), так что «обновление» ничего не продлило.

**С 11.10.2026 — автоматически:**
- `scripts/renew_nginx_cert.sh` → `/usr/local/bin/`, cron
  `/etc/cron.d/nginx-cert-renew` ежедневно в 04:17, лог
  `/var/log/nginx-cert-renew.log`. Если файл сертификата поменялся —
  `nginx -s reload`.
- Алерт Grafana `nginx TLS cert expires soon` (папка Infra) — срабатывает,
  если до истечения < 14 дней или метрика `cert_expiry_timestamp_seconds`
  пропала; шлёт в Telegram через бота портала (contact point
  «Telegram (portal bot)», он же дефолтный в notification policy).

```bash
# Проверить срок действия
echo | openssl s_client -connect localhost:8443 -servername core.tail751bc9.ts.net 2>/dev/null | openssl x509 -noout -enddate

# Обновить вручную
sudo /usr/local/bin/renew_nginx_cert.sh
```

Этот файл использует **только** nginx (маршруты через порт 8443/8445, см.
ниже) — публичный Funnel (443) и прямой доступ к Vaultwarden (8446)
получают TLS от самого `tailscaled` автоматически и от протухания этого
файла не зависят.

### Маршрутизация

| URL | Сервис | Доступ |
|-----|--------|--------|
| `https://core.tail751bc9.ts.net/` | Home Assistant | Публично (Funnel) |
| `https://core.tail751bc9.ts.net/portal/` | Личный портал | Telegram Mini App + Tailscale |
| `https://core.tail751bc9.ts.net/api/` | Portal API | Telegram Mini App + Tailscale |
| `https://core.tail751bc9.ts.net:8446` | Vaultwarden (веб + расширение) | Tailscale (dedicated serve, см. ниже) |
| `https://core.tail751bc9.ts.net:8445` | Личный портал (прямой, для Service Worker) | Только Tailscale |
| `http://100.69.214.120:8888` | Homer | Только Tailscale |
| `http://100.69.214.120:3000` | Grafana | Только Tailscale |
| `http://100.69.214.120:9090` | Prometheus | Только Tailscale |

nginx.conf всё ещё содержит старый `location /vault/` (проксирует на
Vaultwarden с урезанием префикса) — он больше не используется для доступа
(оставлен, но фактически мёртвый маршрут), см. раздел про Vaultwarden ниже.

### Tailscale Funnel

```
Интернет → Tailscale Funnel :443 → nginx :8444 (HTTP) → сервисы
```

```bash
tailscale funnel --bg 8444
```

---

## Vaultwarden — доступ в обход nginx-субпути

**Урок от 24.08.2026:** Vaultwarden не поддерживает работу из-под sub-path
(`/vault/`) на общем домене — его роуты (`/api/`, `/identity/`,
`/notifications/`) всегда на корне независимо от `DOMAIN`. Попытка задать
`DOMAIN=https://core.tail751bc9.ts.net/vault` ломает всё 404-ками. Рабочее
решение — отдельный `tailscale serve` на свой порт, без общего домена/пути:

```bash
tailscale serve --bg --https=8446 http://127.0.0.1:8081
```

Персистентность подтверждена (переживает `tailscaled`/ребут сервера —
конфиг serve хранится в состоянии tailscaled, дополнительный systemd-юнит
не нужен).

В `docker-compose.yml` у сервиса `vaultwarden` должно быть:
```yaml
DOMAIN: https://core.tail751bc9.ts.net:8446
```
(без пути — иначе снова 404). Без `DOMAIN` вообще веб-клиент зависает на
skeleton-плейсхолдерах (`/api/config` отдаёт `environment.api=http://localhost/api`,
и клиент пытается стучаться в localhost браузера пользователя).

Общий принцип для будущих self-hosted сервисов со своим web-клиентом: не
сажать под sub-path общего домена, если сервис явно не заявляет поддержку
этого — выделять отдельный `tailscale serve --https=PORT` + root-level
`DOMAIN`/`PUBLIC_URL`.

**Версия:** обновлена 24.08.2026 с 1.36.0 до **1.37.2** (известный баг
совместимости WASM SDK новых Bitwarden-клиентов со старыми Vaultwarden —
падение `invalid type: JsValue(Object(...)), expected a string` в
браузерном расширении). Апдейт:
```bash
cd /opt/smart-home
sudo -u postgres pg_dump vaultwarden | gzip > /data/backups/vaultwarden_pre_upgrade_$(date +%Y%m%d_%H%M%S).sql.gz
docker compose pull vaultwarden
docker compose up -d vaultwarden
docker logs vaultwarden --tail 50   # проверить миграцию БД без ошибок
```
Если после апдейта расширение всё равно не грузит хранилище (веб-клиент и
мобильное приложение работают, а расширение виснет на пустых
skeleton-плейсхолдерах) — вероятная причина не сервер, а битое локальное
состояние WASM-крипто-ядра самого расширения: помогает **полное удаление и
переустановка расширения** (не просто logout/login).

---

## Личный портал

FastAPI + PostgreSQL + Telegram Mini App. Подробная документация в `config/portal/README.md`.

Вкладки: Todo, Финансы (Кредиты · Д/К · Траты · Крипто), События, Календарь
(заготовка), Гараж, Заметки, 2FA, Настройки. Бот `@ken_sho_portal_bot`:
`/portal`, `/wallet` + исходящие алерты (события, крипто-сигналы).

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
      portal.html, sw.js
      main.py, crypto.py, budget.py
      requirements.txt
      *_schema.sql
  scripts/
    collect_configs.sh    # ночной сбор конфигов (+ вызывает backup_db_cloud.sh)
    git_push.sh
    backup_db_cloud.sh    # pg_dumpall → Яндекс Диск
    check_connectivity.sh
    renew_nginx_cert.sh

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
    statements/           # PDF-справки банка (ФИО, счёт) — НЕ в git и НЕ в бэкапах
  backups/
    db/                   # core_<день недели>.sql — 7 дневных дампов

/var/lib/node-exporter/textfile/
  connectivity.prom
```

---

## Мониторинг

Скрипт `/usr/local/bin/check_connectivity.sh` — крон каждую минуту:

| Метрика | Описание |
|---------|----------|
| `tailscale_up` | Статус Tailscale (1=Running, 0=не работает) |
| `telegram_reachable` | Доступность Telegram дефолтным маршрутом (1=OK, 0=недоступен) |
| `cert_expiry_timestamp_seconds` | Срок TLS-сертификата nginx (панель «TLS Cert» + алерт < 14 дней) |

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
tailscale serve --bg --https=8446 http://127.0.0.1:8081   # Vaultwarden, см. раздел выше — переживает ребут сам

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

# Автообновление TLS-сертификата nginx
install -m 755 /opt/smart-home-git/scripts/renew_nginx_cert.sh /usr/local/bin/
echo "17 4 * * * root /usr/local/bin/renew_nginx_cert.sh >> /var/log/nginx-cert-renew.log 2>&1" > /etc/cron.d/nginx-cert-renew
```

Алерт Grafana на срок сертификата + Telegram contact point создаются через
API Grafana (не в provisioning-файлах) — при восстановлении создать заново
(см. «Nginx — Reverse Proxy»).

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

- `collect_configs.sh` — собирает конфиги, код и схемы портала, дашборды
  Grafana в `/opt/smart-home-git`, в конце вызывает `backup_db_cloud.sh`
- `backup_db_cloud.sh` — `pg_dumpall` всех баз в
  `/data/backups/db/core_<день недели>.sql` (глубина 7 дней, перезапись по
  дню недели) + `rclone copy` в `yadisk:backups/core-db`. Дамп не шифруется.
- `git_push.sh` — коммит + `pull --rebase` + пуш в GitHub, лог
  `/var/log/smart-home-backup.log`; при конфликте/ошибке пуша откатывает
  rebase и шлёт алерт в Telegram

```bash
bash /opt/smart-home-git/scripts/collect_configs.sh
bash /opt/smart-home-git/scripts/git_push.sh
```

**Что где восстанавливается:** всё, что живёт в PostgreSQL (включая фото и
сканы документов Гаража — `garage.service_files`, bytea) — из дампа с
Яндекс Диска. Код и конфиги — из GitHub. Банковские справки
`/data/portal/statements/` намеренно никуда не копируются.

Barman на NAS — планируется.

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

## Известные проблемы / TODO

- **Выгрузка дампов БД на Яндекс Диск не работает** (найдено 30.10.2026,
  с какого числа — неизвестно: вывод `backup_db_cloud.sh` никуда не
  логируется). `rclone` (1.60.1 из apt) получает `410 GoneError — Ресурс
  больше недоступен` на любом листинге, при этом `rclone about` работает.
  Известная проблема: Яндекс перестал пускать к файлам встроенный OAuth-клиент
  rclone ([rclone#10067](https://github.com/rclone/rclone/issues/10067)).
  Лечение: своё приложение на oauth.yandex.ru (доступ к Диску), `rclone
  config` с его client_id/secret, интерактивная авторизация в браузере.
  Пока дампы есть только локально в `/data/backups/db` (тот же диск, что и
  данные). Заодно: логировать вывод `backup_db_cloud.sh` и алертить при
  ошибке `rclone`.
- ~~Ночной git-бэкап с Core не доходил до GitHub 26.08–30.10.2026~~ —
  исправлено 30.10.2026. В `/opt/smart-home-git` висел незавершённый rebase:
  старый `git_push.sh` делал `pull --rebase` до коммита и не обрабатывал
  конфликт. Rebase откачен, зависшие ночные коммиты сохранены в ветке
  `stuck-backup-20261030` (только на Core), репо выровнено по
  `origin/master`. Новый `git_push.sh`: сначала коммит, потом
  `pull --rebase`; при ошибке — `rebase --abort` + алерт в Telegram ботом
  портала.
- **Портал в Telegram после деплоя `portal.html` может не открываться**
  до перезапуска Telegram (30.09.2026) — вероятно, кэш WebView/`sw.js`
  держит старую версию. Не расследовано.

- **Tuya-интеграция в Home Assistant периодически просит
  ре-авторизацию** (`Authentication failed. Please re-authenticate`,
  всплывает после каждого рестарта HA) — чинится вручную в UI
  (Настройки → Интеграции → Tuya → Reauthenticate).
- **AmneziaWG (`awg-quick@wg0`) сейчас не активен** на Core, при этом
  `telegram_reachable=1` (Telegram доступен и без VPN) — `check_connectivity.sh`
  не привязан к конкретному интерфейсу, просто проверяет доступность
  дефолтным маршрутом. Если AWG должен быть постоянно поднят — проверить,
  почему не поднялся после ребута.
- Старый `location /vault/` в `nginx.conf` не используется (см.
  «Vaultwarden — доступ в обход nginx-субпути») — можно вычистить, когда
  дойдут руки.

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
| v0.9 | 2026-08 | Vaultwarden: фикс DOMAIN/sub-path (dedicated `tailscale serve :8446`), апдейт 1.36.0→1.37.2; обновлён протухший TLS-сертификат nginx (каданс в README был неверный — не год, а ~1-2 мес); фикс бага рассинхрона проекта в Заметках портала (кросс-вкладочный localStorage) |
| v0.10 | 2026-10 | TLS-сертификат nginx снова протух → автообновление по cron (`renew_nginx_cert.sh`) + алерт Grafana в Telegram при сроке < 14 дней |
| v0.11 | 2026-09/10 | Портал: Финансы → Крипто (GRAM/TRX/USDT: портфель, индикаторы, планы, советы купить/продать, свечные графики, алерты в Telegram, правило «в минус не продаём») и Траты (импорт PDF-справок Ozon Банка, категории/правила/сплиты, график по месяцам, регулярные платежи); Гараж — подпункты записей (работы/запчасти, итог из подпунктов) и фото/сканы чеков в БД; бот — `/wallet` и меню команд; Ctrl+A в окнах выделяет только содержимое |
