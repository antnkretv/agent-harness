# mcp-stand

Сервер MCP только для чтения одного стенда. Код один, на каждый стенд (production, test и другие) работает
свой экземпляр: свой файл конфигурации, свои ключи и токены, свое состояние OAuth и свое место в сети.

## Инструменты

| Инструмент | Что читает |
| --- | --- |
| `repository_read` | Список путей и содержимое снимка отслеживаемых файлов репозитория, его `repository`, `ref` и `commit` |
| `telemetry_read` | GET по путям чтения источников из `telemetry`: Prometheus, Loki, Tempo, Grafana, Alertmanager, OpenTelemetry Collector |
| `stand_status` | `host_status` - uptime, память, диски, состояние юнитов systemd; `workloads` - контейнеры Docker и pod Kubernetes; `backup_status` - отметки бэкапа |
| `workload_logs` | Логи контейнера Docker или pod Kubernetes, включая предыдущий контейнер pod |
| `read_result` | Следующие 24 000 символов большого ответа |

Пути чтения по виду источника - `READ_PATHS` в [policy.py](policy.py). Источник читается без прокси и без
перехода по редиректу. Имена контейнеров, pod, юнитов, отметок и пространств имен проверяются шаблоном;
pod и логи Kubernetes читаются только из пространств имен конфигурации.

Пределы: ответ источника или команды - 32 MiB, запрос к источнику и команда - 45 секунд, запрос диспетчера -
55 секунд, вызов диспетчера сервером - 60 секунд (по сроку сервер снимает процесс диспетчера вместе с его
командами, у `ssh` - клиент `ssh`), файл снимка - 32 MiB, запрос к диспетчеру - 64 KiB. Ответ длиннее
24 000 символов отдается частями: кеш держит до восьми результатов, 10 минут и 128 MiB. Одновременно идут
до четырех вызовов диспетчера.

`allow_write` в конфигурации - только `false`. С другим значением сервер, диспетчер и `stand.py check`
отказывают.

## Файлы

| Файл | Что это |
| --- | --- |
| [server.py](server.py) | Сервер MCP: streamable HTTP с OAuth и токенами CI или stdio |
| [oauth.py](oauth.py) | Регистрация клиента, согласие паролем владельца, статические токены CI |
| [dispatcher.py](dispatcher.py) | Диспетчер чтения: запрос JSON на stdin, ответ JSON на stdout |
| [policy.py](policy.py) | Схема конфигурации и пути чтения телеметрии |
| [entry.sh](entry.sh) | Принудительная команда ключа SSH на хосте стенда |
| [sudoers.template](sudoers.template) | Строка sudoers пользователя доступа: `@USER@` и `@DIR@` |
| [stand.py](stand.py) | `check`, `token`, `password`, `snapshot`, `verify` |
| [messages.env](messages.env), [messages.py](messages.py) | Сообщения и их читатель |
| [Dockerfile](Dockerfile), [compose.yml](compose.yml), [requirements.txt](requirements.txt) | Образ и экземпляр в Docker Compose |
| [stand.example.json](stand.example.json) | Пример конфигурации |

## Конфигурация

Один файл JSON на стенд, проверка - `../hooks/python.cmd stand.py check <файл>`. Переменные, которые называют
`token_env` и `static_tokens_env`, берутся из окружения процесса, затем из файла `stand.env` рядом с
конфигурацией: строки `ИМЯ=значение`.

| Поле | Значение |
| --- | --- |
| `stand` | Имя стенда: `[a-z0-9-]`, до 63 символов |
| `title` | Название в метаданных сервера и на странице согласия; без поля - `stand` |
| `allow_write` | `false` |
| `access.mode` | `local` - сервер запускает диспетчер отдельным процессом на своей машине; `ssh` - сервер зовет диспетчер на хосте стенда через `ssh` |
| `access.host`, `access.port`, `access.user` | Для `ssh`: хост стенда, порт SSH (без поля - 22), пользователь доступа |
| `access.key`, `access.known_hosts` | Для `ssh`: приватный ключ и файл `known_hosts` с ключом хоста |
| `telemetry.<имя>.kind` | `prometheus`, `loki`, `tempo`, `grafana`, `alertmanager` или `collector` |
| `telemetry.<имя>.url` | `http://<хост>:<порт>[/<путь>]`, `https://<хост>[:<порт>][/<путь>]` или `docker://<контейнер>:<порт>` - адрес контейнера в сети `docker_network` по `docker inspect` |
| `telemetry.<имя>.token_env` | Переменная с токеном заголовка `Authorization: Bearer` |
| `docker_network` | Сеть Docker адресов `docker://` |
| `host.units` | Юниты systemd для `systemctl is-active` |
| `host.backups.directory`, `host.backups.marks` | Абсолютный каталог отметок бэкапа и имена файлов отметок |
| `workloads.docker` | `true` - контейнеры и логи Docker |
| `workloads.kubernetes.command` | Команда kubectl списком: `["kubectl"]` или `["kubectl", "--kubeconfig", "/etc/kube/config"]` |
| `workloads.kubernetes.namespaces`, `workloads.kubernetes.default` | Пространства имен pod и логов; пространство по умолчанию, без поля - первое из списка |
| `repository` | Архив zip снимка репозитория |
| `state` | Каталог состояния: `oauth.sqlite`, копия ключа SSH `id_ssh`; без поля - `/state` |
| `public_url` | Для HTTP: адрес экземпляра без пути, issuer OAuth; ресурс MCP - `<public_url>/mcp` |
| `listen.host`, `listen.port` | Для HTTP: адрес и порт сервера |
| `allowed_hosts`, `allowed_origins` | Допустимые заголовки `Host` и `Origin`; без поля - хост `public_url` и `listen.host:listen.port`, `public_url` |
| `oauth.password_salt`, `oauth.password_hash` | Для HTTP: соль и PBKDF2-SHA256 пароля владельца в hex |
| `oauth.redirect_schemes` | Схемы адресов возврата OAuth помимо `https` и loopback `http`: `["cursor"]` |
| `static_tokens_env` | Переменная со списком sha256 токенов CI в hex через запятую |

