# Удалённые стенды boat.dev

Boat предоставляет отдельную Linux VM для задачи. Агент остаётся в T3 и выполняет
команды через `just boat ssh` / `exec`; его модели, квоты и маршрутизация остаются
в T3. Это снимает очередь на локальный стенд, но не лимиты провайдера модели.
Владелец этой интеграции — workspace. Production и GitHub CI работают как прежде.

## Личное подключение по выбору разработчика

Boat — opt-in, а не зависимость разработки Blockfield. Обычные сборки, проверки
и локальный стенд работают без Boat и его авторизации. Владелец задачи сам
выбирает этот путь и подключает собственный аккаунт; очередь локального стенда
не даёт агенту разрешения расходовать чужую квоту.

**Local first:** используйте свободный подходящий локальный стенд. Boat — запасной
путь для сокращения ожидания занятого стенда или слота сборки. Лёгкие проверки
выполняйте локально; не переносите в облако всё подряд. После сокращения времени
setup/проверок оценивайте фактическую очередь заново, без запуска VM по привычке.

Личный аккаунт и VM не раздаются команде. Не создавайте общую организацию,
не приглашайте участников и не используйте `boat share` для личных стендов.
Разработчики, которым нужен Boat, подключают свои аккаунты. Данные входа CLI
хранятся вне репозитория; не переносите их в workspace, CI или чужие машины.

## Контроль расхода и ответственность координатора

Перед новой партией VM координатор записывает ожидаемую длительность, размер и
число параллельных VM. Проверяет `just boat limits --json` и список VM до запуска,
на обычных progress-checkpoints примерно каждые 15 минут активной работы и после
результатов задач. Отдельный фоновый watcher/цикл ожидания для этого не нужен.

- **Мало остатка:** ≤20% доступного лимита — предупреждение владельцу, ≤10% —
  критическое. Предупредите раньше, если следующая партия с setup и cleanup не помещается.
- **Быстрая трата:** за интервал хотя бы 5 минут расход выше 125% согласованного
  плана партии. Сравнивайте с планом параллельности, а не с равномерной месячной
  тратой: короткий запланированный burst сам по себе не проблема.
- Время нормировано на `default`: минута `large` расходует две минуты `default`.
  В trial берите меньший остаток: `subscriptionRemainingSeconds` и
  `trialComputeCapSeconds - trialComputeUsedSeconds`. Большой плановый остаток
  в API не отменяет отдельный лимит trial.
- В предупреждении дайте остаток в часах, измеренный/плановый темп и ожидаемый
  расход следующей партии. Не повторяйте неизменившийся сигнал. Предупреждение
  не отменяет opt-in и не требует повторного разрешения для уже согласованной работы.
  Не покупайте время, не меняйте тариф и не включайте auto top-up автоматически.

Координатор хранит в своём игнорируемом progress-каталоге реестр VM: точный ID,
задача/thread, session владельца процессов, тип, `BF_DEV_HOME`, deadline и статус
cleanup. Агент возвращает эти данные вместе с evidence и итоговым usage. После
success/partial/blocked/cancelled координатор сверяет реестр с `boat list --all`
и `info`: у завершённой задачи VM должна быть `stopped`, а не `stopping`.
Ожидание ревью или квоты модели — повод остановить VM и возобновить её позже.
Остановку выполняет сессия-владелец; неизвестные или чужие VM не трогают, а
сообщают владельцу. Координатор не выдаёт себя за сессию дочернего агента.

Личные навыки владельца `blockfield-boat` и `blockfield-boat-coordinator` содержат
этот рабочий процесс и read-only helper для предупреждений. Они не являются
зависимостью workspace и не требуют установки или доступа к аккаунту от других
разработчиков; общие правила и команды полностью описаны здесь.

## Подключение

```sh
just boat --version
just boat login --json
```

Откройте выданную ссылку самостоятельно и введите код. Используйте тот же способ
входа, которым уже создан аккаунт: для Google — `just boat login --google --json`.
Вход через другой способ может привести в другой аккаунт с отдельными планом
и окружениями. После завершения входа:

```sh
just boat onboard --wait --json
just boat status --json
just boat limits --json
```

Перед созданием VM убедитесь, что `canStart` равен `true` и `blockedReason`
пуст. `accessTier: trial` и ненулевой `trialComputeCapSeconds` описывают лимиты,
но не подтверждают активацию: при `subscription_required` / `subscriptionStatus: none`
завершите checkout в Billing того же аккаунта, который показывает `boat status`.

