# Blockfield: правила агента

- Один приватный монорепозиторий `Blockfield/blockfield`, ветка `main`; архивы — только история. Этот repo — инструменты и пины, код продукта — в submodules (`client/`, `localmods/*`): коммит — в его repo. Карта связей: [ARCHITECTURE](docs/ARCHITECTURE.md) — при изменении связи обновите её и контракт в той же PR.
- Перед стендом: [README](README.md), [DEVELOPING](docs/DEVELOPING.md). Скилл `blockfield-agent` — каждой задаче первым; Boat — `blockfield-boat` при opt-in владельца ([BOAT](docs/BOAT.md)).
- Shell — POSIX (`bash -c` для Bash). Переиспользуйте helper/stdlib, проследите callers и формат данных; комментарии — только неочевидный замысел; `ponytail:` — ограничение и условие пересмотра. Без mise и глобальных установок: только кеш/venv проекта.
- Сборки и стенды — локально или на Boat по выбору владельца задачи; тяжёлые и параллельные — на Boat. Gradle — только через `just`; лимиты машины, очередь стенда и headless-backend — [DEVELOPING](docs/DEVELOPING.md), VM-реестр — в skill `blockfield-boat`.
- Computer use и браузер владельца — только с явным согласием на задачу и экземпляр; автоматизация — изолированные headless-сессии, чужие снимки/traces — удалять.
- Gates до push: `just lint` и `just test`/`just check` затронутой части; полный прогон — CI, merge — только с зелёным head. CI стартует на `pull_request` сам, ревью идёт параллельно; игровая логика требует сценария до/после, компиляция приёмку не заменяет.
- Одна задача — один PR в `main` (несколько модулей — одна PR); историю `main`/теги не трогать, только своя ветка. Ревью: наш круг (read-only агент-ревьюер, находки — координатору) плюс CodeRabbit и Greptile; PR ведёт `babysit-pr`: находки сверяют с кодом, настоящие чинят одним push, лишние отклоняют с причиной и follow-up; на CI и ботов не ждать. Объём — из брифа; в findings — must/should, nice — в бэклог; ревью — со скомпилированного head. Описания PR и тексты релиза пишет CI по фактам — агенту достаточно названия.
- Релиз — один прогон `deploy.yml` с зелёного SHA `main` (гейт проверяет CI+Images, откат автоматический ≤5 мин). Деплой без отдельного OK — только в действующем режиме релиза владельца; провал гейта/RC/deploy его отменяет. Приёмка — RC на своём стенде, CI и результаты deploy; недозревшее — зелёным и открытым, удержанные ведёт координатор.
- Прод меняют только координатор и CI: конфиг — через Dokploy MCP, дочерние агенты — read-only; секреты — только предусмотренные ограниченные CI secrets, значения — только через их вводы. Агенты не заходят на прод: без тестовых аккаунтов и входа в игру.
- Админка и сайт: без сырого JSON, поля — из каталога игры, новый экран — сначала макет; тексты — грамотный русский без воды; визуал — скриншоты до/после (веб 1440 и 390, игра — headless-клиент).
- Игровые инварианты (хитрег, нож): [GAME-INVARIANTS](docs/GAME-INVARIANTS.md) — менять только по решению владельца.
- Ждите точный сигнал (PID/файл-результат/маркер/проба) с hard deadline, а не имя (`pgrep` видит сам себя); чужие ожидания не бросайте; несуществующее (CI без run, PID без процесса) — не ждите, разбирайтесь.
- Координатор ведёт бэклог `blockfield` в agentdeck и решения владельца дословно; незавершённое — в бэклог, не в чат. Пункт закрывается слитым зелёным SHA без ожидания прода.
- Финал: первая строка `DONE:/PARTIAL:/BLOCKED:`, evidence — в `.dev/<задача>/`, PR URL + head SHA + состояние checks; push budget — из брифа; пункт без evidence — не DONE; свои Boat VM — в `stopped`; свои PR-ветки удалять после merge, чужие — не трогать.

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
