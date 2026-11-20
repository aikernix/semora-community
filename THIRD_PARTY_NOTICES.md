# Third-party notices

Semora 的业务功能由本项目维护。以下列出主要运行依赖与可选集成，不是业务代码来源清单；第三方许可适用于各自组件，完整许可与版权信息随软件包保留。

| 项目 | 用途 | 许可证 |
|---|---|---|
| Tencent WeKnora | 可选外部知识服务 API 集成，不随运行包分发 | MIT |
| TDesign Vue Next | Web UI 组件 | MIT |
| SQLGlot | SQL AST 解析与方言转换 | MIT |
| DuckDB 1.5.6 | 进程内、临时只读查询的跨源结果关联与汇总；不增加常驻服务 | MIT |
| LangGraph | Agent 状态编排 | MIT |
| LangChain 1.4.8 / LangChain Core 1.7.4 | 工具调用 Agent 循环与结构化交付 | MIT |
| LangGraph PostgreSQL Checkpointer | 查询状态与业务澄清中断持久化 | MIT |
| pgvector | PostgreSQL 向量类型与索引 | PostgreSQL License |
| OpenTelemetry Python / Instrumentation 1.45.2 / 0.66b2 | 跨服务 OTLP Trace 与 W3C Trace Context 传播 | Apache-2.0 |
| pgx | Go PostgreSQL 驱动 | MIT |


# Compiled delivery build tool

The optional compiled-delivery pipeline uses Cython 3.3.1 (Apache-2.0) as a build
tool, not as a bundled runtime dependency. Source: https://github.com/cython/cython
and https://github.com/cython/cython/blob/3.3.1/COPYING.txt.
Generated-code notices in native modules must be retained. Compiling owned modules
does not change the licenses or notice obligations of their dependencies.


## Database adapters and MDL export (2026-11-10)

- Wren Core Python 0.8.2: Apache-2.0; used exclusively for exported MDL validation, not query planning. https://github.com/Canner/WrenAI/tree/main/core/wren-core-py
- Unmodified Wren MDL schema at commit `548eee3e864b11e7f11eee15ae82be178a295f22`: Apache-2.0 under upstream `core/**` license map. Source schema and license are retained in the API image at `/app/app/vendor/`.
- JSON Schema Python 4.23.0: MIT; https://github.com/python-jsonschema/jsonschema.
- Jackson Databind 2.18.2 (including Jackson core/annotations): Apache-2.0; https://github.com/FasterXML/jackson-databind.
- Oracle JDBC ojdbc11 23.6.0.24.10: Oracle Free Use Terms and Conditions (FUTC); vendor artifact and license metadata retained in JAR. https://www.oracle.com/database/technologies/appdev/jdbc-downloads.html
- Microsoft JDBC 12.8.1.jre11: MIT; https://github.com/microsoft/mssql-jdbc.
- OceanBase Connector/J 2.4.13: LGPL-2.1; https://github.com/oceanbase/obconnector-j. Shipped unmodified as a separate JAR with license metadata; source available from the linked upstream repository. No driver is statically linked into the Worker.
- Custom JDBC bundles are administrator-supplied; Semora retains hashes and versions. Their own licenses and dependencies remain applicable.

JRE and Maven base images retain their bundled third-party notices. Java Worker is Semora-owned code and communicates through JSON; database credentials are not command-line arguments.
