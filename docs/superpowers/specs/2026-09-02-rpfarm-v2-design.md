# RunPodFarm v2 («rpfarm») — дизайн

Дата: 2026-09-02. Статус: согласован с владельцем, ждёт план реализации.
Репо: MAY4VFX/houdini_runpod_scheduler. Роль: vfx.

## 1. Цель и UX

Артист в Houdini нажимает Cook. Дальше без его участия: зависимости хипа уезжают на
ферму, поднимаются GPU-поды RunPod, считают рендер или симуляцию, результаты
приходят обратно **ровно в те локальные пути, куда были нацелены ROP-ноды**, поды
гаснут. Прогресс всех стадий виден как work items в PDG, Houdini не блокируется.

Новый пользователь стартует за три команды: клонировать репо, `rpfarm setup`
(ключ RunPod, всё остальное автоматически), первый кук. Никаких своих серверов,
консолей и VPN. Единственные интерфейсы — TOP-ноды в Houdini и CLI `rpfarm`.

Модель использования: одна ферма на одном аккаунте RunPod, несколько доверенных
пользователей, каждый со своим API-ключом RunPod (ключи создаются и отзываются в
консоли RunPod). Тот же репо разворачивается и на чужом аккаунте той же командой
`setup` — отличается только введённый ключ.

## 2. Что было не так в v1 (факты по коду, 2026-09-02)

- Цепочка доставки файлов не замкнута: HDA заливал `$JOB` в B2, воркер клал файлы в
  `/workspace/tasks/<id>/input`, а команда PDG ждала их в `/project`. Ничего не
  писало в `/workspace/projects`.
- Результаты назад: воркер собирал `/workspace/tasks/<id>/output`, куда Houdini не
  пишет; HDA качал весь префикс `results/` после каждой задачи и не распаковывал `.zst`.
- Heartbeat парсился как ISO-дата, а был JSON: всегда исключение, поды никогда не
  считались мёртвыми; задачи упавших подов не перевыставлялись.
- Автоскейл-даун недостижим (ранний `return`), `rpfarm_idletimeout` нигде не читался.
- SHA-256 всех зависимостей пересчитывался на **каждый** work item.
- PDG MQ стартовал на машине артиста, поды должны были подключаться к ней входящим
  соединением — за NAT невозможно.
- Три хранилища (B2, Network Volume, JuiceFS) и домашний Redis через Oracle-socat как
  точка отказа. Сервер, дашборд, auth-api, desktop-app — вокруг JuiceFS, который на
  RunPod не работает (нет FUSE).
- Шаблон RunPod `4tee2rtpu4` хранит пароль Redis в env открытым текстом.

Единственная концептуальная ошибка v1 — JuiceFS и всё, что выросло вокруг него.
Сбор зависимостей, компрессия, cost tracker, идея cache manager — переиспользуются.

## 3. Архитектура

```
Houdini (артист)                              RunPod (EU-RO-1)
┌───────────────────────────────┐            ┌──────────────────────────────────┐
│ rpfarm_upload   (TOP)  ───────┼─ sftp ────►│ sync-под (CPU, ~$0.07/ч)         │
│ rpfarm_scheduler(TOP) ────────┼─ HTTPS ───►│  sshd · rclone · worker.py       │
│    │ PDG MQ client ◄──────────┼─ tcp ──────│  mqserver (PDG, hython с volume) │
│ rpfarm_download (TOP) ◄───────┼─ sftp ─────│  housekeeping (du/ls/rm/ledger)  │
│ rpfarm_stats    (TOP)         │            │           │ /workspace           │
│ CLI rpfarm                    │            │   Network Volume (один, зоны)    │
└───────────────────────────────┘            │           │ /workspace           │
                                             │ GPU-поды ×N (живут только пока   │
                                             │  есть задачи): worker.py, hython │
                                             │  hserver → lic.ai-vfx.com:1715   │
                                             └──────────────────────────────────┘
```

Домашняя инфраструктура участвует только лицензиями: `lic.ai-vfx.com:1715` →
Oracle socat → sesinetd `192.168.2.134`. Tailscale не нужен: поды не могут поднять
TUN, а всё остальное идёт через публичный IP/прокси RunPod. Redis, B2, JuiceFS,
сервер на Dokploy — удаляются.

