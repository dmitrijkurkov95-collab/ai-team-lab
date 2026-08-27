# Один образ на оба процесса: сервер и веб — это тот же Python и тот же
# исходник. Городить два образа ради 30 строк статики дороже, чем полезно;
# compose поднимает из него два сервиса с разными командами.
FROM python:3.13-slim

# Не latest и с явной версией: демо у владельца на чужой машине, «сломалось
# после обновления базового образа» — не тот сюрприз, который хочется словить.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при правке requirements,
# а не на каждое изменение кода.
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ server/
COPY web/ web/

# Без рута внутри контейнера. Каталог данных создаём и отдаём пользователю
# заранее: volume монтируется от имени root, и сервер без прав на запись
# упал бы на первой же карточке.
RUN useradd --create-home --uid 10001 kanban \
    && mkdir -p /data \
    && chown -R kanban:kanban /data /app
USER kanban

ENV KANBAN_DB=/data/kanban.db

EXPOSE 8000 5173

CMD ["python", "server/run.py"]