`onboard` может предложить ссылки на подключение GitHub и оплату; регистрацию
плана и ввод карты выполняет владелец. API-ключ передаётся только через stdin
(`just boat login --key-stdin`), без значений в аргументах, логах и репозитории.
CLI `1.0.85` скачивается по версии и SHA256 в `.cache/quality/`; автоматическое
самообновление отключено. Глобальная установка и изменение shell не нужны.

Создайте отдельное окружение только для Blockfield, не меняя `base`:

```sh
just boat env new blockfield
just boat env set blockfield --safe-for-third-parties false --github true --secrets false --boat-credentials false --agents-credentials false
just boat env add-repo blockfield Blockfield/blockfield-workspace --branch main
just boat env add-repo blockfield Blockfield/blockfield-client --branch main
just boat env add-repo blockfield Blockfield/blockfield-mod --branch main
just boat env add-repo blockfield Blockfield/blockfield-server --branch main
just boat env info blockfield --json
```

В `effective` включён только `passGithub`; список `selectedRepositories` содержит
workspace и три приватных репозитория. Ошибка `repository ... not found in your connected GitHub repos`
означает, что подключение GitHub ещё не даёт Boat доступ к репозиторию.

В dashboard подключите GitHub с доступом к нужным приватным репозиториям
Blockfield. Доступ нужен также для submodules и скачивания release artifacts.
Выбор клонируемого репозитория сам по себе не ограничивает права GitHub token.
Не копируйте локальные `gh`/SSH/AI credentials или production `.env` в VM.
R2-входы стенда скачиваются по публичному HTTPS; R2-ключи здесь не нужны.

## Первая VM

Для сборок и серверных сценариев начинайте с `default` (8 ГБ RAM). Для полного
профиля сервер 3 ГБ + клиент 4 ГБ и запаса ОС выбирайте `large` (16 ГБ RAM).
В trial `large` и сохранённые именованные snapshots недоступны; проверьте
актуальные ограничения своего аккаунта командой `just boat limits --json`.

```sh
just boat new --environment blockfield --type default --ttl 7200 --name bf-build --json
```

В ответе JSONL возьмите `id` из события `ready`; далее `bx_ID` обозначает именно
этот идентификатор. Сохраните ID и имя текущей T3-сессии в её progress-файле:
только создавшая VM задача вправе её останавливать. Не используйте `current`.
Перед созданием проверьте `just boat list --all --json`: существующая VM другой
задачи не становится вашей из-за совпадения имени или репозитория.

```sh
just boat ssh
```

Внутри VM, в клоне workspace:

```sh
cd ~/blockfield-workspace
python3 --version
node --version
gh auth status
gh auth setup-git
git status --short
git submodule update --init --recursive
python3 -I scripts/quality.py run just --version
python_bin=$(python3 -I scripts/quality.py run python -c 'import sys; from pathlib import Path; print(Path(sys.executable).parent)')
export PATH="$python_bin:$(python3 -c "import sys; sys.path.insert(0, 'scripts'); from toolchain import pinned_bins; print(':'.join(pinned_bins()))"):$PATH"
unset DISPLAY WAYLAND_DISPLAY
export AI_AGENT_SESSION_ID=boat-bx_ID-task-name
export BF_DEV_HOME="$PWD/.dev/boat-task-name"
export BLOCKFIELD_BOAT_VM=1  # Gradle guard: allows just recipes that run Gradle on this VM
export JAVA_HOME="$(python3 -c "import sys; sys.path.insert(0, 'scripts'); from toolchain import java_home; print(java_home())")"  # pinned Temurin 21 for bare shells (`boat exec`/`ssh`, direct `./gradlew`); `just` recipes export it themselves
export PATH="$JAVA_HOME/bin:$PATH"
just status
free -g
ss -ltnp
```

Инициализируйте submodules только в новом чистом клоне; при повторном запуске
сначала сохраните локальные изменения. Python должен быть 3.12+, Node — 22+.
Если образ не содержит нужного инструмента или X11/Mesa-библиотек, остановите
подготовку и сообщите точную недостающую зависимость. Не используйте `sudo` или
Docker для обхода политики доступа к root. Системные пакеты этого компьютера
не меняются. JDK и инструменты проверок уже устанавливает workspace по checksum.

Одинаковые `AI_AGENT_SESSION_ID` и `BF_DEV_HOME` нужны во всех SSH/exec-вызовах
одной задачи, включая остановку. В другой задаче используйте другую VM и другие
значения. Не сохраняйте идентификатор агентской сессии в общем environment.

Для прогрева остановленного стенда, после проверки памяти:

```sh
just setup --testbot
```