### 3.1 Компоненты

| Компонент | Где | Роль |
|---|---|---|
| `rpfarm/` (Python-пакет) | машина артиста, внутри Houdini Python 3.11 и как CLI | клиент RunPod REST, синк (rclone), сбор зависимостей, диспетчер, журнал |
| `rpfarm_upload` HDA (TOP processor) | Houdini | work items = пакеты файлов → sync-под |
| `rpfarm_scheduler` HDA (TOP scheduler, PyScheduler) | Houdini | поды, диспатч, MQ, автоскейл, бюджет, автоскачивание выходов, вкладка Farm |
| `rpfarm_download` HDA (TOP processor) | Houdini | work items = пакеты файлов ← sync-под |
| `rpfarm_stats` HDA (TOP processor) | Houdini | аналитика: журнал + биллинг RunPod как work items и итоги |
| `pod/` Dockerfile + `worker.py` + `entrypoint.sh` | образ `ghcr.io/may4vfx/rpfarm-pod` | один образ для sync- и GPU-подов; Houdini берётся с volume |
| CLI `rpfarm` | терминал | setup, doctor, storage, houdini, farm, costs, smoke |

Удаляется из репо: `server/`, `dashboard/`, `auth-api/`, `desktop-app/`, `AWSECS/`,
`infrastructure/provision.py`, `infrastructure/setup-juicefs.sh`, старый `docker/`,
старый `worker/` (executor и compression переезжают), `.worktrees/`.

### 3.2 Sync-под

CPU-под (флейвор `cpu3c`/`cpu5c`, 2–4 vCPU, EU-RO-1) с примонтированным volume,
публичным IP и портами `22/tcp` (sshd), `8000/http` (worker), два `tcp` для MQ.
Поднимает первая нода, которой он нужен; переиспользуется всеми; гаснет по
idle-таймауту (параметр, по умолчанию 15 мин без активных задач и синков) либо
`rpfarm farm kill`. Имя: `rpfarm-sync-<user>`. Одновременно один sync-под на
пользователя; GPU-поды его не заменяют (заливка идёт на дешёвый CPU).

Факты спайка (2026-09-02, EU-RO-1, флейвор `cpu3c`, 2 vCPU, `cloudType: SECURE`,
volume `2ze7qdwkt3`): `POST /v1/pods` с `computeType: "CPU"`, `cpuFlavorIds`,
`vcpuCount`, `dataCenterIds: ["EU-RO-1"]`, `networkVolumeId`, `volumeMountPath`,
`ports: ["22/tcp","4440/tcp","4442/tcp","8000/http"]`, `supportPublicIp: true`
создаёт под и подключает volume без ошибок — CPU-поды с volume в EU-RO-1
работают. Под стал `RUNNING` с присвоенным `publicIp` и заполненным
`portMappings` за **19 секунд**. Ответ `GET /v1/pods/{id}` отдаёт публичный IP
в поле `publicIp` (строка) и маппинг портов в `portMappings` — объекте вида
`{"<internal-port>": <external-port int>}`, например
`{"22": 21829, "4440": 21830, "4442": 21831}`; `8000/http` в `portMappings` не
попадает (http-порты не мапятся напрямую, доступ только через
`https://<podId>-8000.proxy.runpod.net/` — прокси подтверждён, `curl` вернул
200 за 0.48с). `nc -vz <ip> <external-port>` подтвердил, что внешние TCP-порты
4440/4442 открыты и достижимы с внешней машины. `costPerHr` для `cpu3c`/2vCPU —
**$0.06/ч**.

`mqserver` из `/workspace/houdini/bin/mqserver` (Houdini 20.5.684 с volume,
`source houdini_setup_bash` из `/workspace/houdini`) запускается и слушает без
GPU на CPU-поде; connection-файл (`-c /tmp/mq.txt`) содержит
`PDG_MQ <internal-ip> 4440 4440 4442`. Единственная граница, на которую стоит
обратить внимание в реализации: `source <file> | tail` (или любой пайп) в bash
исполняет `source` в сабшелле — переменные окружения (`PATH`, `HFS`) теряются
после пайпа; `rpfarm/pods.py` должен звать `source houdini_setup_bash` без
пайпа в той же команде, что и `mqserver`/`hython`.

