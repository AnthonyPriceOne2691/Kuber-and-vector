# Панель и генератор нагрузки

Панель для ролика «Kubernetes + Qdrant»: под ровной нагрузкой выключаем machine-2 и смотрим, как растёт p99,
узел уходит в NotReady, поды переезжают, а пользователь ошибок не видит; машина возвращается — реплики
Qdrant догоняют, на экране время восстановления. Только стандартная библиотека Python (3.12+) и ванильный
JS: ничего не скачивается, шрифтов и CDN нет.

![Панель в mock-режиме](docs/mock.png)

| Файл | Что это |
|---|---|
| `loadgen.py` | генератор нагрузки на asyncio, пишет статистику в JSON-файл |
| `aggregator.py` | HTTP-сервер: статика панели, `GET /api/state`, `POST /api/event`; сборка состояния, события, хронология сбоя |
| `sources.py` | источники, каждый — свой класс: `LoadSource`, `KubeSource`, `QdrantSource`, `ServiceChecks`, `EventsSource` |
| `mockstand.py` | `MockSource`: сценарий «machine-2 выключена» по кругу, без кластера |
| `static/index.html` | панель 1920×1080, CSS и JS внутри, графики на canvas |
| `questions.txt` | вопросы для генератора (по строке, по кругу) |
| `tests/` | тесты на `unittest` и примеры JSON (`tests/fixtures/`) |

## Mock на Маке

```bash
python3 -I -B panel/aggregator.py --mock
open http://127.0.0.1:8090/
```

- `--mock-start 80` — начать с 80-й секунды сценария; история графика, лента и хронология дорисуются.
- `--port 8091` — другой порт. Остановить — Ctrl+C.
- `http://127.0.0.1:8090/?still` — без анимаций (для скриншотов). Окно любого размера: сцена 1920×1080
  масштабируется целиком.
- В mock-режиме на панели всегда плашка **SIMULATED DATA**: числа правдоподобны, но выдуманы.

Сценарий, секунды круга (165 с, потом повтор):

| с | Что происходит |
|---|---|
| 0–20 | норма: 3 узла Ready, 12/12 реплик Active, 25 req/s, p99 ≈ 45 мс |
| 20 | machine-2 выключена: пир Qdrant молчит, первые попытки к ней висят до таймаута 2 с, повтор спасает; p99 ≈ 2 с |
| 23 | реплики на machine-2 — Dead |
| 27–66 | балансировщик убрал machine-2, но часть трафика ещё идёт на её поды: p99 ≈ 0,3 с |
| 30 | у kubelet нет пульса (аренда не продлевается 15 с), Kubernetes всё ещё считает узел Ready |
| 64 | узел NotReady (+44 с) |
| 74–81 | под search выселен, новый Ready на machine-3 (+61 с) |
| 100 | machine-2 включена; 116 — узел Ready (+96 с); 120 — пир Qdrant отвечает |
| 121–135 | 4 шарда догоняют по очереди; все реплики Active (+115 с, восстановление 19 с) |
| 140 | только в mock: под search возвращается на machine-2, чтобы круг начинался так же |

Сценарий предполагает, что поды search выселяются через 10 с после NotReady (`tolerations` для
`node.kubernetes.io/unreachable` и `not-ready` с `tolerationSeconds: 10`). По умолчанию Kubernetes ждёт
300 с — без этой настройки в Deployment сервиса поиска поды в ролике переедут только через 5 минут.

Mock подменяет только транспорт: отдаёт JSON как `kubectl get nodes,pods -A -o json` и как Qdrant
`/cluster`, а синтетические запросы пишет в тот же `Stats`, что и настоящий генератор. Разбор, карта
шардов, события и хронология — тот же код, что с живым кластером.

## Живые источники на machine-4

На стенде панель ставит `deploy/panel/install.sh` (шаг `deploy/stand-up.sh` после мониторинга): служба
systemd `stand-panel` на machine-4, страница на Маке http://127.0.0.1:18090. Что где лежит и почему так,
написано в разделе «На стенде» ниже; здесь параметры генератора и агрегатора.

### 1. Генератор нагрузки

На стенде его запускают сцены: `deploy/scenes/load.sh start [RATE]` идёт во вход с ключом API и проверкой
TLS по УЦ стенда и пишет снимок в `/run/stand-load/load.json`, его и читает панель. Вручную:

```bash
python3 -I -B panel/loadgen.py \
  --url https://127.0.0.1/search --cafile /etc/stand/ca.pem --api-key-file /etc/stand/api-key \
  --rate 25 --concurrency 64 --timeout 2 --retry other \
  --questions panel/questions.txt --stats-file /run/stand-load/load.json
```

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `--url` | — | цель; можно несколько раз: первые попытки идут по кругу, повтор — на следующую |
| `--rate` | 20 | запросов в секунду, ровно (open loop: темп не зависит от ответов) |
| `--concurrency` | 64 | потолок одновременных запросов; сверх него запрос не уходит и считается `skipped` |
| `--timeout` | 2 | таймаут одной попытки, с |
| `--api-key-file` | — | файл с ключом API входа; уходит заголовком `X-API-Key` (без ключа вход отвечает 401) |
| `--retry` | `other` | `none` — без повтора; `other` — один повтор на другую цель (если цель одна — на неё же) |
| `--questions` | `panel/questions.txt` | вопросы по строке; `#` и пустые строки пропускаются; 3–500 знаков |
| `--k` | 5 | сколько результатов просить |
| `--stats-file` | — | куда писать снимок для панели (раз в `--interval` = 0,5 с, атомарно) |
| `--cafile` / `--insecure` | — | свой корневой сертификат / не проверять сертификат (только для стенда) |
| `--arrival` | `uniform` | `poisson` — случайные промежутки с тем же средним темпом |
| `--duration`, `--smooth`, `--no-keepalive`, `--quiet` | | остановка через N с, окно сглаживания ряда, без keep-alive, без сводки в консоль |

Повторяются только «временные» сбои: таймаут, обрыв или отказ соединения, 5xx, битый ответ; 4xx — нет.
Каждый запрос учитывается дважды: `without_retry` — исход первой попытки (что увидел бы пользователь без
повтора), `with_retry` — итог после повтора (что он получил). Задержка — от начала первой попытки до
итогового ответа, вместе с повтором. Поэтому один прогон с `--retry other` даёт обе цифры ролика сразу.

С одной целью (HAProxy) повтор уходит на тот же вход, а обходит мёртвую машину уже HAProxy. Чтобы показать
именно «повтор на другую цель», дайте несколько `--url` — по адресу на машину.

Из ответов сервиса генератор берёт `model` и `collection` (и `timing_ms`): над графиком видно, какая модель
отвечала последние 10 с и сколько заняли векторизация и поиск. При смене модели через алиас коллекции под
нагрузкой там же видна доля старой и новой («bge-m3 62% · e5 38%»).

**Как статистика попадает в панель.** Генератор и агрегатор — отдельные процессы, их можно перезапускать
по одному. Генератор раз в 0,5 с пишет снимок во временный файл рядом и переименовывает его в
`--stats-file` (читатель не увидит половину), агрегатор читает файл раз в 0,5 с. Снимок старше 3 с или с
`"running": false` — «генератор остановлен». На стенде это `/run/stand-load/load.json`: у службы панели
свой `/tmp`, а папку в `/run` (0755, пишет пользователь машины, панель только читает) на каждом старте
создаёт `load.sh`.

### 2. Агрегатор

Так его запускает служба на стенде (`deploy/panel/stand-panel.service`, адреса подставляет установка):

```bash
python3 -I -B /opt/stand-panel/aggregator.py --bind 127.0.0.1 --port 8090 \
  --event-bind <адрес lima0 machine-4> --event-token-file /etc/stand-panel/event-token \
  --load-stats /run/stand-load/load.json \
  --kube-api https://127.0.0.1:6443 --kube-ca /etc/stand-panel/kube-ca.crt \
  --kube-token-file /etc/stand-panel/kube-token \
  --qdrant machine-1=http://<адрес machine-1>:30633 \
  --qdrant machine-2=http://<адрес machine-2>:30633 \
  --qdrant machine-3=http://<адрес machine-3>:30633 \
  --qdrant-collection contracts_rf2 \
  --observer-check 'HAProxy · TLS=127.0.0.1:443'
```

Источник включается, если заданы его параметры; без них блок панели честно пишет «not connected».

