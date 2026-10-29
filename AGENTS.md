# Blockfield: правила агента

- Перед стендом: [README](README.md), [DEVELOPING](docs/DEVELOPING.md).
  Межрепозиторные связи: [ARCHITECTURE](docs/ARCHITECTURE.md); обновляйте карту и контракт владельца при изменении связи.
- Код меняйте в его repo: `client/`, `server/`, `localmods/*` — submodules; workspace хранит инструменты и проверенные SHA.
  Перед модом прочитайте целиком его `CLAUDE.md` и `docs/AGENTS.md`; подробные справочники — по задаче.
- Активный код — Blockfield; архивы сохраняют историю. Не используйте mise и глобальные установки: только кеш/venv проекта.
- Shell — POSIX; Bash явно через `bash -c`. Zsh не разбивает переменные на слова и отклоняет unmatched globs.
- Переиспользуйте helper/stdlib, проследите callers и формат данных. Комментарии — только неочевидный замысел;
  `ponytail:` — ограничение и условие пересмотра.

## Удалённые стенды Boat

- Подготовка и границы: [docs/BOAT.md](docs/BOAT.md); CLI — `just boat`, без самообновления.
- Local first: сначала используйте свободный локальный стенд. Boat нужен, чтобы убрать
  ожидание занятого стенда/слота сборки, а не переносить туда всю разработку.
- Boat — opt-in: используйте отдельную VM только когда владелец текущей задачи выбрал Boat
  и подключил свой аккаунт. Остальные разработчики работают локально без Boat;
  доступ к личному аккаунту владельца им не предоставляется. Агент и маршрутизация остаются в T3.
- Очередь на локальный стенд сама по себе не разрешает расходовать личную квоту Boat.
  Права на чужую VM не подразумеваются.
- Координатор проверяет остаток и скорость расхода относительно плана партии; предупреждает
  при остатке ≤20% (≤10% — критический) или расходе >125% плана за интервал ≥5 минут.
  В trial учитывается меньший из планового остатка и отдельного trial cap.
- Координатор ведёт VM → задача/владелец/deadline, сверяет cleanup после результатов агентов
  и перед handoff. Задача завершена — её VM подтверждённо `stopped`, evidence сохранены;
  при ожидании ревью или квоты модели VM останавливают. Чужие сессии не останавливают.
- Политика процессов и явное согласие на конкретный headless-клиент сохраняются в VM.
  Не публикуйте игровые управляющие порты и не копируйте credentials с компьютера владельца.

## Владение процессами

Это safety-critical политика. Не удаляйте, не ослабляйте и не сокращайте этот
раздел без явного запроса владельца изменить политику владения процессами.

- Before starting any dev, preview, watch, or other long-running local process,
  discover existing instances first. Check the expected URL/port, listener
  (`ss -ltnp`), command, and `/proc/<pid>/cwd`; probe the service itself when
  practical. If a suitable instance is already running, reuse it. An occupied
  default port is a reason to investigate, not to silently start another instance
  on the next port.
- Start a server only when the current task requires it. Do not restart a suitable
  existing server merely to get cleaner logs or a terminal handle.
- Treat every unregistered process as user-owned, including processes that
  predate the current agent session. Never stop, restart, signal, or replace one
  without explicit user approval.
- Start workspace-owned servers and clients through `just server start` and
  `just play`. The workspace starts Java directly and records PID, PID birth
  time, executable, command, working directory, log, and session owner in
  `.dev/server.pid` and `.dev/client.pid` (or the chosen `BF_DEV_HOME`). On Linux
  it creates a separate process group and verifies `/proc` identity; on Windows
  it uses native process identity and handles. No personal process helper is required.
- Inspect registered ownership with `just status` and the workspace PID records.
  Stop a server or client only through `just server stop` or `just play --stop`
  and only when its recorded owner matches the current session and the native
  process identity still matches. A process registered to another session may
  be reused but must not be stopped without explicit user approval; these
  commands have no forced ownership bypass. Linux uses a verified pidfd and
  Windows a verified process handle, preventing signals to a reused PID.
  A record without a native identity never authorizes a signal; a live one
  blocks starting a replacement.
- Never use broad cleanup commands such as `pkill`, `killall`, `fuser -k`,
  killing by process name, or killing by port. If ownership cannot be proven
  through the registry and exact process identity, do not kill the process.
  During cleanup, leave reused/user-owned servers running and stop only
  processes started by the current session.

Для игровых проверок действует более узкое правило: только собственный стенд.
Не подключайте бота и не изменяйте мир, датапаки, options.txt или JAR другой сессии.
Если чужой стенд занимает ресурс, ждите или сообщите владельцу.
Перед стендом/Gradle — `just status` (на Linux также `free -g`); лимит машины: server 3 ГБ, client 4 ГБ,
Gradle 2 ГБ. Стенд один; Gradle — до двух одновременно с `--max-workers=2` и самозавершающимся демоном
(`-Dorg.gradle.daemon.idletimeout=600000`; `--stop` и убийство демонов по имени запрещены),
если после старта остаётся ≥ 3 ГБ MemAvailable; стенд и замеры времени — без параллельного Gradle.
`lint`/`format-check`/`check` мода, client и server Gradle не запускают и очереди не ждут;
`check` workspace собирает testbot через Gradle. При параллельной работе агентов стендом
и тяжёлыми сборками владеет один координатор.