Факты по загрузке пода (2026-09-02, Task 4, образ `ghcr.io/may4vfx/rpfarm-pod`):

- **`set -u` и `houdini_setup_bash` несовместимы.** Скрипт SideFX читает
  переменные, которых сам не задаёт (на 21.0.792 это `SHFS`, на других сборках
  `PYTHONPATH`/`LD_LIBRARY_PATH`), а так как его делают `source`, аборт по
  unbound variable убивает **саму оболочку entrypoint**, а не сабшелл. Именно это
  четыре раунда выглядело как «под не поднимается»: контейнер падал сразу после
  запуска sshd, RunPod перезапускал его каждые ~17 секунд, а снаружи это читалось
  как «`publicIp`/`portMappings` появились и исчезли, SSH refused, `/health` 404».
  Любой код, который делает `source houdini_setup_bash`, обязан снимать `set -u`
  на время вызова (`build_shell_command` в `worker.py` безопасен: он работает в
  `bash -c` без `set -u`).
- **Boot-лог живёт на volume.** Логи контейнера RunPod доступны только в его
  веб-интерфейсе, программно их не прочитать. Поэтому entrypoint дублирует весь
  свой stdout+stderr (включая вывод `worker.py` после `exec`) в
  `/workspace/ledger/logs/boot-<podId>-<epoch>.log`. Volume переживает под, так
  что следующий под или ssh-сессия читают, почему умер предыдущий — это
  единственный способ диагностики недоступного пода. Крашлупящийся под пишет по
  файлу на перезапуск, поэтому housekeeping (Task 12) должен чистить
  `boot-*.log` по возрасту/количеству.
- **Cloudflare перед `proxy.runpod.net` режет `Python-urllib/3.x`** — отдаёт
  `403` там, где `curl` получает настоящий ответ. Любой HTTP-клиент к прокси
  (в частности `WorkerClient`, Task 7) обязан слать браузерный `User-Agent`,
  иначе он неверно прочитает каждый ответ.
- **`lastStatusChange` не является признаком рестарта** — поле осталось на
  «Rented by User» через 52 перезапуска контейнера. Здоровье пода определяется
  только опросом `/health`, не полями пода в REST.
- **Образ не должен нести CUDA runtime.** База переведена с
  `nvidia/cuda:12.4.1-runtime-ubuntu22.04` на `ubuntu:22.04`: драйвер и `libcuda`
  на GPU-подах инжектит контейнерный рантайм RunPod, а тулкит в образе стоил
  1310 MiB, которые оплачивались при каждом холодном старте. Слои сжались с
  1505.8 MiB до 137.5 MiB (−90.9%).
- **Тайминги живого пода** (CPU, `HOUDINI_VERSION=.`, volume `2ze7qdwkt3`):
  `/health` 200 через **43.5 секунды** после `POST /pods`, ровно один boot-лог.
  `hserver -S lic.ai-vfx.com:1715` отработал (`Successfully changed server
  listing.`), `hserver -l` показал `Connected To: http://lic.ai-vfx.com:1715`
  (`sesinetd22.0.368`, Houdini 20.5.684), а `hou.licenseCategory()` вернул
  `licenseCategoryType.Commercial` — лицензия реально берётся с фермы. Первый
  запуск `hython` с volume занял **~104 секунды** (холодный кэш сетевого диска);
  таймауты в `WorkerClient` должны это учитывать. Безобидный шум в логах:
  `hserver` пишет `sh: 1: systemctl: not found` и всё равно работает.

### 3.3 GPU-поды

Создаются шедулером через `POST /pods`: шаблон `rpfarm-pod`, volume, `supportPublicIp`,
GPU из приоритетного списка ноды (первый доступный в стоке EU-RO-1 — для этого в теле
запроса `gpuTypePriority: "custom"`, а не дефолтный `"availability"`: по openapi
`"custom"` значит «всегда пытаться арендовать типы в порядке `gpuTypeIds`», тогда как
`"availability"` порядок игнорирует и берёт то, чего у RunPod больше. Разница в цене
трёхкратная: в живом smoke с `"availability"` вместо A4500 (~$0.25/ч) достался
RTX 4090 за $0.740/ч. То же для `cpuFlavorPriority` на sync-поде), env:
`RPFARM_TOKEN`, `RPFARM_ROLE=gpu`, `PDG_RESULT_SERVER=<sync-ip>:<mq-port>`,
`SESINETD_HOST/PORT`, `HFS=/workspace/houdini/<version>`. Имя:
`rpfarm-<user>-<project>-<cook8>-<n>`. Живут только пока шедулер держит для них
задачи; гаснут по idle-таймауту и в `onStopCook`.

