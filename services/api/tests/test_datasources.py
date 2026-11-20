from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app.datasources import DataSourceRepository


def _load_executor_module():
    path = Path(__file__).parents[2] / "executor" / "app" / "main.py"
    spec = spec_from_file_location("services.executor.app.semora_executor_datasources", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_seeds_demo_and_persists_scan_state_in_memory():
    repository = DataSourceRepository(dsn="")
    item = repository.get("retail-credit-pg")

    assert item.secret_ref == "DATASOURCE_RETAIL_CREDIT_PG_URL"
    repository.record_scan("retail-credit-pg", {"tables": [{"name": "loans",'columns':[{'name':'id','type':'bigint'}]}], 'status':'completed',
        'scan_manifest':{'complete':True,'scope':'full','scanned_tables':1}})

    updated = repository.get("retail-credit-pg")
    assert updated.status == "healthy"
    assert updated.access == "read_only"
    assert updated.objects == 1
    assert updated.last_scan_at is not None


def test_sensitivity_classification_is_deterministic():
    executor = _load_executor_module()

    assert executor.sensitivity_level("customers", "id_no") == "internal"
    assert executor.sensitivity_level("customers", "mobile") == "internal"
    assert executor.sensitivity_level("arbitrary", "contact_email") == "internal"
    assert executor.sensitivity_level("loans", "outstanding_balance") == "internal"
