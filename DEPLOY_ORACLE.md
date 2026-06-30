# Деплой на Oracle Cloud Free Tier (24/7, бесплатно навсегда)

Папка `deploy_bundle/` — это всё, что нужно на сервере (8 МБ, без кэша). Дальше по шагам.

## 1. Завести инстанс (один раз)
1. Регистрация: https://www.oracle.com/cloud/free/ — нужна карта для верификации, **Always Free не списывает**.
2. Console → ☰ Menu → **Compute → Instances → Create instance**.
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape:** Change shape → **Ampere (VM.Standard.A1.Flex)** → 1–2 OCPU, 6–12 GB
     (Always Free даёт до 4 OCPU / 24 GB). Если пишет «out of capacity» — повтори позже или
     выбери Always-Free **VM.Standard.E2.1.Micro** (AMD). Образ у нас мультиарх — пойдёт и там.
   - **SSH keys:** добавь свой публичный ключ. Сгенерировать на Windows (PowerShell):
     ```powershell
     ssh-keygen -t ed25519        # Enter на все вопросы
     type $env:USERPROFILE\.ssh\id_ed25519.pub   # вставь это в Oracle
     ```
   - **Create.** Запиши **Public IP**.

> Порт 5000 наружу открывать НЕ нужно — дашборд смотрим через SSH-туннель (безопасно).
> Открыт только SSH(22), он по умолчанию доступен.

## 2. Поставить Docker на сервере
```bash
ssh ubuntu@PUBLIC_IP
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu && exit      # перелогиниться, чтобы docker без sudo
```

## 3. Закинуть бандл (с твоей Windows-машины, PowerShell)
```powershell
scp -r D:\polymoney\deploy_bundle ubuntu@PUBLIC_IP:/home/ubuntu/polymoney
```

## 4. Запустить (на сервере)
```bash
ssh ubuntu@PUBLIC_IP
cd ~/polymoney
docker compose up -d --build
docker compose logs -f          # убедиться, что поднялось (Ctrl+C для выхода из логов)
```
Контейнер с `restart: unless-stopped` — сам переживает падения и ребут сервера. Состояние
(`paper_book.json`, `perf_history_*`) пишется в эту же папку на сервере и сохраняется.

## 5. Смотреть дашборд (с твоей машины)
```powershell
ssh -L 5000:127.0.0.1:5000 ubuntu@PUBLIC_IP
# затем открой http://localhost:5000
```

## Управление
```bash
docker compose restart        # перезапуск
docker compose down           # стоп
docker compose up -d --build  # обновить после правок кода
git ... / scp ...             # обновить файлы и пересобрать
```

## Обновление watchlist без рестарта
Дашборд перечитывает `copy_watchlist.json` каждый цикл — правишь файл на сервере, изменения
подхватываются сами (как и у нас локально).

## Реальные деньги (потом)
Ключ Polymarket в этот образ НЕ кладём. Исполнение — отдельный модуль под твоим контролем
(тикеты на подтверждение / бот с ключом в env только у тебя). Дашборд держим за SSH-туннелем.