Без `host.backups` раздел `backup_status` возвращает ошибку, без `workloads.docker` и
`workloads.kubernetes` - раздел `workloads`. Без `host.units` в `host_status` нет поля `services`. Без
`repository` инструмент `repository_read` возвращает ошибку.

## Экземпляр в Docker Compose

Каталог стенда, например `/etc/mcp-stand/app-production/`, держит `stand.json`, `stand.env`, для `ssh` -
`id_ed25519` и `known_hosts`, для `repository_read` - `repository.zip`. Подготовка и запуск из каталога
`.harness/mcp-stand`:

```bash
../hooks/python.cmd stand.py password       # {"password": ..., "oauth": {...}}: пароль владельцу, oauth - в stand.json
../hooks/python.cmd stand.py snapshot --repository <репозиторий> --ref origin/main --out /etc/mcp-stand/app-production/repository.zip
../hooks/python.cmd stand.py check /etc/mcp-stand/app-production/stand.json
STAND=app-production STAND_CONFIG_DIR=/etc/mcp-stand/app-production docker compose -f compose.yml up -d --build
```

| Переменная | Значение |
| --- | --- |
| `STAND` | Имя стенда: проект Compose и контейнер `mcp-stand-<STAND>`, том состояния `mcp-stand-<STAND>-state` |
| `STAND_CONFIG_DIR` | Каталог стенда; монтируется в `/config` только для чтения, сервер читает `/config/stand.json` |
| `STAND_NETWORK_MODE` | Сетевой режим контейнера: `host` без переменной, `container:<контейнер>` - сеть другого контейнера, например VPN-клиента |

Контейнер работает от UID 10001 без capabilities, с корневой файловой системой только для чтения, лимитами
512 MiB памяти, одного CPU и 64 процессов. Образ несет `ssh` и `procps`, `docker`, `kubectl` и `systemctl` в
нем нет. Снимок `repository.zip` сервер открывает при запуске: новый снимок вступает в силу после
`docker compose -f compose.yml restart`.

Сервер слушает HTTP без TLS. TLS для `public_url` завершает обратный прокси, который передает запросы на
`listen.host:listen.port` с исходным заголовком `Host`.

## Доступ по SSH

На машине экземпляра:

```bash
ssh-keygen -q -t ed25519 -N "" -C mcp-stand-app-production -f /etc/mcp-stand/app-production/id_ed25519
printf '192.0.2.10 %s\n' "$(ssh root@192.0.2.10 cat /etc/ssh/ssh_host_ed25519_key.pub)" > /etc/mcp-stand/app-production/known_hosts
```

На хосте стенда от root, из копии каталога `.harness/mcp-stand` с `stand.json`, `stand.env` и
`id_ed25519.pub` стенда. Нужны `python3`, `sudo` и программы включенных разделов: `docker`, команда
`workloads.kubernetes.command`, `systemctl`.

