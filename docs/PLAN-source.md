# blockfield-workspace с dev-стеком

## Context

Текущий процесс разработки разорван: rig (`BlockField-Private-Project/dev/rig/`)
работает только на машине владельца (podman, agent-process, `~/.cache`, rsync с
homeserver), а devstack — кроссплатформенный, но урезанный: нет подмены клиентского
мода в `play`, нет HMR для клиента, сценарии и MCP привязаны к ригу. Homeserver
больше не используется. Актуальные репо живут в организации `Blockfield`:
`blockfield-client`, `blockfield-server`, `blockfield-mod`, форки, лаунчер, proxy, site.
sbw-drone выбыл (форк заброшен, апстрим удалён).

Цель: корневое репо `Blockfield/blockfield-workspace` с сабмодулями `client` +
`server` (паки) и `localmods/` (исходники мода + 4 форков); в нём живут dev-стек
(`scripts/dev/`), HMR, MCP. Архивный модпак прежней организации заморожен, архивные
клоны после миграции удаляются с локальной машины, `game-harness` — в архив.

Исследование: `dev/rig` разобран поштучно, devstack изучен полностью, harness +
dev-режим лаунчера + сборка мода изучены. Разведка архивов vs новых репо проведена:
мигрировать — 5 коммитов модпака + 3 коммита мода + 3 локальных коммита сервера.
Готовые HMR-решения для Minecraft проверены в интернете (см. этап 3).

## Plan

### Этап 0. blockfield-workspace + миграция из архивов — ВЫПОЛНЕН (коммиты 5f9a6ce, 250d722, 89582e5, 11fad0c, d3b4459)

- `Blockfield/blockfield-workspace`: `README.md`, `LICENSE` (как в client), `.gitignore`
  (`.dev/`, `temp/`, `.tools-java/`, `localmods/*` с исключениями под сабмодули),
  `.mcp.json` (заготовка), сабмодули `client`, `server` (main) + `localmods/`:
  `blockfield-mod`, `create-fabric`, `parcool-fabric`, `superbwarfare-fabric`,
  `simplebedrockmodel-fabric` (main). Описание + топики
  (`minecraft`, `fabric`, `modpack`, `dev-environment`, `minecraft-server`) выставлены.
- `localmods/mods.json.example`: явный маппинг metafile → dirname для мода + 4 форков
  (без sbw-drone: форк заброшен, апстрим удалён с GitHub).
- Мигрировано из архивного модпака (`2aeb2dc`, `663c64a`, `3ddedbe`, `3aa63ce`):
  devstack (`scripts/dev/devstack.py`, `pmc_launch.py`, `rcon.py`), `scripts/dev/modmap.py`
  (новый), `bf.py`, `justfile` (только dev-рецепты), `docs/DEVELOPING.md`, pre-commit/pyright/vscode.
  `PACK_ROOT` → `ROOT` + `CLIENT_PACK`/`SERVER_PACK`/`LOCALMODS`; `pack`/`play` собирают
  клиент через `client/scripts/build-client.py`, `setup` — сервер через
  `server/scripts/build-distribution.py`; music-релиз из `Blockfield/blockfield-music`,
  world-seed из `Blockfield/blockfield-server`; тесты в `scripts/dev/tests/`
  (`test_devstack.py` + `test_modmap.py`, 9 шт., зелёные).
- Архивная работа перенесена в PR, не в workspace:
  - Copper Age 0.1.4→0.1.5 (`c70fd2a`, `side=both`): client#33 + server#44 (у сервера нет index.toml).
  - Мод (`144856a` PackVersionGate-dev, `2deb2c8` portablemc-фикс): mod#23, ветка
    `migrate-archive-work`. `a1e8500` (снос devstack из мода) не переносили — неактуально.
  - Сервер (`5464a1f` баланс АК/М4, `b41c293` Pylance-фиксы): server#45, ветка
    `migrate-archive-work`. `1efb2bb` (devstack-пути на архивный модпак) не переносили —
    неактуально в новой раскладке.
- Осталось: архивный модпак не трогать (заморожен); архивные клоны
  (`blockfield-modpack/`, `blockfield-mod-leaks/`, `minecraft-server/`, `forks/`,
  `BlockField-Private-Project/`, `forks/sbw-drone/` — последняя копия
  удалённого форка) удалить с локальной машины. Удаление — пользователь или явное
  поручение, не агент молча.

