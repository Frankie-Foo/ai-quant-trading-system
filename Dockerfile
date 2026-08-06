FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system trading && useradd --system --gid trading --home /app trading
WORKDIR /app

COPY requirements-prod.txt pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-prod.txt

COPY . .
RUN mkdir -p /var/lib/trading-system/data /var/lib/trading-system/state /run/trading-system \
    && chown -R trading:trading /var/lib/trading-system /run/trading-system

USER trading
VOLUME ["/var/lib/trading-system"]

ENTRYPOINT ["python", "-m", "schedule.postmarket"]
CMD ["--data-root", "/var/lib/trading-system/data", "--state-db", "/var/lib/trading-system/state/jobs.sqlite3", "--lock-file", "/run/trading-system/postmarket.lock", "--llm-mode", "optional"]
