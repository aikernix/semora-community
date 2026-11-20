REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'fixture_reader'@'%';
GRANT SELECT, SHOW VIEW ON fixture.* TO 'fixture_reader'@'%';
USE fixture;
CREATE TABLE customer (tenant_id int NOT NULL, customer_id bigint NOT NULL, name varchar(100) COMMENT '客户显示名',PRIMARY KEY(tenant_id,customer_id)) COMMENT '合成客户';
CREATE TABLE loan (id bigint PRIMARY KEY,tenant_id int NOT NULL,customer_id bigint NOT NULL,balance decimal(30,8),ts timestamp NULL,UNIQUE(tenant_id,id),FOREIGN KEY(tenant_id,customer_id) REFERENCES customer(tenant_id,customer_id));
CREATE VIEW loan_view AS SELECT id,balance FROM loan;
INSERT INTO customer VALUES (1,10,'synthetic-a'),(1,20,NULL);
INSERT INTO loan VALUES (101,1,10,12345678901234567890.12345678,'2025-01-02 03:04:05'),(102,1,20,NULL,NULL);
