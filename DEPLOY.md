# Деплой: 24/7 без твоего ноута

Чтобы система копировала и собирала данные круглосуточно, она должна жить на **всегда-включённой
машине** (дешёвый VPS или мини-сервер дома). На ноуте, который выключается, — не получится.
Я подготовил всё «под ключ»; нужен только сервер (его заводишь ты — это твой аккаунт и оплата,
я не могу провизионить чужой сервер).

## Что уже готово в репозитории
- `run_all.py` — супервизор: поднимает дашборд + демон снимков, перезапускает упавшее.
- `Dockerfile` + `docker-compose.yml` — контейнер с авто-рестартом (`restart: unless-stopped`),
  переживает падения и ребут сервера.
- `requirements.txt`, состояние (`paper_book.json`, `copy_watchlist.json`, `perf_history_*.jsonl`,
  `wallet_sources.json`) сохраняется на хосте через volume.

## Вариант A — VPS + Docker (рекомендую, ~€4/мес)

Подойдёт Hetzner (CX22), а также Oracle Cloud **Free tier** (бесплатный ARM-инстанс — навсегда).

```bash
# 1) на сервере (Ubuntu): поставить docker
curl -fsSL https://get.docker.com | sh

# 2) скопировать проект на сервер (с твоей машины)
#    (Windows PowerShell): scp -r D:\polymoney root@SERVER_IP:/opt/polymoney
scp -r ./polymoney  user@SERVER_IP:/opt/polymoney

# 3) запустить
cd /opt/polymoney
docker compose up -d --build

# готово. Контейнер сам перезапускается при падении и ребуте.
docker compose logs -f          # смотреть логи
```

**Смотреть дашборд безопасно (он не торчит в интернет):** SSH-туннель с твоей машины —
```bash
ssh -L 5000:127.0.0.1:5000 user@SERVER_IP
# затем открой http://localhost:5000 в браузере
```

## Вариант B — без Docker (systemd на Linux-сервере)
```bash
cd /opt/polymoney && pip install -r requirements.txt
# создать /etc/systemd/system/polymoney.service:
#   [Service]
#   WorkingDirectory=/opt/polymoney
#   ExecStart=/usr/bin/python3 run_all.py
#   Restart=always
#   Environment=DASH_HOST=0.0.0.0 PYTHONUTF8=1
#   [Install] WantedBy=multi-user.target
systemctl enable --now polymoney
```

## Вариант C — мини-сервер дома
Любой всегда-включённый ПК / Raspberry Pi / NAS с Docker: те же 3 команды из варианта A.

## Параметры (env)
`BANKROLL`, `PER_TRADE`, `INTERVAL`, `DASH_PORT`, `DASH_HOST` — задаются в `docker-compose.yml`.

## Важно про РЕАЛЬНЫЕ деньги
Это бумажный контур. Для живой торговли ключ Polymarket НИКОГДА не кладём в этот репозиторий/образ;
исполнение — отдельный модуль под твоим контролем (см. наш план: тикеты на подтверждение / бот с
ключом в env только у тебя). Дашборд за SSH-туннелем, наружу не публиковать.