| Источник | Параметры | Что нужно от стенда |
|---|---|---|
| load | `--load-stats FILE` | путь из `loadgen.py --stats-file` |
| kube, API напрямую | `--kube-api https://…` (несколько — запасные), `--kube-ca`, `--kube-token-file`, `--kube-timeout` (4 с), `--kube-namespaces` (`search,qdrant`), `--no-kube-leases` | токен учётной записи только на чтение (`get, list, watch` на `nodes`, `pods` во всех ns и `leases` в `kube-node-lease`, `deploy/panel/rbac.yaml`) и CA кластера; только https, проверку сертификата не отключить |
| kube, через kubectl | `--kubectl`, `--kubeconfig`, `--kube-server` (несколько — запасные), `--kube-context`, те же `--kube-timeout`, `--kube-namespaces`, `--no-kube-leases` | kubeconfig с теми же правами; вместе с `--kube-api` не задаётся |
| qdrant | `--qdrant [машина=]URL` (по одному на пир), `--qdrant-key-file`, `--qdrant-collection` (коллекция или алиас), `--qdrant-peer peer_id=машина`, `--qdrant-timeout` (1,5 с), `--qdrant-cafile`, `--qdrant-insecure` | на стенде адрес шлюза каждой машины (`:30633`, `deploy/qdrant/gate.py`): ключ Qdrant шлюз добавляет сам, на machine-4 ключа нет, поэтому `--qdrant-key-file` не нужен |
| observer | `--observer-check ИМЯ=хост:порт` | TCP-проверка служб machine-4 (HAProxy) |
| events | `--event-token-file FILE`, `--event-bind АДРЕС`, `--event-port` (как `--port`) | токен для внешних событий; без него `POST /api/event` — только с localhost. `--event-bind` открывает второй адрес только для `POST /api/event` и только с токеном: страница, `/api/state` и `/healthz` там 404 |

Оформление: `--machines` (порядок карточек, имена = имена узлов k8s), `--observer` (`machine-4`),
`--title`, `--subtitle`, `--caption`, `--spike-ms` (порог события «всплеск p99», 250 мс),
`--lease-stale` (пульс старше 15 с — «нет пульса»), `--verbose` (журнал HTTP).

Как устроены живые источники:

- **kube.** Раз в 2 с три списка: узлы, поды всех пространств и аренды kubelet в `kube-node-lease` (пульс
  kubelet: по нему «нет пульса» видно раньше NotReady). С `--kube-api` это три `GET` (`/api/v1/nodes`,
  `/api/v1/pods`, `…/namespaces/kube-node-lease/leases`) по HTTPS: сертификат сервера сверяется с CA кластера
  (системные УЦ не в счёт) и с адресом из URL, токен из файла уходит только заголовком `Authorization` и
  перечитывается при замене файла, в журналы и состояние не попадает, переадресации не выполняются. Через
  kubectl это `kubectl get nodes,pods -A -o json` и `kubectl get leases -n kube-node-lease -o json` с
  `--request-timeout` и жёстким таймаутом процесса. Зависший API не держит панель: показываются последние
  удачные данные с пометкой «data N s old». Если текущий сервер не ответил, берётся следующий и остаётся
  текущим.
- **qdrant.** Каждый пир — `GET /cluster` и `GET /collections/{c}/cluster` (заголовок `api-key`, ключ
  читается из файла и перечитывается при замене файла; в журналы и в состояние не попадает), пиры
  опрашиваются параллельно. Алиас коллекции разрешается через `GET /aliases`, на панели видно «alias →
  collection» (сцена со сменой модели). Номер пира → машина: `--qdrant-peer` > под Kubernetes по адресу
  пира (`qdrant-1.qdrant-headless` → под `qdrant-1` → его узел; или IP пода) > машина у `--qdrant`.
  Адреса нужны **по пиру** (Service на под через селектор `statefulset.kubernetes.io/pod-name`, или NodePort
  с `externalTrafficPolicy: Local`): через общий балансирующий Service доступность отдельного пира видна
  только по `message_send_failures` консенсуса.
- **events.** Переходы выводит сам агрегатор (узлы, поды, реплики, нагрузка, здоровье источников).
  Внешние события — `POST /api/event`, например из Falco (на стенде адрес лежит в Secret, см. ниже):

  ```yaml
  # falco.yaml
  json_output: true
  http_output:
    enabled: true
    url: "http://<адрес lima0 machine-4>:8090/api/event?token=<токен из --event-token-file>"
  ```

  В журнале HTTP (`--verbose`) токен из `?token=` заменяется на `…`. Отказ (403, 404) дочитывает небольшое
  тело, поэтому соединение keep-alive остаётся годным; большое тело не читается, соединение закрывается.