### 3.4 worker.py (на каждом поде)

Stdlib-HTTP-демон, ~200 строк, токен в заголовке. Эндпоинты:
`GET /health` (роль, слоты, gpu, uptime, активные задачи), `POST /tasks` (cmd, env,
cwd → task_id), `GET /tasks/{id}` (state, exit_code, started/ended, tail лога),
`GET /tasks/{id}/log`, `DELETE /tasks/{id}` (kill), `POST /exec` (только sync-под:
короткие служебные команды — mqserver, du, ls, rm, tar), `GET /exec/{handle}`
(статус открепления). Доступ снаружи через `https://<podId>-8000.proxy.runpod.net`.

**`/exec` — только для коротких команд; долгие идут через `detach: true` (Ruling R31).**
Прокси RunPod стоит за Cloudflare и рвёт ответ примерно на 100-й секунде, поэтому
синхронный вызов длиннее этого физически недоставляем (`timeout_s` зажат до 90 с).
Цепочка отказа, стоившая Task 14 одной установки Houdini: прокси обрывает ответ на
~100 с → на своей отметке `subprocess.run(timeout=)` шлёт SIGKILL порождённому shell
и закрывает его stdout-pipe → но установщик SideFX — **внук**, он этот SIGKILL
переживает и продолжает распаковку → на следующей записи прогресса в закрытый pipe
получает SIGPIPE и умирает уже после главного tar, но до `install_python` → на диске
остаётся 11 ГБ, выглядящих как готовая установка, без каталога `python/`, и hython
падает на `libpython3.13.so.1.0`. Проверка «файл `bin/hython` на месте» такую
установку принимает. Поэтому: `detach: true` пишет команду в скрипт, запускает её в
отдельной сессии с выводом **в файл** (а не в HTTP-pipe), сразу отдаёт `202` с
`handle`/`log_path`/`rc_path`; код возврата пишется в `.rc` последним действием и
переживает рестарт воркера; вызывающий опрашивает `GET /exec/{handle}` и читает лог
через `GET /files`. Таймаут по размеру пакета остаётся, но как **дедлайн наблюдения**:
его достижение прекращает опрос и сообщает об этом, не трогая саму команду.
Синхронный путь при таймауте убивает всю группу процессов, а не только shell.

Задача = `bash -lc` с `source $HFS/houdini_setup_bash` и переданным окружением PDG;
stdout/stderr стримятся в файл лога на volume (`/workspace/ledger/logs/<cook>/<task>.log`),
чтобы лог переживал под.

### 3.5 Синк: rclone по sftp

Артист-сторона. `rpfarm setup` скачивает статический бинарь rclone под ОС (macOS,
Linux, Windows) в `~/.rpfarm/bin`, генерирует ключ SSH `~/.rpfarm/id_ed25519`;
публичный ключ передаётся подам в env `PUBLIC_KEY` при создании (entrypoint кладёт его
в `authorized_keys`). Передача: `rclone copy --files-from <пакет> --transfers 4
--checkers 8 --sftp-*`, инкрементально по размеру+mtime. Компрессия: опциональная
стадия (см. 4.3).

Абстракция `SyncBackend` с одной реализацией `SftpBackend`; интерфейс оставлен, чтобы
позже добавить `S3Backend` (RunPod S3-API волюма, endpoint `s3api-eu-ro-1.runpod.io`),
если понадобится заливка без пода. Сейчас не реализуется.

### 3.6 PDG-результаты: MQ на ферме

