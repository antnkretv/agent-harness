# Feature Specification: [FEATURE NAME]

**Feature Branch**: `[feature/###-feature-name]`

**Created**: [DATE]

**Status**: Draft

**Input**: User description: "$ARGUMENTS"

<!--
  Правила проекта (см. .specify/memory/constitution.md и .harness/docs/sdd.md):
  - Язык спеки — РУССКИЙ.
  - Требования — в нотации EARS, с приоритетом MUST/SHOULD/COULD и статусом as-is (✅ есть / 🟡 частично / 🔴 нет).
    Статус as-is описывает состояние ДО трека и после реализации не пересматривается.
  - Спека описывает ПОВЕДЕНИЕ и контракты, не реализацию (без имен классов хранилища и SQL - это для plan.md).
    Для участка UI это ПОВЕДЕНИЕ и UI-контракт, не верстка (без имен компонентов и CSS - это для plan.md).
  - Граница областей проходит по пути в дереве (`docs/platform/architecture/area-map.md`): домен, инварианты и контракты API живут
    в серверной области, страницы, формы и wiring - в приложении интерфейса.
  - Фичи, которые трогают права, данные пользователей или внешние входы, ОБЯЗАНЫ нести NFR безопасности
    по CONTRIBUTING §13, "Безопасность и приватность", плюс NFR из принципов продукта в дельте конституции.
  - Не дублировать docs/: продуктовая спека живет здесь (specs/<трек>/), docs/features/<feature>/ ссылается на нее.
-->

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories should be PRIORITIZED as user journeys ordered by importance.
  Each user story/journey must be INDEPENDENTLY TESTABLE - meaning if you implement just ONE of them,
  you should still have a viable MVP (Minimum Viable Product) that delivers value.

  Assign priorities (P1, P2, P3, etc.) to each story, where P1 is the most critical.
  Think of each story as a standalone slice of functionality that can be:
  - Developed independently
  - Tested independently
  - Deployed independently
  - Demonstrated to users independently
-->

### User Story 1 - [Brief Title] (Priority: P1)

[Describe this user journey in plain language]

**Why this priority**: [Explain the value and why it has this priority level]

**Independent Test**: [Describe how this can be tested independently - e.g., "Can be fully tested by [specific action] and delivers [specific value]"]

**Acceptance Scenarios**:

1. **Given** [initial state], **When** [action], **Then** [expected outcome]
2. **Given** [initial state], **When** [action], **Then** [expected outcome]

---

### User Story 2 - [Brief Title] (Priority: P2)

[Describe this user journey in plain language]

**Why this priority**: [Explain the value and why it has this priority level]

**Independent Test**: [Describe how this can be tested independently]

**Acceptance Scenarios**:

1. **Given** [initial state], **When** [action], **Then** [expected outcome]

---

### User Story 3 - [Brief Title] (Priority: P3)

[Describe this user journey in plain language]

**Why this priority**: [Explain the value and why it has this priority level]

**Independent Test**: [Describe how this can be tested independently]

**Acceptance Scenarios**:

1. **Given** [initial state], **When** [action], **Then** [expected outcome]

---

[Add more user stories as needed, each with an assigned priority]

### Edge Cases

<!--
  ACTION REQUIRED: The content in this section represents placeholders.
  Fill them out with the right edge cases.
-->

- What happens when [boundary condition]?
- How does system handle [error scenario]?

## Requirements *(mandatory)*

<!--
  ACTION REQUIRED: The content in this section represents placeholders.
  Fill them out with the right functional requirements.
-->

### Functional Requirements

