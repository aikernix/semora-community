"""Portable result column metadata for embedded cross-source SQL."""
from decimal import Decimal


def column_types(description, mysql=False, rows=(), mysql_fields=None):
    values=[]
    for index,c in enumerate(description or []):
        code=c[1]
        if code in ({0,246} if mysql else {1700}):
            precision,scale=c[4],c[5]
            if mysql and mysql_fields and index<len(mysql_fields):
                field=mysql_fields[index]
                # PyMySQL's DBAPI precision is protocol display width, which
                # includes the sign and decimal separator (not numeric digits).
                precision=field.length-(0 if field.flags & 32 else 1)-(1 if field.scale else 0)
                scale=field.scale
            if precision and precision<=38 and scale is not None and scale<=precision:
                name=f'DECIMAL({precision},{scale})'
            else:
                # SUM(NUMERIC) may have no typmod (PostgreSQL), or precision
                # 65 (MySQL). Infer an exact bounded type from returned Decimal
                # values, never from strings such as identifiers.
                observed=[row[index] for row in rows if isinstance(row[index],Decimal) and row[index].is_finite()]
                if observed:
                    scale=max(max(-v.as_tuple().exponent,0) for v in observed)
                    integral=max(max(len(v.as_tuple().digits)+v.as_tuple().exponent,0) for v in observed)
                    precision=max(1,integral+scale)
                    name=f'DECIMAL({precision},{scale})' if precision<=38 else 'VARCHAR'
                else:
                    # Only NULL/empty results: retain a numeric type so empty
                    # SUM/COALESCE is valid; there is no value to round.
                    name='DECIMAL(38,18)'
        elif code in ({1,2,3,8,9,13} if mysql else {20,21,23}):name='HUGEINT'
        elif code in ({4,5} if mysql else {700,701}):name='DOUBLE'
        elif not mysql and code==16:name='BOOLEAN'
        elif code in ({10,14} if mysql else {1082}):name='DATE'
        elif not mysql and code==1184:name='TIMESTAMPTZ'
        elif not mysql and code==1266:name='TIMETZ'
        elif code in ({7,12} if mysql else {1114}):name='TIMESTAMP'
        elif code in ({11} if mysql else {1083}):name='TIME'
        else:name='VARCHAR'
        values.append(name)
    return values


def encode_scalar(value):
    if isinstance(value,Decimal):return str(value)
    return value.isoformat() if hasattr(value,'isoformat') else value