Шедулер использует штатный механизм PDG «MQ на ферме» (как HQueue/Tractor в режиме
Farm): `mqserver` запускается на sync-поде через `/exec`, слушает два tcp-порта,
шедулер подключается по публичному IP sync-пода и его внешним портам, GPU-поды
получают тот же адрес в `PDG_RESULT_SERVER`. `pdgcmd.addOutputFile` и статусы работают
штатно. Параллельно шедулер опрашивает `/tasks/{id}` — источник истины по
завершению и exit code; MQ — источник выходных файлов и атрибутов. Ignore RPC
errors = «When cooking batches» (как у HQueue).

### 3.7 TLS изнутри Houdini

Houdini поставляется со своим OpenSSL, у которого вкомпилированные пути к CA указывают
на сборочную машину SideFX (`/Users/prisms/builder-new/.../ssl/cert.pem`). На машине
любого пользователя этого пути нет, поэтому под `hython` `ssl.create_default_context()`
возвращает контекст с **нулём** сертификатов, и любой https-запрос падает с
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`. Это ломает весь
транспорт v2 разом: REST RunPod, GraphQL-запрос баланса и каждый вызов `WorkerClient`
через `*.proxy.runpod.net` — первый живой smoke умер на этом в `onStartCook`, не создав
ни одного пода. Поэтому **любой https-вызов, который делается изнутри Houdini, обязан
идти через `rpfarm.tls.ssl_context()`** (`urlopen(..., context=rpfarm.tls.ssl_context())`):
модуль ищет настоящий CA-бандл в `RPFARM_CA_BUNDLE`, `SSL_CERT_FILE`, `certifi` (если
импортируется) и в системных путях платформы, и строит из него проверяющий контекст.
Проверка сертификата никогда не отключается: по этим соединениям идут API-ключ аккаунта
и токен воркеров. Касается Task 9 (upload-нода), Task 10 (download-нода) и Task 13 (CLI,
когда его запускают под `hython`).

## 4. Данные

### 4.1 Volume: одна сущность, зоны

Один Network Volume `rpfarm-<owner>` в EU-RO-1 (пользователь решил: один volume,
директории под сущности; растить через `PATCH /networkvolumes/{id}`).

| Зона | Содержимое | Кто пишет | Чистка |
|---|---|---|---|
| `/workspace/houdini/<ver>/` | установленные Houdini (22.0.393 сейчас) | upload-пресет / CLI | `rpfarm houdini rm <ver>` |
| `/workspace/apps/` | плагины, Python-окружения, дистрибутивы | upload custom | вручную |
| `/workspace/projects/<user>/<project>/` | зависимости хипа, выходы | upload, задачи | `storage rm`, `storage prune` |
| `/workspace/ledger/` | журнал задач, логи | шедулер, worker | никогда (ротация логов > 90 дней) |
| `/workspace/.rpfarm/` | индекс использования (размеры, mtime, владелец) | housekeeping | служебное |

Ограничение RunPod, зафиксированное осознанно: **ресайз только вверх**, удаление файлов
не уменьшает счёт. Для этого есть `rpfarm storage recreate --size N`: создать новый
volume, поставить туда Houdini (пресет), переключить конфиг, старый удалить; проекты
пересинхронизируются при следующем куке (локальная копия — источник правды).
Ресайз вверх автоматический: при заполнении > 85% перед заливкой шедулер/upload
увеличивает volume на нужный объём (с округлением до 10 ГБ) и пишет об этом в лог ноды.

Текущий volume `2ze7qdwkt3` (50 ГБ, Houdini 20.5.684) переиспользуется: 20.5 удаляется,
22.0.393 ставится, размер растёт по потребности.

**Ловушка, проверенная вживую (Task 12, Ruling R27):** изнутри пода `shutil.disk_usage`
(и любой `statvfs` на `/workspace`) показывает ёмкость backing storage pool хостовой
машины, а не реальный/оплаченный размер network volume — на настоящем 50 ГБ volume это
дало ~2.14 PiB. У пода нет ключа RunPod API, чтобы узнать реальный размер самому; его
обязан передавать вызывающий (`RunPodAPI.get_volume(volume_id)["size"]`, кешируется —
`rpfarm.packages.get_volume_size_gb`). `pod/housekeeping.py`'s `ls`/`disk-usage` берут
размер только через `--volume-size-gb`; без него `total`/`used_pct` — `null`, а не
подставленное число из `shutil.disk_usage`.

### 4.2 Зависимости и маппинг путей

`rpfarm/deps.py`: `hou.fileReferences()` (раскрытые `$HIP/$JOB`, `$F`, UDIM, папки
секвенций целиком) + сам `.hip` + входные файлы work items апстрима. Никакого ручного
выявления. Хеширования нет; сравнение делает rclone.

PDG Path Map: `$JOB` ↔ `/workspace/projects/<user>/<project>`. Файлы вне `$JOB`
уезжают в `…/<project>/_ext/<абсолютный путь без корня>` и получают отдельные записи
в path map, так что хип на поде находит их без правок. `$HIP` вне `$JOB` — та же
логика.

Выходы: work item сообщает выходной файл (удалённый путь) через MQ; шедулер
делокализует его и ставит в очередь скачивания **сразу**, не в конце кука. Файл
ложится ровно туда, куда был нацелен ROP. `rpfarm_download` в режиме «Upstream
outputs» делает то же для всего апстрима (для повторного забора), в режиме «Custom
paths» — любые пути volume → локально.

### 4.3 Компрессия (переиспользуется из v1)

Опциональная стадия заливки в `rpfarm_upload`: `classify_file` из v1 решает по типу
и пробе сжимаемости; `.hip`, `.abc`, `.usd*`, несжатые VDB/bgeo → zstd на лету в
staging, на sync-поде post-шаг распаковывает. Уже сжатые (`.bgeo.sc`, `.exr` с
компрессией, `.rat`, `.tex`) пропускаются. Включается параметром ноды, по умолчанию
включена для uplink < 200 Мбит/с (измеряется в `doctor`).

### 4.4 Журнал и стоимость

- **Детали задач**: шедулер пишет `/workspace/ledger/<user>/<cook_id>.jsonl`, запись на
  work item: `cook_id, user, project, node, item, frame, pod, gpu, started, ended,
  duration_s, exit_code, cost_est`. `cost_est` = тариф пода × длительность + доля
  простоя пода в этом куке.
- **Локальная копия**: в конце кука и по кнопке «Sync ledger» новые записи (свои и
  чужие, что есть на volume) тянутся в `~/.rpfarm/ledger/`.
- **Деньги (истина)**: `GET /billing/pods` и `GET /billing/networkvolumes` RunPod по
  именам подов `rpfarm-<user>-<project>-<cook8>-*`. Видит всех пользователей аккаунта.
- **`rpfarm_stats`**: сшивает журнал и биллинг, work item = задача, поля = атрибуты;
  параметры-фильтры (период, проект, пользователь); итоги в UI ноды: $ всего, GPU-часы,
  $/кадр, разбивка по проектам и людям, стоимость volume, кандидаты на чистку
  (проекты без куков > N дней, с $/мес). Кнопка CSV. CLI-эквивалент `rpfarm costs`.

## 5. Ноды (параметры — только реально используемые)

**rpfarm_upload** — Mode: Project dependencies | Custom paths; Package size (ГБ, 1.5);
Compression (auto/on/off); Custom: multiparm (local → remote), Post-command (на
sync-поде после пакета). Пресеты: «Install Houdini from tarball» (источник —
локальный файл или `sftp://host/path`, назначение `/workspace/apps/dist/`, post:
распаковать и `houdini.install --auto-install --accept-EULA 2021-10-13
--no-install-license --no-install-menus --no-install-avahi --no-install-hfs-symlink
--no-install-bin-symlink --make-dir /workspace/houdini/<ver>` — каталог установки у
`houdini.install` позиционный, флага `--install-dir` нет; проверено на 22.0.393
в Task 14). Атрибуты item: files, bytes, seconds, MB/s.

