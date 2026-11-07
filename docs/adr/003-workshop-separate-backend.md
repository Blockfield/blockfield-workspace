# ADR 003: Game и Workshop — разные backend

- Статус: принято, реализовано.
- Game backend и Workshop (editor / candidate-test) работают одновременно
  как отдельные backend за proxy; controller управляет только Workshop,
  game продолжает работать независимо.
- Проверено в пине мода `50083d6`: tablet/workshop-клиент
  (`client/.../workshop/`, `WorkshopAiClient`) и общие WorkshopAi-пакеты
  (`common/.../network/packet/WorkshopAi*`); детали — mod
  `docs/DEVELOPMENT-REFERENCE.md`.
- Хранилище карт (CAS, активные heads) и lifecycle принадлежат
  controller: workshop `docs/OPERATIONS.md`.
- Механика образов/тегов Workshop здесь не верифицирована
  (приватный репозиторий) и намеренно не зафиксирована.
