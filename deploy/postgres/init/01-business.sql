-- Platform roles only. Business schemas/data are configured externally.
CREATE USER semora_reader WITH PASSWORD 'semora_reader';
CREATE USER semora_api WITH PASSWORD 'semora_api';
GRANT CONNECT ON DATABASE semora_business TO semora_reader, semora_api;
ALTER USER semora_reader SET default_transaction_read_only = on;
