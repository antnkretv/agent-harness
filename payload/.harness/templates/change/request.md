## Проблема

<!-- fill -->

## Что сделано

<!-- fill -->

## Что не сделано и почему
<!-- optional -->

<!-- fill -->

## Проверки

<!-- fill -->

## Связанные артефакты (SDD)
<!-- optional -->
<!-- pick: any -->

- **Спека / FR:** <!-- fill -->
- **ADR:** <!-- fill -->
- **План:** <!-- fill -->

## Критерии приемки

| Пункт постановки | Чем закрыт |
| --- | --- |
<!-- fill -->

## Находки
<!-- optional -->

<!-- fill -->

## Тип изменения
<!-- pick: some -->

- [ ] feat - новая функциональность
- [ ] fix - исправление
- [ ] refactor / arch - рефакторинг / изменение арх-зависимостей
- [ ] docs - документация
- [ ] chore / build / ci

## Чеклист
<!-- pick: all -->

- [ ] {{#if setting:agents.findings=same-pr}}Находки по пути починены здесь и перечислены в разделе "Находки"{{else}}Находки разобраны по политике `agents.findings`, дефекты PR починены в нем{{/if}} - `.harness/CONTRIBUTING.md` §10
- [ ] Для каждой сущности определен свой SoT - `.harness/CONTRIBUTING.md` §13, SoT
- [ ] Код соответствует принципам DRY и KISS - `.harness/CONTRIBUTING.md` §13, DRY и KISS <!-- when: feat fix refactor chore -->
- [ ] Новые запросы к данным сверены с запросами модуля - `.harness/review/requirements.md` <!-- when: feat fix refactor -->
- [ ] Права и принадлежность данных берутся из серверного контекста - `.harness/CONTRIBUTING.md` §13, безопасность <!-- when: feat fix refactor -->
- [ ] Новые тесты проверяют бизнес-правила, по сценарию на правило - [критерий полезности теста](/docs/platform/standards/test-usefulness.md) <!-- when: feat fix -->
- [ ] Тексты, ошибки и новые комментарии соответствуют стандартам - [текста](/docs/platform/standards/text.md) и [комментариев](/docs/platform/standards/code-comments.md)
- [ ] Документация обновлена у владельца факта - [карта ролей](/docs/platform/standards/documentation.md#карта-ролей)
- [ ] Документация не пересказывает код, конфигурацию и вывод команд - `.harness/CONTRIBUTING.md` §13, DRY
- [ ] Документы об измененном поведении найдены поиском по репозиторию - скилл `issue-dev`, шаг `finding-audit`, п. 1 <!-- when: feat fix docs -->
- [ ] Смена архитектурных границ оформлена правкой действующего ADR или новым ADR - разделы "Жизненный цикл" и "Порог" `.harness/docs/adr.md` <!-- when: feat refactor -->