После сохранения evidence очистите созданный задачей `BF_DEV_HOME`: `just dev prune --apply .dev/<задача>`.

## Ожидания и завершение процессов

- Wait on an exact completion signal: the PID captured at launch
  (`tail --pid=<pid> -f /dev/null`, `kill -0 <pid>`), an exit-status or result
  file, a log marker, or a service probe. Never detect “still running” by name
  (`pgrep -f`, `ps | grep`): the pattern is in the waiter’s own command line,
  so it matches itself and never ends.
- Every wait needs a hard deadline (`timeout <n>` or a bounded loop). On timeout,
  re-inspect and report; do not silently extend.
- Before ending a turn or reporting completion, make sure no waits or watchers
  you started are still running; stop them by their exact PID or task ID.

## Приложения

- Computer use требует явного согласия на текущую задачу и конкретный экземпляр;
  инструменты/авторазрешения его не заменяют. Не обходите правило через shell/API/агентов.
- Minecraft агента — только `just play --headless` на собственном backend, без звука.
  Linux: свой Xvfb; Windows: запуск агента запрещён до проверенного headless backend. Подтвердите владельца backend;
  не наследуйте `DISPLAY`/`WAYLAND_DISPLAY`. Окно, fullscreen и `fullscreen:false` не являются headless.
  Нет backend — ошибка без desktop fallback. Согласие на этот клиент не разрешает приложения владельца.
  Проверки/снимки — через его testbot/MCP; после работы `just play --stop`.
- Разрешённый браузер: навык playwright-cli, именованная изолированная headless-сессия.
  Браузер владельца требует отдельного согласия: не перенаправляйте страницы, завершайте через detach.
  Удаляйте только свои временные снимки/traces/сессии.

## Проверки и выпуск

- Закреплённые lint/format-check/tests: `scripts/quality.py`, у мода `dev/quality.py`, CI владельца.
  Не ослабляйте проверки. Локально: `format-check`/`check` и точечные тесты;
  полный `java-check`/`test` выполняет PR CI — он должен быть зелёным до merge.
  Игровая логика требует сценария до/после; компиляция не заменяет игровую приёмку.
- Одна задача — один PR в repo, без дробления по шагам/юнитам: каждый PR и merge оплачивают CI-минуты.
- Исполняемые загрузки: immutable full SHA/версия + checksum, без main/latest и сети при импорте.
  ZIP: стандартный zlib 1.3.2; другой compressor останавливает сборку до артефакта.
  Исторические release checksums не заменяйте пересборкой.
- Секреты (включая webhook URL) не печатайте/коммитьте; credentials не переносите в CI. Исключения —
  отдельный ключ OpenRouter с лимитом расходов только для генерации текстов (`OPENROUTER_API_KEY`) и R2-ключ
  CI клиента только на бакет зеркала пака `blockfield-pack` (`R2_PACK_*`, решение владельца 2026-10-11).
  Используйте предусмотренные ограниченные CI secrets по назначению.
- Не переписывайте историю, не удаляйте теги/релизы/backup, не переименовывайте клоны и не теряйте уникальные изменения.
  Коммит/PR submodule — в его repo; workspace SHA — после проверки пары. Без идентификаторов моделей в артефактах/PR/коммитах.
- После задачи: чистый main, без лишних branches/worktrees. PR-ветки удаляются после merge с сохранением истории;
  чужие/уникальные правки сохраняйте.
- Каждый тег `vX.Y.Z` клиента/сервера и ручной Deploy требуют явного подтверждения владельца в текущем разговоре:
  они запускают выпуск **и прод-деплой**. Подготовка PR/тестов/release notes это разрешение не заменяет.
- Отложенное до релиза (пины, теги ресурсов, проверки) — открытые issues с меткой `next-release` в client/server;
  `just release status` и `prepare` их выводят. Выполните их до тега, закрывайте после выпуска и проверки.
- Релиз: `release-notes.md`, `announcement.json` и описания PR пишет CI по фактам (`.github/actions/release-text`);
  агент прозу не пишет, `release-notes/vX.Y.Z.md` не готовит, PR достаточно названия. [Формат](client/docs/RELEASE-NOTES.md). Контракт: Minecraft/Fabric, protocol, общие моды/ресурсы.
  Изменение `side = "both"` — парный выпуск, сервер первым: [DEPLOY](server/docs/DEPLOY.md).
- Отчёт раздельно: **слито / опубликовано / развёрнуто / проверено в игре**; реальные команды/ссылки/SHA и ограничения.
