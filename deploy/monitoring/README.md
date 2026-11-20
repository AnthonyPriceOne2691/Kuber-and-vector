# Мониторинг стенда

Prometheus и Grafana на machine-4, node_exporter на всех четырёх машинах. **Черновик на утверждение:** комплект не собран, на стенде ничего не ставилось, все проверки ниже по документации и исходникам.

| Машина | Что | Где слушает |
|---|---|---|
| machine-4 | Prometheus 3.16.0, служба systemd | 127.0.0.1:9090 |
| machine-4 | Grafana 13.3.1 OSS, служба systemd | 127.0.0.1:3000, на Маке 127.0.0.1:13000 через проброс |
| machine-1…3 | node_exporter 1.13.0, служба systemd | адрес lima0, порт 9100 открыт только machine-4 |
| machine-4 | node_exporter 1.13.0 | 127.0.0.1:9100 |

| Файл | Что это |
|---|---|
| `prometheus/prometheus.yml` | задачи скрейпа; цели по машинам `install.sh` пишет в `/etc/prometheus/targets/*.json` |
| `systemd/*.service` | службы с потолком памяти и системой только на чтение |
| `grafana/grafana.ini` | только localhost, обращения наружу выключены, просмотр без входа |
| `grafana/provisioning/` | источник данных Prometheus и папка дашбордов |
| `grafana/dashboards/stand.json` | дашборд для ролика |
| `k8s.yaml` | NodePort и правило сети для метрик Falco |
| `install.sh` | ставит всё из `kit/vm-monitoring` |
| `../vm/kit-fetch-monitoring.sh` | собирает комплект в одноразовой ВМ (`kit-build.sh monitoring`) |

## Где живут Prometheus и Grafana

**Рекомендация: machine-4, бинарники под systemd.** Главная причина: в кластере Prometheus хранит данные на томе local-path, а такой том привязан к машине. Выключили её, и графики стоят до её возвращения, ровно в той сцене, ради которой мониторинг.

| | machine-4, systemd | в кластере, Helm |
|---|---|---|
| Сцена «выключаем машину» | измеритель вне кластера, переживает любую из трёх | теряем графики, если выключили машину Prometheus |
| Память | на machine-4 только HAProxy, генератор и панель | машины кластера держат Qdrant (предел 2 ГиБ), поиск, Falco; запас нужен при переезде реплик |
| Сеть | поды видны через NodePort с `externalTrafficPolicy: Local`, по машине; новые входы только для machine-4 | Prometheus нужен вход в qdrant, search, falco и выход к узлам и к HAProxy, чья статистика сейчас только на localhost machine-4 |
| Комплект | 3 архива с закреплённым sha256 | образы prometheus, grafana, node-exporter и 2 чарта; node-exporter как DaemonSet требует доступа к хосту, второе исключение privileged после Falco |
| Минус | нет обнаружения целей через API Kubernetes: адреса пишет `install.sh` по lima0, как для HAProxy | |

## Комплект на утверждение

Утверждён владельцем 19.11.2026 («да, собирай сейчас, по рекомендациям»). Собран в тот же день (`kit-build.sh monitoring`, около 3 минут): все три sha256 совпали с закреплёнными и со списками выпусков, бинарники arm64 статические, источник Prometheus у Grafana лежит в `plugins-bundled` (prometheus@13.2.5), суммы сверены ещё раз на Маке.

