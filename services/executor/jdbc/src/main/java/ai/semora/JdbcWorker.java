package ai.semora;

import com.fasterxml.jackson.databind.*;
import java.io.*;
import java.math.BigDecimal;
import java.sql.*;
import java.time.*;
import java.util.*;

/** One request, one connection, one process. Stdout is exclusively the JSON protocol. */
public final class JdbcWorker {
    static final ObjectMapper JSON = new ObjectMapper();
    static PrintStream PROTOCOL;
    static Map<String,Object> map(Object... pairs) {
        Map<String,Object> m = new LinkedHashMap<>();
        for (int i=0;i<pairs.length;i+=2) m.put((String)pairs[i], pairs[i+1]);
        return m;
    }
    static String text(JsonNode n, String k) { return n.path(k).asText(""); }
    static String val(ResultSet r, String key) throws SQLException { String s=r.getString(key); return s==null?"":s; }
    static List<Map<String,Object>> rows(ResultSet r) throws SQLException {
        try (r) {
            List<Map<String,Object>> rows=new ArrayList<>(); ResultSetMetaData m=r.getMetaData();
            while(r.next()) { if(rows.size()>=100000) throw new SQLException("metadata budget exceeded","54000");
                Map<String,Object> row=new LinkedHashMap<>();
                for(int i=1;i<=m.getColumnCount();i++) row.put(m.getColumnLabel(i).toLowerCase(Locale.ROOT),r.getObject(i)); rows.add(row); }
            return rows;
        }
    }
    static void readonly(Connection c, String strategy, long timeout) throws SQLException {
        c.setAutoCommit(false);
        try { c.setReadOnly(true); } catch (SQLException ignored) {} // Only the explicit boundary below is authoritative.
        String sql=switch(strategy) {
            case "postgres", "oracle" -> "SET TRANSACTION READ ONLY";
            case "mysql" -> "START TRANSACTION READ ONLY";
            // Do not trust Connection.setReadOnly: SQL Server treats it as a hint.
            case "sqlserver_permissions" -> "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE') WHERE permission_name IN ('CONTROL','ALTER','INSERT','UPDATE','DELETE','EXECUTE','CREATE TABLE','CREATE PROCEDURE','IMPERSONATE ANY USER','TAKE OWNERSHIP') UNION ALL SELECT permission_name FROM fn_my_permissions(NULL, 'SERVER') WHERE permission_name IN ('CONTROL SERVER','ALTER ANY LOGIN','IMPERSONATE ANY LOGIN') UNION ALL SELECT p.permission_name FROM sys.objects o CROSS APPLY fn_my_permissions(QUOTENAME(SCHEMA_NAME(o.schema_id))+'.'+QUOTENAME(o.name),'OBJECT') p WHERE o.is_ms_shipped=0 AND p.permission_name IN ('CONTROL','ALTER','INSERT','UPDATE','DELETE','EXECUTE','TAKE OWNERSHIP')";
            default -> throw new SQLException("scan-only source; configure database-enforced read-only strategy","0A000");
        };
        try(Statement s=c.createStatement()) { s.setQueryTimeout(jdbcTimeoutSeconds(timeout));
            if(strategy.equals("sqlserver_permissions")) { try(ResultSet r=s.executeQuery(sql)) {
                if(r.next()) throw new SQLException("read-only account required","42501");
            }} else s.execute(sql);
        }
    }
    static Map<String,Object> inspect(Connection c, JsonNode q) throws SQLException {
        DatabaseMetaData m=c.getMetaData();
        boolean safe=false; String failure="";
        try { readonly(c,text(q,"strategy"),5000); safe=true; } catch(SQLException e) { failure=Objects.toString(e.getSQLState(),"unknown"); } finally { if(!c.getAutoCommit())c.rollback(); }
        List<String> schemas=new ArrayList<>(),catalogs=new ArrayList<>(),gaps=new ArrayList<>();
        try(ResultSet r=m.getSchemas()) { while(r.next() && schemas.size()<10000) schemas.add(r.getString(1)); } catch(SQLException e) { gaps.add("schemas:"+e.getSQLState()); }
        try(ResultSet r=m.getCatalogs()) { while(r.next() && catalogs.size()<10000) catalogs.add(r.getString(1)); } catch(SQLException e) { gaps.add("catalogs:"+e.getSQLState()); }
        Map<String,Object> info=map("database",c.getCatalog(),"user",m.getUserName(),"product",m.getDatabaseProductName(),"version",m.getDatabaseProductVersion(),
            "driver",m.getDriverName(),"driver_version",m.getDriverVersion(),"read_only",safe,"readonly_failure",failure,
            "schemas",schemas,"catalogs",catalogs,"permission_gaps",gaps,"supports_transactions",m.supportsTransactions(),
            "window_functions","unverified","cte","unverified","cancel","worker_termination");
        if(text(q.path("connection"),"type").equals("oceanbase")) {
            String detected="unknown";
            try(Statement s=c.createStatement()) {
                s.setQueryTimeout(5);
                try(ResultSet r=s.executeQuery("SHOW VARIABLES LIKE 'ob_compatibility_mode'")) {
                    if(r.next()){String mode=r.getString(2).toLowerCase(Locale.ROOT);if(Set.of("mysql","oracle").contains(mode))detected=mode;}
                }
            }catch(SQLException ignored){}finally{c.rollback();}
            info.put("detected_compatibility_mode",detected);
            String configured=text(q.path("connection"),"compatibility_mode");
            if(!detected.equals("unknown")&&!detected.equals(configured)){
                info.put("read_only",false);info.put("readonly_failure","compatibility_mode_mismatch");
            }
        }
        if(text(q.path("connection"),"type").equals("vastbase")) {
            String detected="unknown";
            try(Statement s=c.createStatement()) {
                s.setQueryTimeout(5);
                try(ResultSet r=s.executeQuery("SHOW dbcompatibility")) {
                    if(r.next()) {
                        String mode=r.getString(1).toUpperCase(Locale.ROOT);
                        detected=switch(mode) {
                            case "A","ORACLE" -> "oracle";
                            case "B","MYSQL" -> "mysql";
                            case "PG","POSTGRESQL","POSTGRES" -> "postgres";
                            case "MSSQL","SQLSERVER","TSQL" -> "tsql";
                            default -> "unknown";
                        };
                    }
                }
            }catch(SQLException ignored){}finally{c.rollback();}
            info.put("detected_compatibility_mode",detected);
            info.put("compatibility_detection",detected.equals("unknown")?"unavailable":"verified");
            info.put("driver_selection","automatic_builtin_v");
        }
        if(q.path("probe_capabilities").asBoolean(false)&&safe)info.put("probes",probes(c,q));
        return info;
    }
    static Map<String,Object> probes(Connection c,JsonNode q) {
        String dialect=text(q,"dialect"),leaf="SELECT 1 AS n"+(dialect.equals("oracle")?" FROM DUAL":"");
        Map<String,String> sql=new LinkedHashMap<>();
        sql.put("cte","WITH semora_probe AS ("+leaf+") SELECT n FROM semora_probe");
        sql.put("window_functions","SELECT ROW_NUMBER() OVER (ORDER BY n) FROM ("+leaf+") semora_probe");
        sql.put("recursive_cte","WITH "+(Set.of("postgres","mysql").contains(dialect)?"RECURSIVE ":"")+"semora_probe(n) AS ("+leaf+" UNION ALL SELECT n+1 FROM semora_probe WHERE n<2) SELECT MAX(n) FROM semora_probe");
        String offset="SELECT n FROM ("+leaf+" UNION ALL SELECT 2 AS n"+(dialect.equals("oracle")?" FROM DUAL":"")+") semora_probe ORDER BY n";
        sql.put("offset_pagination",offset+(Set.of("tsql","oracle").contains(dialect)?" OFFSET 1 ROWS FETCH NEXT 1 ROWS ONLY":" LIMIT 1 OFFSET 1"));
        sql.put("pagination",dialect.equals("tsql")?"SELECT TOP 1 n FROM ("+leaf+") semora_probe":dialect.equals("oracle")?leaf+(text(q,"pagination").equals("rownum")?" WHERE ROWNUM <= 1":" FETCH FIRST 1 ROWS ONLY"):leaf+" LIMIT 1");
        sql.put("types","SELECT CAST(123.45 AS DECIMAL(18,2)), CAST(NULL AS "+(dialect.equals("oracle")?"VARCHAR2(10)":"VARCHAR(10)")+")"+(dialect.equals("oracle")?" FROM DUAL":""));
        JsonNode catalog=q.path("probe_catalog");
        if(catalog.isObject()) {
            sql.clear();
            catalog.fields().forEachRemaining(e->{ if(e.getValue().has("sql"))sql.put(e.getKey(),text(e.getValue(),"sql")); });
        }
        Map<String,Object> results=new LinkedHashMap<>();
        sql.forEach((key,query)->{
            try { readonly(c,text(q,"strategy"),3000);
                try(Statement s=c.createStatement()){s.setQueryTimeout(3);s.setMaxRows(2);try(ResultSet r=s.executeQuery(query)){
                    boolean hasRow=r.next(),valid=false;
                    if(hasRow&&catalog.isObject()) {
                        JsonNode spec=catalog.path(key); Object actual=r.getObject(1);
                        valid=switch(text(spec,"expected_kind")) {
                            case "null" -> actual==null;
                            case "number" -> actual!=null&&r.getBigDecimal(1).compareTo(new BigDecimal(text(spec,"expected")))==0&&(!key.equals("types")||r.getObject(2)==null);
                            default -> Objects.equals(r.getString(1),text(spec,"expected"));
                        };
                    } else if(hasRow) valid=key.equals("types")?r.getBigDecimal(1).compareTo(new BigDecimal("123.45"))==0&&r.getObject(2)==null:r.getInt(1)==(Set.of("recursive_cte","offset_pagination").contains(key)?2:1);
                    if(!valid||r.next())throw new SQLException("unexpected capability probe result","HY000");
                }}
                results.put(key,map("status","verified","method","synthetic_readonly_probe"));
            } catch(SQLException e){results.put(key,map("status","unavailable","sqlstate",e.getSQLState(),"code",e.getErrorCode()));}
            finally {try{c.rollback();}catch(SQLException ignored){}}
        });
        return results;
    }
    static String id(String cat,String schema,String table) { return String.join(".",Arrays.stream(new String[]{cat,schema,table}).filter(s->s!=null&&!s.isEmpty()).toArray(String[]::new)); }
    static List<Map<String,Object>> constraints(DatabaseMetaData m,String cat,String schema,String name, boolean foreign) throws SQLException {
        Map<String,Map<String,Object>> groups=new LinkedHashMap<>();
        try(ResultSet r=foreign?m.getImportedKeys(cat,schema,name):m.getPrimaryKeys(cat,schema,name)) {
            while(r.next()) {
                String key=val(r,foreign?"FK_NAME":"PK_NAME");
                if(key.isEmpty()) {
                    if(foreign)throw new SQLException("unnamed constraint cannot be grouped reliably","0A000");
                    key="primary_key";
                }
                Map<String,Object> group=groups.computeIfAbsent(key,k->map("name",k,"ordered_columns",new TreeMap<Integer,String>()));
                @SuppressWarnings("unchecked") Map<Integer,String> cols=(Map<Integer,String>)group.get("ordered_columns");
                cols.put(r.getInt("KEY_SEQ"),r.getString(foreign?"FKCOLUMN_NAME":"COLUMN_NAME"));
                if(foreign) {
                    group.put("target_table",id(val(r,"PKTABLE_CAT"),val(r,"PKTABLE_SCHEM"),val(r,"PKTABLE_NAME")));
                    @SuppressWarnings("unchecked") Map<Integer,String> target=(Map<Integer,String>)group.computeIfAbsent("ordered_target",k->new TreeMap<Integer,String>());
                    target.put(r.getInt("KEY_SEQ"),r.getString("PKCOLUMN_NAME"));
                    group.put("update_rule",r.getInt("UPDATE_RULE"));group.put("delete_rule",r.getInt("DELETE_RULE"));
                }
            }
        }
        for(Map<String,Object> g:groups.values()) {
            g.put("columns",new ArrayList<>(((Map<?,?>)g.remove("ordered_columns")).values()));
            if(foreign) g.put("target_columns",new ArrayList<>(((Map<?,?>)g.remove("ordered_target")).values()));
        }
        return new ArrayList<>(groups.values());
    }
    static Map<String,Object> scan(Connection c,JsonNode q) throws SQLException {
        DatabaseMetaData m=c.getMetaData(); JsonNode cfg=q.path("connection");
        String cat=text(cfg,"catalog"),schema=text(cfg,"schema_name");
        if(cat.isEmpty())cat=c.getCatalog(); if(schema.isEmpty())schema=null;
        List<Map<String,Object>> tables=new ArrayList<>(),foreignKeys=new ArrayList<>(); List<String> gaps=new ArrayList<>();
        Set<String> selection=new HashSet<>(); q.path("tables").forEach(n->selection.add(n.asText()));
        List<Map<String,Object>> tableRows;
        if(q.path("metadata_queries").has("tables")) {
            tableRows=new ArrayList<>();
            for(Map<String,Object> row:templateRows(c,q,"tables")) {
                if(!(row.get("table_name") instanceof String n)||n.isEmpty())throw new SQLException("tables template requires table_name","0A000");
                tableRows.add(map("table_cat",row.get("catalog"),"table_schem",row.get("schema_name"),"table_name",row.get("table_name"),
                    "table_type",row.getOrDefault("kind","TABLE"),"remarks",row.get("description")));
            }
        } else tableRows=rows(m.getTables(cat,schema,"%",new String[]{"TABLE","VIEW"}));
        for(Map<String,Object> row:tableRows) {
            String tc=(String)row.get("table_cat"),ts=(String)row.get("table_schem"),tn=(String)row.get("table_name");
            String tableId=id(tc,ts,tn);
            if(schema!=null&&!schema.equals(ts))continue;
            if(!selection.isEmpty()&&!selection.contains(tableId)&&!selection.contains(id(null,ts,tn)))continue;
            if(tables.size()>=5000)throw new SQLException("scan budget exceeded","54000");
            Map<String,Object> t=map("id",tableId,"catalog",tc,"schema",ts==null?"":ts,"name",tn,"kind",row.get("table_type"),"description",row.get("remarks"));
            List<Map<String,Object>> cols=new ArrayList<>();
            if(!q.path("metadata_queries").has("columns"))try(ResultSet r=m.getColumns(tc,ts,tn,"%")) {
                while(r.next()) cols.add(map("name",val(r,"COLUMN_NAME"),"type",val(r,"TYPE_NAME"),"raw_type",val(r,"TYPE_NAME"),
                    "jdbc_type",r.getInt("DATA_TYPE"),"precision",r.getInt("COLUMN_SIZE"),"scale",r.getInt("DECIMAL_DIGITS"),
                    "nullable",r.getInt("NULLABLE")!=DatabaseMetaData.columnNoNulls,"description",val(r,"REMARKS"),"sensitivity","internal"));
            } catch(SQLException e){gaps.add(tableId+":columns:"+e.getSQLState());}
            t.put("columns",cols);
            if(!q.path("metadata_queries").has("constraints"))try { List<Map<String,Object>> keys=constraints(m,tc,ts,tn,false);t.put("primary_keys",keys);
                Set<String> names=new HashSet<>();for(Map<String,Object> k:keys) for(Object n:(List<?>)k.get("columns"))names.add((String)n);
                for(Map<String,Object> col:cols)col.put("primary_key",names.contains(col.get("name")));
            }catch(SQLException e){gaps.add(tableId+":primary_keys:"+e.getSQLState());}
            if(!q.path("metadata_queries").has("constraints"))try { List<Map<String,Object>> keys=constraints(m,tc,ts,tn,true);t.put("foreign_keys",keys);
                for(Map<String,Object> k:keys) foreignKeys.add(map("source_table",tableId,"columns",k.get("columns"),"target_table",k.get("target_table"),"target_columns",k.get("target_columns"),"name",k.get("name"),"origin","declared"));
            }catch(SQLException e){gaps.add(tableId+":foreign_keys:"+e.getSQLState());}
            if(!q.path("metadata_queries").has("constraints"))try { Map<String,SortedMap<Integer,String>> indices=new TreeMap<>();Set<String> unsupportedIndices=new HashSet<>();
                // Read existing metadata; exact statistics may invoke DBMS_STATS on Oracle.
                try(ResultSet r=m.getIndexInfo(tc,ts,tn,true,true)) { while(r.next()) {
                    String n=r.getString("INDEX_NAME"),col=r.getString("COLUMN_NAME");
                    String filter=r.getString("FILTER_CONDITION");
                    if(n!=null&&(col==null||(filter!=null&&!filter.isEmpty())))unsupportedIndices.add(n);
                    if(n!=null&&col!=null)indices.computeIfAbsent(n,k->new TreeMap<>()).put(r.getInt("ORDINAL_POSITION"),col);
                }}
                for(String n:unsupportedIndices){indices.remove(n);gaps.add(tableId+":conditional_or_expression_unique_index:"+n);}
                List<Map<String,Object>> unique=new ArrayList<>();indices.forEach((k,v)->unique.add(map("name",k,"columns",new ArrayList<>(v.values()),"origin","unique_index")));t.put("unique_constraints",unique);
            }catch(SQLException e){gaps.add(tableId+":unique_constraints:"+e.getSQLState());}
            if(!q.path("metadata_queries").has("constraints"))try { Map<String,SortedMap<Integer,String>> indices=new TreeMap<>();Map<String,Boolean> uniqueness=new HashMap<>();Set<String> unsupportedIndices=new HashSet<>();
                try(ResultSet r=m.getIndexInfo(tc,ts,tn,false,true)) { while(r.next()) {
                    if(r.getShort("TYPE")==DatabaseMetaData.tableIndexStatistic)continue;
                    String n=r.getString("INDEX_NAME"),col=r.getString("COLUMN_NAME");
                    String filter=r.getString("FILTER_CONDITION");
                    if(n!=null&&(col==null||(filter!=null&&!filter.isEmpty())))unsupportedIndices.add(n);
                    if(n!=null&&col!=null){indices.computeIfAbsent(n,k->new TreeMap<>()).put(r.getInt("ORDINAL_POSITION"),col);uniqueness.put(n,!r.getBoolean("NON_UNIQUE"));}
                }}
                for(String n:unsupportedIndices){indices.remove(n);gaps.add(tableId+":conditional_or_expression_index:"+n);}
                List<Map<String,Object>> allIndices=new ArrayList<>();indices.forEach((k,v)->allIndices.add(map("name",k,"columns",new ArrayList<>(v.values()),"unique",uniqueness.getOrDefault(k,false))));t.put("indexes",allIndices);
            }catch(SQLException e){gaps.add(tableId+":indexes:"+e.getSQLState());}
            tables.add(t);
            try { PROTOCOL.println(JSON.writeValueAsString(map("progress",map("completed_tables",tables.size(),"visible_tables",tableRows.size(),"phase","metadata")))); PROTOCOL.flush(); }
            catch(IOException e) { throw new SQLException("progress serialization failed","HY000"); }
        }
        applyTemplates(c,q,tables,gaps);
        foreignKeys.clear();
        for(Map<String,Object> table:tables) {
            if(((List<?>)table.get("columns")).isEmpty())gaps.add(table.get("id")+":columns:empty_or_inaccessible");
            if(!table.containsKey("primary_keys")||!table.containsKey("foreign_keys")||!table.containsKey("unique_constraints"))gaps.add(table.get("id")+":constraints:incomplete");
            @SuppressWarnings("unchecked") List<Map<String,Object>> pk=(List<Map<String,Object>>)table.getOrDefault("primary_keys",List.of());
            Set<Object> pkNames=new HashSet<>();for(Map<String,Object> k:pk)pkNames.addAll((List<?>)k.getOrDefault("columns",List.of()));
            @SuppressWarnings("unchecked") List<Map<String,Object>> cols=(List<Map<String,Object>>)table.get("columns");
            for(Map<String,Object> col:cols)col.put("primary_key",pkNames.contains(col.get("name")));
            @SuppressWarnings("unchecked") List<Map<String,Object>> keys=(List<Map<String,Object>>)table.getOrDefault("foreign_keys",List.of());
            for(Map<String,Object> k:keys) foreignKeys.add(map("source_table",table.get("id"),"columns",k.get("columns"),"target_table",k.get("target_table"),"target_columns",k.get("target_columns"),"name",k.get("name"),"origin","declared"));
        }
        return map("snapshot_version",2,"tables",tables,"foreign_keys",foreignKeys,"permission_gaps",gaps,"sensitive_columns",List.of());
    }
    static List<Map<String,Object>> templateRows(Connection c,JsonNode q,String key) throws SQLException {
        int timeout=q.path("timeout_ms").asInt(60000);
        if(!c.getAutoCommit())c.rollback();readonly(c,text(q,"strategy"),timeout);
        try(Statement s=c.createStatement()) {
            s.setQueryTimeout(Math.max(1,(timeout+999)/1000));s.setMaxRows(100001);
            return rows(s.executeQuery(q.path("metadata_queries").path(key).asText()));
        }finally{c.rollback();}
    }
    static void applyTemplates(Connection c,JsonNode q,List<Map<String,Object>> tables,List<String> gaps) throws SQLException {
        JsonNode templates=q.path("metadata_queries");if(templates.isEmpty())return;
        // Fixed metadata row contracts. Only administrator-provided read-only
        // SQL results can replace standard metadata, never model-authored fields.
        Iterator<Map.Entry<String,JsonNode>> it=templates.fields();
        while(it.hasNext()) {
            Map.Entry<String,JsonNode> e=it.next();
            if(e.getKey().equals("tables"))continue; // Applied before enumeration.
            {
                List<Map<String,Object>> records=templateRows(c,q,e.getKey());
                for(Map<String,Object> row:records) {
                    if(!row.containsKey("table_id"))throw new SQLException("template requires table_id","0A000");
                    Map<String,Object> table=tables.stream().filter(t->t.get("id").equals(row.get("table_id"))).findFirst().orElse(null);
                    if(table==null)throw new SQLException("template references unknown table","0A000");
                    if(e.getKey().equals("columns")||e.getKey().equals("comments")) {
                        @SuppressWarnings("unchecked") List<Map<String,Object>> cols=(List<Map<String,Object>>)table.get("columns");
                        Map<String,Object> col=cols.stream().filter(v->v.get("name").equals(row.get("column_name"))).findFirst().orElse(null);
                        if(col==null&&e.getKey().equals("columns")) {
                            if(!(row.get("column_name") instanceof String))throw new SQLException("template requires column_name","0A000");
                            if(row.get("nullable")!=null&&!(row.get("nullable") instanceof Boolean))throw new SQLException("nullable must be boolean or null","0A000");
                            col=map("name",row.get("column_name"),"nullable",row.get("nullable"),"precision",row.get("precision"),"scale",row.get("scale"),"sensitivity","internal");cols.add(col);
                        }
                        if(col==null)throw new SQLException("template references unknown column","0A000");
                        if(row.containsKey("description"))col.put("description",row.get("description"));
                        if(e.getKey().equals("columns")) {
                            if(!(row.get("raw_type") instanceof String raw)||raw.isEmpty())throw new SQLException("template requires raw_type","0A000");
                            col.put("raw_type",row.get("raw_type"));col.put("type",row.get("raw_type"));
                        }
                    } else if(e.getKey().equals("constraints")) {
                        // Never flatten compound constraints from arbitrary row sets.
                        if(!row.containsKey("constraints_json"))throw new SQLException("template requires constraints_json","0A000");
                        try {
                            JsonNode constraints=JSON.readTree(Objects.toString(row.get("constraints_json")));
                            for(String k:List.of("primary_keys","unique_constraints","foreign_keys")) {
                                if(!constraints.path(k).isArray())throw new IOException("missing constraint array");
                                for(JsonNode item:constraints.path(k)) {
                                    if(!item.path("columns").isArray()||item.path("columns").isEmpty())throw new IOException("missing compound columns");
                                    for(JsonNode column:item.path("columns"))if(!column.isTextual())throw new IOException("invalid column name");
                                    if(k.equals("foreign_keys")&&(!item.path("target_table").isTextual()||!item.path("target_columns").isArray()||item.path("target_columns").size()!=item.path("columns").size()))throw new IOException("invalid foreign key");
                                }
                                table.put(k,JSON.convertValue(constraints.path(k),List.class));
                            }
                        }catch(IOException ex){throw new SQLException("invalid constraints JSON","0A000");}
                    }
                }
            }
        }
        c.rollback();
    }
    static String duckType(ResultSetMetaData m,int i) throws SQLException {
        if(timezoneType(m,i))return "TIMESTAMPTZ";
        return switch(m.getColumnType(i)) {
            case Types.TINYINT,Types.SMALLINT,Types.INTEGER -> "INTEGER";
            case Types.BIGINT -> "BIGINT";
            case Types.NUMERIC,Types.DECIMAL -> m.getPrecision(i)>0&&m.getPrecision(i)<=38&&m.getScale(i)>=0&&m.getScale(i)<=m.getPrecision(i)?"DECIMAL("+m.getPrecision(i)+","+m.getScale(i)+")":"VARCHAR";
            case Types.FLOAT,Types.REAL,Types.DOUBLE -> "DOUBLE";
            case Types.BOOLEAN,Types.BIT -> "BOOLEAN";
            case Types.DATE -> "DATE";
            case Types.TIMESTAMP -> "TIMESTAMP";
            case Types.TIMESTAMP_WITH_TIMEZONE -> "TIMESTAMPTZ";
            case Types.TIME -> "TIME";
            default -> "VARCHAR";
        };
    }
    static boolean timezoneType(ResultSetMetaData m,int i) throws SQLException {
        String raw=m.getColumnTypeName(i).toLowerCase(Locale.ROOT);
        return m.getColumnType(i)==Types.TIMESTAMP_WITH_TIMEZONE||raw.equals("timestamptz")||raw.equals("datetimeoffset")||raw.contains("time zone");
    }
    static Object scalar(ResultSet r,int i) throws SQLException {
        Object v=r.getObject(i);if(v==null)return null;
        if(timezoneType(r.getMetaData(),i)) {
            try{return r.getObject(i,OffsetDateTime.class).toString();}
            catch(SQLException e){return r.getTimestamp(i,Calendar.getInstance(TimeZone.getTimeZone("UTC"))).toInstant().toString();}
        }
        if(v instanceof BigDecimal d)return d.toPlainString();
        if(v instanceof byte[] b)return Base64.getEncoder().encodeToString(b);
        if(v instanceof java.sql.Timestamp t)return t.toLocalDateTime().toString();
        if(v instanceof java.sql.Date d)return d.toLocalDate().toString();
        if(v instanceof Number||v instanceof Boolean||v instanceof String)return v;
        if(v instanceof Blob||v instanceof Clob||v instanceof SQLXML||v instanceof Array)throw new SQLException("unsupported result type","0A000");
        return v.toString();
    }
    static int jdbcTimeoutSeconds(long timeoutMs) throws SQLException {
        long seconds = Math.max(1, (timeoutMs - 1) / 1000 + 1);
        if (seconds > Integer.MAX_VALUE) throw new SQLException("timeout exceeds JDBC range", "22003");
        return (int) seconds;
    }
    static Map<String,Object> query(Connection c,JsonNode q) throws SQLException,IOException {
        long started=System.nanoTime(),max=q.path("max_rows").asLong(1000),timeout=q.path("timeout_ms").asLong(30000);
        readonly(c,text(q,"strategy"),timeout);
        try(PreparedStatement s=c.prepareStatement(text(q,"sql"))) {
            s.setQueryTimeout(jdbcTimeoutSeconds(timeout));
            if(max < Integer.MAX_VALUE) s.setMaxRows((int) max+1);
            int index=1;
            for(JsonNode p:q.path("parameters")) {
                if(p.isNull())s.setObject(index,null);
                else if(p.isBoolean())s.setBoolean(index,p.asBoolean());
                else if(p.isIntegralNumber())s.setObject(index,p.bigIntegerValue().bitLength()<63?p.longValue():p.decimalValue());
                else if(p.isNumber())s.setBigDecimal(index,p.decimalValue());
                else s.setString(index,p.asText());index++;
            }
            try(ResultSet r=s.executeQuery()) {
                ResultSetMetaData m=r.getMetaData();List<String> columns=new ArrayList<>(),types=new ArrayList<>();List<List<Object>> rows=new ArrayList<>();List<Map<String,Object>> warnings=new ArrayList<>();
                for(int i=1;i<=m.getColumnCount();i++){
                    columns.add(m.getColumnLabel(i));String dtype=duckType(m,i);types.add(dtype);
                    int code=m.getColumnType(i);
                    if(dtype.equals("VARCHAR")&&!Set.of(Types.VARCHAR,Types.CHAR,Types.NVARCHAR,Types.NCHAR,Types.LONGVARCHAR,Types.LONGNVARCHAR).contains(code))
                        warnings.add(map("column",m.getColumnLabel(i),"source_type",m.getColumnTypeName(i),"result_type","VARCHAR","reason","exact_text_transport_without_local_numeric_or_temporal_semantics"));
                }
                long bytes=0,maxBytes=q.path("max_bytes").asLong(10485760);boolean truncated=false;
                while(r.next()) {
                    if(rows.size()==max){truncated=true;break;}
                    List<Object> row=new ArrayList<>();for(int i=1;i<=m.getColumnCount();i++)row.add(scalar(r,i));
                    bytes+=JSON.writeValueAsBytes(row).length;
                    if(bytes>maxBytes)throw new SQLException("result byte budget exceeded","54000"); rows.add(row);
                }
                return map("columns",columns,"column_types",types,"rows",rows,"row_count",rows.size(),"truncated",truncated,"duration_ms",(System.nanoTime()-started)/1000000,
                    "summary",map("transaction","read_only","row_limit",max,"timeout_ms",timeout,"type_warnings",warnings));
            }
        } finally { c.rollback(); }
    }
    public static void main(String[] args) throws Exception {
        PrintStream output=System.out;PROTOCOL=output;
        // Vendor logging must never corrupt the protocol or expose credentials.
        System.setOut(new PrintStream(OutputStream.nullOutputStream()));System.setErr(new PrintStream(OutputStream.nullOutputStream()));
        Map<String,Object> result;
        try {
            JsonNode q=JSON.readTree(System.in);
            Driver selected=(Driver)Class.forName(text(q,"driver_class")).getDeclaredConstructor().newInstance();
            Properties p=new Properties();q.path("connection").path("properties").fields().forEachRemaining(e->p.setProperty(e.getKey(),e.getValue().asText()));
            p.setProperty("user",text(q.path("connection"),"username"));p.setProperty("password",text(q.path("connection"),"password"));
            DriverManager.setLoginTimeout(10);
            Connection opened=selected.connect(text(q,"url"),p);
            if(opened==null)throw new SQLException("selected driver did not accept URL","08001");
            try(Connection c=opened) {
                result=switch(text(q,"operation")) {
                    case "test" -> inspect(c,q);
                    case "scan" -> { Map<String,Object> snapshot=scan(c,q);snapshot.put("connection",inspect(c,q));yield snapshot; }
                    case "query" -> query(c,q);
                    default -> throw new SQLException("operation unsupported","0A000");
                };
            }
            result=map("ok",true,"result",result);
        } catch(Throwable e) {
            SQLException sql=e instanceof SQLException?(SQLException)e:null;
            result=map("ok",false,"sqlstate",sql==null?null:sql.getSQLState(),"code",sql==null?null:sql.getErrorCode(),"error_class",e.getClass().getSimpleName(),"sql_message",sql==null?null:sql.getMessage());
        }
        output.println(JSON.writeValueAsString(result));output.flush();
    }
}