### Этап 1. Приоритет локального над пином — ВЫПОЛНЕН (живой прогон 09-29)

Правило: сабмодуль в `localmods/` есть и собран → devstack берёт jar оттуда везде,
пины по `gh` не затирают; нет сборки → обычное поведение по пинам.

Что сделано (коммитить: workspace + client-сабмодуль + server-сабмодуль отдельно):

- `scripts/dev/modmap.py`: `newest_jar()` (переехал из devstack), `local_jars()` →
  `{metafile: jar}`, все 6 маппингов; у `blockfield-mod` стороны раздельные
  (`_side_jar()`: client/ vs server/), форки делят один jar.
- Проброс: `BLOCKFIELD_BUILT_MODS` JSON (env, не CLI — меньше дифф в сабмодулях) через
  `run_pack_build()` в обе сборки; `resolve_mod` обеих сторон: сначала точный ключ
  по metafile (переименованные форки), затем префикс-фолбэк; `built_from_env()`.
- `fetch_mod_jars()`: покрытые сборкой пины не качаются (`gh` только music + world-seed).
- `install_mod` / `client_prepare --mod` / `dev_extras --mod`: `blockfield_jar()`,
  localmods first, legacy `--mod-repo` как фолбэк; `bf build-mods [имена]` + `just build-mods`.
- PackVersionGate: `setup` с local jars скипает `check_client` и штампует маркер локальным
  контрактом; `client [--mod]` синкает маркер с сервера в game dir.
- Живой прогон 09-29: 5/5 `BUILD SUCCESSFUL`, `setup` → 6 jar override, 5/5 jar побайтово
  совпадают в `.dev/server/mods/`, сервер стартует + RCON, `pack --mod` 5/5 jar побайтово
  в архиве, `client [--mod]` + `mod` — маркер и jar совпадают. Маркер архива (client
  digest) ≠ маркеру сервера (contract) всегда — зафиксировано в `docs/DEVELOPING.md`,
  `play --mod` обязан делать тот же синк маркера (см. этап 2).

Факты (сверены с кодом 09-29, учитывай при реализации):

- `localmods/` — уже сабмодули (не plain-клоны): `blockfield-mod`, `create-fabric`,
  `parcool-fabric`, `superbwarfare-fabric`, `simplebedrockmodel-fabric`. Маппинг
  metafile → dirname — `scripts/dev/modmap.py` (`mods.json.example`), тесты в
  `scripts/dev/tests/test_modmap.py`.
- `resolve_mod(root, item, built)` в `server/scripts/build-distribution.py` и
  `client/scripts/packlib.py` уже принимает `built: {prefix: jar_path}` и подменяет
  по префиксу имени — но оба билда вызываются с `built={}`. Отдельного флага
  `--mod-source` НЕТ (в плане он упоминался ошибочно). Клиентский `build-client.py`
  принимает только `--root/--out/--public-base`.
- Префикс-матчинг `resolve_mod` не покрывает переименованные форки
  (`ParCool-*-bf6.jar` vs `parcool.pw.toml`): для форков нужен маппинг через modmap,
  либо расширение `built` до `{metafile: jar_path}` в обоих скриптах сборки
  (меняет client + server — согласовать, чей `resolve_mod` правится; `packlib.py`
  общий для клиентских скриптов).

Задачи:

- Хелпер `local_jars()` в devstack: по маппингу ищет
  `localmods/<dir>/{client,server}/build/libs/*.jar` (newest по mtime, игнор
  `*sources*`/`*dev*` — переиспользовать `newest_jar()`).
- Пробросить локальные jar в обе сборки: devstack резолвит `built`-словарь из
  `localmods/` и отдаёт в `build-distribution.py` / `build-client.py` (сейчас `built`
  всегда `{}` — нужен новый параметр CLI или env; правится client + server).
- `fetch_mod_jars()`: пины, покрытые локальными сборками, не качать.
- `client_prepare --mod`, `dev_extras --mod`, `install_mod`: сначала `localmods/`.
- `just`-рецепты workspace: `just build-mods [имена]` (gradlew build по `localmods/`,
  ОС-независимо через toolchain `bf.py`), доку в `README.md`/`docs/`.
