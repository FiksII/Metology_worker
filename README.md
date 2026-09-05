# Metology FaceLift worker

Воркер для Linux с NVIDIA GPU. WSL2 Ubuntu подходит для локальной отладки.
Он забирает `photo_3D_fl` из PostgreSQL через `worker_api/v1`, скачивает фото из
закрытого S3 bucket, вызывает FaceLift и загружает бинарный PLY.

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

Профили: `cu124` = PyTorch 2.4.0 / torchvision 0.19.0 / xformers 0.0.27.post2;
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

- Один GPU slot: следующая job берётся после окончания текущей.
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