**rpfarm_scheduler** — вкладки: *Farm* (GPU priority list, Min/Max pods, Tasks per pod,
Idle timeout, Budget limit $ на кук, Sync-pod idle); *Paths* (project name = имя папки
`$JOB` по умолчанию, дополнительные path maps); *Status* (живые поды, задачи, $ кука,
кнопки Kill all / Kill pod / Sync ledger); *Volume* (браузер зон и проектов: размер,
mtime; кнопки Delete project, Prune, Grow volume, Recreate). Внутри: диспетчер
(least-loaded, ретрай ×2 при смерти пода), автоскейл (прогноз по скользящему среднему,
как в v1, с починенным idle), бюджет (80% предупреждение, 100% стоп подъёма и
дожить текущие, 120% гасить всё).

**rpfarm_download** — Mode: Upstream outputs | Custom paths; Package size; Overwrite
policy (newer/always/never). Атрибуты как у upload.

**rpfarm_stats** — Source: local ledger + RunPod billing; фильтры; итоги; CSV.

## 6. CLI `rpfarm`

`setup` (ключ RunPod → `~/.rpfarm/config.toml`; SSH-ключ; rclone; поиск Houdini и
установка HDA в `otls`; найти/создать volume и шаблон; проверить лицензии) ·
`doctor` (всё то же в режиме проверки + замер uplink + сток GPU в EU-RO-1) ·
`houdini install --tar <path|sftp url> --version 22.0.393` / `houdini ls` / `houdini rm` ·
`storage ls|du|rm|prune|grow|recreate` · `farm status|kill [--all|--pod]` ·
`costs [--by user|project] [--month]` · `smoke` (сквозной тест, п. 8).

