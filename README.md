# RunPodFarm 2.4

Houdini TOPs/PDG на RunPod: зависимости и результаты находятся на одном Network
Volume; CPU sync-под обеспечивает передачу файлов и общий MQ, GPU-поды выполняют
задачи. Отдельного сервера приложения или веб-дашборда нет.

## Начало работы

Из клона репозитория:

```sh
python3 -m rpfarm setup
python3 -m rpfarm doctor
```

Настройки и ключ RunPod хранятся в `~/.rpfarm/config.toml`. Лицензионный сервер
SideFX задаётся в этом конфиге; публичного сервера лицензий по умолчанию нет.
Работа с файлами использует rclone/SFTP. Python-код runtime требует Python 3.11+,
без дополнительных Python-зависимостей; UI использует PySide из Houdini.

В TOP-сети: **TAB → RunPod Farm Setup**. Создаётся цепочка:

```text
Upload → Wait for All → Render → Download
             + один farm scheduler
```

Выберите ROP на Render и GPU/лимиты на scheduler. Обычный Cook конечного Download
выполняет всю необходимую цепочку. Повторный cook использует кэш PDG и проверенные
локальные результаты.

## Один контекст фермы

Upload, Download и управление томом получают настройки выбранного scheduler:
account, volume, template, datacenter и Project. Upload наследует Project;
`Override Project` — явное исключение. File Review показывает фактическое
назначение. Несовместимый volume существующего sync-пода вызывает понятную ошибку,
а не незаметную отправку на другой том.

Отдельные процессы передачи получают ссылку на приватный локальный снимок
настроек. Ключи не записываются в work-item JSON или атрибуты передачи.

## File Review и прогресс upload

`Review Local / Farm Files…` и автоматический preflight перед cook открывают одно
окно с двумя деревьями: локальные ссылки сцены и файлы проекта на ферме.

- Зелёный: одинаковые размер и время изменения. Янтарный: различаются.
- `Unknown` означает недоступное сравнение, а не отсутствие файла.
- Галочки выбирают upload. Выделенные строки выбирают файлы для удаления.
- Локальные файлы идут в Trash; удаление на ферме требует подтверждения.
  Защищённые/активные проекты и чужие данные не удаляются.
- `Reset Upload Selection` возвращает исходный выбор. Подтверждённое удаление
  выполняется сразу и не отменяется закрытием окна.

Ручное открытие заканчивается **Save Selection**, без отправки. Перед уже
запущенным cook кнопка называется **Continue Upload**.

В том же окне доступны `Packages by size` и `One work item per file`, лимит
пакета и список его файлов. Например, 106 файлов общим размером 1,37 GiB могут
быть одним item при лимите 1,5 GiB. Совпадающие файлы пропускаются до сжатия:
выбранный размер не является обещанием объёма передачи.

Панель work item использует штатные cook percentage и custom state Houdini.
Для Task Graph Table доступны `phase`, `progress`, `bytes_done`,
`bytes_total`, `speed_mib_s`, `eta_seconds`, `current_files`,
`files_skipped`, `package_index`, `package_count`, `package_files`.
ETA относится к передаче; до измерения скорости она неизвестна. Упаковка,
передача и распаковка — разные фазы. Ошибка доступа 401/403 сразу объясняется
как отказ авторизации.

## Как выполнять работу

| Режим | Где контроллер | Что можно выключить |
|---|---|---|
| Обычный Cook | Текущая сессия Houdini | Оставьте Houdini открытым до конца |
| Run Job → On This Computer (Background) | Отдельный локальный hython | Houdini можно закрыть; компьютер должен работать |
| Run Job → On Farm | Отдельный CPU host-под | После завершения отправки можно выключить компьютер |

В **Run Job** задаётся явный `Farm Target` — compute/render TOP. Локальный
background доводит его работу через Download до локальных файлов; farm-режим
останавливается на выбранной compute-границе. Upload выполняется на отправляющей
машине до аренды host-пода.