| Файл | Что и зачем | Версия | Источник | Размер | Чем проверяем | Лицензия | Память |
|---|---|---|---|---|---|---|---|
| `prometheus-3.16.0.linux-arm64.tar.gz` | сбор и хранение метрик | 3.16.0 от 04.11.2026, Latest (LTS сейчас 3.13.5 от 21.10.2026) | [github.com/prometheus/prometheus/releases/tag/v3.16.0](https://github.com/prometheus/prometheus/releases/tag/v3.16.0) | 99 МБ, страница выпуска | sha256 закреплён в скрипте и сверяется с `sha256sums.txt` выпуска; выпуск помечен immutable; подписей нет | Apache-2.0 | в документации нет; оценка ниже, потолок 512 МБ |
| `node_exporter-1.13.0.linux-arm64.tar.gz` | процессор, память, нагрузка машин | 1.13.0 от 18.11.2026, Latest | [github.com/prometheus/node_exporter/releases/tag/v1.13.0](https://github.com/prometheus/node_exporter/releases/tag/v1.13.0) | 11,5 МБ, страница выпуска | sha256 закреплён и сверяется с `sha256sums.txt`; совпал с digest на странице выпуска; подписей нет | Apache-2.0 | в документации нет; потолок 128 МБ |
| `grafana_13.3.1_42849527790_linux_arm64.tar.gz` | дашборд | 13.3.1 OSS от 10.11.2026, Latest, закрывает 6 CVE | [grafana.com/grafana/download](https://grafana.com/grafana/download?edition=oss&platform=arm), файл с dl.grafana.com | 452 МБ (451 598 391 байт по заголовку Content-Length) | sha256 со страницы загрузки закреплён и сверяется с файлом `.sha256` рядом с архивом; подписи у архива нет | AGPL-3.0 | документация: рекомендуемый минимум 512 МБ; потолок 768 МБ |

Закреплённые sha256 (сняты 19.11.2026, `kit-fetch-monitoring.sh`):

- `fd8571b8162f4020232d886f17340bcf92e1722985ae3b4c1d096157826c0fbe` prometheus-3.16.0.linux-arm64.tar.gz
- `01f65e7ab3e1f7c04e167fbe097968609eebc9d54cbe523b71d101b830f5d54a` node_exporter-1.13.0.linux-arm64.tar.gz
- `a96fa3dfe94eebb036e53bb4c9c19a9b35bb19fb42f788baf17000f8b6972b33` grafana_13.3.1_42849527790_linux_arm64.tar.gz

Издатели эти архивы не подписывают, поэтому опора на две вещи: сумма закреплена в git до сборки и сверяется со списком сумм выпуска. У выпуска Prometheus есть аттестация GitHub, но её проверяет только `gh`, а его в сборочной ВМ нет. Внутри архивов сборка ничего не запускает: читает список файлов, заголовок ELF (статический ли бинарник, нужен ли musl) и `plugin.json` Grafana. Остановка, если Grafana собрана под musl или в архиве нет источника Prometheus (развилка 2).

Без новых загрузок:

| Что | Откуда |
|---|---|
| экспортёр HAProxy | встроен в деб `haproxy_2.8.30-0ubuntu0.24.04.2` из `kit/vm`: в бинарнике строка «Built with the Prometheus exporter as a service» и опция сборки `USE_PROMEX=1` (деб прочитан как архив, бинарник не запускался) |
| метрики Falco | встроены в Falco 0.45.2, включаются в чарте 9.2.2 (`metrics.enabled`) |
| метрики Qdrant | встроены в Qdrant 1.20.2 (`GET /metrics`) |
| шлюз Qdrant (развилка 1) | образ python:3.13-slim уже лежит в `kit/vm` |

Не берём:

- **kube-state-metrics.** Поды и узлы панель берёт из API Kubernetes, дашборду состояние объектов k8s не нужно.
- **kubelet и cAdvisor.** Процессор и память машин даёт node_exporter, память Qdrant даёт сам Qdrant. Иначе на machine-4 нужен токен кластера (развилка 3).
- **Alertmanager.** Тревог никуда не шлём, ролик смотрит на экран; события Falco панель и так получает.
- **blackbox_exporter.** HAProxy сам проверяет машины раз в секунду и отдаёт состояние серверов.
- **prometheus-operator, kube-prometheus-stack.** Оператор и CRD ради шести задач скрейпа.
- **falcosidekick, falco-exporter.** Метрики встроены в сам Falco.
- **Grafana image renderer.** По документации около 1 ГБ на воркер, картинки не нужны.
- **Loki.** Логи вне объёма.
- **prometheus_client для Python.** Текстовый формат пишется без библиотеки; иначе ещё одно колесо в `kit/vm` (решает автор сервиса).

Память machine-4, **оценка**. Потолки служб 512 + 768 + 128 МБ, вместе около 1,4 ГБ из 4 ГБ, плюс HAProxy, генератор и панель. Для Prometheus документация чисел по памяти не даёт. Серий будет около 1,5 тыс. (оценка по составу целей: node около 80 на машину, HAProxy около 600, поиск около 90 на машину, Qdrant около 25 на пир после фильтра, Falco несколько десятков на машину); при таком объёме память определяет сам процесс, а не серии. Сверяем замером: `process_resident_memory_bytes` в самом Prometheus и `systemctl status` после установки.

Диск, **оценка** по формуле документации Prometheus (1–2 байта на сэмпл): 1,5 тыс. серий раз в 5 с за 7 дней дают 0,2–0,4 ГБ. Потолок `--storage.tsdb.retention.size=2GB`.

## Что снимаем

| Задача | Цель с machine-4 | Что берём |
|---|---|---|
| haproxy | `127.0.0.1:8404/metrics?scope=frontend&scope=server` | запросы и ответы по кодам на фронтенде https; ответы, состояние, повторы по серверам search, то есть по машинам |
| node | lima0:9100, у machine-4 127.0.0.1:9100 | коллекторы cpu, meminfo, loadavg |
| search | lima0:30080/metrics | контракт ниже |
| falco | lima0:30765/metrics | `falcosecurity_falco_rules_matches_total{rule_name,priority,source}`, потери событий ядра |
| qdrant | lima0:30633/metrics, шлюз | `collection_active_replicas_min/max`, `collection_dead_replicas`, `collection_shard_transfer_*`, `cluster_*`, `collection_points`, `memory_resident_bytes` |
| prometheus | 127.0.0.1:9090 | своя память и число серий |

У каждой цели метка `machine`; у HAProxy её получают серверы бэкенда search (они названы именами машин). Скрейп раз в 5 с.

### HAProxy 2.8.30

- Экспортёр встроен в наш деб (см. выше). Включается одной строкой `http-request use-service prometheus-exporter if { path /metrics }` в HTTP-фронтенде (документация 2.8 из того же деба). Ставим её во фронтенд stats на 127.0.0.1:8404, наружу ничего не открывается.
- Метки по исходнику 2.8 (git.haproxy.org, `addons/promex/service-prometheus.c`): `proxy`, `server`, `code` (`1xx`…`5xx`, `other`), `state` у состояния сервера (`UP`, `DOWN`, `MAINT`, `DRAIN`, `NOLB`).
- Перцентилей экспортёр 2.8 не отдаёт: из времён в бинарнике только средние по последним 1024 соединениям и максимумы (`*_time_average_seconds`, `max_*_time_seconds`). Поэтому p95 по машинам берём из гистограммы сервиса, снятой по машинам; запросы в секунду по машинам из `haproxy_server_http_responses_total`.

### Qdrant 1.20.2

- **Нужен ли ключ на `/metrics`.** Да, когда ключи включены: без ключа открыты только `/`, `/healthz`, `/readyz`, `/livez` (`src/actix/mod.rs`, v1.20.2).
- **Подходит ли ключ только на чтение.** Да: обработчик проверяет глобальный доступ без прав управления (`check_global_access(AccessRequirements::new())`, `src/actix/api/service_api.rs`), а ключ на чтение даёт `Access::full_ro` (`src/common/auth/mod.rs`). Чарт qdrant 1.20.2 в своём ServiceMonitor тоже берёт `read-only-api-key`.
- **По пиру.** Каждый пир отдаёт только своё (документация), снимаем каждый.
- **Шарды и реплики** (`src/common/metrics/collections.rs`): `collection_active_replicas_min` и `_max` (минимум и максимум активных реплик по всем шардам всех коллекций), `collection_dead_replicas` (сколько шардов с неактивной репликой), `collection_shard_transfer_incoming/outgoing/failed`. Состояния реплик взяты из консенсуса, поэтому все живые пиры видят одну картину. Карты «шард → машина» в метриках нет, её даёт только `GET /collections/{c}/cluster`, это работа панели.
- Без записей Qdrant помечает реплику Dead не сразу (как и написано в README панели), поэтому на дашборде рядом доступность пира (`up`).

### Falco 0.45.2

- `metrics.enabled: true` в чарте 9.2.2 включает вебсервер с `/metrics` на порту 8765 и раздел `metrics` в falco.yaml (шаблон `falco.metricsConfiguration` в чарте из комплекта). Своему Service чарта предпочитаем NodePort из `k8s.yaml`.
- Тревоги: `falcosecurity_falco_rules_matches_total{rule_name, priority, source}`, priority числом, «4» это Warning (пример на falco.org). Дашборд фильтрует Warning и выше и понимает оба вида меток, числом и словом.
- Счётчик правила появляется после первого срабатывания, поэтому дашборд рисует накопленное число, а не `increase()`: первая тревога видна ступенькой с нуля.

### kubelet и cAdvisor

Не снимаем (развилка 3). Если понадобится память отдельных подов: ClusterRole с `nodes/metrics: get` (так в примере RBAC самого Prometheus, `documentation/examples/rbac-setup.yml`), токен сервисного аккаунта в файле на machine-4, прямой порт 10250 каждой машины, `/metrics/cadvisor` и `/metrics/resource`, проверка TLS по `/var/lib/rancher/k3s/server/tls/server-ca.crt` (сертификат kubelet в k3s подписан k3s-server-ca, docs.k3s.io). `nodes/proxy` не нужен и не даём.

### Сервис поиска: контракт

Дашборд и конфиг построены на этих именах, реализация за автором сервиса.

- `search_requests_total{endpoint="search|answer",code}`, счётчик.
- `search_request_seconds{endpoint}`, гистограмма; корзины 0.025 0.05 0.1 0.25 0.5 1 2.5 5 10 30 60 120.
- `search_stage_seconds{stage="embed|qdrant|generate"}`, гистограмма.
- `search_qdrant_retries_total`, повторы сверх первой попытки.
- `search_answer_total{result="answered|refused|unsupported|error"}`.
- `search_route_total{result="routed|not_in_base|plain"}`: выбор договора (app/routing.py); not_in_base это отказ гейта «этого договора в базе нет», plain это поиск по всей коллекции (вопрос без «;» или нет коллекции документов).
- `GET /metrics` на 8080; через 443 наружу не выходит.

Добавка (развилка 4): метка `pod` со значением `POD_NAME` на всех `search_*`. После переезда пода на машине их два, и NodePort отдаёт `/metrics` то одного, то другого; без метки счётчики двух подов смешаются в одну серию, и `rate()` нарисует ложные всплески. Запросы дашборда суммируют по машине, метка им не мешает.

## Сеть

machine-4 получает только чтение метрик, всё остальное как было:

| Порт | Куда | Кого пускает | Изменение |
|---|---|---|---|
| 30080 | под поиска своей машины, 8080 | machine-4 | ничего: `search-ingress` уже пускает ENTRY_IP, NodePort Local сохраняет адрес |
| 30765 | под Falco своей машины, 8765 | machine-4 | новое, `k8s.yaml`: NodePort Local и NetworkPolicy в falco. Сейчас правил в falco нет совсем, то есть 8765 открыт любому поду |
| 30633 | шлюз Qdrant своей машины | machine-4 | новое, если выбран шлюз (развилка 1) |
| 9100 | node_exporter машины | machine-4 и сама машина | новое: фильтр nftables `inet node_exporter_guard`, ставит сама служба перед стартом; поды и соседние машины отрезаны |
| 8404, 9090, 3000 | HAProxy, Prometheus, Grafana | только 127.0.0.1 machine-4 | строка в `haproxy.cfg.in` |

Исходящие machine-4 к сети машин песочница уже разрешает. Правила сети Qdrant и поиска не меняются: новые правила это отдельные объекты, а NetworkPolicy в Kubernetes складываются.

## Развилки

Решено 19.11 по рекомендациям: шлюз, Grafana 13.3.1, kubelet не снимаем, метка `pod`, просмотр без входа, node_exporter 1.13.0.

1. **Состояние Qdrant для панели и Prometheus.** Рекомендую **шлюз**: он один закрывает и панель, и Prometheus.
   - Шлюз: наш маленький сервис на Python (стандартная библиотека), DaemonSet в qdrant, по поду на машину. Под ходит только в пир своей машины (Service с `internalTrafficPolicy: Local`) с ключом только на чтение и отдаёт наружу только GET: `/metrics` (метрики пира как есть), `/cluster`, `/collections/{c}/cluster`, `/collections/{c}`, `/aliases`. Наружу NodePort 30633 с `externalTrafficPolicy: Local`, правило сети пускает только machine-4. Ключ не выходит из кластера, данные коллекции machine-4 недоступны. Панели меняются только адреса: `--qdrant machine-N=http://<адрес machine-N>:30633` и без `--qdrant-key-file`; выключенная машина пропадает вместе со своим пиром, как сейчас ожидает панель. Образ из `kit/vm`, новых загрузок нет. Память: замера нет, лимит 64 Ми на под.
   - Исключение: NodePort 30633 Local прямо на 6333 пиров и правило «machine-4 → 6333». Кода нет, но ключ на чтение лежит на machine-4 (у Prometheus и панели), и с machine-4 читается вся коллекция (search, scroll).
   - Шлюз написан: `deploy/qdrant/gate.py` (тесты `tests/test_gate.py`), `gate.yaml`, `gate-install.sh`; работает на Python образа поиска `localhost/search:stage2`, новых загрузок нет.
2. **Grafana 13.3.1 или 13.1.10.** Рекомендую **13.3.1** (последняя). С 13.2 источник Prometheus в Grafana это отдельный плагин: он лежит в архиве, но на каждом старте Grafana пытается обновить его из каталога grafana.com (документация источника Prometheus). Это закрыто `preinstall_auto_update = false`, путём к `plugins-bundled` и двумя остановками в сборке. Запасной вариант 13.1.10 (вышла 10.11.2026 вместе с 13.3.1, тоже с исправлениями безопасности): там Prometheus встроен.
3. **kubelet и cAdvisor.** Рекомендую **не снимать**: для дашборда хватает node_exporter, а токен кластера на machine-4 лишний.
4. **Метка `pod` в метриках поиска.** Рекомендую **добавить** (см. контракт).
5. **Просмотр Grafana без входа.** Рекомендую **оставить** на время записи ролика: Grafana видна только через проброс на Мак, как и панель. Выключается строкой `enabled = false` в `[auth.anonymous]`.
6. **node_exporter 1.13.0 вышел 18.11.2026.** Для трёх наших коллекторов в заметках выпуска изменений нет (правка только частоты процессора). Беру 1.13.0; запасной 1.12.1 от 14.07.2026.

## Grafana

- Слушает 127.0.0.1:3000 на machine-4. На Мак: правило проброса Lima в `up.sh` (применится, когда machine-4 создаётся заново) или сразу, без пересоздания: `ssh -F ~/.lima/machine-4/ssh.config -N -L 127.0.0.1:13000:127.0.0.1:3000 lima-machine-4`. Открыть http://127.0.0.1:13000.
- Доступ: просмотр без входа с ролью Viewer; admin с паролем из `/etc/grafana/admin-password` (его создаёт `install.sh`): `limactl shell machine-4 sudo cat /etc/grafana/admin-password`.
- Provisioning: источник Prometheus с uid `prometheus`, дашборд из файла и как домашняя страница; правки в интерфейсе не сохраняются, источник правды в git.
- Наружу Grafana не ходит: выключены статистика, проверка обновлений, новости, gravatar, внешние снимки, обновление плагинов на старте и получение ключа подписей плагинов.
- Дашборд «Stand: Kubernetes + Qdrant», подписи по-английски, как у панели в ролике; цвета машин как у панели:
  - плитки: запросы в секунду на входе, p95, доля 5xx у пользователей, машин в работе, минимум активных реплик Qdrant, тревоги Falco;
  - запросы в секунду и p95 по машинам; переключатель `endpoint` (search или answer);
  - ошибки и повторы: 5xx пользователям, 4xx на входе (сцена «атака снаружи»), повторы HAProxy, повторы сервиса к Qdrant, ошибки ответа;
  - полоса состояний по машинам: машина, сервер HAProxy, пир Qdrant;
  - реплики и переносы шардов Qdrant, p95 по стадиям (embed, qdrant, generate);
  - процессор и память машин;
  - тревоги Falco по правилам и по машинам.

## Команды

```bash
deploy/vm/kit-build.sh monitoring     # только после «да» на список: комплект kit/vm-monitoring
deploy/monitoring/install.sh          # machine-4 и node_exporter на всех машинах
```

Повторный запуск безопасен. В конце `install.sh` печатает цели Prometheus с ✓ и ✗ и подсказку, чего не хватает.

## Что поменять в других файлах

Применены 19.11 после «да» владельца (пункты 1–8; сервис `qdrant-local` уже был в `deploy/qdrant/local-svc.yaml`). Ниже оставлены как справка.

**1. `deploy/observer/haproxy.cfg.in`**, фронтенд stats (потом `deploy/observer/install.sh`):

```
frontend stats
    bind 127.0.0.1:8404
    # метрики для Prometheus на этой же машине (экспортёр встроен в деб 2.8.30)
    http-request use-service prometheus-exporter if { path /metrics }
    stats enable
    stats uri /stats
    stats refresh 2s
    stats show-legends
```

и в шапке: «API Kubernetes, статистика и метрики Prometheus слушают только localhost машины».

**2. `deploy/falco/values.yaml`**, в конец (потом `deploy/falco/install.sh`, затем `deploy/monitoring/install.sh`):

```yaml
# Метрики для Prometheus на machine-4: счётчики сработавших правил и потерь событий ядра.
# Вебсервер Falco отдаёт их на 8765 (/metrics); снаружи NodePort 30765 и правило сети
# из deploy/monitoring/k8s.yaml пускают только machine-4. Свой Service чарта не нужен.
metrics:
  enabled: true
  rulesCountersEnabled: true
  kernelEventCountersEnabled: true
  resourceUtilizationEnabled: false
  stateCountersEnabled: false
  libbpfStatsEnabled: false
  pluginsMetricsEnabled: false
  kernelIterEventCountersEnabled: false
  service:
    create: false
```

**3. `deploy/vm/up.sh`**, создание machine-4:

```bash
    # Вход стенда: 443 на machine-4 виден на Маке как https://127.0.0.1:8443 («интернет»);
    # Grafana: только localhost machine-4, на Маке http://127.0.0.1:13000
    limactl create --tty=false --name "$name" --set ".param.ROLE = \"$role\"" \
      --set '.portForwards = [{"guestPort": 443, "hostPort": 8443}, {"guestIP": "127.0.0.1", "guestPort": 3000, "hostPort": 13000}] + .portForwards' "$TEMPLATE"
```

**4. `deploy/stand-up.sh`**, после шага Falco; в шапку «комплект мониторинга (kit-build.sh monitoring)» в список «Нужно заранее»:

```bash
step "мониторинг на machine-4"
"$HERE/monitoring/install.sh" machine-4
```

**5. `deploy/check.sh`**, раздел перед «песочница машин»:

```bash
echo "мониторинг (machine-4)"
check "Prometheus: все цели отвечают" bash -c "limactl shell machine-4 curl -sf 'http://127.0.0.1:9090/api/v1/query?query=min(up)' | $PY -I -c 'import json,sys; r=json.load(sys.stdin)[\"data\"][\"result\"]; assert r and r[0][\"value\"][1]==\"1\"'"
check "Grafana отвечает на localhost machine-4" bash -c "limactl shell machine-4 curl -sf http://127.0.0.1:3000/api/health"
check "Grafana не видна из сети машин" not limactl shell machine-1 curl -s -m 3 -o /dev/null "http://$ENTRY:3000/"
check "node_exporter machine-1 закрыт для machine-2" not limactl shell machine-2 curl -s -m 3 -o /dev/null "http://$(ip_of machine-1):9100/metrics"
check "метрики через вход не видны (404)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' --cacert '$CA' 'https://$ENTRY/metrics') = 404 ]"
```

**6. `deploy/vm/README.md`**, раздел «Команды»: «`./kit-build.sh monitoring` собирает `kit/vm-monitoring` (Prometheus, node_exporter, Grafana) только после «да» на список в `deploy/monitoring/README.md`».

**7. Сервис поиска** (`app/api.py`): `GET /metrics` по контракту, текстовый формат Prometheus (`text/plain; version=0.0.4`), у гистограмм `_bucket{le}` с `+Inf`, `_sum`, `_count`; метка `pod` (развилка 4); сам `/metrics`, `/readyz`, `/healthz` в `search_requests_total` не считать. Пример строки: `search_requests_total{endpoint="search",code="200",pod="search-6c9f7d-abcde"} 1234`.

**8. Если выбран шлюз Qdrant** (развилка 1): новый файл `deploy/qdrant/gate.yaml`, ENTRY_IP подставляет установка. Правила дополняют `deploy/qdrant/netpol.yaml`, сам он не меняется.

```yaml
# под шлюза на машине N попадает только в пир на машине N
apiVersion: v1
kind: Service
metadata: {name: qdrant-local, namespace: qdrant}
spec:
  internalTrafficPolicy: Local
  selector: {app.kubernetes.io/name: qdrant, app.kubernetes.io/instance: qdrant}
  ports: [{name: http, port: 6333, targetPort: 6333}]
---
apiVersion: v1
kind: Service
metadata: {name: qdrant-gate, namespace: qdrant}
spec:
  type: NodePort
  externalTrafficPolicy: Local
  selector: {app: qdrant-gate}
  ports: [{name: http, port: 9633, targetPort: 9633, nodePort: 30633}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: qdrant-gate, namespace: qdrant}
spec:
  podSelector: {matchLabels: {app: qdrant-gate}}
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [{ipBlock: {cidr: ENTRY_IP/32}}]
      ports: [{port: 9633, protocol: TCP}]
  egress:
    - to: [{podSelector: {matchLabels: {app.kubernetes.io/name: qdrant}}}]
      ports: [{port: 6333, protocol: TCP}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: qdrant-from-gate, namespace: qdrant}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/name: qdrant}}
  policyTypes: [Ingress]
  ingress:
    - from: [{podSelector: {matchLabels: {app: qdrant-gate}}}]
      ports: [{port: 6333, protocol: TCP}]
```

DaemonSet шлюза: метка `app: qdrant-gate` (не метки Qdrant, иначе его выберут Service и правила Qdrant); образ `docker.io/library/python:3.13-slim` с `imagePullPolicy: Never` (архив из `kit/vm` загрузить в containerd всех трёх машин); код из ConfigMap, `python3 -I`; адрес `qdrant-local` подставляется как ClusterIP, как у поиска; из Secret `qdrant-keys` монтируется только `read-only-api-key`; без токена сервисного аккаунта, не root, корень только на чтение, без capabilities; requests 10m и 32Mi, limit 64Mi.

В панели (`panel/README.md`, запуск агрегатора): `--qdrant machine-1=http://<адрес machine-1>:30633` и так для каждой машины, строку `--qdrant-key-file` убрать.

Для варианта «исключение» вместо шлюза: Service NodePort 30633 Local на поды Qdrant с портом 6333, правило «ENTRY_IP → 6333» на поды Qdrant, а задаче qdrant в `prometheus.yml` добавить `authorization: {type: Bearer, credentials_file: /etc/prometheus/qdrant-read-only-key}`.

## Проверено и не проверено

Проверено по источникам 19.11.2026:

- версии, даты, размеры и sha256 трёх архивов по страницам выпусков и загрузки; для Grafana ещё по файлу `.sha256`;
- экспортёр в деб HAProxy из комплекта (строки бинарника), метки по исходнику 2.8;
- Qdrant 1.20.2: ключ на `/metrics`, годность ключа на чтение, метрики шардов и реплик (исходники и документация);
- Falco: включение метрик и имена значений по чарту 9.2.2 из комплекта, имена метрик по falco.org;
- Grafana: минимум памяти, отдельный плагин Prometheus с 13.2 и `preinstall_auto_update`, формат provisioning; файл дашборда читается как модель дашборда целиком (`pkg/services/provisioning/dashboards/types.go`, v13.3.1);
- Prometheus: флаги хранения и байты на сэмпл; пример RBAC с `nodes/metrics`;
- node_exporter: флаги коллекторов и `--web.disable-exporter-metrics` (exporter-toolkit v0.20.0).

Не проверено:

- ничего не запускалось: ни сборка, ни установка, ни дашборд в живой Grafana;
- считает ли Falco `/metrics` на каждый запрос или раз в `metrics.interval` (1 ч): исходник Falco вне списка источников. Косвенно за первое: дашборд из чарта 9.2.2 считает `increase()` по этому счётчику на интервале скрейпа. На стенде проверить первым делом;
- есть ли в архиве Grafana `plugins-bundled` с Prometheus и под какую libc собран бинарник: проверит сборка, иначе остановится;
- что Grafana 13.3.1 без интернета не трогает встроенный плагин при `preinstall_auto_update = false`: по документации так, на деле увидим по проверке источника в конце `install.sh`;
- что правила сети k3s пропускают пробы kubelet к Falco при новом запрете входа: так уже работают пробы поиска, но для Falco не проверено;
- память Prometheus и node_exporter: чисел в документации нет, только замер.

## Ограничения

- Адреса целей пишет `install.sh` по lima0; сменились адреса машин, перезапусти его (так же устроен `deploy/observer/install.sh`).
- Без kubelet нет памяти отдельных подов, только машин и процесса Qdrant.
- Пока открыт проброс, дашборд без входа видит любой, кто сидит за Маком.
- Шлюз Qdrant и метрики поиска написаны 19.11 (`/metrics` в `app/api.py`, `app/metrics.py`), на стенде ещё не проверены.
