import pytest
from fastapi import HTTPException

from app.auth import Principal, enforce_roles, required_roles


def test_only_admin_can_delete_resources_but_stewards_can_unbind():
    assert required_roles("DELETE", "/api/v1/datasources/source") == frozenset({"admin"})
    assert required_roles("DELETE", "/api/v1/ontology/domains/domain") == frozenset({"admin"})
    assert "data_steward" in required_roles(
        "DELETE", "/api/v1/ontology/configurations/domain/sources/source"
    )


def principal(*roles: str) -> Principal:
    return Principal(subject="u-1", username="user", roles=frozenset(roles), claims={})


def test_rbac_policy_uses_least_privilege_roles():
    assert required_roles("PUT", "/api/v1/settings") == frozenset({"admin"})
    assert required_roles("POST", "/api/v1/approvals/id/decision") == frozenset({"approver", "admin"})
    assert required_roles("POST", "/api/v1/ontology/publish") == frozenset({"data_steward", "admin"})
    assert required_roles("GET", "/api/v1/audits/run") == frozenset({"auditor", "admin"})
    assert "business_user" in required_roles("GET", "/api/v1/domains")


def test_rbac_allows_appropriate_role_and_admin():
    enforce_roles(principal("approver"), "POST", "/api/v1/approvals/id/decision")
    enforce_roles(principal("admin"), "PUT", "/api/v1/settings")


def test_rbac_rejects_analyst_from_admin_operation():
    with pytest.raises(HTTPException) as error:
        enforce_roles(principal("analyst"), "PUT", "/api/v1/settings")
    assert error.value.status_code == 403
