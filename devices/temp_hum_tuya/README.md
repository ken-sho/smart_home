# Устройство: Tuya датчик температуры/влажности

**Плата:** RH-MAGv3B E1  
**Модуль:** CBU (BK7231N)  
**MAC:** d8:d6:68:38:c5:30 (генерируется ESPHome) / d8:d6:68:38:bb:5e (оригинальный)  
**Сенсор:** SHT3X (маркировка U3, на плате подписан как CHT8305 — но работает через SHT3X драйвер)  
**Питание:** 2x AAA (тест с Li-ion 1.5V)  
**Статус:** В работе на OpenBK

---

## Железо

| Компонент | Описание |
|-----------|----------|
| MCU | BK7231N (CBU модуль) |
| Сенсор T/H | SHT3X / CHT8305 совместимый, I2C, U3 на плате |
| ADC батарейки | P23 |
| Питание схемы измерения | P17 (BAT_Relay) |
| Кнопка | P20 |
| LED | P26 |

---

## Пины (рабочая конфигурация OpenBK)

| Пин | Роль | Канал | Примечание |
|-----|------|-------|------------|
| P7 | SHT3X_SCK | — | SCL сенсора |
| P8 | SHT3X_SDA | 2, 3 | SDA сенсора (температура=2, влажность=3) |
| P17 | BAT_Relay | 0 | Питание схемы измерения батарейки |
| P20 | Btn_SmartLED | 0 | Кнопка + индикация LED при нажатии |
| P23 | BAT_ADC | — | АЦП напряжения батарейки |
| P26 | LED | — | Индикатор |

> **Важно:** Из user_param_key оригинальной прошивки пины i2c указаны как P7=SCL, P8=SDA — это верно. Но драйвер CHT83XX не работает — нужно использовать SHT3X драйвер с каналами 2 и 3 для SDA.

---

## Инструменты для прошивки

### Адаптер
- **Модель:** USB-TTL конвертер на CH340G
- **Порт:** `/dev/ttyUSB0`
- **VID:PID:** `1a86:7523`

### Ноутбук
- **ОС:** Ubuntu
- **Инструменты:**
```bash
pipx install bk7231tools    # чтение/запись флеша
pipx install ltchiptool     # работа с UF2, прошивка через OTA формат
```

### Подключение CH340G к датчику

| Плата (датчик) | CH340G |
|----------------|--------|
| GND | GND |
| RXD | TXD |
| TXD | RXD |

**Важно:**
- Перемычку RXD/TXD на адаптере **снять**
- Питание датчика во время прошивки — **от батареек**, провод V от адаптера не подключать (иначе адаптер переподключается)
- Для входа в режим прошивки: **зажать кнопку P20** при включении питания

### Режим прошивки
1. Подключить CH340G к датчику (GND, RXD, TXD)
2. Зажать кнопку на датчике
3. Вставить батарейки
4. Отпустить кнопку
5. Датчик в режиме прошивки — можно писать/читать флеш

---

## Прошивка

**Используется:** OpenBK7231N 1.18.288_sensors

### Путь прошивки (первый раз)

Стандартный путь через ltchiptool не работает — bootloader загружает только с `0x011000` (защищён от записи), а `0x132000` не используется.

**Рабочий путь:**

1. Сделать бэкап оригинальной прошивки:
```bash
bk7231tools read_flash -d /dev/ttyUSB0 bk7231n full_flash.bin
cp full_flash.bin backup_original.bin
```

2. Скачать ESPHome Kickstart:
```bash
curl -s https://api.github.com/repos/libretiny-eu/esphome-kickstart/releases/latest \
  | grep browser_download_url | grep "kickstart-bk7231n" \
  | cut -d'"' -f4 | xargs wget -O kickstart.uf2
```

3. Распаковать UF2:
```bash
mkdir kickstart_dump
ltchiptool uf2 dump -o kickstart_dump kickstart.uf2
# Получим три файла:
# uf2dump_bk7231n_device_0x12A000.bin  — образ Kickstart
# uf2dump_bk7231n_flasher_0x011000.bin — flasher
# uf2dump_bk7231n_flasher_0x129F0A.bin — флаг переключения слота
```

4. Записать флаг переключения слота:
```bash
bk7231tools read_flash -d /dev/ttyUSB0 -s 0x129000 -l 0x1000 block_129000.bin
dd if=kickstart_dump/uf2dump_bk7231n_flasher_0x129F0A.bin \
   of=block_129000.bin bs=1 seek=$((0xF0A)) conv=notrunc
bk7231tools write_flash -d /dev/ttyUSB0 -s 0x129000 block_129000.bin
```

5. Записать Kickstart:
```bash
bk7231tools write_flash -d /dev/ttyUSB0 -s 0x12A000 \
  kickstart_dump/uf2dump_bk7231n_device_0x12A000.bin
```

