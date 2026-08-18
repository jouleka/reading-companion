"""Release-blocking owner-scope and operation-inventory gates for LIT-48."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

from app.hosted.tenant.repository import PostgresTenantRepository

INVENTORY = Path(__file__).parents[2] / "app" / "hosted" / "tenant" / "endpoints.json"


def test_every_public_tenant_repository_method_requires_owner_id() -> None:
    infrastructure_methods = {"check_runtime_role"}
    methods = {
        name: member
        for name, member in inspect.getmembers(PostgresTenantRepository, inspect.isfunction)
        if not name.startswith("_") and name not in infrastructure_methods
    }
    assert methods
    for name, method in methods.items():
        parameters = inspect.signature(method).parameters
        assert "owner_id" in parameters, f"{name} is missing the explicit owner boundary"


def test_endpoint_inventory_names_cross_tenant_evidence_and_unavailable_surfaces() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert inventory["version"] == 12
    assert all(item["cross_tenant_test"] for item in inventory["enabled"])
    assert all(item["cross_tenant_test"] for item in inventory["unavailable"])
    assert all(item["cross_tenant_test"] for item in inventory["background"])
    evidence_files = Path(__file__).parent.glob("test_*.py")
    evidence_names = {
        node.name
        for evidence_file in evidence_files
        for node in ast.walk(ast.parse(evidence_file.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    named_evidence = {
        item["cross_tenant_test"]
        for group in ("enabled", "unavailable", "background")
        for item in inventory[group]
    }
    assert named_evidence <= evidence_names
    surfaces = {
        item["surface"] for group in ("enabled", "unavailable") for item in inventory[group]
    }
    assert surfaces == set(inventory["required_surface_classes"])
    assert {item["resource"] for item in inventory["background"]} == {
        "worker-claim-and-mutations",
        "worker-credential-resolution",
        "filesystem-source-storage",
        "s3-source-storage",
        "runtime-cache-and-locks",
    }
    unavailable = {item["access"] for item in inventory["unavailable"]}
    assert unavailable == {"export"}
