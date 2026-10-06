FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite 数据存放目录（Compose 中挂载持久卷）
RUN mkdir -p /data

EXPOSE 8000

# 容器启动时由 create_app 自动建表并写入固定窑位
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "60", "wsgi:app"]
