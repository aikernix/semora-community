FROM maven:3.9.9-eclipse-temurin-17 AS jdbc
WORKDIR /build
COPY services/executor/jdbc /build
RUN --mount=type=cache,target=/root/.m2 mvn -B -q -DskipTests package

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/executor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/executor/app /app/app
COPY packages/contracts/python/semora_contracts /app/semora_contracts
COPY --from=jdbc /build/target/jdbc-worker-1.0.0.jar /opt/semora-jdbc/
COPY --from=jdbc /build/target/lib /opt/semora-jdbc/lib
COPY packages/telemetry/python/semora_telemetry /app/semora_telemetry
RUN mkdir -p /data/config && chown 10001:10001 /data/config \
    && addgroup --system --gid 10001 semora \
    && adduser --system --uid 10001 --ingroup semora --no-create-home semora
USER 10001:10001
EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]

