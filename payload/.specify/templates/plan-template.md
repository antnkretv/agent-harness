# Implementation Plan: [FEATURE]

**Branch**: `[feature/###-feature-name]` | **Date**: [DATE] | **Spec**: [link]

**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

[Extract from feature spec: primary requirement + technical approach from research]

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

<!--
  Профиль заполняется на каждое затронутое приложение из apps[] манифеста .harness/project/manifest.json;
  граница проходит по пути в дереве (`docs/platform/architecture/area-map.md`). Версии не переписываются: их держат
  машинные манифесты стека, здесь - ссылка на файл.
-->

### Профиль приложения `[apps[].id]` (`[apps[].path]`)

<!-- Дефолты - документ приложения (apps[].doc) и его карта архитектуры. -->

**Language/Version**: [язык; версия - ссылкой на машинный манифест стека]

**Primary Dependencies**: [фреймворки и библиотеки, на которых строится фича]

**Storage**: [хранилища или "нет"]

**Testing**: [формы тестов области; verify-гейт - команда `check` приложения из манифеста; архитектурный гейт стека - docs/platform/architecture/gates.md]

**Target Platform**: [среда исполнения]

**Project Type**: [модульный монолит, сервис, SPA, библиотека - по карте архитектуры]

**Затронутые модули / слои**: [перечислить реальные модули и слои]

**Constraints**: [ограничения из ADR и принципов продукта; ADR-границы не нарушать (иначе правка ADR)]

**Scale/Scope**: [число эндпоинтов, экранов, сущностей, миграций - domain-specific]

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Чек-лист гейтов из `.specify/memory/constitution.md` (отметить применимые):

- [ ] **MVP и подтвержденная польза:** по принципу конституции; объем не шире постановки.
- [ ] **Общие требования harness:** план не нарушает требований, на которые ссылается раздел конституции "Общие требования harness"; нужен ли новый ADR по порогу реестра решений (`docs/platform/adr/README.md`).
- [ ] **Принципы продукта:** каждый применимый принцип из дельты конституции отмечен отдельной строкой.

> **Платформенный трек:** этот `plan.md` живет в `specs/<трек>/plan.md` так же, как продуктовый
> (Context → Goal/postcondition → файлы что→куда → зависимости → затронутые проекты → порядок PR),
> а решение фиксируется **правкой действующего ADR или новым ADR** (`docs/platform/adr/<область>/`), если проходит порог реестра решений (`docs/platform/adr/README.md`). Нарушения, требующие обоснования, - в Complexity Tracking ниже.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Use the real layout of the touched application from its
  architecture map; fill the tree of every application the feature touches.
  The delivered plan must not include placeholder labels.
-->

```text
[apps[].path]/
├── [модуль или слой]/   # что меняется
└── [тесты]/             # acceptance, критерий docs/platform/standards/test-usefulness.md
```

**Structure Decision**: [какие конкретные модули, проекты и слои затрагиваются; перечислить реальные пути]

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
