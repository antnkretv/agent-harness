FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client procps && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 mcp && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mcp && mkdir -m 0700 /state && chown mcp:mcp /state
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py oauth.py policy.py dispatcher.py messages.py messages.env ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python", "server.py"]
CMD ["--config", "/config/stand.json"]
