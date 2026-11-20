import pytest
from fastapi import HTTPException
from services.executor.app.main import assert_readonly
from services.executor.app.mysql_adapter import begin_readonly




@pytest.mark.parametrize('sql', [
    'SELECT 1; DROP TABLE loans',
    "SELECT * FROM loans INTO OUTFILE '/tmp/export' LIMIT 1",
    'SELECT * FROM loans LIMIT 1 FOR UPDATE',
    'SELECT SLEEP(20) LIMIT 1',
])
def test_mysql_blocks_writes_locks_exports_and_side_effects(sql):
    with pytest.raises(HTTPException):
        assert_readonly(sql, 'mysql')


def test_mysql_readonly_transaction_sets_server_timeout_first():
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): calls.append(args)
    class Connection:
        def cursor(self): return Cursor()
    begin_readonly(Connection(), 3000)
    assert calls == [
        ('SET SESSION MAX_EXECUTION_TIME = %s', (3000,)),
        ('START TRANSACTION READ ONLY',),
    ]