<!--
  Формулируйте требования в нотации EARS (Easy Approach to Requirements Syntax):
  - Ubiquitous:   «Система ДОЛЖНА <действие>.»
  - Event-driven: «КОГДА <событие>, система ДОЛЖНА <действие>.»
  - State-driven: «ПОКА <состояние>, система ДОЛЖНА <действие>.»
  - Optional:     «ГДЕ <вариант/фича>, система ДОЛЖНА <действие>.»
  - Unwanted:     «ЕСЛИ <нежелательное условие>, ТО система ДОЛЖНА <реакция>.»
  Каждое FR независимо проверяемо и трассируется в тест (критерий `docs/platform/standards/test-usefulness.md`): связь держат
  `tasks.md` и сообщение коммита/PR, а не комментарий кода (`docs/platform/standards/code-comments.md` §7).
  Acceptance-сценарии (Given/When/Then) выразимы как тест.
  Статус as-is: ✅ есть · 🟡 частично · 🔴 нет. Приоритет: MUST/SHOULD/COULD.
  Колонка as-is - снимок состояния ДО реализации трека, а не текущее состояние продукта. После
  реализации она не пересматривается; строка-заметка под заголовком обязательна. Что построено
  сейчас - `tasks.md` трека и реестр требований.
-->

Статус as-is - снимок состояния до реализации трека (спека создана <ДАТА или базовый коммит>). После нее он не пересматривается: отмеченные задачи в `tasks.md` показывают, что из требований уже построено, а действующий вердикт по требованиям держит реестр требований проекта, если он ведется.

| ID | Требование (EARS) | Статус as-is | Приоритет |
|---|---|---|---|
| **FR-001** | КОГДА <событие>, система ДОЛЖНА <действие> | 🔴 | MUST |
| **FR-002** | Система ДОЛЖНА <возможность> | 🔴 | MUST |
| **FR-003** | ЕСЛИ <нежелательное условие>, ТО система ДОЛЖНА <реакция> | 🔴 | SHOULD |

*Пример пометки неоднозначного требования (вход для `/speckit-clarify`):*

- **FR-00X**: Система ДОЛЖНА аутентифицировать через [NEEDS CLARIFICATION: метод не задан - какой стандарт принят в проекте?]

### Non-Functional Requirements *(include if feature involves data)*

<!--
  Сквозные ограничения. Для ЛЮБОЙ фичи с данными NFR безопасности ОБЯЗАТЕЛЕН (CONTRIBUTING §13, "Безопасность и приватность") вместе
  с NFR из принципов продукта.
  Оси сервера: права и изоляция данных, наблюдаемость, надежность (одна транзакция на факт),
  безопасность входа модели, производительность.
  Оси клиента: безопасность (права у сервера, секреты не в сборке, персональные данные не логируются),
  доступность (a11y), i18n, производительность (ленивая загрузка маршрутов, размер бандла).
-->

| ID | Требование |
|---|---|
| **NFR-001 (multi-tenant)** | Все чтения/мутации фильтруются по арендатору из серверного контекста; запрет доверять идентификатору арендатора из тела запроса; регрессионный ACL-тест |
| **NFR-002 (client security)** | *(если фича несет участок UI)* Авторизация и тенант определяются сервером, не клиентом; secrets и эндпоинты - только через переменные окружения сборки клиента; PII не логируется и не уходит в сторонние сервисы |

### Key Entities *(include if feature involves data)*

- **[Entity 1]**: [What it represents, key attributes without implementation]
- **[Entity 2]**: [What it represents, relationships to other entities]

## Success Criteria *(mandatory)*

<!--
  ACTION REQUIRED: Define measurable success criteria.
  These must be technology-agnostic and measurable.
-->

### Measurable Outcomes

- **SC-001**: [Measurable metric, e.g., "Users can complete account creation in under 2 minutes"]
- **SC-002**: [Measurable metric, e.g., "System handles 1000 concurrent users without degradation"]
- **SC-003**: [User satisfaction metric, e.g., "90% of users successfully complete primary task on first attempt"]
- **SC-004**: [Business metric, e.g., "Reduce support tickets related to [X] by 50%"]

## Assumptions

<!--
  ACTION REQUIRED: The content in this section represents placeholders.
  Fill them out with the right assumptions based on reasonable defaults
  chosen when the feature description did not specify certain details.
-->

- [Assumption about target users, e.g., "Users have stable internet connectivity"]
- [Assumption about scope boundaries, e.g., "Mobile support is out of scope for v1"]
- [Assumption about data/environment, e.g., "Existing authentication system will be reused"]
- [Dependency on existing system/service, e.g., "Requires access to the existing user profile API"]
