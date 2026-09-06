# Metology FaceLift worker

Воркер для Linux с NVIDIA GPU. WSL2 Ubuntu подходит для локальной отладки.
Он забирает `photo_3D_fl` из PostgreSQL через `worker_api/v1`, скачивает фото из
закрытого S3 bucket, вызывает FaceLift и загружает бинарный PLY.

## Быстрый запуск в Docker

Образ содержит Python 3.10, CUDA toolkit, PyTorch, зависимости воркера и код
вашего FaceLift. На хосте нужны Docker Compose v2+, NVIDIA driver и доступ GPU
из контейнеров. Для Linux настройте
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html);
для Windows используйте Docker Desktop с WSL2 и Linux containers.
CUDA toolkit и Python на хосте при таком запуске не нужны.

Перед сборкой клонируйте **ваш изменённый FaceLift** в
`worker_processors/FaceLift` (см. раздел о двух Git-репозиториях ниже).
Без него сборка не пройдёт. Веса, фотографии, `.env` и SSH-ключи исключены из
контекста сборки; небольшой `clr_embeds.pt` из репозитория FaceLift включён.

```bash
cp .env.example .env
chmod 600 .env
# Заполнить DATABASE_URL, S3_* и при необходимости SSH_*.
```

В PowerShell вместо этих двух команд: `Copy-Item .env.example .env`.
Не перезаписывайте уже настроенный `.env`. По умолчанию выбран `CUDA_PROFILE=cu128`
для RTX 50 / Blackwell. Для RTX 20/30/40 и исходного стека FaceLift задайте
`CUDA_PROFILE=cu124`. GPU выбирается через `GPU_DEVICE_ID=0`.
Профиль `cu128` ещё требует проверки реального инференса на целевой GPU.
Для xformers 0.0.30 на Blackwell воркер отключает выбор Hopper-only FlashAttention 3:
иначе возникает `no kernel image is available`. Остальные реализации внимания
выбираются xformers автоматически. Причина описана в
[upstream issue](https://github.com/facebookresearch/xformers/issues/1251).

### Если PostgreSQL подключается через SSH

В `.env` задайте пути **на хосте Docker** (при локальном Docker Engine это
машина, с которой запускаете Compose):

```dotenv
SSH_TUNNEL_ENABLED=1
SSH_HOST=your-server
SSH_USER=worker
SSH_KEY_PATH=/home/user/.ssh/id_ed25519
SSH_KNOWN_HOSTS_PATH=/home/user/.ssh/known_hosts
SSH_KEY_PASSPHRASE=
SSH_REMOTE_HOST=127.0.0.1
SSH_REMOTE_PORT=5432
SSH_DATABASE_SSLMODE=disable
```

Для Compose из PowerShell пути записывайте с прямыми слешами:
`SSH_KEY_PATH=C:/Users/Ilya/.ssh/id_ed25519` и
`SSH_KNOWN_HOSTS_PATH=C:/Users/Ilya/.ssh/known_hosts`.
Для Compose из WSL нужны Linux-пути, например `/home/user/.ssh/id_ed25519`
или `/mnt/c/Users/Ilya/.ssh/id_ed25519`. Используйте абсолютные пути без `~`.
Оба файла должны существовать; `known_hosts` должен содержать проверенный
ключ SSH-сервера. Для нестандартного порта запись имеет вид `[host]:port`.

```bash
docker compose -f compose.yaml -f compose.ssh.yaml build
docker compose -f compose.yaml -f compose.ssh.yaml run --rm worker check-db
docker compose -f compose.yaml -f compose.ssh.yaml up -d
docker compose -f compose.yaml -f compose.ssh.yaml logs -f --tail=100
```

**`SSH_KEY_PATH` подцепится:** `compose.ssh.yaml` использует его как источник
bind mount и передаёт воркеру путь `/run/secrets/ssh_private_key` внутри
контейнера. `known_hosts` подключается аналогично. Оба файла доступны только
для чтения. Неверный путь остановит запуск: воркер проверяет, что это файлы,
и сообщает `ssh_private_key_missing` или `ssh_known_hosts_missing`.
Ключ остаётся на хосте и не попадает в образ. Парольная фраза приходит через
окружение из `.env`; значения с `$` заключайте в одинарные кавычки.
Монтировать всю `.ssh` или публиковать порт `15432` не требуется: туннель и
клиент PostgreSQL работают внутри одного контейнера.

### Если PostgreSQL доступен напрямую

Оставьте `SSH_TUNNEL_ENABLED=0`. SSH-файлы в этом режиме не нужны:

```bash
docker compose build
docker compose run --rm worker check-db
docker compose up -d
docker compose logs -f --tail=100
```

`127.0.0.1` в `DATABASE_URL`, `SSH_HOST` или S3 endpoint относится к самому
контейнеру. Для сервисов на Windows-хосте используйте `host.docker.internal`,
для удалённого сервера — его DNS-имя/IP. `SSH_REMOTE_HOST=127.0.0.1` по-прежнему
относится к **SSH-серверу**. Если нужен частный CA PostgreSQL, отдельно подключите
его файл read-only через Compose override и задайте `PGSSLROOTCERT` путём внутри
контейнера; один путь с хоста в `.env` не даёт доступа к файлу.

### Две GPU: две задачи одновременно

В существующем `.env` задайте:

```dotenv
COMPOSE_PROFILES=multi-gpu
GPU_DEVICE_ID=0
GPU_DEVICE_ID_2=1
```

`worker` работает на первой карте, `worker-gpu-1` — на второй. Укажите разные
ID из `nvidia-smi`; также можно использовать UUID карт. Привязка использует
[`device_ids` Docker Compose](https://docs.docker.com/compose/how-tos/gpu-support/).
Каждый контейнер видит только выбранную GPU, поэтому внутренний `cuda:0`
FaceLift относится к своей карте. Каждый процесс держит собственную модель
в видеопамяти и обрабатывает одну задачу за раз: суммарный concurrency — 2.
Вся модель должна помещаться на каждой карте; память двух GPU не складывается.
Задачи распределяются через существующий PostgreSQL claim, у каждого процесса
свой `worker_id`, heartbeat и остановка. Более быстрая карта сразу берёт новую
задачу, не дожидаясь второй.

Перед первым параллельным запуском подготовьте общие веса: перенесите готовые
checkpoints по инструкции ниже и выполните `infer-local` на `worker`, чтобы
скачать недостающие файлы. Это исключает одновременную первую загрузку моделей
двумя процессами. Веса и model cache общие, временные файлы контейнеров хранятся
в отдельных volumes. На хосте также нужна RAM для двух экземпляров модели.

Для прямого подключения к PostgreSQL:

```bash
docker compose build worker
docker compose config --quiet
docker compose up -d
docker compose logs -f --tail=100 worker worker-gpu-1
```

Для подключения через SSH:

```bash
docker compose -f compose.yaml -f compose.ssh.yaml build worker
docker compose -f compose.yaml -f compose.ssh.yaml config --quiet
docker compose -f compose.yaml -f compose.ssh.yaml up -d
docker compose -f compose.yaml -f compose.ssh.yaml logs -f --tail=100 worker worker-gpu-1
```

SSH overlay подключает ключ и `known_hosts` к обоим контейнерам. Каждый создаёт
свой туннель; одинаковый `SSH_LOCAL_PORT` допустим, поскольку контейнеры имеют
отдельные сетевые пространства. Порты на хост не публикуются.

Проверить выбор карт можно без запуска очереди:

```bash
docker compose run --rm --entrypoint nvidia-smi worker
docker compose run --rm --entrypoint nvidia-smi worker-gpu-1
```

При SSH добавляйте `-f compose.yaml -f compose.ssh.yaml` к этим командам.
Не используйте `--scale worker=2`: обе копии получат один и тот же `GPU_DEVICE_ID`.
Чтобы вернуться к одной GPU, сначала остановите второй контейнер командой
`docker compose stop worker-gpu-1` (с SSH-флагами при необходимости), затем
очистите `COMPOSE_PROFILES` в `.env`. Обычный запуск без профиля использует одну GPU.

### Веса, проверка GPU и обновление

Первая сборка скачивает несколько гигабайт CUDA/PyTorch и компилирует rasterizer.
`DOCKER_BUILD_JOBS=2` ограничивает параллелизм компиляции. Архитектуры по умолчанию:
`cu124` — `7.5;8.0;8.6;8.9;9.0+PTX`, `cu128` — `10.0;12.0+PTX`.
Для сборки только под RTX 50 можно задать `CUDA_ARCH_LIST=12.0`.
Расширение собирается без GPU; доступ GPU нужен при запуске.
Зависимости кэшируются отдельно от кода. Rasterizer закреплён на Git commit,
а фактические версии пакетов записаны в `/opt/worker/installed-requirements.txt`.

Compose сохраняет checkpoints, Hugging Face/rembg/torch cache и рабочие
временные файлы в отдельных named volumes. При первом инференсе FaceLift
скачивает недостающие веса; `doctor` их не скачивает и до этого может показывать
`MISSING`. Обычные `down`, пересоздание контейнера и пересборка сохраняют volumes.
**`down -v` удаляет их, включая скачанные веса.** Уже скачанные на хосте
checkpoints можно перенести перед первым запуском:

```bash
docker compose create worker
docker compose cp worker_processors/FaceLift/checkpoints/. worker:/opt/worker/worker_processors/FaceLift/checkpoints/
```

Проверка локального фото без получения заданий из backend (Linux/WSL):

```bash
mkdir -p outputs
docker compose run --rm --entrypoint nvidia-smi worker
docker compose run --rm worker doctor
docker compose run --rm -v "$PWD/photo.jpg:/input/photo.jpg:ro" -v "$PWD/outputs:/output" worker infer-local /input/photo.jpg /output/result.ply
```

Файл `photo.jpg` должен существовать, `result.ply` не должен существовать.
В PowerShell используйте `${PWD}/photo.jpg` и `${PWD}/outputs` в аргументах `-v`.
Контейнер запускается от root, чтобы читать закрытый SSH-ключ через bind mount.
На Linux результат локального инференса также принадлежит root; при необходимости
передайте его своему пользователю: `sudo chown "$(id -u):$(id -g)" outputs/result.ply`.
При SSH добавляйте `-f compose.yaml -f compose.ssh.yaml` ко всем командам Compose
в этом разделе, включая остановку и обновление.
После обновления checkout воркера или FaceLift: `docker compose up -d --build`.
Остановка: `docker compose down`; контейнер даёт воркеру 330 секунд на завершение.
Если увеличиваете `WORKER_SHUTDOWN_SECONDS`, увеличьте `WORKER_STOP_GRACE_PERIOD`
ещё минимум на 30 секунд. `restart: unless-stopped` восстанавливает процесс
после сбоя или перезапуска Docker.

Для проверки конфигурации без вывода секретов: `docker compose config --quiet`
(для SSH добавьте оба `-f`). Отдельная лёгкая сборка запускает CPU-тесты без
CUDA и весов: `docker build --target test -t metology-worker:cpu-test .`.
Она не заменяет проверку GPU и создание настоящего PLY.
Опциональный тест GPU (в установленном CUDA-окружении) запускается командой
`WORKER_TEST_GPU=1 python -m unittest discover -s tests -p test_gpu.py -v`.
Он сравнивает результат внимания с эталонным расчётом, выполняет CUDA-rasterizer
и импортирует FaceLift без загрузки весов реконструкции.

## Как связаны проекты

```text
worker/                         отдельный Git-репозиторий воркера
├── metology_worker/             очередь, heartbeat, S3, адаптер, CLI
├── scripts/install.sh
├── .env.example
└── worker_processors/
    └── FaceLift/                отдельный Git-репозиторий FaceLift
        ├── inference.py
        ├── checkpoints/
        └── ...
```

`FACELIFT_PATH` указывает на checkout FaceLift. Адаптер добавляет этот путь в
Python import path и импортирует `inference.py`. Он загружает модели один раз
до получения заданий, затем вызывает `process_single_image` для каждого фото.
HTTP-сервер или Gradio для этого не нужны.

В FaceLift добавлены необязательные аргументы `ply_only`, `on_stage`,
`check_cancel`. Режим воркера отключает сохранение превью и turntable-видео;
существующий CLI FaceLift сохраняет прежнее поведение. Результат — Gaussian
splatting PLY, для просмотра нужен совместимый GS viewer.

## 1. Окружение Linux / WSL2

Целевая базовая среда — Ubuntu 22.04, Python 3.10, NVIDIA driver, CUDA toolkit,
компилятор C++, Git, ffmpeg. На Linux установите подходящий NVIDIA driver.
В WSL2 GPU предоставляется Windows-драйвером: Linux display driver внутри WSL
не устанавливают. См. [инструкцию NVIDIA](https://docs.nvidia.com/cuda/wsl-user-guide/index.html).

Из PowerShell открыть существующий дистрибутив:

```powershell
wsl -d Ubuntu-22.04
```

Внутри Ubuntu:

```bash
cd '/mnt/d/BPR_PRODUCTION/Metology backend/worker'
sudo apt update
sudo apt install -y python3.10-venv python3.10-dev build-essential git ffmpeg libgl1 libglib2.0-0
nvidia-smi
```

Проект можно отлаживать на `/mnt/d`, но создание окружения и сборка расширений
там могут работать медленнее, чем в Linux filesystem. На сервере разместите
оба checkout в указанной выше структуре.

## 2. Установка

Только воркер и CPU-тесты, без загрузки PyTorch и моделей:

```bash
bash scripts/install.sh worker-only
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Для GPU нужен **CUDA toolkit с `nvcc`**, а не только драйвер или CUDA runtime
из PyTorch. Установите toolkit по документации NVIDIA для вашей ОС, настройте
`PATH`/`CUDA_HOME`, затем выберите профиль:

```bash
# Исходный стек FaceLift, для GPU, поддерживаемых CUDA 12.4:
bash scripts/install.sh cu124

# RTX 50 / Blackwell — отдельный профиль, требует CUDA toolkit 12.8:
bash scripts/install.sh cu128
```

Профили: `cu124` = PyTorch 2.4.1 / torchvision 0.19.1 / xformers 0.0.28.post1;
`cu128` = PyTorch 2.7.0 / torchvision 0.22.0 / xformers 0.0.30.
Поддержка Blackwell и CUDA 12.8 появилась в
[PyTorch 2.7](https://pytorch.org/blog/pytorch-2-7/).
**Профиль cu128 ещё не проверен реальным инференсом FaceLift.**
Наличие 8 ГБ VRAM не гарантирует, что обе модели и расчёт поместятся в память.

Установщик создаёт `.venv` и не изменяет системный Python, не устанавливает
драйверы и не запускает сервис. Полный GPU-профиль скачивает большие пакеты
и собирает rasterizer из upstream Git. Его фактическая версия вместе с
остальными пакетами записывается в `installed-requirements.txt`; это снимок
окружения, не заранее проверенный lockfile. `facenet-pytorch` устанавливается
с `--no-deps`, как в FaceLift: его metadata требует старые torch/torchvision,
поэтому `pip check` может сообщать этот известный конфликт.

## 3. Проверка фото без backend

```bash
source .venv/bin/activate
python -m metology_worker doctor
python -m metology_worker infer-local /absolute/path/photo.jpg ./outputs/result.ply
```

Команда не перезаписывает существующий результат. Для локальной проверки не
нужны PostgreSQL, S3 или `.env`. `doctor` не скачивает веса; проверяет локальные
файлы, Python, зависимости и доступность CUDA. FaceLift при загрузке моделей
использует свою загрузку из Hugging Face. Для автономного запуска заранее
подготовьте файлы по путям:

```text
worker_processors/FaceLift/checkpoints/mvdiffusion/pipeckpts/
worker_processors/FaceLift/checkpoints/gslrm/ckpt_0000000000021125.pt
worker_processors/FaceLift/mvdiffusion/data/fixed_prompt_embeds_6view/clr_embeds.pt
```

До получения реального PLY на целевой GPU установку нельзя считать проверенной.

## 4. Подключение очереди

```bash
cp .env.example .env
chmod 600 .env
# Заполнить .env значениями инфраструктуры воркера.
python -m metology_worker --env-file .env run
```

Нужны `DATABASE_URL` отдельной PostgreSQL-роли и S3 endpoint, region, bucket,
access key, secret key. Роль вызывает только функции `worker_api`, без доступа
к таблицам Django. Bucket из claim должен совпадать с `S3_BUCKET`.
Канал задаётся отдельно от URL базы:

```dotenv
DATABASE_NOTIFY_CHANNEL=metology_jobs
```

По умолчанию используется `metology_jobs` из контракта v1. Если переопределяете
канал, backend должен отправлять `NOTIFY` в тот же канал: настройка воркера
не меняет backend. Допустимы латинские буквы, цифры и `_`, первый символ —
буква или `_`, длина до 63 символов. Регистр сохраняется.
Канал PostgreSQL не требует предварительного создания; воркер подписывается
через `LISTEN`. Проверочный сигнал из той же базы:

```sql
SELECT pg_notify('metology_jobs', '{"v":1,"type":"photo_3D_fl"}');
```

Сигнал только пробуждает воркер и не создаёт задание. Если сигнал пропущен,
очередь всё равно проверяется не реже раза в 30 секунд при свободном GPU slot.

При прямом production-подключении PostgreSQL использует `sslmode=verify-full`, S3 — HTTPS.
Для частного центра сертификации PostgreSQL укажите `PGSSLROOTCERT`.
`WORKER_ALLOW_INSECURE=1` разрешён только для локальной тестовой инфраструктуры.

На момент проверки локального backend 2026-09-05 функции `worker_api` были
описаны в документации, но ещё не реализованы. Пока миграции не готовы,
`run` не сможет пройти проверку версии. Backend здесь не изменяется.

## Поведение и восстановление

- Один GPU slot на процесс: следующая job берётся после окончания текущей.
  Compose-профиль `multi-gpu` запускает два процесса на разных GPU, до двух jobs одновременно.
- Отдельное autocommit LISTEN-соединение; claim после подключения, сигнала
  или ожидания до 30 секунд. NOTIFY не является заданием.
- Heartbeat каждые 20 секунд в отдельном потоке и при смене этапа.
- Отмена проверяется между шагами диффузии и этапами; один CUDA-вызов может
  задержать остановку до безопасной точки. Heartbeat работает в это время.
- Потеря lease запрещает публикацию и terminal mutations старой попытки.
- Временные файлы находятся в закрытой директории процесса. Старые директории
  старше суток удаляются при старте только если их flock не удерживает процесс.
- SHA-256 вычисляется локально; загрузка проверяется через HEAD. Result key
  берётся только из claim.
- При неопределённом ответе complete запрос повторяется с теми же ID. Если
  ответ остаётся неизвестным, PLY сохраняется в S3 и пишется
  `completion_unknown`: он мог быть уже принят backend. Такое событие требует
  сверки состояния backend и его политики очистки orphan-объектов.
- Сетевой сбой удаления результата оставляет `result_cleanup_failed`; очистку
  оставшихся объектов должна обеспечивать инфраструктура/backend.
- SIGTERM/Ctrl+C прекращает claim и даёт активной работе до 300 секунд.
  Затем расчёт остановится в ближайшей безопасной точке. Жёсткую границу
  зависшего CUDA-вызова обеспечивает supervisor (`TimeoutStopSec` в systemd).

## Встроенный SSH-туннель

Воркер может подключаться через [AsyncSSH](https://asyncssh.readthedocs.io/en/stable/#port-forwarding)
внутри Python. Команда `ssh` и отдельный терминал не нужны. Туннель работает
в отдельном потоке с asyncio-циклом; это не блокирует FaceLift и heartbeat.

Пример `.env`, если PostgreSQL доступен на loopback SSH-сервера:

```dotenv
DATABASE_URL=postgresql://metology_worker:URL_ENCODED_PASSWORD@127.0.0.1:5432/metology
DATABASE_NOTIFY_CHANNEL=metology_jobs

SSH_TUNNEL_ENABLED=1
SSH_HOST=your-server
SSH_PORT=22
SSH_USER=worker
SSH_KEY_PATH=/home/user/.ssh/id_ed25519
SSH_KNOWN_HOSTS_PATH=/home/user/.ssh/known_hosts
SSH_KEY_PASSPHRASE=
SSH_LOCAL_PORT=15432
SSH_REMOTE_HOST=127.0.0.1
SSH_REMOTE_PORT=5432
SSH_DATABASE_SSLMODE=disable

WORKER_ALLOW_INSECURE=0
```

`DATABASE_URL` задаёт пользователя, пароль и имя базы. Адрес сокета и порт
воркер подменяет только в памяти на адрес своего туннеля; `.env` не изменяется.
Локальный порт слушает только `127.0.0.1`. `SSH_LOCAL_PORT=0` позволяет выбрать
свободный порт автоматически; при переподключении выбранный порт сохраняется.

Ключ сервера должен быть заранее проверен и добавлен в указанный `known_hosts`.
Отключение проверки ключа не поддерживается. Для ключа с парольной фразой
задайте `SSH_KEY_PASSPHRASE` через secret storage или локальный `.env` с правами
`0600`; содержимое ключа и парольная фраза в логи не выводятся.
SSH-пользователю требуется разрешение TCP forwarding к адресу базы.

`SSH_DATABASE_SSLMODE=disable` отключает только TLS PostgreSQL внутри туннеля,
когда база находится на самом SSH-сервере. S3 продолжает требовать HTTPS.
Если база находится на другом хосте, укажите `SSH_DATABASE_SSLMODE=verify-full`,
`SSH_REMOTE_HOST` — адрес базы, а hostname в `DATABASE_URL` — имя из её TLS
сертификата. При частном CA задайте `PGSSLROOTCERT`.

Обновить локальное окружение и проверить доступ без GPU/S3:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m metology_worker --env-file .env check-db
```

`check-db` открывает туннель, проверяет `worker_api/v1` и закрывает соединения.
Отсутствие функций `worker_api` даст ошибку, даже если сам SSH и доступ к базе
работают. Постоянная работа запускается обычной командой `run`.

Туннель восстанавливается с задержками 1, 2, 5, 10, 30 секунд. Пока он недоступен,
новые подключения к базе отклоняются, а существующая логика heartbeat/lease
определяет, можно ли продолжить текущую попытку. После потери lease результат
не публикуется. Ошибки ключа/аутентификации требуют исправления конфигурации
и перезапуска. При завершении воркер закрывает только свой туннель.

## Сервис на Linux

`deploy/metology-worker.service` — пример для `/srv/metology-worker` и
пользователя `metology-worker`. Создайте пользователя, выдайте ему доступ к
проекту/GPU и подготовьте `.env`, затем адаптируйте пути и установите unit.
Пока локальное фото и подключение очереди не проверены, используйте foreground
команду `run`. В WSL2 для отладки systemd не обязателен.

## Git: два отдельных проекта

`worker_processors/FaceLift/` исключён из Git воркера; у FaceLift свой `.git`.
При первом создании репозитория воркера:

```bash
git init
git add .
git commit -m "Add Metology FaceLift worker"
```

Изменение адаптерного API FaceLift фиксируется отдельно:

```bash
git -C worker_processors/FaceLift add inference.py
git -C worker_processors/FaceLift commit -m "Add cancellable PLY-only inference API"
```

На сервер нужно клонировать **оба ваших репозитория**, включая изменённый
FaceLift; исходный upstream без этих изменений не содержит `ply_only`.
Внешний Git clone не загрузит вложенный проект автоматически.
Веса, `.env`, `.venv`, фотографии и результаты не включать в коммиты.

## Проверки

```bash
python -m unittest discover -s tests -v
python -m compileall -q metology_worker
ruff check metology_worker tests
```

Тесты CPU проверяют порядок claim, fencing, heartbeat, отмену, исходы,
ограничения streaming download и PLY-only ветку FaceLift с заменой GPU операций.
Они не доказывают качество реконструкции, CUDA-совместимость, права настоящей
PostgreSQL-роли или работоспособность конкретного S3 endpoint.
Сквозной критерий — создание job через API backend и получение её PLY клиентом,
согласно `worker-interface.md`.
