FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 perprisk
WORKDIR /opt/perp-risk

COPY pyproject.toml requirements.lock ./
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir . \
    && mkdir -p /home/perprisk/.local/share/monitor-perp-risk-positioning \
    && chown -R perprisk:perprisk /home/perprisk/.local

USER perprisk
VOLUME ["/home/perprisk/.local/share/monitor-perp-risk-positioning"]
ENTRYPOINT ["perp-risk"]
CMD ["watch"]