Это скачивает входы и собирает testbot, без запуска игрового клиента. Для правок
нужного мода отдельно выполните обычный цикл `build-mods` / `setup --force` из
[DEVELOPING](DEVELOPING.md), прочитав инструкции мода. Передача локальной папки
через `boat push` не заменяет checkout workspace с корректными submodules.
Не переносите целиком грязную рабочую копию, `.dev`, миры и локальные конфиги.
Переносите конкретные ветки/SHA нужных репозиториев; несохранённые правки требуют
отдельного явного переноса и сверки diff.

## Повторные задачи

На платном плане подготовьте `large` VM, прогрейте зависимости и сохраните
шаблон. Перед snapshot убедитесь, что сервер, клиент, Xvfb, watcher и сборки
завершены; проверьте `just status`, listeners и записи процессов. Шаблон содержит
кеши и подготовленные файлы, без процессов и данных незавершённой задачи.

```sh
just boat snapshot bx_ID bf-dev-YYYYMMDD
just boat info bx_ID --json
```

Дождитесь готовности snapshot с ограниченным сроком ожидания. Имена версионируйте:
повторное имя заменяет snapshot. Новый стенд создаётся из готового шаблона:

```sh
just boat new --from bf-dev-YYYYMMDD --environment blockfield --type large --ttl 7200 --name bf-task-name --json
```

Проверьте SHA workspace и каждого изменяемого submodule, назначьте нового
владельца и `BF_DEV_HOME`. Дисковый snapshot не гарантирует актуальный код.
В trial можно последовательно `stop`/`resume` одной VM или fork её последнего
snapshot в пределах лимитов, без именованного шаблона.

## Рабочий цикл агента (helper)

`just`-рецепты, запускающие Gradle (mod `gradle`/`build`/`java-check`/`java-format`/`format`/`test`,
client/server `compile`/`build` при наличии wrapper), отказываются работать на компьютере владельца:
только Boat VM или CI. Разрешают запуск `BLOCKFIELD_BOAT_VM=1` (helper и шаблон выше ставят его автоматически),
либо `CI`/`GITHUB_ACTIONS=true` (CI ставит сам). Обходной путь только для владельца:
`BF_ALLOW_LOCAL_GRADLE=1` — агентам его выставлять запрещено.

Правки — локально, VM — только на сборку. Один вызов helper на сборку:

```sh
just boat-build --name bf-task --branch my-branch --timeout 1800 -- \
  'cd localmods/blockfield-mod && ./gradlew :server:test --max-workers=2'
```

(`--name` один: helper сначала resume'ит собственную остановленную VM с
этим именем, иначе форкает newest golden; `--vm`/`--fork-from` прикалывают
источник явно.)

Helper сам resume/fork'ает VM, переносит точные коммиты без push в origin
(git bundle от merge-base с origin/main либо полный ref, когда база неизвестна;
старый `^SHA-VM` падал, если VM стояла на неизвестном локально коммите),
сверяет rev-parse, поднимает JDK 21 и env, запускает команду с жёстким
таймаутом. Вердикт берётся только из history (`exitCode` и `finishedAt`); обрыв
exec stream не означает падение команды. Незавершённый вызов возвращает 75
(RUNNING, ожидание не более 540 с) и печатает `--resume-run`: повторите эту
команду без повторного запуска build. Пока команда работает, VM не останавливается.
Перед stop лог и файлы `--evidence '<glob>'` (повторяемый, пути VM) копируются
в `.dev/<task>/<run>/`; `manifest.json` содержит команду, VM id, exitCode,
finishedAt и SHA256 каждого файла. `--fetch` тоже сохраняется здесь; `--out`
получает дополнительную копию. Ошибка копирования — неуспешный результат,
VM остаётся доступной для восстановления evidence. При ошибке setup до
вердикта helper также оставляет VM работающей: проверьте history и сохраните
evidence перед ручным stop. Незакоммиченные правки и untracked-файлы тоже
едут (патч `git diff HEAD`, применяется через `git apply --check`; отказ —
сразу с именами файлов, а не в середине прогона; `--no-worktree` отключает).
Локально собранный fork-jar: `--local-jar DIR=PATH.jar` (DIR — localmods/ или
его pw.toml; мод обязан быть LOCAL по `just build-mods --plan`, иначе helper
откажет до биллинга VM). Между итерациями VM остановлена: правьте локально, следующий прогон — новый
вызов helper. Никогда не останавливайте чужие VM.