- Проверка PackVersionGate: маркер `blockfield-pack-version.txt` уже копируется
  `client_prepare` — убедиться что цепочка работает с localmods-сборками.

### Этап 2. `play --mod` (база HMR) — ВЫПОЛНЕН (живой прогон 09-29)

Что сделано:

- `devstack.py`: `client_mod_jar()` (localmods first, `--mod-repo` фолбэк) — общий для
  `pack`/`client`/`play`; `install_client_mod(game_dir, mod, mod_repo, sync_marker)` —
  удаляет опубликованные `blockfield-client-*.jar`, ставит локальный, синкает маркер сервера.
  `client_prepare` и `play` вызывают её; `play` синкает маркер только при `--server`
  127.0.0.1/localhost (удалённый сервер проверяет маркер архива). `play --mod` — флаг.
- Тесты: `InstallClientMod` (замена jar + синк маркера; без синка для удалённого сервера).
- Живой прогон 09-29: `setup` с local jars → `server start` → `play --mod`: в game dir один
  `blockfield-client-*.jar`, побайтово = `localmods/` сборке; маркер архива `22f74e4f…`
  заменён серверным `bfc7c0dd…`; `ModCheck joined the game`, кика гейта нет. Повторный
  `play --mod` по существующему game dir переустанавливает jar и маркер.
- Попутно: SIGBUS клиента в `flite_init` — libffi режет путь из `/proc/self/maps` по пробелу
  (каталог с пробелом в имени) и мапил пустой каталог; файл удалён, в `DEVELOPING.md` заметка.

Исходная постановка:

- `play` получает флаг `--mod`: после распаковки/проверки game dir подменять
  `mods/blockfield-client-*.jar` локальной сборкой (та же логика, что `client_prepare`,
  вынести в общую функцию). Защита от двойного мода: удалять опубликованный jar при
  установке локального. Для `play` freeze не нужен (packwiz там не участвует).
- Dev-лаунчерный путь (`client --mod` + `BLOCKFIELD_DEV_FREEZE_PACK=1`) — сохранить,
  переключить резолв на `localmods/`.

### Этап 3. HMR (JDWP + fallback рестарт)

Что показало исследование готовых решений:

- Стандартный путь сообщества — IDE Debug Hotswap (IntelliJ «Reload changed classes»
  через JDWP `RedefineClasses`), что идеологически совпадает с нашим `hotswap.sh`
  (remapJar → diff → `jdb redefine`). Готовой CLI-утилиты «вотчер + redefine для Fabric»
  не существует — наш вотчер будет своей разработкой, механизм подмены стандартный.