6. Включить датчик — появится точка `kickstart-bk7231n`
7. Подключиться, открыть `http://192.168.4.1`, ввести WiFi credentials
8. Залить OpenBK через ESPHome OTA (порт 8892):
```bash
# Скачать OpenBK UA бинарник
wget https://github.com/openshwprojects/OpenBK7231T_App/releases/download/1.18.288/OpenBK7231N_UA_1.18.288_sensors.bin

# Залить через UART
bk7231tools write_flash -d /dev/ttyUSB0 -s 0x011000 OpenBK7231N_UA_1.18.288_sensors.bin
```

### Обновление прошивки (OTA)

После первой установки обновления через веб-интерфейс OpenBK: `http://<IP>/index` → OTA

---

## Настройка OpenBK

### Configure Module (пины)

| Пин | Роль | Канал 1 | Канал 2 |
|-----|------|---------|---------|
| 7 | SHT3X_SCK | — | — |
| 8 | SHT3X_SDA | 2 | 3 |
| 17 | BAT_Relay | 0 | — |
| 20 | Btn_SmartLED | 0 | — |
| 23 | BAT_ADC | — | — |
| 26 | LED | 0 | — |

### Startup Command

```
Battery_Setup 2200 3100 1.365 4096 4096
PowerSave 1
```

### Калибровка батарейки

Для Li-ion AAA 1.5V (2 шт):
- Min: 2200mV (разряжен)
- Max: 3100mV (заряжен)
- vdivider: 1.365

Для щелочных AAA (2 шт):
- Min: 2200mV
- Max: 3000mV
- vdivider: 1.365 (подобрать по реальному напряжению)

---

## Карта флеша BK7231N

| Адрес | Содержимое |
|-------|------------|
| 0x000000 | Bootloader |
| 0x011000 | App (Tuya / OpenBK UA) — **bootloader грузит отсюда** |
| 0x129000 | Флаг переключения слота OTA |
| 0x12A000 | App slot 2 (Kickstart / OpenBK OTA) |
| 0x1d1000 | OpenBK конфиг (WiFi credentials, настройки) |
| 0x1d4000 | RF partition (калибровка радио) — **не стирать!** |
| 0x1ee000 | Tuya storage (WiFi credentials для Tuya прошивки) |
| 0x1f4000 | Конец флеша |

---

## Известные проблемы и решения

### CHT83XX драйвер показывает 125°C/100%
Сенсор на этой плате совместим с SHT3X протоколом. Используй **SHT3X** драйвер вместо CHT83XX. Каналы SDA должны быть **2 и 3**, не 1 и 2.

### Bootloader не загружает новую прошивку
Bootloader BK7231N на этом устройстве однослотовый — всегда грузит с `0x011000`. Для загрузки с `0x12A000` нужен специальный флаг по адресу `0x129F0A`.

### OpenBK уходит в safe mode (AP режим) после смены пинов
Нормальное поведение при смене критичных пинов. Не используй P20 для i2c — это кнопка, конфликт вызывает краш при загрузке.

### Storage заполнен нулями — датчик не стартует
При стирании storage командой `dd if=/dev/zero` нужно заполнять FF а не нулями:
```bash
dd if=/dev/zero bs=1 count=32768 | tr '\000' '\377' > storage_ff.bin
bk7231tools write_flash -d /dev/ttyUSB0 -s 0x1ee000 storage_ff.bin
```

---

## Данные из оригинальной прошивки (user_param_key)

```json
{
    "i2c_scl_pin": 7,
    "i2c_sda_pin": 8,
    "net_led_pin": 26,
    "bt_pin": 20,
    "samp_pin": 23,
    "samp_sw_pin": 17,
    "max_V": 3000,
    "min_V": 2200,
    "module": "CBU"
}
```

---

## Файлы в директории

| Файл | Описание |
|------|----------|
| `README.md` | Эта карточка |
| `backup_original.bin` | Полный дамп оригинальной Tuya прошивки (2MB) — хранить! |
| `backup_original_user_param_key.json` | Пины и параметры из оригинальной прошивки |
| `backup_original_storage.json` | Storage оригинальной прошивки (WiFi, device ID и др.) |
| `kickstart_fresh.uf2` | ESPHome Kickstart v25.12.05 для BK7231N |
| `kickstart_fresh_dump/` | Распакованный Kickstart: флаг переключения слота (`0x129F0A`) и образы |
| `OpenBK7231N_sensors_fresh.bin` | OpenBK 1.18.288_sensors OTA образ (.bin) |
| `OpenBK7231N_UA_sensors.bin` | OpenBK 1.18.288_sensors UA образ для записи через UART по `0x011000` |

---

## TODO

- [ ] Настроить MQTT → Home Assistant
- [ ] Настроить deep sleep для экономии батарейки
- [ ] Добавить фото платы
- [ ] Откалибровать температуру (чип греет датчик, +2-3°C смещение)
- [ ] Прошить и настроить остальные 5 датчиков из партии
