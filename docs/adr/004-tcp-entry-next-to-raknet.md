# ADR 004: вход игроков — RakNet рядом с TCP

- Статус: принято, реализовано (proxy#74: proxy#81, client#18,
  Launcher#109, server#355). Заменяет ADR 001 (только RakNet).
- Основной вход — RakNet: Game — `raknet;…:25566`, Workshop —
  `raknet;…:25567`. Raknetify — сборка Blockfield `bf3`
  (`0.1.0+alpha.5.1-bf3`, GitHub Release `Blockfield/raknetify`,
  SHA256 закреплён в Dockerfile; `tests/test_raknetify_pin.py`
  проверяет пин): upstream `76ab30a` + фикс утечки SYNC FrameData +
  второй UDP-порт мастерской.
- Рядом TCP-вход на 25565 того же хоста для сетей с порезанным UDP:
  TCP-логин проходит тот же Drasl-шлюз, что и RakNet, и всегда
  попадает в Game (порт 25565 — не порт мастерской). Лаунчер
  (>= 1.0.11) пингует `raknet;`-адрес по TCP 25565 и при недоступности
  RakNet падает назад на TCP того же хоста, запоминая на 24 ч, затем
  повторяет RakNet; Workshop-цели назад не падают. TCP 25565 также
  обслуживает status ping.
- Владелец транспортного контракта: proxy (`blockfield-proxy`).
- Fail-closed (решение владельца 2026-10-18): несовпадение
  внутренностей Velocity/Raknetify после обновления отклоняет каждый
  вход (`Velocity/Raknetify internals mismatch`); status ping не
  затронут. Отказы входа считаются по причинам; в лог попадает только
  первый отказ каждой причины
  (`login-rejections first <причина>` + текущие счётчики).
- Проверено в пинах proxy `178704d`, client `37bff1f`,
  launcher `215796a`, server#355; стенд — `.dev/px74-u4/report.md`.
  При обновлении Velocity или Raknetify перепроверяйте вход по RakNet
  (оба порта) и по TCP.