- Стоковая JVM меняет только тела методов; структурные изменения + Mixin-своп требуют
  JetBrains Runtime (DCEVM) + `-XX:+AllowEnhancedClassRedefinition` + mixin-`javaagent`
  ([Fabric wiki: mixin_hotswaps](https://fabricmc.net/wiki/tutorial:mixin_hotswaps),
  [HotswapAgent](https://github.com/HotswapProjects/HotswapAgent)). JBR можно положить в
  `.tools-java/` рядом с Temurin — та же механика, что `bf.py download_java()`.
- Изменение ресурсов без redefine: `assets/` → `F3+T`, `data/` → `/reload` — упомянуть в доке,
  код не нужен. Mixin-правки без JBR идут через рестарт по определению.

Отсюда конструкция:

- `server start --debug` уже даёт JDWP `:5005`. `play` получает `--debug` → JDWP `:5006`
  (флаги в команду из `pmc_launch.py`, порт параметром).
- Новая команда `bf watch [--side server|client]` (stdlib, без новых зависимостей: цикл по
  mtime исходников `localmods/<dir>/src` + `gradlew :side:remapJar`): после сборки diff
  классов → `jdb redefine` через порт стороны (логика портируется из `hotswap.sh`,
  сам `hotswap.sh`/`jdbtrace.py`/`process_guard.py`/`entry.sh`/`Containerfile` выбрасываются).
- Неудача redefine (schema change / not implemented / новый класс) → авто-fallback:
  сервер `stop → mod → start` (~40 с), клиент — переустановка jar в game dir + рестарт `play`.
- Mixin-классы: без JBR сразу fallback; с JBR (`bf.py` ставит JBR вместо Temurin + флаги
  `-XX:+AllowEnhancedClassRedefinition`, mixin-`javaagent` из gradle-кэша) — тоже через redefine.
  JBR — опциональный второй шаг этапа, не блокер.
- Один клиент в первой итерации (`BOT_PORTS[1]`); второй — позже без переделки
  (порты/директории уже параметризованы).

### Этап 4. Переезд rig-функционала — ВЫПОЛНЕН

Исходная постановка (по файлам, источники — архивный `dev/rig`):

Переезжают как есть (чистые, ОС-независимые) в `scripts/dev/`: `rcon.py` (сверить с
devstack `Rcon`, оставить один), `tools/nbt_find.py`, `delay-proxy.py` (параметризовать
адрес/порт), `testbot/src/` + сборочные файлы (`build.gradle*`, `settings.gradle`,
`gradle.properties`, без `build/`) → `scripts/dev/testbot/` (testbot живёт в workspace,
не в localmods). `forge`-команды протокола — пометить rig-only, не использовать.

Переписываются (сценарии → devstack `bot()` + `Rcon`, Python вместо bash, без хардкодов
портов/путей `~/.cache`): `stress.py` (Carpet-часть — только если devstack научится ставить
моды; `/proc`-метрики заменить кроссплатформенным сбором или выбросить; stress-сервер на
отдельном порту через devstack-процессы вместо agent-process), `hitreg.py` (свой
bot-сокет удалить), `parkour-test.py` (чистые `judge/on_roof/start_reachable/load_nodes` —
как есть + существующие юниты; оркестрацию переписать, чтение `server.out` заменить
Rcon), `dm-test.sh` + `dm-test2.sh` + `range-test.sh` (bash → Python на devstack API),
`check-benelli-animation.py` / `check-consent-machinery.py` / `check-gameplay.py`
(функциональную часть — да; сбор кадров для ручного визуального ревью — только если есть
GUI-клиент, иначе выбросить; двухклиентские — отложить до второго клиента).

Выбрасываются: `Containerfile`, `entry.sh`, `hotswap.sh` (заменён `bf watch`),
`jdbtrace.py`, `process_guard.py` + тесты, `agent-process`, `install-client.py`/
`install-packwiz.py` (если devstack ставит пак иначе — проверить при переносе),
`server.sh sync`-аналог (rsync с homeserver мёртв, мир — `world-seed` +
`build-distribution.py` из сервера).

Тесты: `test_mod_jars.py` + `test_parkour_test.py` переезжают с кодом;
`mod_jars.py`-логика уже частично есть в devstack (`install_mod`, `client_prepare`)
— сверить и оставить одну реализацию.

Что сделано:

- Чистое: `scripts/dev/testbot/` (копия `src/` + сборочные файлы из rig, без `build/`;
  резолвер `testbot_jar()` смотрит туда первым, `bf build-testbot` + `just build-testbot`),
  `scripts/dev/tools/nbt_find.py` (как есть), `scripts/dev/delay_proxy.py` (argparse:
  `--listen-port/--server-host/--server-port`), `rcon.py` — один (rig-версия отличалась
  лишь неиспользуемым `time`, оставлен devstack-`Rcon`).
- Сценарии → `scripts/dev/scenarios/` на devstack API (`bf scenario <name>`, `just scenario`):
  `parkour` (judge/on_roof/start_reachable/load_nodes как есть + юниты; оркестрация на
  RCON, лог — `server.out` devstack), `hitreg` (devstack TCP-клиент бота вместо своего
  сокета), `stress` (только RCON-метрики `tick query`, без `/proc` и agent-process;
  Carpet/JFR — ручные шаги), `dm` (dm-test.sh + dm-test2.sh; duo-шаги скипаются без
  второго клиента), `range` (duo-шаги скипаются), `check-gameplay`, `check-consent`
  (скип до второго клиента), `check-benelli` (кадры только с `--frames` при GUI-клиенте).
  `forge`-команды — rig-only, не используются.
- Выброшено без переноса: `Containerfile`, `entry.sh`, `hotswap.sh` (заменён `bf watch`),
  `jdbtrace.py`, `process_guard.py`, `agent-process`, `install-client.py`/`install-packwiz.py`,
  `server.sh sync`, podman/homeserver/Xvfb-пути, `isolated/`.
- Тесты: `test_parkour_test.py` → `test_scenarios.py` (judge/roof/reach/load_nodes +
  hitreg/stress-юниты), `test_mod_jars.py` → `test_mod_jars.py` (install/deployed/prune
  против `install_mod` devstack + атомарная замена через temp-файл). `just test` — 40 зелёных.

### Этап 5. MCP в workspace (поверх devstack) — ВЫПОЛНЕН

- MCP-сервер (stdio, Python stdlib) в `scripts/dev/mcp/` + запись в `.mcp.json` workspace.
  Говорит только с devstack-сущностями (`.dev/server.pid`, `server.out`, TCP ботов, RCON) —
  своих процессов не стартует.
- Tools: `status`, `server_{start,stop,rcon,log}`, `bot_send`, `client_prepare/play`
  (запуск — с явного запроса), `logs_snapshot`, плюс перенесённые сценарии
  `stress/hitreg/parkour/dm/range/check-*` как tools.
- `game-harness` — в архив после того, как MCP покрывает его использование.
  `stack.sh`/podman-пути не портировать.

Что сделано:

- `scripts/dev/mcp/server.py` (stdio MCP, чистый stdlib, JSON-RPC 2.0): говорит только
  с devstack-сущностями (`.dev/server.pid`, `server.out`, TCP ботов, RCON), своих процессов
  не стартует (`client_play` — неблокирующий запуск `bf play`, только по явному вызову).
- Tools (11): `status`, `server_start/stop/rcon/log`, `client_prepare/play/stop`,
  `bot_send`, `logs_snapshot`, `run_scenario(name=…)` для всех портированных сценариев.
- `.mcp.json` workspace: сервер `blockfield-dev` (`python3 scripts/dev/mcp/server.py`).
- Проверено: initialize/ping/tools/list/`status` через stdio; `just test` — 6 MCP-юнитов зелёные.

### Этап 6. Доки и зачистка — ВЫПОЛНЕН

Исходная постановка:

- `README.md` + `docs/`: localmods, `play --mod`, `bf watch`, JBR-опция, MCP, раскладка
  workspace (сабмодули client/server + localmods).
- Архив `game-harness`; удаление архивных клонов с диска (по поручению пользователя).
- Архивный модпак остаётся замороженным (ничего не пушить).

Что сделано:

- `README.md`: структура `scripts/dev/` целиком (watch, scenarios, MCP, tools, testbot),
  `build-testbot`/`watch`/`scenario`/MCP в dev loop.
- `docs/DEVELOPING.md`: layout с подкаталогами, `build-testbot`, раздел rig-сценариев
  (`bf scenario`, delay proxy, nbt_find), раздел MCP-сервера (11 tools), раздел тестов
  (`just test`), rig помечен архивным референсом (read-only).
- Сабмодули и workspace: `git status` чист везде; архивные клоны не тронуты
  (удаление — пользователь или явное поручение).

## Порядок и границы

Порядок: 0 → 1 → 2 → 3 → 4 → 5 → 6. MCP строго после стабильного devstack-API.
Не переносить: podman, agent-process, homeserver/rsync, Xvfb/VirtualGL, второй клиент
(отложен), JBR как обязательный (опция внутри этапа 3). Лаунчер/proxy/site/bot/decor —
не сабмодули, для dev-цикла не нужны.

## Verification

- `python -m unittest discover` в workspace (перенесённые `test_devstack.py` + новые юниты
  маппинга/резолва; перенесённые `test_mod_jars.py`, `test_parkour_test.py`).
- Сквозной цикл: `just setup` → `just build-mods` → `just server start` → `just play --mod`
  → правка тела метода → `bf watch` подменяет за секунды → `bot state` подтверждает;
  структурная правка → авто-рестарт, клиент возвращается в мир.
- `bf.py client --mod` + dev-лаунчер с freeze-env: локальный jar на месте после старта.
- MCP: `status`/`server rcon list`/`bot state` через MCP-клиент против поднятого стека.
- Проверка отсутствия регрессий паков: сборка без `localmods/` идёт по пинам как раньше
  (`verify_archives` в сервере проходит, клиентский `refresh` не затронут).
