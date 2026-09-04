<!-- hub-kit identity block (добавлен /project-register; не удалять) -->
# houdini_runpod_scheduler — проект системы may-hub

Ты — агент роли, указанной в `.dept.md` (симлинк на слой роли в штабе).
Штаб: MAY4VFX/may-hub (~/Github/may-hub).

**Обязательно перед работой** (если твой раннер не подгрузил это сам):
1. Прочитай `.dept.md` в корне этого репо — правила твоей активной роли.
2. `git -C ~/Github/may-hub pull`, затем HQ.md штаба и открытые issues своей роли по
   этому проекту.

В конце сессии: `/sync` (нет скиллов — вручную: work-record-комментарии в затронутые
issues, статусы на доске, push).

@./.dept.md

<!-- Всё ниже — local-правила проекта. Они СИЛЬНЕЕ правил роли. Секции ниже маркера
     НИКОГДА не трогает project-register --update. -->
<!-- /hub-kit identity block -->

## Что это

RunPodFarm v2 — рендер/симуляционная ферма для SideFX Houdini на RunPod GPU Pods.
Управляется целиком из Houdini: **сервера нет, Redis нет, B2 нет, dashboard нет**.
Каждая сессия Houdini сама поднимает и гасит поды через RunPod REST API, раздаёт
задачи по HTTP на воркер внутри пода и делит с ним общий Network Volume.

```
Houdini (PDG)
  runpodfarm_scheduler ──REST──> RunPod API      (создать/убить GPU-поды)
        │            └───HTTP──> worker.py на поде (submit/poll/kill задач)
  runpodfarm_upload   ──sftp───> sync-под (CPU) ─┐
  runpodfarm_download <──sftp─── sync-под (CPU) ─┤
  runpodfarm_stats                                │
                                                  ▼
                              Network Volume `2ze7qdwkt3` (EU-RO-1, /workspace)
                              зоны: houdini/ apps/ projects/ ledger/
```

Лицензии — sesinetd через Oracle: `lic.ai-vfx.com:1715` → socat-юнит
`lic-proxy.service` → домашний лицензионный сервер (его адрес — в
`departments/infra/map.md` штаба, сюда не пишем: см. правило про домашние IP ниже).
Поду не нужен ни ключ RunPod, ни доступ к чему-либо, кроме тома и лицензий.

## Структура

```
rpfarm/           — общий Python-слой (только stdlib), его же импортируют HDA
  cli.py            CLI `python3 -m rpfarm`: setup, doctor, houdini, storage, farm, costs
  config.py         ~/.rpfarm/config.toml (RPFARM_HOME переопределяет), token, rclone
  runpod_api.py     REST + GraphQL RunPod (поды, тома, шаблоны, биллинг, наличие GPU)
  pods.py           жизненный цикл подов, sync-под, env пода
  worker_client.py  HTTP-клиент воркера пода (/health /tasks /exec)
  dispatch.py       раздача work items по подам и слотам
  sync.py           rclone/sftp-перегон, планирование пакетов, сжатие
  packages.py       планы upload/download, пресет установки Houdini, авторост тома
  deps.py           сбор зависимостей hip-файла и их path-map
  ledger.py         локальный журнал куков и стоимости (~/.rpfarm/ledger)
  package_runner.py запуск одного work item вне hython
  houdini_local.py  поиск локальных установок Houdini, установка HDA
  compression.py    упаковка перед заливкой
  tls.py            самоподписанный TLS для воркера

pod/              — образ пода (CI собирает ghcr.io/may4vfx/rpfarm-pod)
  Dockerfile        база + rpfarm/, без Houdini (Houdini берётся с тома)
  entrypoint.sh     монтирование тома, HFS с тома, hserver на лицензии, запуск воркера
  worker.py         HTTP-воркер: /health, /tasks (submit/poll/kill), /exec (только sync)
  housekeeping.py   обслуживание тома: ls, du, touch, rm, prune, houdini ls|rm,
                    invalidate <zone>, disk-usage, sync-idle

hda/              — четыре HDA в развёрнутом (expanded) виде
  runpodfarm_scheduler.hda   PDG-шедулер: поды, слоты, бюджет, вкладка Volume
  runpodfarm_upload.hda      заливка (deps/custom) + пресет установки Houdini
  runpodfarm_download.hda    выкачивание (outputs/custom)
  runpodfarm_stats.hda       статистика кука

scripts/          — сборка HDA (build_runpodfarm_*_hda.py) и headless-смоуки
tests/            — pytest, без сети и без Houdini
docs/superpowers/ — спека и план v2
```

## Команды

```bash
python3 -m pytest -q                 # тесты (ничего наружу не ходят)

python3 -m rpfarm setup              # ~/.rpfarm, том, шаблон, HDA — безопасно перезапускать
python3 -m rpfarm doctor             # сквозная проверка: ключ, том, шаблон, HDA, наличие GPU
python3 -m rpfarm houdini ls         # версии Houdini на томе
python3 -m rpfarm houdini install --tar <путь|sftp://host/путь> --version 22.0.393
python3 -m rpfarm houdini rm <версия|legacy> --yes
python3 -m rpfarm storage ls|du|rm|prune|grow|recreate
python3 -m rpfarm farm status        # живые поды rpfarm-* со ставкой и оценкой стоимости
python3 -m rpfarm farm kill --all    # погасить всё
python3 -m rpfarm costs              # журнал + биллинг

# HDA пересобрать и поставить в ~/Library/Preferences/houdini/22.0/otls/
python3 scripts/build_runpodfarm_upload_hda.py

# смоук против живой фермы (тратит деньги, поднимает под)
RPFARM_ROOT=$PWD hython scripts/smoke_scheduler_headless.py
# то же бесплатно, на локальном шедулере PDG:
RPFARM_ROOT=$PWD hython scripts/smoke_scheduler_headless.py --scheduler localscheduler
```

**Удаление всегда с явным подтверждением**: `--dry-run` — поведение по умолчанию,
реальное удаление требует `--yes`/`--force`.

## Что где живёт

- Конфиг артиста: `~/.rpfarm/config.toml` (chmod 600) — ключ RunPod, id тома и
  шаблона, датацентр, версия Houdini, лицензионный хост, список GPU. В репо его нет.
- Том `2ze7qdwkt3` (EU-RO-1, 50 ГБ), зоны `/workspace/{houdini,apps,projects,ledger}`.
  `houdini` и `ledger` защищены от `prune`/`rm`.
- Шаблон RunPod `rpfarm-pod` = `3i1l2ufjts`, образ `ghcr.io/may4vfx/rpfarm-pod`.
  CI (`.github/workflows/docker-build.yml`) на пуш в `pod/**` собирает образ и
  двигает шаблон; id шаблона — переменная репо `RPFARM_TEMPLATE_ID`, ключ — секрет
  `RUNPOD_API_KEY`.
- Префикс параметров HDA — `rpfarm_`.

## Правила

- Комментарии вида «ported from v1's `worker/…`, `infrastructure/…`, `docker/…`» ссылаются
  на код v1, удалённый в Task 14: искать его в истории git (до коммита чистки), в рабочем
  дереве этих каталогов больше нет.

- Секреты никогда не коммитятся; ключ RunPod читается из `~/.rpfarm/config.toml`
  в момент использования, домашние IP в отслеживаемые файлы не пишем.
- Не оставлять поды running: любой сценарий, поднимающий под, гасит его в `finally`.
- Никаких новых зависимостей: `rpfarm/` и `pod/` — только стандартная библиотека.
- Коммит и пуш после правок; conventional commits.
