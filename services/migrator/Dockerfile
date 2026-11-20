FROM postgres:16-alpine
COPY services/migrator/migrate.sh /usr/local/bin/semora-migrate
COPY deploy/postgres/migrations /migrations
RUN sed -i 's/\r$//' /usr/local/bin/semora-migrate \
    && chmod 0555 /usr/local/bin/semora-migrate \
    && chmod 0444 /migrations/*.sql
USER 70:70
ENTRYPOINT ["/usr/local/bin/semora-migrate"]