## На стенде (deploy/panel)

`deploy/panel/install.sh [machine-4]`, повторный запуск безопасен. Переменные: `NODES` (машины кластера),
`KC=machine-1` (где запускать kubectl), `TOKEN_TTL=24h`, `COLLECTION=contracts_rf2`.

| Что | Где |
|---|---|
| код панели (`aggregator.py`, `sources.py`, `static/`), владелец root | `/opt/stand-panel` на machine-4 |
| служба `stand-panel` под системным пользователем `stand-panel` без входа и без дома | `/etc/systemd/system/stand-panel.service` |
| страница и `/api/state` | `127.0.0.1:8090` на machine-4; на Маке http://127.0.0.1:18090 (проброс Lima) |
| только `POST /api/event` с токеном | адрес lima0 machine-4, порт 8090; файрвол machine-4 пускает туда только машины кластера |
| CA кластера (`server-ca.crt` k3s), 0644 | `/etc/stand-panel/kube-ca.crt` (папка 0750 root:stand-panel) |
| токен Kubernetes только на чтение, 0600 у `stand-panel` | `/etc/stand-panel/kube-token` |
| токен событий, 0600 у `stand-panel` | `/etc/stand-panel/event-token`; он же в адресе в Secret `falco/falco-panel-output` |
| учётная запись и права | `stand-panel/panel-reader`, `deploy/panel/rbac.yaml` |

**Kubernetes только на чтение.** Учётная запись `panel-reader` в своём пространстве `stand-panel`: `get, list,
watch` на `nodes` и `pods` (все пространства: на карточке машины число системных подов, в состоянии поды без
узла) и на `leases` только в `kube-node-lease`. Без secrets, configmaps, `pods/exec`, `pods/log`,
`nodes/proxy` и записи. Панель делает только `list`; `get` и `watch` дают те же объекты. Токен выпускает
TokenRequest (`kubectl create token --duration`, по умолчанию на 24 ч): в Secret он не хранится и идёт
трубой с machine-1 на machine-4, в аргументы команд и на диск Мака не попадает. API идёт через вход HAProxy
`127.0.0.1:6443` (frontend `kube-api` ведёт на три сервера), сертификат проверяется по CA кластера.
`127.0.0.1` есть в SAN сертификата k3s по умолчанию: на него смотрит собственный `k3s.yaml` с проверкой по
тому же CA. `check.sh` сверяет это на каждом из трёх серверов.

Новый токен — повторный запуск установки: учётная запись пересоздаётся с новым UID, поэтому прежний токен
перестаёт действовать сразу, а не по сроку. Истёк срок — на панели «Kubernetes API: HTTP 401: token
rejected», а `check.sh` печатает, сколько часов осталось. Сменить токен событий: удалить
`/etc/stand-panel/event-token` на machine-4 и повторить установку (она же обновит Secret и перезапустит
Falco). Адреса машин (lima0) вписываются в юнит при установке; сменились — повторить установку, как для
входа и мониторинга.

**Почему без kubectl.** Бинарник k3s из комплекта работает как kubectl через ссылку с этим именем, но
сначала распаковывает свой комплект в каталог данных (строки самого бинарника: «Preparing data dir»,
«extracting data», `${HOME}/.rancher/`). У системного пользователя под `ProtectSystem=strict` и без дома это
не выйдет без распаковки от root в `/var/lib/rancher/k3s`, то есть на смотрящей наружу машине лёг бы рантайм
k3s. Ещё это два процесса на 74 МБ раз в 2 с. Свой клиент на стандартной библиотеке делает ровно три GET,
токен не появляется в аргументах процессов, проверку TLS не отключить, и его проверяют тесты с настоящим
HTTPS. Путь через kubectl в панели остался (`--kubectl`, `--kubeconfig`).

