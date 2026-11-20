import sqlite3
import pytest
import sqlglot
from app.sql_grain import validate_join_grain
from app.sql_errors import SQLIssue

OBJECTS=[{'table':n,'primaryKey':'id','properties':[{'name':'id','column':'id'}]} for n in ('root','detail_a','detail_b')]


def check(sql):
    validate_join_grain(sqlglot.parse_one(sql,read='postgres'),OBJECTS)


@pytest.mark.parametrize('metric',['COUNT(a.id)','SUM(a.value)','AVG(a.value)','SUM(DISTINCT a.value)'])
def test_raw_sibling_join_cannot_aggregate_multiplied_metrics(metric):
    with pytest.raises(SQLIssue,match='连接放大'):
        check(f'SELECT r.id,{metric} FROM root r LEFT JOIN detail_a a ON a.root_id=r.id LEFT JOIN detail_b b ON b.root_id=r.id GROUP BY r.id')


def test_preaggregation_returns_correct_counts_sums_averages_and_empty_subject():
    sql='''SELECT r.id,COALESCE(a.n,0) AS a_count,COALESCE(b.n,0) AS b_count,a.total,a.mean
    FROM root r LEFT JOIN (SELECT root_id,COUNT(*) AS n,SUM(value) AS total,AVG(value) AS mean FROM detail_a GROUP BY root_id) a ON a.root_id=r.id
    LEFT JOIN (SELECT root_id,COUNT(*) AS n FROM detail_b GROUP BY root_id) b ON b.root_id=r.id ORDER BY r.id'''
    check(sql)
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE root(id INTEGER PRIMARY KEY);CREATE TABLE detail_a(id INTEGER PRIMARY KEY,root_id INTEGER,value INTEGER);
    CREATE TABLE detail_b(id INTEGER PRIMARY KEY,root_id INTEGER);
    INSERT INTO root VALUES(1),(2); INSERT INTO detail_a VALUES(1,1,10),(2,1,20); INSERT INTO detail_b VALUES(1,1),(2,1),(3,1);''')
    assert db.execute(sql).fetchall()==[(1,2,3,30,15.0),(2,0,0,None,None)]


def test_composite_grain_cte_is_safe_when_every_join_key_is_present():
    check('''WITH a AS (SELECT root_id,kind,COUNT(*) n FROM detail_a GROUP BY root_id,kind),
    b AS (SELECT root_id,kind,COUNT(*) n FROM detail_b GROUP BY root_id,kind)
    SELECT a.root_id,SUM(a.n) FROM a LEFT JOIN b ON a.root_id=b.root_id AND a.kind=b.kind GROUP BY a.root_id''')
    with pytest.raises(SQLIssue):
        check('''WITH a AS (SELECT root_id,kind,COUNT(*) n FROM detail_a GROUP BY root_id,kind),
        b AS (SELECT root_id,kind,COUNT(*) n FROM detail_b GROUP BY root_id,kind)
        SELECT a.root_id,SUM(a.n) FROM a LEFT JOIN b ON a.root_id=b.root_id GROUP BY a.root_id''')


def test_detail_to_unique_parent_and_existence_filter_are_safe():
    check('SELECT a.root_id,SUM(a.value) FROM detail_a a JOIN root r ON a.root_id=r.id GROUP BY a.root_id')
    check('SELECT r.id FROM root r WHERE EXISTS(SELECT 1 FROM detail_a a WHERE a.root_id=r.id)')
    check('SELECT a.id,b.id FROM detail_a a JOIN detail_b b ON a.root_id=b.root_id')