Farm job получает собственные HIP/HDA/Python, checkpoint и каталог по job ID.
Snapshot сохраняет логическое расположение HIP и поведение `$HIP`/`$HIPNAME`.
Host публикует checkpoint и manifest результатов, затем гасит свои render-поды
и себя. Общий MQ принадлежит sync-поду: завершение одного кука не убивает MQ
другого.

На **Status** выбирается сохранённый Job; видны состояние, счётчики work items,
target и лог. Есть `Cancel Selected Job`, `Show Job Log File` и
`Refresh Farm / Ledger`. Остановка выбранной job отличается от
`Stop My Farm…`, которая относится ко всем подам этого пользователя.
Обновление статуса выполняется без сетевых запросов в UI-потоке.

## Получение результатов после возвращения

Откройте сохранённую сцену и выполните обычный **Cook Download**. После Submit
нужная job уже выбрана в `Results From`; другую отправку можно выбрать там же.

Штатный Switch внутри Download исключает upstream из cook для выбранной job.
Затем восстанавливаются checkpoint и manifest, и работает общий downloader.
Upload/render не запускаются повторно. Manifest нужен, потому что legacy
checkpoint Houdini не предоставляет динамические атрибуты во всех контекстах
генерации одинаково.

Скачанные файлы зарегистрированы как PDG Output Files. Локальные receipts и
блокировки исключают повторную передачу тех же результатов, в том числе между
старым автоскачиванием scheduler и Download TOP. Совпадение локальной копии
проверяется перед повторным использованием.

`Custom Paths` получает указанные каталоги без запуска подключённого рендера.
Отмена или ошибка скачивания оставляет результаты на ферме. Автоочистка проверяет
доставку конкретного cook; наличие любого локального файла само по себе не
считается доказательством доставки соответствующего результата.

## Установка обновления

После изменений кода разработчик собирает согласованный комплект:

```sh
hython scripts/rebuild_assets.py --no-install
python3 -m pytest -q
python3 scripts/install_release.py
```

Версионный installer создаёт неизменяемый каталог
`~/.rpfarm/releases/<content-id>` и переключает startup package Houdini.
Файлы уже работающей сессии не заменяются. **Новая версия активируется при
следующем запуске Houdini.** После перехода на этот способ обычный
`rpfarm setup` также использует версионную установку.

Миграция сохраняет остальные настройки `houdini.env` и его резервную копию.
Если Python/HDA не совпадают уже в свежей сессии, требуется согласованная сборка;
повторный restart эту ошибку не исправляет. Намеренно изменённые внутренности
старых HDA не перезаписываются автоматически.

## Команды и проверка

```sh
python3 -m rpfarm farm status
python3 -m rpfarm costs
python3 -m rpfarm houdini ls
python3 -m rpfarm storage ls
python3 -m rpfarm smoke
```

`smoke` использует живую ферму и оплачиваемые поды. Для локальной проверки есть:

- `scripts/verify_file_review_ui.py` — Qt-окно на fixtures.
- `scripts/verify_transfer_progress_pdg.py` — native progress в локальном PDG.
- `scripts/verify_job_roundtrip.py` — три отдельных процесса client/host/download
  с реальными HDA и fixture transport, без RunPod.
- `scripts/verify_controller_outcomes.py` — реальные native failure/cancel cases.

## Стоимость и границы

Live budget scheduler относится к GPU compute. Host, sync и storage считаются
отдельно; фактические начисления доступны через billing/Stats.

GPU/host-поды кука завершаются своим контроллером. Sync-под общий; его idle
housekeeping работает, пока открыта Houdini. Собственного автономного watchdog
у sync-пода пока нет. Если все пользовательские контроллеры выключены, его
состояние и счёт нужно проверить через Farm Status/RunPod.

Source nodes с произвольными пользовательскими скриптами, сторонние renderers и
все платформы не могут быть полностью проверены одним smoke. Проверки этой
версии и конкретные ограничения записаны в issues и work records.

План и статус: [эпик MAY4VFX/houdini_runpod_scheduler#2 «Единый Houdini flow»](https://github.com/MAY4VFX/houdini_runpod_scheduler/issues/2).