**Тревоги Falco.** Токен событий генерируется на machine-4 при первой установке и дальше не меняется. Адрес
с токеном установка кладёт в Secret `falco/falco-panel-output` (кусок `falco.yaml` с `http_output`).
`deploy/falco/values.yaml` монтирует его папкой `/etc/falco/panel` (`optional`: без панели папка пустая), и
Falco дочитывает её после основного конфига (`config_files`). Адрес поменялся — установка перезапускает
Falco. Компромисс: Secret, а не ConfigMap чарта. В ConfigMap (через values) токен лежал бы в etcd открытым
текстом и в истории релиза Helm, его видел бы любой с правом читать configmaps в `falco`, а установка Falco
зависела бы от панели. Secret в etcd шифруется (`secrets-encryption` из `deploy/k3s/cis.sh`), Falco ставится
как раньше; цена —
лишний том в values и перезапуск Falco при смене адреса. Сам запрос идёт по сети машин открытым HTTP:
увидевший токен может подделать событие в ленте, но только с адреса машины кластера (файрвол machine-4 и
`IPAddressAllow` службы).

**Служба.** `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, без
capabilities, `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute`, сокеты только `AF_INET` и
`AF_UNIX`. `IPAddressDeny=any` и `IPAddressAllow` с localhost и тремя машинами кластера: Мак и остальная сеть
машин отрезаны ещё до файрвола. Потолок памяти 256 МБ (замера нет). Адрес lima0 после загрузки появляется
не сразу, поэтому служба повторяет запуск через 5 с.

**Страница на Маке.** Правило проброса Lima `127.0.0.1:8090 → 127.0.0.1:18090` ставит `deploy/vm/up.sh` при
создании machine-4. Для уже созданной машины: `ssh -F ~/.lima/machine-4/ssh.config -N -L
127.0.0.1:18090:127.0.0.1:8090 lima-machine-4`. Адрес событий на lima0 правило не берёт.

Журнал: `limactl shell machine-4 journalctl -u stand-panel -f`. Проверка: раздел «панель» в
`deploy/check.sh`; `CHECK_FALCO=1 deploy/check.sh` ещё запускает оболочку в поде поиска и ждёт тревогу
Falco в ленте.

## Контракт `GET /api/state`

Один JSON на всю панель, собирается раз в 0,5 с, панель берёт его раз в секунду. Время — unix-секунды
(float), длительности — секунды, задержки — миллисекунды. Номера пиров Qdrant — **строки** (u64 не
помещается в число JavaScript). Поле, для которого нет источника, — `null`. Тест `tests/test_server.py`
сверяет набор ключей с этим описанием.

```
{
  "schema": "panel/1",
  "ts": 1763460391.5,             время сборки на сервере
  "mode": "live" | "mock",
  "ui": {"title", "subtitle", "caption"},
  "sources": {                    только подключённые источники
    "load"|"kube"|"qdrant"|"observer": {"ok": bool, "age_s": float|null, "error": str|null, "stale"?: bool}
  },
  "load": {...} | null,
  "machines": [...],
  "kube": {...} | null,
  "qdrant": {...} | null,
  "incident": {...} | null,
  "events": [...]
}
```

**`load`** — снимок генератора (формат `loadgen/1`, тот же пишет `loadgen.py`) плюс `age_s`, `stale`:

| Поле | Тип | Смысл |
|---|---|---|
| `schema`, `ts`, `started_at` | str, float, float | версия формата, время снимка, первый запрос |
| `source`, `running`, `inflight` | str, bool, int | `loadgen` или `mock`; идёт ли; запросов в полёте |
| `config` | obj | `targets[]`, `rate`, `concurrency`, `timeout_s`, `retry` (`none`/`other`), `k`, `arrival` |
| `windows` | obj | `"1s"`, `"10s"`, `"60s"` — окна по завершённым секундам (текущая неполная не входит) |
| `windows.*` | obj | `seconds`, `count`, `qps`, `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `without_retry`, `with_retry`, `retries`, `skipped` |
| `…without_retry`, `…with_retry` | obj | `errors` (int), `rate` (доля), `by_kind`: `timeout`, `conn`, `5xx`, `4xx`, `bad` |
| `totals` | obj | с начала работы: `count`, `without_retry`, `with_retry`, `retries`, `skipped` |
| `series` | list | 180 точек, по секунде: `t`, `qps`, `p50`, `p95`, `p99` (по последним 5 с), `errors_without_retry`, `errors_with_retry`, `retries` (этой секунды) |
| `serving` | list | за 10 с: `model`, `collection`, `count`, `share` — какие модель и коллекция отвечали |
| `server_timing_ms` | obj\|null | за 10 с, среднее из `timing_ms` ответов: `embed`, `search` |
| `age_s`, `stale` | float, bool | возраст снимка; старше 3 с |

