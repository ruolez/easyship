FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    freetds-dev freetds-bin gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "300", "--reload", "app:create_app()"]
