# ADR 002: pack-контракт и протокол мода

- Статус: принято, реализовано в моде.
- Login-канал `blockfield:pack_contract`: клиент отвечает
  `<contract>:<PROTOCOL>`; несовпадение контракта — OUTDATED,
  несовпадение протокола — INCOMPATIBLE.
- Проверено в пине мода `50083d6`:
  `common/.../network/PackContract.java` (`PROTOCOL = 10`),
  рядом `PackVersion.java`; контракт мода описан в
  `docs/DEVELOPMENT-REFERENCE.md`.
- Любое изменение общих пакетов/payload бампит `PROTOCOL` и выходит
  парой client+server; client-only арт без бампа молча рассинхронил бы хитбоксы.
- Совместимость сборок пака (моды, ресурсы, protocol) — отдельно:
  client `compatibility.json` и server `client-compatibility.json`,
  формат — `docs/DEPLOY.md` сервера.
