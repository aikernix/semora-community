from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_executor_module():
    path = Path(__file__).parents[2] / "executor" / "app" / "main.py"
    spec = spec_from_file_location("services.executor.app.semora_executor_main", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sensitive_values_are_masked_before_leaving_executor():
    executor = _load_executor_module()

    for column,value in [('name','张明'),('mobile','13800001001'),('id_no','310101199001011234'),
                         ('customer_name','张明'),('customer_mobile','13800001001'),('customer_id_no','310101199001011234')]:
        assert executor.mask_sensitive(column,value)==value
        assert executor.mask_sensitive(column,value,{column.upper()})!=value
    assert executor.mask_sensitive('name','张明',{'name'})=='张*'
    assert executor.mask_sensitive('name',None,{'name'}) is None
    assert executor.mask_sensitive("account_number", "622200001234", {"account_number"}) == "6***********"
    assert executor.mask_sensitive("branch_name", "上海分行") == "上海分行"
    assert executor.mask_sensitive("loan_no", "L20260001") == "L20260001"