```bash
STAND=app-production
DIR=/usr/local/libexec/mcp-stand/$STAND
ACCOUNT=mcp-$STAND
install -d -m 0755 "$DIR"
install -m 0755 dispatcher.py entry.sh "$DIR"/
install -m 0644 policy.py messages.py messages.env "$DIR"/
install -m 0600 stand.json stand.env "$DIR"/
useradd --system --no-create-home --home-dir "/var/lib/$ACCOUNT" --shell /bin/sh "$ACCOUNT"
usermod --lock "$ACCOUNT"
install -d -m 0755 "/var/lib/$ACCOUNT/.ssh"
printf 'from="%s",restrict,command="%s/entry.sh" %s\n' '<адрес экземпляра>' "$DIR" "$(cat id_ed25519.pub)" \
  > "/var/lib/$ACCOUNT/.ssh/authorized_keys"
sed -e "s|@USER@|$ACCOUNT|" -e "s|@DIR@|$DIR|" sudoers.template > "/etc/sudoers.d/$ACCOUNT"
chmod 0440 "/etc/sudoers.d/$ACCOUNT"
visudo -cf "/etc/sudoers.d/$ACCOUNT"
```

Пользователь доступа не входит в группу `docker`. Ключ принимается только с адреса экземпляра, `restrict`
запрещает PTY и перенаправления, ключ запускает только `entry.sh`. `entry.sh` принимает только команду
`diagnostics` без аргументов и запускает `dispatcher.py` через `sudo -n`; sudoers разрешает только его и
только без аргументов. Диспетчер читает `stand.json` и `stand.env` своего каталога, исполняет команды без
shell и отвергает операции вне списка.

Закрытие доступа: удалить `/etc/sudoers.d/$ACCOUNT`, пользователя `$ACCOUNT` и каталог `$DIR`.

## OAuth для облачных агентов

Поля подключения своего MCP-сервера в облачном клиенте:

| Поле | Значение |
| --- | --- |
| Name | Имя стенда: `app-production` |
| Server URL | `<public_url>/mcp`: `https://mcp.example.com/mcp` |
| Authentication | OAuth |
| OAuth client | Dynamic client registration, поля клиента пустые |
| Scope | `stand:read` |

Согласие - страница `<public_url>/login`: пароль владельца из `stand.py password`, кнопка
"Разрешить чтение". Адрес возврата клиента - `https`, loopback `http` (`127.0.0.1`, `[::1]`, `localhost`) или
схема из `oauth.redirect_schemes`. Регистрация клиента живет час до согласия и 10 лет после; access token -
час, refresh token - 30 дней; отзыв - `<public_url>/revoke`. Ожидающих регистраций и согласий - не больше
256 каждых; после 20 неверных паролей за 5 минут вход отвечает 429.

## Клиенты MCP

Запись с токеном CI берет его из переменной окружения, запись без токена проходит OAuth при первом
подключении: Claude Code - `/mcp`, Codex - `codex mcp login <имя>`, Cursor - кнопка входа в настройках MCP.

Claude Code, `.mcp.json`:

```json
{
  "mcpServers": {
    "app-production": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${STAND_APP_PRODUCTION_TOKEN}"}
    }
  }
}
```

Codex, `.codex/config.toml`:

```toml
[mcp_servers.app-production]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "STAND_APP_PRODUCTION_TOKEN"
```

Cursor, `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "app-production": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${env:STAND_APP_PRODUCTION_TOKEN}"}
    }
  }
}
```

Без токена запись Claude Code - `type` и `url`, Codex - `url`, Cursor - `url`.

Агент на машине экземпляра подключает сервер через stdio, без OAuth. Нужны пакеты
[requirements.txt](requirements.txt):

```json
{
  "mcpServers": {
    "app-production": {
      "command": "python3",
      "args": [".harness/mcp-stand/server.py", "--config", "/etc/mcp-stand/app-production/stand.json", "--transport", "stdio"]
    }
  }
}
```

## Доступ CI

```bash
../hooks/python.cmd stand.py token          # {"token": ..., "sha256": ...}
```

1. `sha256` добавить в переменную `static_tokens_env` экземпляра через запятую и перезапустить экземпляр.
2. `token` положить в секрет CI, например `STAND_APP_PRODUCTION_TOKEN`.
3. Агент CI получает сервер файлом `--mcp-config` скрипта `.harness/scripts/agent/agent-run.py`:

```json
{"mcpServers": {"app-production": {"url": "https://mcp.example.com/mcp", "bearerTokenEnv": "STAND_APP_PRODUCTION_TOKEN"}}}
```

Токен CI дает scope `stand:read` без срока действия. Отзыв - удалить его `sha256` из переменной и
перезапустить экземпляр.

## Проверка

```bash
STAND_APP_PRODUCTION_TOKEN=<токен> ../hooks/python.cmd stand.py verify https://mcp.example.com \
  --token-env STAND_APP_PRODUCTION_TOKEN --service prometheus
```

`verify` проверяет `/healthz` с `allow_write: false`, метаданные OAuth, ответ 401 без токена, пять
инструментов с `readOnlyHint`, отказ `repository_read` для пути вне снимка и `stand_status` с `host_status`.
С `--service` - запрос `up` к источнику вида `prometheus` и отказ его пути записи. Итог - строка `PASS` и
код 0 или строка `FAIL` и код 1.
