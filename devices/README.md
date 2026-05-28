# Devices

Здесь хранится всё касательно IoT-устройств умного дома:

- перечень устройств и их текущий статус
- карточки устройств с описанием перепрошивки
- конфиги ESPHome / Tasmota
- прошивки (например MikroTik)

---

## Инструменты и окружение

### Адаптер для прошивки
- **Модель:** Конвертер USB-TTL на базе CH340G
- **Порт:** `/dev/ttyUSB0`
- **VID:PID:** `1a86:7523` (QinHeng Electronics)

### Ноутбук (shogun-AuBox)
- **ОС:** Ubuntu
- **ESPHome CLI:** 2026.4.5 (установлен через pipx)
- **Пользователь:** `shogun` — в группе `dialout`

### Установка инструментов на Ubuntu
```bash
sudo apt install pipx
pipx install esphome
pipx install bk7231tools
pipx install ltchiptool
pipx ensurepath
sudo usermod -aG dialout $USER
newgrp dialout
```

### Workflow перепрошивки
1. **Первая прошивка** — с ноутбука через USB (CH340G)
2. **Дальнейшие обновления** — OTA через веб-интерфейс OpenBK или ESPHome на Core
3. **Конфиги устройств** — хранятся в поддиректориях этой папки

---

## Устройства

| Устройство | MAC | IP | Статус |
|------------|-----|----|--------|
| temp_sensor_2 | d8:d6:68:38:bb:5e | 192.168.11.10 | в работе (OpenBK) |

---

## Карточки устройств

| Директория | Устройство | Прошивка |
|------------|------------|----------|
| `temp_hum_tuya/` | Tuya датчик T/H, плата RH-MAGv3B, модуль CBU (BK7231N) | OpenBK 1.18.288_sensors |
