#!/usr/bin/env bash
# Отсутствие манифеста проверяется на копии .harness/lib вне рабочего дерева.
# Контракт и причина такой изоляции - .harness/README.md.

set -uo pipefail

# Проверки печатают русский: под Windows stdout питона иначе открыт в кодировке
# платформы, и вывод падает еще до сравнения.
export PYTHONIOENCODING=utf-8

# Хук git экспортирует GIT_DIR: с ним git init в "$mimic" сделал бы общий репозиторий bare.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1
[ -d "$root/.harness" ] || root="$(dirname "$root")"
for required in project/manifest.json methodology.json pipelines.json lib/manifest.py lib/manifest.sh; do
  if [ ! -r "$root/.harness/$required" ]; then
    printf 'Installed harness file is missing: %s\n' "$root/.harness/$required" >&2
    exit 1
  fi
done
cd "$root" || exit 1

failed=0
checked=0

ok()   { checked=$((checked + 1)); printf '  ok    %s\n' "$1"; }
fail() { checked=$((checked + 1)); failed=$((failed + 1)); printf '  FAIL  %s\n' "$1" >&2
         printf '        ждали: %s\n        было:  %s\n' "$2" "$3" >&2; }

eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else fail "$1" "$2" "$3"; fi
}

# Сценарии питоновского читателя отличаются каталогом запуска и путем к слою,
# а не разбором: преамбула у них поэтому одна.
py() {  # $1 = выражение, $2 = каталог запуска, $3 = путь к читателю
  (cd "${2:-$root}" && harness_python -c "import sys; sys.path.insert(0, '${3:-.harness/lib}')
from manifest import get, get_list, get_records
print($1)" 2>/dev/null)
}

# shellcheck disable=SC1091
. "$root/.harness/lib/manifest.sh"

# Ожидаемые значения берутся из самого файла: тест проверяет читателя, а не содержимое манифеста.
live() {  # $1 = выражение над data, $2 = файл манифеста
  harness_python -c 'import json, sys; data = json.load(open(sys.argv[1], encoding="utf-8")); print(eval(sys.argv[2], {"data": data}))' \
    "$root/.harness/${2:-project/manifest.json}" "$1"
}
# Пустой список читатель отдает запасным значением, как и отсутствующий.
live_apps="$(live '" ".join(a["id"] for a in data["apps"])')"
live_apps="${live_apps:-ЗАПАСНОЕ}"
live_name="$(live 'data["project"]["name"]')"
live_prefixes="$(live '" ".join(data["docs"]["machineTextPrefixes"])')"
live_passes="$(live '" ".join(step["id"] for step in data["processes"]["review"]["steps"])' methodology.json)"

echo "manifest.sh - манифест на месте:"
eq "скаляр читается" "$live_name" "$(manifest_get '.project.name' 'ЗАПАСНОЕ')"
eq "неизвестный ключ отдает запасное" "ЗАПАСНОЕ" "$(manifest_get '.project.nothingHere' 'ЗАПАСНОЕ')"
eq "список читается" "$live_apps" "$(manifest_list '.apps[].id' 'ЗАПАСНОЕ' | tr '\n' ' ' | sed 's/ $//')"
eq "пустой путь отдает запасное" "a b" "$(manifest_list '.nope[].id' 'a b' | tr '\n' ' ' | sed 's/ $//')"
eq "массив без итератора читается по элементу" "$live_prefixes" \
  "$(manifest_list '.docs.machineTextPrefixes' 'ЗАПАСНОЕ' | tr '\n' ' ' | sed 's/ $//')"

sandbox="$(mktemp -d)"
mimic="$(mktemp -d)"
trap 'rm -rf "$sandbox" "$mimic"' EXIT
mkdir -p "$sandbox/.harness"
cp -r "$root/.harness/lib" "$sandbox/.harness/lib"

echo "manifest.sh - манифеста нет:"
out="$(cd "$sandbox" && bash -c '. .harness/lib/manifest.sh; manifest_get ".project.name" "ЗАПАСНОЕ"' 2>/dev/null)"
eq "скаляр отдает запасное" "ЗАПАСНОЕ" "$out"
out="$(cd "$sandbox" && bash -c '. .harness/lib/manifest.sh; manifest_list ".apps[].id" "a b"' 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
eq "список отдает запасное" "a b" "$out"
out="$(cd "$sandbox" && bash -c '. .harness/lib/manifest.sh; manifest_get ".a.b" "X"; echo "ЖИВ"' 2>/dev/null | tail -n1)"
eq "чтение не роняет вызывающий скрипт" "ЖИВ" "$out"

echo "manifest.py:"
eq "скаляр читается" "$live_name" "$(py 'get("project.name", "ЗАПАСНОЕ")')"
eq "неизвестный ключ отдает запасное" "ЗАПАСНОЕ" "$(py 'get("project.nothingHere", "ЗАПАСНОЕ")')"
eq "список объектов собирается" "$live_apps" \
  "$(py '" ".join(get_list("apps[].id", ["ЗАПАСНОЕ"]))')"
eq "без манифеста отдает запасное" "ЗАПАСНОЕ a b" \
  "$(py 'get("project.name", "ЗАПАСНОЕ"), " ".join(get_list("apps[].id", ["a", "b"]))' "$sandbox")"

# Читатель отдает объявление своей копии: иначе правка methodology.json решала бы,
# чем ревьюют тот же PR.
mkdir -p "$mimic/.harness/project" "$mimic/.review-base/.harness/project" "$mimic/.review-base/.harness/scripts"
git -C "$mimic" init -q . 2>/dev/null
printf '%s\n' '{ "project": { "name": "ЧУЖОЙ-ПРОЕКТ" } }' > "$mimic/.harness/project/manifest.json"
printf '%s\n' '{ "processes": { "review": { "steps": [ { "id": "чужой", "marker": "Ч", "budget": 9, "job": "ч" } ] } } }' \
  > "$mimic/.harness/methodology.json"
cp -r "$root/.harness/lib" "$mimic/.review-base/.harness/lib"
cp "$root/.harness/project/manifest.json" "$mimic/.review-base/.harness/project/"
cp "$root/.harness/methodology.json" "$root/.harness/pipelines.json" "$mimic/.review-base/.harness/"
cp "$root/.harness/scripts/check/check-pipeline.py" "$root/.harness/scripts/ci_common.py" \
  "$mimic/.review-base/.harness/scripts/"

echo "слой рядом с рабочей копией:"
eq "python-читатель держится своего слоя" "$live_name" \
  "$(py 'get("project.name", "ЗАПАСНОЕ")' "$mimic" ".review-base/.harness/lib")"

out="$(cd "$mimic" && bash -c '. .review-base/.harness/lib/manifest.sh; manifest_get ".project.name" "ЗАПАСНОЕ"' 2>/dev/null)"
eq "shell-читатель держится своего слоя" "$live_name" "$out"

out="$(cd "$mimic" && harness_python .review-base/.harness/lib/manifest.py methodology.json 'processes.review.steps[].id' 2>/dev/null \
  | tr -d '\r' | tr '\n' ' ' | sed 's/ $//')"
eq "состав проходов приходит из своего слоя" "$live_passes" "$out"

# Контракт у конвейера тот же, что у идентичности проекта: нет файла - вернется
# запасное.
echo "pipelines.json:"
eq "без манифеста конвейера отдает запасное" "1" \
  "$(py 'len(get_records("processes.review.steps", ["ЗАПАСНОЕ"], file="methodology.json"))' "$sandbox")"

echo
if [ "$failed" -ne 0 ]; then
  echo "x манифест: $failed из $checked проверок не прошли" >&2
  exit 1
fi
echo "манифест: $checked проверок пройдено"