Перцентили — по ближайшему рангу (наблюдённое значение). `qps` — завершённые запросы в секунду.

**`machines[]`** — карточки в порядке `--machines`, затем узлы, которых нет в списке:

| Поле | Тип | Смысл |
|---|---|---|
| `name`, `color` | str | имя (= узел k8s), цвет машины (тот же на карте Qdrant, в ленте, на графике) |
| `role` | str | `server` (узел k3s), `observer`, `machine` (узла с таким именем нет) |
| `status` | str | `ready`, `unreachable` (Ready, но пульса kubelet нет дольше `--lease-stale`), `notready`, `unknown`, `observer` |
| `status_since` | float\|null | с какого времени статус (переход Ready или последний пульс) |
| `heartbeat_at`, `heartbeat_age_s` | float\|null | последнее продление аренды kubelet и его возраст |
| `ip` | str\|null | InternalIP узла |
| `kube` | obj\|null | `ready` (`True`/`False`/`Unknown`), `reason`, `message`, `roles[]`, `kubelet`, `unschedulable`, `taints[]` |
| `pods[]` | list | поды приложения (`--kube-namespaces`) на узле: `namespace`, `name`, `short`, `app`, `status` (как STATUS у kubectl), `state` (`ready`, `starting`, `notready`, `terminating`, `failed`), `ready`, `restarts`, `owner_kind`, `age_s` |
| `system_pods` | int | прочие поды на узле (без завершённых Job) |
| `qdrant` | obj\|null | пир на машине: `peer`, `pod`, `reachable`, `leader`, `replicas` (число по состояниям) |
| `services` | list\|null | только у наблюдателя: `name`, `ok` (true/false/null), `detail` |

**`kube`**: `ok`, `age_s`, `error`, `server` (какой API ответил), `heartbeats` (читаются ли аренды),
`nodes[]` (`name`, `ready`, `since`, `reason`, `unschedulable`, `taints`), `unscheduled[]` (поды без узла,
поля как в `pods[]`).

**`qdrant`**:

| Поле | Тип | Смысл |
|---|---|---|
| `ok` | bool | ответил хотя бы один пир |
| `collection`, `alias` | str, str\|null | настоящая коллекция и алиас, если задан алиасом |
| `status`, `points` | str\|null, int\|null | из `GET /collections/{c}`: green/yellow/red, число точек |
| `shard_count`, `replication_factor` | int | |
| `peers[]` | list | `id` (строка), `machine`, `pod`, `uri`, `reachable`, `leader` |
| `shards[]` | list | `id`, `replicas[]`: `peer`, `machine`, `state` (как у Qdrant: Active, Dead, Partial…), `display`, `progress` (0..1 у переноса), `from_machine` |
| `transfers[]` | list | `shard`, `from`, `to`, `from_machine`, `to_machine`, `method`, `sync`, `progress`, `comment` |
| `summary` | obj | число реплик по `display` + `total`, `healthy` (все Active, переносов нет, все пиры отвечают) |
| `raft` | obj | `leader`, `leader_machine`, `term`, `commit` |

`display` — состояние клетки на карте: `active`; `recovering` (Partial с идущим переносом, Recovery,
PartialSnapshot); `partial` (Partial без переноса, Initializing, Resharding); `dead`; `offline` — пир не
отвечает, а консенсус ещё числит реплику живой (без записей Qdrant помечает реплику Dead не сразу);
`listener`; `unknown`.

**`incident`** — последний сбой машины (остаётся на экране до следующего), `null` — сбоев не было:

| Поле | Тип | Смысл |
|---|---|---|
| `active`, `machine`, `color`, `phase` | bool, str, str, str | `phase` — последний пройденный этап |
| `started` | float | момент потери машины (оценка, см. ограничения) |
| `steps[]` | list | `key` (`lost`, `notready`, `moved`, `back`, `recovered`), `label`, `ts`, `done`, `offset_s` (от `started`); `moved` нет, если на машине не было подов Deployment |
| `elapsed_s` | float | от потери до сейчас (или до восстановления) |
| `downtime_s`, `recovery_s`, `total_s` | float\|null | потеря → возвращение; возвращение → все реплики Active (**время восстановления**); потеря → восстановление |
| `apps` | obj | приложения Deployment с машины и сколько их подов должно быть Ready в другом месте |
| `load` | obj | за время сбоя: `requests`, `without_retry`, `with_retry`, `p99_max` |