Все команды — тонкие обёртки над теми же модулями `rpfarm/`, что используют HDA.

## 7. Ошибки и границы

- Под умер (нет `/health` 60 с): его задачи → ретрай на другом поде (до 2), потом fail
  work item. Логи на volume сохраняются.
- Houdini закрылся/кук отменён: `onStopCook` гасит поды кука; сироты (по префиксу
  имени и cook_id без живого шедулера) показываются в Status и убираются `farm kill`.
  При старте любого кука шедулер сначала ищет сирот своего пользователя и предлагает
  их убить.
- Нет GPU в стоке: перебор списка приоритетов; если пусто — ошибка кука с текстом и
  ссылкой на `doctor`. Volume привязан к EU-RO-1; смена DC = `storage recreate` в
  другом DC (тот же механизм).
- Лицензии: `doctor` проверяет `lic.ai-vfx.com:1715`; задача с ошибкой лицензии в логе
  не ретраится, шедулер останавливает кук с понятным сообщением.
- Баланс RunPod ниже порога (параметр): предупреждение перед куком.
- Секреты: только `~/.rpfarm/config.toml` (chmod 600) и env подов; в репо — имена.
  Старый шаблон `4tee2rtpu4` с паролем Redis в env удаляется.

## 8. Тестирование

- Юнит (pytest, без Houdini): `deps` (на фикстурах путей), `sync` (планировщик пакетов,
  files-from), `ledger` (записи, сшивка с биллингом), `runpod_api` (моки HTTP),
  `worker.py` (локальный запуск, задачи, kill).
- Интеграция `rpfarm smoke`: поднять sync-под → залить тестовый хип (3 кадра, Karma CPU
  и один GPU-кадр) → шедулер поднимает 1 GPU-под → кадры приходят в локальный путь →
  поды погашены → в журнале и биллинге видны записи. Критерий готовности проекта.
- Ручная проверка на реальном шоте 5–50 ГБ: время заливки, инкрементальность второго
  кука, скачивание по мере рендера.

## 9. Миграция с v1 (часть плана)

1. Удалить с volume `/workspace/houdini` (20.5.684), поставить 22.0.393 пресетом upload
   (тарболл с mayfx02 `/home/may/Downloads/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz`
   или с Mac). Скачивание с sidefx.com с mayfx02 не использовать (SNI-фильтрация,
   см. departments/agents/map.md).
2. Новый шаблон `rpfarm-pod`; старый `4tee2rtpu4` удалить.
3. Dokploy: удалить проект `runpodfarm` (сервер + Redis). Oracle: снять socat 6381,
   оставить 1715. Домен `redis-runpod.ai-vfx.com` и `db.ai-vfx.com` — убрать.
4. Штаб: обновить `departments/vfx/map.md` (RunPodFarm v2, без Redis/B2/сервера).
5. Локально: удалить старую HDA из `otls`, `RunPodFarm.app` из /Applications.

## 10. Вне скоупа (осознанно)

Заливка без пода через S3-API (интерфейс оставлен) · отдельные волюмы под зоны ·
серверный учёт с выдачей токенов · веб-дашборд · Windows-инсталлятор с GUI (CLI
работает и на Windows) · логин-лицензии SideFX.
