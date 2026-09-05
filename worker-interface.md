# Интерфейс вычислительного воркера jobs

**Протокол:** `worker_api/v1`
**Backend:** Metology
**Первый тип:** `photo_3D_fl`

Этот документ является контрактом между Metology backend и отдельным проектом
вычислительного воркера. Воркер не вызывает HTTP API backend. Он получает задания через
PostgreSQL и читает/пишет файлы напрямую в закрытый S3-совместимый bucket.

## 1. Основные правила

1. Таблицы PostgreSQL являются долговечной очередью и источником истины.
2. `LISTEN/NOTIFY` — только сигнал пробуждения; пропуск уведомления не должен терять job.
3. Воркер не читает и не изменяет таблицы напрямую, а вызывает функции схемы
   `worker_api`.
4. Любое изменение running job требует актуальные `attempt_id` и `lease_token`.
5. Воркер поддерживает только явно перечисленные типы jobs.
6. Воркер не отправляет процент выполнения. Он сообщает только реальный `stage`.
7. Подписанные клиентские S3 URL, installation credentials и access tokens воркеру не
   передаются.

## 2. Совместимость

Имя схемы и функций содержит версию контракта. Текущая версия равна `1`.

```sql
SELECT worker_api.protocol_version_v1();
-- 1
```

Существующие функции v1 не меняются несовместимым образом. Новые nullable поля могут
добавляться в конец результата. Изменение смысла, типа или обязательности поля требует
новых функций `*_v2` и отдельного migration guide.

Воркер при старте проверяет protocol version и завершает запуск с явной ошибкой, если
версия не поддерживается. Он не должен пытаться угадать новый контракт.

## 3. Подключения

Воркеру нужны два независимых подключения PostgreSQL:

- постоянное autocommit-подключение только для `LISTEN metology_jobs`;
- обычное короткоживущее или pooled-подключение для вызова функций `worker_api`.

LISTEN-подключение нельзя оставлять внутри открытой транзакции: уведомления доставляются
между транзакциями. После подключения порядок всегда такой:

1. выполнить `LISTEN metology_jobs` и дождаться завершения команды;
2. вызвать claim до пустого результата, чтобы подобрать уже существующие jobs;
3. ждать уведомление не более 30 секунд;
4. по уведомлению или timeout снова вызывать claim до пустого результата;
5. при reconnect повторить весь порядок.

Все соединения используют TLS вне локальной разработки. PostgreSQL-role выдаются только:

- `CONNECT` к нужной базе;
- `USAGE` на schema `worker_api`;
- `EXECUTE` на функции v1;
- право выполнить `LISTEN` на согласованном канале.

Прямых прав на таблицы Django роль не получает.

## 4. Идентификатор экземпляра

Каждый запуск процесса создаёт `worker_id` UUID. Он стабилен до завершения процесса и
меняется после рестарта. Один процесс с несколькими слотами может использовать общий
`worker_id`; `attempt_id` однозначно различает назначения.

Не используйте hostname, IP или username как единственный идентификатор: они могут
повторяться и раскрывать инфраструктуру в диагностике.

## 5. Канал NOTIFY

Канал:

```sql
LISTEN metology_jobs;
```

Payload — небольшой JSON без идентификаторов пользователя и S3 keys:

```json
{"v": 1, "type": "photo_3D_fl"}
```

Воркер может использовать `type`, чтобы не будить неподдерживающий pool, но не должен
считать payload заданием. Несколько одинаковых сигналов могут объединиться, а все
слушатели могут получить один сигнал. После любого сигнала воркер вызывает claim.

## 6. Общий жизненный цикл попытки

```text
claim -> starting -> downloading -> processing
      -> exporting_ply -> uploading_result -> complete
```

Альтернативные окончания:

```text
heartbeat says cancel -> stop safely -> acknowledge_cancellation
recoverable error     -> fail(retryable=true)
permanent error       -> fail(retryable=false)
lost lease            -> stop locally; never complete or fail
```

Heartbeat отправляется не реже одного раза в 30 секунд, включая длительные вызовы
вычислительной библиотеки. Серверный lease действует 120 секунд. Если библиотека не
позволяет вернуть управление за это время, heartbeat должен работать в отдельном
контролируемом потоке процесса.

## 7. `claim_job_v1`

Сигнатура:

```sql
SELECT *
FROM worker_api.claim_job_v1(
    p_worker_id       => '019...'::uuid,
    p_supported_types => ARRAY['photo_3D_fl']::text[]
);
```

Функция возвращает либо ноль строк, либо одну строку:

| Поле | PostgreSQL type | Описание |
|---|---|---|
| `protocol_version` | integer | Всегда `1` |
| `job_id` | uuid | UUIDv7 job |
| `job_type` | text | Регистр значим |
| `parameters` | jsonb | Валидированные параметры; для первого типа `{}` |
| `attempt_id` | uuid | UUIDv7 новой попытки |
| `attempt_number` | integer | От 1 до `max_attempts` |
| `lease_token` | uuid | Случайный fencing token |
| `lease_expires_at` | timestamptz | UTC timestamp |
| `input_bucket` | text | Закрытый bucket |
| `input_object_key` | text | Ключ входного объекта |
| `input_content_type` | text | Проверенный MIME |
| `input_size_bytes` | bigint | Проверенный размер, `1..52428800` по default |
| `result_bucket` | text | Обычно тот же bucket |
| `result_object_key` | text | Уникальный `.ply` key этой попытки |

Функция атомарно:

1. находит доступную queued job поддерживаемого типа;
2. использует row lock с `SKIP LOCKED`;
3. закрывает просроченную попытку, если она существует;
4. проверяет лимит попыток;
5. создаёт новую попытку со случайным lease token;
6. создаёт уникальный result key с `attempt_id` в пути;
7. переводит job в `running`, устанавливает `stage='starting'`;
8. возвращает снимок задания.

Воркер не конструирует result key самостоятельно и не подменяет его при завершении.
Каждая попытка имеет отдельный key, поэтому очистка объекта потерявшей lease попытки не
может удалить PLY, созданный новой попыткой.

## 8. Контракт `photo_3D_fl`

Вход:

- ровно один объект;
- MIME `image/jpeg`, `image/png`, `image/heic` или `image/heif`;
- максимальный размер в claim уже учитывает текущую конфигурацию, default 50 MiB;
- object key оканчивается логическим input-объектом, но расширение имени не является
  источником MIME.

Результат:

- ровно один бинарный PLY-файл;
- object key из `result_object_key`, содержащий `attempt_id` и окончание `result.ply`;
- content type `application/octet-stream`;
- размер больше нуля;
- SHA-256 передаётся lowercase hex из 64 символов.

Формат выдаваемого backend ключа:

```text
jobs/v1/{installation_id}/{job_id}/result/{attempt_id}/result.ply
```

Разрешённые stages:

```text
starting
downloading
processing
exporting_ply
uploading_result
```

Нельзя отправлять произвольные тексты, проценты, имена пациентов, исходные имена файлов
или сообщения вычислительной библиотеки в `stage`.

## 9. Чтение input из S3

Endpoint, region, addressing style и credentials приходят из защищённой конфигурации
проекта воркера. Claim возвращает только bucket и object key.

Порядок:

1. heartbeat со stage `downloading`;
2. `HEAD` input и сверка размера с claim;
3. потоковое скачивание в отдельный временный файл;
4. ограничение локального размера тем же `input_size_bytes`;
5. закрытие файла перед передачей вычислительной библиотеке;
6. удаление локального input в `finally` при любом исходе.

S3 temporary redirect и retry обрабатываются SDK. Нельзя логировать signed request,
credentials или содержимое изображения.

## 10. `heartbeat_job_v1`

Сигнатура:

```sql
SELECT *
FROM worker_api.heartbeat_job_v1(
    p_job_id     => '019...'::uuid,
    p_attempt_id => '019...'::uuid,
    p_lease_token => '...'::uuid,
    p_stage      => 'processing'
);
```

Одна возвращаемая строка:

| Поле | Type | Описание |
|---|---|---|
| `accepted` | boolean | Lease всё ещё принадлежит попытке |
| `cancel_requested` | boolean | Клиент или администратор запросил отмену |
| `lease_expires_at` | timestamptz | Новый срок lease, если accepted |
| `job_status` | text | Текущее состояние для диагностики |

Если `accepted=false`, воркер немедленно прекращает обработку, удаляет локальные и
частично загруженные result-файлы и не вызывает complete/fail для этой попытки.

