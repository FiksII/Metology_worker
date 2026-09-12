# Сервер воркера

Сборку, перезапуск и проверку работающего воркера выполняйте на этом сервере,
если пользователь явно не указал другую среду. Не запускайте локальный Docker
Desktop для серверной пересборки.

## Подключение

- SSH: `95.79.44.129:2221`, пользователь `ii`.
- Приватный ключ на Windows: `C:\Users\Ilya\.ssh\id_rsa`.
- Соответствующий публичный ключ: `C:\Users\Ilya\.ssh\id_rsa.pub`.
  Для `ssh -i` используйте приватный ключ, не `.pub`.
- Каталог проекта на сервере: `/home/ii/_develop/Metology_worker`.

```powershell
ssh -p 2221 -i C:/Users/Ilya/.ssh/id_rsa ii@95.79.44.129
```

Не выводите содержимое приватных ключей и секреты из `.env`.

## Окружение

Проверено 2026-09-12; перед изменениями сверяйте актуальную конфигурацию сервера.

- Linux, Docker Compose; две NVIDIA GeForce RTX 3090, драйвер `595.84`.
- Профиль сборки: `CUDA_PROFILE=cu124`, `CUDA_ARCH_LIST=8.6`.
- Образ: `metology-worker:cu124`.
- Сервисы: `worker` и `worker-gpu-1`, профиль Compose `multi-gpu`.
- Контейнеры: `metology-worker-worker-1`, `metology-worker-worker-gpu-1-1`.
- Оба воркера настроены на `WORKER_PROCESSOR=orbithead`.
- Каждый контейнер видит одну карту; внутри `ORBITHEAD_GPU=0`.
  Физические карты выбираются через `GPU_DEVICE_ID` и `GPU_DEVICE_ID_2`.
- OrbitHead использует отдельное окружение Python 3.12 с extras `gpu` и `da3`.

## Сборка и перезапуск

Используйте серверный `.env`; не заменяйте его локальным `.env.example`.
Перед сборкой проверьте `git status --short` в основном репозитории и подмодулях:
обновление основного репозитория само по себе не обновляет checkout OrbitHead.
Сохраняйте существующие серверные правки перед переключением версии подмодуля.

```bash
cd /home/ii/_develop/Metology_worker
docker compose -f compose.yaml -f compose.ssh.yaml --profile multi-gpu build worker
docker compose -f compose.yaml -f compose.ssh.yaml --profile multi-gpu up -d --no-build worker worker-gpu-1
```

Пересборка образа не обновляет уже работающие контейнеры. Для применения образа
нужен `up -d`, а не просто `restart`. Выполняйте перезапуск, когда он входит
в запрос пользователя.

**Для запуска обязательно подключайте `compose.ssh.yaml`.** Без него оба
воркера завершаются с `ssh_private_key_missing`. Overlay монтирует серверные
файлы только для чтения:

- `/home/ii/.ssh/id_ed25519` → `/run/secrets/ssh_private_key`;
- `/home/ii/.ssh/known_hosts` → `/run/secrets/ssh_known_hosts`.

Это ключ SSH-туннеля воркера; он отличается от Windows-ключа для входа на сервер.

## Проверка после запуска

```bash
docker compose -f compose.yaml -f compose.ssh.yaml --profile multi-gpu ps
docker compose -f compose.yaml -f compose.ssh.yaml --profile multi-gpu logs --tail=50 worker worker-gpu-1
docker inspect --format '{{.Name}} image={{.Image}} status={{.State.Status}} restarts={{.RestartCount}}' metology-worker-worker-1 metology-worker-worker-gpu-1-1
docker image inspect metology-worker:cu124 --format '{{.Id}}'
```

Проверьте новый образ у обоих контейнеров, состояние `running`, отсутствие
повторных перезапусков и сообщения `ssh_tunnel_ready`, `worker_ready processors=orbithead`.
Один статус `Started` ещё не подтверждает успешный запуск приложения.