Размер: `small` (2 vCPU/4 ГБ, половина тарифа) для `:server:test`
не медленнее `default` (замер: small ~259 с, default ~296 с — медианы
`--rerun-tasks` после прогрева; оба упираются в один предсуществующий
падающий тест) и вдвое дешевле — используйте `small`, если VM не
упирается в память. Задачи по умолчанию стартуют с newest golden
(`bf-golden-*`, без суффикса ` fork`, свежий `snapshotCompletedAt`;
`latest_golden_vm` в `scripts/dev/boat_build.py`): сначала resume
собственной остановленной VM (`--name`), иначе fork golden. Golden v2
печётся скриптом (`scripts/dev/boat_bake.py`, рецепт `just boat-bake
--name bf-golden-YYYYMMDD`): pinned Temurin 21, репозитории на
origin/main, прогрев `just setup --testbot` + один compile+remap проход
(`:common:compileJava :client:compileJava :client:remapJar`), кеши
cargo/npm/uv, лимиты в `~/.gradle/gradle.properties` (caching + cleanup,
14 дней). Перепекайте раз в месяц или при материальных сменах пинов
workspace/submodules: fork newest golden, bake, stop; старый golden
оставляйте остановленным, не удаляйте. Fork всегда с re-pin окружения
(`--environment blockfield`, helper делает это сам): GitHub-авторизация
только из окружения Boat, в golden нет baked-конфига `gh`
(протухший baked токен даёт 401 на скачивании релизов).
При подготовке golden замаскируйте snapd (`sudo -n systemctl mask --now
snapd.service snapd.socket snapd.seeded.service snapd.snap-repair.service
snapd.refresh.timer`): в образе Boat он падает с кодом 42 примерно раз в
минуту и жрёт CPU на каждом форке; маска переживает snapshot'ы, новый
golden ради неё не нужен.
`just` на golden нет (образ без него): агенты ставят pinned `just` 1.57.0
из workspace (`python3 -I scripts/quality.py run just --version` проверяет
checksum и кладёт бинарь в `.cache/quality/bin/<pin>`) и добавляют в PATH
(`JBIN=$(python3 -c "import sys; sys.path.insert(0, 'scripts'); from toolchain import tool; print(tool('just'))") && install -m755 "$JBIN" "$HOME/.local/bin/just"`). При обновлении
golden ставьте `just` так же, чтобы `just ...` работал сразу, без `bf.py`.
Правила cleanup из раздела «Проверка и завершение» действуют как прежде.

## Проверка и завершение

Политика владения процессами из [AGENTS.md](../AGENTS.md) действует и в VM.
На каждой VM один игровой стенд; Gradle и замеры времени не совмещайте.
Перед запуском инспектируйте listeners, PID, command и `/proc/<pid>/cwd`.
Изоляция между VM позволяет параллельные задачи без вмешательства в чужой стенд.

Игровой клиент требует явного согласия на задачу и конкретный headless-экземпляр.
После согласия используйте только `just play --headless --no-gpu` с собственным
Xvfb, без звука и подключения к desktop Boat. Сначала проверьте backend и
сохраните доказательства; пример probe есть в [DEVELOPING](DEVELOPING.md).
Результаты software Mesa подтверждают поведение, но не FPS на компьютере игрока.
Порты Minecraft/RCON/testbot/JDWP не публикуйте через `boat host`; команды
выполняются внутри VM. Для доступа к testbot извне используйте SSH-туннель.

У `boat exec` стандартный таймаут 30 секунд. Для долгой конечной команды явно
задайте срок, например `--timeout 1800`. Работа SSH/exec не продлевает auto-stop:
перед длинным сценарием проверьте deadline и при необходимости `boat extend`.
Не запускайте второй оркестратор через `boat prompt` в обход маршрутизации T3.

Указывайте параметры `exec` перед ID и командой: всё после начала команды
передаётся ей как аргументы. Shell-синтаксис требует явного shell:

```sh
just boat exec --timeout 1800 bx_ID bash -lc 'cd ~/blockfield-workspace; git status --short'
```

Скопируйте evidence и сохраните изменения в соответствующих репозиториях до
остановки VM. Затем в том же окружении владельца внутри VM:

```sh
just play --stop
just server stop
just status
just dev prune --apply .dev/boat-task-name
```

На локальной машине остановите только VM своей задачи и проверьте результат:

```sh
just boat stop bx_ID
just boat info bx_ID --json
```

`stop` завершает процессы и сохраняет диск на 14 дней. Это не долговременное
хранилище: нужные изменения и evidence должны быть уже скопированы.
Не удаляйте VM с уникальными данными и не останавливайте чужие VM.

Официальные справочники: [CLI](https://docs.boat.dev/cli-reference),
[environments](https://docs.boat.dev/environments),
[snapshots](https://docs.boat.dev/snapshots/templates),
[trial](https://docs.boat.dev/free-trial).