Невалидный stage является ошибкой контракта и не продлевает lease. Повтор heartbeat с
тем же stage разрешён.

## 11. Успешное завершение

Перед complete воркер:

1. формирует PLY во временный локальный файл;
2. проверяет, что размер больше нуля;
3. вычисляет SHA-256;
4. делает heartbeat `uploading_result`;
5. загружает файл строго в `result_bucket/result_object_key`;
6. выполняет S3 HEAD и сверяет размер;
7. вызывает complete;
8. удаляет локальный result в `finally`.

Сигнатура:

```sql
SELECT *
FROM worker_api.complete_job_v1(
    p_job_id           => '019...'::uuid,
    p_attempt_id       => '019...'::uuid,
    p_lease_token      => '...'::uuid,
    p_result_size_bytes => 123456::bigint,
    p_result_sha256    => '0123456789abcdef...'
);
```

Результат:

| Поле | Type | Описание |
|---|---|---|
| `accepted` | boolean | Job успешно завершена этой попыткой |
| `job_status` | text | `succeeded` при успехе |
| `result_available_until` | timestamptz | `finished_at + 2 часа` по default |

Complete принимается только при актуальном lease и status `running`. Если между
последним heartbeat и complete пришла отмена, функция возвращает `accepted=false`.
Воркер удаляет уже загруженный result object и подтверждает отмену, если lease ещё его.

Повтор complete с теми же идентификаторами после успешного ответа идемпотентно возвращает
тот же итог. Complete другого attempt никогда не принимается.

## 12. Ошибка и retry

Сигнатура:

```sql
SELECT *
FROM worker_api.fail_job_v1(
    p_job_id       => '019...'::uuid,
    p_attempt_id   => '019...'::uuid,
    p_lease_token  => '...'::uuid,
    p_error_code   => 'model_temporarily_unavailable',
    p_error_summary => 'bounded technical summary',
    p_retryable    => true
);
```

Результат:

| Поле | Type | Описание |
|---|---|---|
| `accepted` | boolean | Ошибка принята для текущей попытки |
| `job_status` | text | `queued` или `failed` |
| `retry_at` | timestamptz nullable | Следующее доступное время |
| `attempts_remaining` | integer | Оставшийся лимит |

`error_code` — lowercase machine code до 64 символов. `error_summary` — очищенное
техническое описание до 1000 символов без stack trace, путей пользователя, имён и
содержимого файлов.

При `retryable=true` и наличии попыток job возвращается в `queued`: после первой ошибки
на 30 секунд, после второй на 120 секунд. В остальных случаях job становится `failed`.
Третья неуспешная попытка всегда окончательная.

## 13. Отмена

Heartbeat с `cancel_requested=true` требует cooperative cancellation. Воркер:

1. прекращает запуск новых этапов;
2. просит вычислительную библиотеку остановиться;
3. удаляет локальные temporary files;
4. удаляет частично или полностью загруженный result object;
5. вызывает функцию подтверждения.

```sql
SELECT *
FROM worker_api.acknowledge_cancellation_v1(
    p_job_id      => '019...'::uuid,
    p_attempt_id  => '019...'::uuid,
    p_lease_token => '...'::uuid
);
```

Результат содержит `accepted boolean` и `job_status text`. При успехе status равен
`canceled`. Функция идемпотентна для той же попытки.

Если библиотеку нельзя быстро остановить, heartbeat продолжает продлевать lease с тем
же stage, пока контролирующий поток ждёт безопасную точку. Результат отменённой job не
публикуется.

## 14. Потерянный lease и fencing

Lease считается потерянным при любом из условий:

- heartbeat вернул `accepted=false`;
- PostgreSQL connection недоступен дольше оставшегося lease;
- локальное время прошло `lease_expires_at`, а продление не подтверждено;
- mutating function вернула отсутствие актуальной попытки.

После потери lease воркер не пытается «дозавершить» job. Он очищает локальные файлы и
удаляет result object, если успел его загрузить. Новый claim создаёт новый attempt и
lease token, поэтому запоздавшая запись старого процесса отклоняется.

`lease_token` нельзя переиспользовать между попытками или сохранять для следующего
запуска процесса.

## 15. Ошибки инфраструктуры

