# RunPodFarm: UX-аудит относительно Houdini TOPs / PDG

Дата: 2026-09-10. Read-only аудит кода; единственное изменение — этот документ.
Срез: checkout `b93ce1c` плюс текущие незакоммиченные изменения scheduler/host render. Это анализ реализации, не подтверждение поведения установленной HDA в открытом Houdini.
Вывод: основные PDG-механизмы используются, но пользовательский flow распался на несколько источников настроек, способов запуска, получения результатов и мониторинга. Ниже восемь подтверждённых областей; рекомендации отделены от конвенций SideFX.

## 1. Три способа старта с различными правилами выбора цели

**Факт кода.** Обычный Cook использует стандартный TOP flow. Submit As Job требует отдельного `Node To Submit`, причём `Use Output Node` навсегда включён и disabled: [DialogScript:577](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/DialogScript#L577). Background Cook расположен в самостоятельной вкладке с дополнительными enable toggle и кнопкой: [DialogScript:607](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/DialogScript#L607). Его callback берёт родительский topnet и display node, затем передаёт topnet фоновому процессу: [PythonModule:4314](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L4314), [PythonModule:4345](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L4345). Поэтому смена способа запуска меняет и способ определения части графа.

**Конвенция.** SideFX различает обычный Cook и автономный Submit Graph As Job; у HQueue нормальный cook зависит от открытой Houdini-сессии, автономный выполняет сохранённый граф отдельно. Само наличие этих двух режимов соответствует Houdini, а не является дефектом. [Cooking](https://www.sidefx.com/docs/houdini/tops/cooking.html), [HQueue Scheduler](https://www.sidefx.com/docs/houdini/nodes/top/hqueuescheduler.html).

**Рекомендация.** Один выбранный Output Node и одна видимая сводка «что/где выполняется/где останется результат». Оставить стандартный Cook; автономный запуск оформить одним разделом с выбором местоположения управляющего процесса, а не третьей самостоятельной концепцией. Выбор отдельного render target при отсечении download оправдан, но границу надо показывать, а не объяснять только текстом.

## 2. Автономная задача теряет постоянную точку наблюдения

**Факт кода.** Background Cook сообщает PID и путь к логу одноразовым диалогом: [PythonModule:4347](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L4347). Callback не сохраняет привязку к задаче в параметрах, не подключает отображение remote graph и не предоставляет рядом кнопки повторного открытия лога/остановки этого процесса. Submit As Job возвращает URL пода, записывает submission как `cook_summary` с нулевыми task/cost и сбрасывает локальное состояние: [PythonModule:4227](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L4227). Это подтверждает разрыв локального наблюдения, но не доказывает отсутствие всех удалённых логов или self-termination.

**Конвенция.** Task Graph Table показывает work items, допускает соединение с remote PDG instance; TOP UI имеет управление remote session. HQueue Submit возвращает status URI автономного задания. [TOP UI](https://www.sidefx.com/docs/houdini/tops/ui.html), [HQueue Scheduler](https://www.sidefx.com/docs/houdini/nodes/top/hqueuescheduler.html).

**Рекомендация.** Сохранять job/cook ID, target, owner, режим, лог и состояние получения результатов. После перезапуска сцены показывать «работает / готово на ферме / скачано / ошибка» и действия для выбранной задачи. Remote Graph — возможный native механизм, но его сетевую применимость к RunPod надо отдельно проверить.

## 3. Получение кадров принадлежит одновременно scheduler и Download TOP

**Факт кода.** Scheduler содержит `Download Outputs`: [DialogScript:69](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/DialogScript#L69). Download TOP отдельно планирует retrieval из upstream `resultData`. При включённом автоскачивании он только выдаёт warning о повторном получении тех же файлов, не исключая эти задания: [build_runpodfarm_download_hda.py:258](../scripts/build_runpodfarm_download_hda.py#L258). Проверка ориентируется на default scheduler topnet, не на scheduler каждого upstream item: [build_runpodfarm_download_hda.py:162](../scripts/build_runpodfarm_download_hda.py#L162).

**Конвенция.** PDG показывает работу и её зависимости через граф; результат передаётся downstream. SideFX не запрещает scheduler локализовывать файлы — дефект здесь в двух одновременно действующих владельцах одной операции. [Introduction to PDG](https://www.sidefx.com/docs/houdini/tops/intro.html).

**Рекомендация.** Один механизм retrieval и один пользовательский выбор «автоматически по готовности / забрать позже». Download TOP может оставаться видимой стадией и ручной точкой восстановления; scheduler должен пользоваться тем же планом/состоянием, не порождать независимый второй flow. Проверить реальную повторную передачу отдельно: rclone может пропускать совпадающие файлы, поэтому сам факт второго прохода ещё не доказывает повтор всех байтов.

## 4. Download завершён, но локальные файлы не объявлены его результатами PDG

**Факт кода.** `package_runner` после скачивания отправляет лишь `bytes`, `files`, `seconds`, `mbps`, затем возвращает exit 0: [package_runner.py:158](../rpfarm/package_runner.py#L158), [package_runner.py:188](../rpfarm/package_runner.py#L188). Генератор Download задаёт payload/атрибуты/command, но не expected outputs: [build_runpodfarm_download_hda.py:371](../scripts/build_runpodfarm_download_hda.py#L371). In-process fallback также заканчивается только атрибутами: [build_runpodfarm_download_hda.py:438](../scripts/build_runpodfarm_download_hda.py#L438). Новые локальные пути не регистрируются как собственные outputs Download item; возможное наследование upstream outputs это не заменяет.

**Конвенция.** Job API позволяет процессу сообщить файлы через `addOutputFile`, после чего они становятся Output files work item; атрибуты и output files — разные каналы. [SideFX Job API](https://www.sidefx.com/docs/houdini/tops/jobapi.html).

**Рекомендация.** Регистрировать локальные файлы с подходящими file tags; дать пользователю штатные View Output и downstream `@pdg_output`, а также проверяемый признак «файл уже здесь». Аналогично дать Upload явный результат передачи/manifest, не смешивая remote storage с локальными outputs.

## 5. Важные этапы подготовки схлопнуты в непрозрачное ожидание

**Факт кода.** До передачи `package_runner` ожидает sync pod; `progress` появляется только из callback самой передачи: [package_runner.py:138](../rpfarm/package_runner.py#L138), [package_runner.py:153](../rpfarm/package_runner.py#L153). `wait_ready` проверяет SSH mapping и health каждые три секунды, после чего выдаёт общее `not ready`: [pods.py:335](../rpfarm/pods.py#L335). HTTP health сводит любой не-200 ответ к `None`: [worker_client.py:85](../rpfarm/worker_client.py#L85). Следовательно неверный токен и недоступный воркер неразличимы на этом пути. Это объясняет, почему screenshot показывает долгий upload failure без ясной стадии; текущий повторный запуск этим аудитом не проверялся.

**Конвенция.** TOP UI отображает состояния work items и пользовательские атрибуты через Task Graph Table; custom scheduler должен возвращать статус задач в PDG. [TOP UI](https://www.sidefx.com/docs/houdini/tops/ui.html), [Custom schedulers](https://www.sidefx.com/docs/houdini/tops/custom_scheduler.html).

**Рекомендация.** Видимые `phase`, done/total bytes, текущий файл, throughput, elapsed; фазы «ожидание машины / запуск / проверка доступа / упаковка / передача / проверка результата». Ошибку авторизации показывать сразу. Это рекомендация по представлению, SideFX не предписывает конкретный набор этих атрибутов.

## 6. «Project / farm settings» имеют несколько независимых владельцев

**Факт кода.** Project существует и у Upload, и у Scheduler. Upload строит remote project из своего параметра: [build_runpodfarm_upload_hda.py:465](../scripts/build_runpodfarm_upload_hda.py#L465). Scheduler независимо вычисляет проект из своего параметра: [PythonModule:2721](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L2721). Defaults совпадают, но редактирование одного не меняет другой. Transfer runner берёт account/volume из `config.toml`: [package_runner.py:147](../rpfarm/package_runner.py#L147); scheduler допускает node overrides, например volume: [PythonModule:1823](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L1823). Кроме того volume callback `_node_config` учитывает node API key, но возвращает cfg без прочих node overrides: [PythonModule:678](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L678).

**Конвенция.** Houdini schedulers явно задают local/remote shared roots и mapping. Это не требование единственной project entity, но хороший базис для одного согласованного контекста. [Scheduler common parameters](https://www.sidefx.com/docs/houdini/nodes/top/_scheduler_common.html).

**Рекомендация.** Один effective farm/project context на cook; Upload/Download наследуют его и показывают итоговые local → remote пути. Overrides остаются явными overrides со ссылкой на источник. До передачи проверять согласованность контекста всех участвующих нод.

## 7. Refresh и отчётность скрывают длительную работу вне видимого графа

**Факт кода.** Stats Refresh поднимает/ожидает sync pod, синхронизирует ledger и вызывает `cookWorkItems(block=True)` прямо из callback: [build_runpodfarm_stats_hda.py:577](../scripts/build_runpodfarm_stats_hda.py#L577). Его отдельные work items создаются уже после вычисления отчёта; cooktask пустой: [build_runpodfarm_stats_hda.py:650](../scripts/build_runpodfarm_stats_hda.py#L650). Аналогичная сеть выполняется через Volume Refresh и Sync Ledger в scheduler: [PythonModule:780](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L780), [PythonModule:804](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L804). Число точек обновления растёт, а нет явного общего «данные актуальны на…». Степень заморозки UI надо замерить в Houdini: статически подтверждён синхронный callback, а не конкретная длительность зависания.

**Конвенция.** PDG work items могут представлять данные, так что Stats как TOP сам по себе допустим. SideFX также различает in-process и out-of-process выполнение. [Introduction to PDG](https://www.sidefx.com/docs/houdini/tops/intro.html).

**Рекомендация.** Единое обновление farm snapshot с отметкой времени и видимым прогрессом. Оставить Stats TOP для downstream analytics, а operational status/ledger/volume читать из одного snapshot; тяжёлый refresh должен быть отменяемым, а не скрытым внутри кнопки.

## 8. Parameter pane используется как длинный журнал разработки и mini-dashboard

**Факт кода.** Инструкции о режимах оформлены disabled multiline string parameters: [DialogScript:560](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/DialogScript#L560), [DialogScript:610](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/DialogScript#L610). Background help до сих пор утверждает, что Submit As Job не может сам погасить поды без ключа, хотя актуальный Submit help обещает self-termination и код специально передаёт credentials. Farm Status — ещё одно большое disabled поле с режимом, подами, задачами, стоимостью, ошибками и notes: [PythonModule:2499](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule#L2499). `Kill All Pods` не включает sync pod, рядом существует `Stop Everything, Sync Pod Too`: [DialogScript:204](../hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/DialogScript#L204). Названия требуют прочтения help, чтобы понять scope.

**Конвенция.** Houdini предоставляет отдельные parameter help, labels, menus, folders и условную видимость. Disabled strings не запрещены SideFX, поэтому это оценка удобства, а не нарушение API. [ParmTemplate](https://www.sidefx.com/docs/houdini/hom/hou/ParmTemplate.html), [LabelParmTemplate](https://www.sidefx.com/docs/houdini/hom/hou/LabelParmTemplate.html).

**Рекомендация.** Параметры — для выбора поведения; короткие labels — для effective state; подробности — в help/логах/Task Graph Table. Убрать Ruling/историю предыдущих багов из пользовательских tooltips. Явно назвать scope остановки: «Cancel This Cook», «Stop My Render Pods», «Stop My Farm»; показывать затрагиваемые активные работы. Свести Manage Volume и ручной Delete Target к одному управлению выбранными объектами.

## Что сохранить

Ноды Upload → Render → Download — нормальная PDG-модель стадий, а не дубль сама по себе. Task Graph Table и work-item attributes уже используются, так что проекту не нужен новый standalone dashboard. Нужна согласованность: одна цель, один farm context, один владелец retrieval и восстанавливаемое состояние job. Отдельные native Cook и Submit As Job тоже имеют смысл и подтверждены документацией SideFX.

Живые поды не запускались/останавливались, Houdini-сцена не менялась, тесты не запускались. Установленные бинарные HDA могут отличаться от исследованного checkout.
