# Semora

**Ontology-grounded analytics for relational data.**

English · [简体中文](README.zh-CN.md)

Semora helps teams query relational data in natural language. It organizes data sources into business domains, clarifies ambiguous requests, and returns read-only query results with SQL and execution records.

This repository contains a product preview and a runnable example with synthetic data. The Semora application and services are maintained privately.

## Screenshots

The screenshots use synthetic data.

![Query Workbench](docs/images/workbench-demo.png)

![Ontology Studio](docs/images/ontology-demo.png)

## Run the example

Run this standalone SQL example with Python 3. It uses synthetic orders to calculate revenue and order count by channel, without requiring a database service, model, or API key.

```sh
python3 examples/sales_summary.py
```

## Workflows

### Schema scanning and relationship analysis

```mermaid
flowchart LR
    A["Add a data source"] --> B["Scan table metadata"]
    B --> C["Start relationship analysis"]
    C --> D{"Validate candidate relationships"}
    D -->|Verified| E["Publish to the ontology"]
    D -->|Insufficient evidence| F["Keep pending"]
    D -->|Disproved| G["Exclude"]
```

### Natural-language queries

```mermaid
flowchart LR
    A["Submit a business question"] --> B["Interpret the request"]
    B -->|Ambiguous| C["Ask the user to clarify"]
    C --> B
    B -->|Clear| D["Generate and validate SQL"]
    D --> E["Execute read-only"]
    E --> F["Return results and execution records"]
```

## License

The materials published in this repository are available under the [MIT License](LICENSE).

See the [contribution terms](CONTRIBUTING.md) for licensing and attribution of pull requests and the [name and logo policy](TRADEMARKS.md) for use of the Semora identity.