| Ситуация | Действие воркера |
|---|---|
| PostgreSQL временно недоступен до потери lease | Повторить с bounded backoff |
| PostgreSQL недоступен дольше lease | Считать lease потерянным, остановить job |
| S3 GET временно недоступен | Несколько коротких SDK retry, затем retryable fail |
| Input отсутствует или размер не совпал | Permanent fail `input_object_invalid` |
| Алгоритм временно недоступен | Retryable fail |
| Вход не поддерживается алгоритмом | Permanent fail `input_not_supported` |
| S3 result upload временно недоступен | Retryable fail и удалить partial result |
| Диск воркера заполнен | Retryable fail `worker_storage_exhausted` |
| Неизвестный job type или stage contract | Не claim либо завершить процесс как несовместимый |

Backoff подключения воркера ограничивается, например, последовательностью 1, 2, 5, 10,
30 секунд с jitter. Он не заменяет серверный retry jobs.

## 16. Локальные файлы

Каждая попытка использует отдельный случайно созданный каталог, не основанный на имени
изображения:

```text
<worker-temp>/<attempt_id>/input
<worker-temp>/<attempt_id>/result.ply
```

Каталог удаляется рекурсивно в `finally`. При старте воркер удаляет оставшиеся каталоги
неактивных предыдущих запусков старше безопасного порога. Путь temporary root задаётся
локальной конфигурацией и до удаления проверяется как разрешённый root.

## 17. S3-права воркера

Минимальные операции:

- `GetObject` и `HeadObject` для `jobs/v1/*/input/*`;
- `PutObject`, `GetObject`, `HeadObject` и `DeleteObject` для
  `jobs/v1/*/result/*`;
- без `ListAllMyBuckets`, изменения bucket policy и публичных ACL.

Bucket name, endpoint, region, access key и secret key задаются только в secret storage
окружения воркера. Их нельзя получать из job parameters, PostgreSQL payload или S3
object metadata.

## 18. Рекомендуемый основной цикл

Псевдокод описывает порядок, а не конкретную библиотеку:

```python
check_protocol_version()
listen_connection.execute("LISTEN metology_jobs")

while not shutting_down:
    while capacity_available:
        assignment = claim_job(worker_id, supported_types)
        if assignment is None:
            break
        start_attempt(assignment)

    wait_for_notification_or_timeout(seconds=30)
```

Каждая attempt-задача:

```python
try:
    heartbeat("downloading")
    download_input()
    heartbeat("processing")
    build_model()
    heartbeat("exporting_ply")
    export_ply()
    heartbeat("uploading_result")
    upload_and_verify_result()
    complete_job()
except CancellationRequested:
    delete_partial_result()
    acknowledge_cancellation()
except LeaseLost:
    delete_partial_result()
except KnownPermanentError as error:
    fail_job(error, retryable=False)
except KnownTransientError as error:
    fail_job(error, retryable=True)
finally:
    delete_local_attempt_directory()
```

Необработанное исключение сначала преобразуется в безопасный внутренний error code.
Stack trace остаётся только в защищённом логе воркера и не отправляется в PostgreSQL.

## 19. Shutdown

При graceful shutdown воркер перестаёт claim новые jobs, продолжает heartbeat активных
попыток и даёт им ограниченное время завершиться. Затем безопасно останавливает расчёт.
Если времени недостаточно, lease естественно истекает и job будет назначена повторно.

Воркер не вызывает retryable fail только из-за планового shutdown: это могло бы создать
лишнюю попытку одновременно с ещё работающим процессом.

## 20. Проверка реализации внешнего проекта

Перед совместным запуском внешний проект должен пройти контрактные сценарии:

1. protocol version 1 распознаётся;
2. существующая queued job находится даже без NOTIFY;
3. два воркера не получают одну попытку;
4. heartbeat продлевает lease без поля progress;
5. потерянный lease блокирует complete;
6. retryable failure создаёт не более трёх попыток;
7. permanent failure не повторяется;
8. running cancellation останавливает публикацию PLY;
9. JPEG, PNG, HEIC и HEIF скачиваются потоково;
10. PLY загружается только по выданному result key;
11. локальные файлы удаляются при success, failure, cancellation и crash recovery;
12. логи не содержат credentials, object contents или signed URLs.

Для интеграционного теста backend создаёт job через тот же Flutter API, test worker
обрабатывает её через функции v1, а клиент получает PLY через result endpoint. Это
является сквозным критерием совместимости двух проектов.
