# Metology GPU worker

Согласованная среда: Linux с NVIDIA GPU; локальная отладка в WSL2.
FaceLift остаётся отдельным репозиторием в `worker_processors/FaceLift`.
Воркер импортирует его функции; модели загружаются один раз до claim.
Один процесс обрабатывает одну попытку за раз. Контракт — `worker_api/v1`
из `worker-interface.md`, тип строго `photo_3D_fl`.

## Компоненты

- `metology_worker/config.py`: конфигурация из environment, без секретов в Git.
- `metology_worker/database.py`: только функции worker_api, отдельный autocommit LISTEN.
- `metology_worker/storage.py`: HEAD, ограниченный streaming GET, PUT/HEAD, DELETE.
- `metology_worker/runtime.py`: heartbeat в отдельном потоке, lease, отмена, исходы.
- `metology_worker/facelift.py`: загрузка FaceLift и нормализация JPEG/PNG/HEIC/HEIF.
- `metology_worker/__main__.py`: run, doctor, infer-local.
- FaceLift `inference.py`: необязательные callbacks этапов/отмены, режим только PLY.

## Поток

Проверка версии, загрузка моделей, LISTEN, claim до отсутствия доступного задания,
ожидание сигнала не более 30 секунд. Каждая попытка имеет отдельную случайную
временную директорию. Heartbeat каждые 20 секунд и при смене этапа; срок lease
контролируется monotonic clock на основе серверного UTC timestamp.
При отмене вычисление заканчивается в безопасной точке; heartbeat продолжает
работать. Потеря lease запрещает fail/complete. Объект результата использует
только выданный ключ. Повтор complete после неопределённого сетевого ответа
использует те же идентификаторы; успешный результат не удаляется при неизвестном
исходе complete. Окончательное разрешение неопределённого исхода требует backend.

## Установка и проверка

Отдельное Python 3.10 environment для совместимости с зависимостями FaceLift;
CUDA toolkit необходим для сборки rasterizer, драйвер NVIDIA предоставляется хостом.
Сначала doctor и infer-local, затем run с PostgreSQL/S3 credentials.
Unit tests работают без CUDA и внешних сервисов; GPU и сквозные тесты отдельно.
Backend worker_api в проверенной локальной копии ещё не реализован.