**`events[]`** — до 50 последних, новые первыми: `id`, `ts`, `kind` (`node`, `pod`, `replica`, `load`,
`source`, `security`, `scenario`, `info`), `level` (`info`, `ok`, `warn`, `error`), `title`, `detail`,
`machine`, `pod`, `source`, `count` (одинаковые события за 10 с склеиваются), `last_ts` (если склеивались).

### `POST /api/event`

Тело — JSON-объект или массив (до 50) не больше 64 КиБ. Ответ `201 {"ok": true, "ids": [...]}`; `400` —
не JSON или нет заголовка, `403` — нет токена (или не localhost), `413` — большое тело, `429` — больше
20 событий в секунду.

- Общий формат: `{"title": "Pod quarantined", "level": "ok", "kind": "security", "detail": "...",
  "machine": "machine-1", "pod": "search-…", "ts": 1763460391.5}` (обязателен только `title`).
- Формат Falco `http_output` узнаётся сам: `rule` → заголовок, `output` → подробности, `priority` →
  уровень, `hostname` → машина, `output_fields["k8s.pod.name"]` → под, `time` → время.
- Токен: заголовок `X-Panel-Token`, `Authorization: Bearer …` или `?token=` (для Falco).

Текст событий на панели выводится только как текст (`textContent`), страница отдаётся с CSP по хэшам
встроенных скрипта и стилей: чужая разметка из события не исполнится.

## Тесты

```bash
python3 -I -B -m unittest discover -s panel/tests -t panel
```

125 тестов, 4–6 с (замер 19.11 на Python 3.14): перцентили и окна, учёт ошибок с повтором и без,
генератор с подменённым транспортом и с настоящим HTTP-сервером (повтор, keep-alive, «протухшее»
соединение), разбор kubectl и аренд, API Kubernetes без kubectl по настоящему HTTPS (свой УЦ и сертификат на
127.0.0.1 выпускает `openssl` во временной папке: чужой УЦ и чужое имя отклоняются до отправки токена,
новый файл токена, 401, 403 на аренды, переадресация не выполняется, запасной сервер; нет `openssl` — эти
тесты пропускаются), разбор Qdrant `/cluster` и `/collections/{c}/cluster` (карта, доступность пиров,
переносы, алиас, ключ только в заголовке), лента и формат Falco, хронология сбоя, mock-сценарий целиком на
два круга, HTTP-сервер и контракт `/api/state`, второй адрес только для событий, токен в журнале скрыт,
keep-alive после отказа.

## Ограничения

- Числа mock выдуманы под это железо; живые цифры дают только живые источники.
- Момент потери машины — оценка: пир Qdrant на ней перестал отвечать (±1 с) → иначе последний пульс
  kubelet + 5 с (±5 с) → иначе переход NotReady. Без Qdrant и без прав на `leases` отсчёт пойдёт от NotReady.
- Хронология ведёт одну машину за раз; второй сбой во время первого виден в ленте, но не в хронологии.
- Генератор: перцентили точные до 2000 запросов в секунду (дальше — выборка-резервуар); окна — по
  времени завершения запроса.
- Через kubectl агрегатор запускает два процесса раз в 2 с, на слабой ВМ это заметно по процессору; с
  `--kube-api` (так на стенде) процессов нет, три запроса HTTPS раз в 2 с.
- Запросы панели к API Kubernetes попадают в журнал аудита k3s (уровень Metadata, `deploy/k3s/cis.sh`):
  три записи раз в 2 с от `stand-panel/panel-reader`; журнал ротируется по 50 МБ.
- Страница без авторизации на чтение: держите `--bind 127.0.0.1` и смотрите через проброс порта.
  В состоянии нет ключей и токенов.
- Токен Kubernetes живёт `TOKEN_TTL` (24 ч): стенд, поднятый вчера, показывает «HTTP 401: token rejected»,
  пока не повторить `deploy/panel/install.sh`.
- Нужен современный браузер (Chrome/Safari 2023+: `color-mix`, `AbortSignal.timeout`).
