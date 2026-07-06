"""Builtin tools that let the orchestrator build & run file-transfer integrations
(the Transfer-Hub port). With these the assistant can: discover connectors, define
hosts/credentials, compose a route from FROM/TO endpoints, test connectivity, and
run it. Paired with the `transfer_route_building` skill (instructions).
"""
from __future__ import annotations

from typing import Any, Dict, List

from component.logging import get_logger
from services.builtin.internal_tool_registry import internal_tool_registry

log = get_logger(__name__)


def _svc():
    from db.database import SessionLocal
    from services.transfer.transfer_service import TransferService
    db = SessionLocal()
    return db, TransferService(db)


@internal_tool_registry.register(
    name="transfer_list_connectors",
    title="List transfer connectors",
    description="List available transfer connectors (protocols like file/sftp/s3/azure-storage-blob/sharepoint) and the credential type each expects. Use before building a route.",
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "transfer"],
)
async def transfer_list_connectors(args: Dict[str, Any]) -> Any:
    from services.transfer.connectors import CONNECTORS
    return [{"protocol": p, "fields": c.fields, "credential_type": c.credential_type} for p, c in sorted(CONNECTORS.items())]


@internal_tool_registry.register(
    name="transfer_list_inventory",
    title="List transfer hosts/credentials/routes",
    description="List the configured transfer hosts, credentials (no secrets) and routes, so you can reference them by id when building or running a route.",
    input_schema={"type": "object", "properties": {}},
    tags=["internal", "transfer"],
)
async def transfer_list_inventory(args: Dict[str, Any]) -> Any:
    db, svc = _svc()
    try:
        return {
            "hosts": [h.model_dump() for h in svc.list_hosts()],
            "credentials": [c.model_dump() for c in svc.list_credentials()],
            "routes": [r.model_dump() for r in svc.list_records()],
            "parameters": [p.model_dump() for p in svc.list_parameters()],
        }
    finally:
        db.close()


@internal_tool_registry.register(
    name="transfer_create_host",
    title="Create a transfer host",
    description="Create a host (server/account) a transfer endpoint connects to. For the 'file' connector the host is just a label.",
    input_schema={"type": "object", "properties": {
        "hostname": {"type": "string"}, "port": {"type": "integer"}, "description": {"type": "string"},
    }, "required": ["hostname"]},
    tags=["internal", "transfer"],
)
async def transfer_create_host(args: Dict[str, Any]) -> Any:
    from schemas.transfer import HostCreate
    db, svc = _svc()
    try:
        return svc.create_host(HostCreate(hostname=args["hostname"], port=args.get("port"), description=args.get("description"))).model_dump()
    finally:
        db.close()


@internal_tool_registry.register(
    name="transfer_create_credential",
    title="Create a transfer credential",
    description="Create a credential (secrets stored encrypted). credential_type one of SFTP|FILE|OAUTH|SAS_TOKEN|ACCESS_KEY. Provide only the relevant secret fields (e.g. username/password for SFTP; username=account + password=key for ACCESS_KEY/S3; token for SAS_TOKEN; tenant_id/client_id/client_secret for OAUTH).",
    input_schema={"type": "object", "properties": {
        "credential_type": {"type": "string"}, "name": {"type": "string"}, "username": {"type": "string"},
        "password": {"type": "string"}, "token": {"type": "string"}, "client_id": {"type": "string"},
        "client_secret": {"type": "string"}, "tenant_id": {"type": "string"},
        "private_key": {"type": "string"}, "key_phrase": {"type": "string"}, "public_key": {"type": "string"},
    }, "required": ["credential_type"]},
    tags=["internal", "transfer"],
)
async def transfer_create_credential(args: Dict[str, Any]) -> Any:
    from schemas.transfer import CredentialCreate
    db, svc = _svc()
    try:
        return svc.create_credential(CredentialCreate(**{k: v for k, v in args.items() if v is not None})).model_dump()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@internal_tool_registry.register(
    name="transfer_create_route",
    title="Create a transfer route",
    description="Create a route from endpoints. Each endpoint: {direction: FROM|TO, protocol, host_id, credential_id (optional), path, parameter (optional JSON string)}. Reference host_id/credential_id from transfer_list_inventory.",
    input_schema={"type": "object", "properties": {
        "description": {"type": "string"},
        "endpoints": {"type": "array", "items": {"type": "object"}},
    }, "required": ["endpoints"]},
    tags=["internal", "transfer"],
)
async def transfer_create_route(args: Dict[str, Any]) -> Any:
    from schemas.transfer import TransferRecordCreate, EndpointCreate
    db, svc = _svc()
    try:
        eps = [EndpointCreate(**e) for e in args.get("endpoints", [])]
        return svc.create_record(TransferRecordCreate(description=args.get("description"), endpoints=eps)).model_dump()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@internal_tool_registry.register(
    name="transfer_test_endpoint",
    title="Test a transfer endpoint",
    description="Test connectivity to an endpoint config before building/running a route: {protocol, host_id, credential_id (optional), path}.",
    input_schema={"type": "object", "properties": {
        "protocol": {"type": "string"}, "host_id": {"type": "integer"},
        "credential_id": {"type": "integer"}, "path": {"type": "string"},
    }, "required": ["protocol", "host_id"]},
    tags=["internal", "transfer"],
)
async def transfer_test_endpoint(args: Dict[str, Any]) -> Any:
    from services.transfer import transfer_engine
    db, _ = _svc()
    try:
        return transfer_engine.test_endpoint(db, protocol=args["protocol"], host_id=args["host_id"],
                                             credential_id=args.get("credential_id"), path=args.get("path"))
    finally:
        db.close()


@internal_tool_registry.register(
    name="transfer_define_connector",
    title="Define a new connector type",
    description=(
        "Add a NEW connector type at runtime (declarative — no code). kind 'fsspec': "
        "bind any fsspec protocol (e.g. 'gcs','abfs') — needs its backend installed. "
        "kind 'rest': a templated HTTP connector, config={base_url, read_path, write_path, "
        "write_method, list_path, delete_path, auth_header, auth_template (e.g. 'Bearer {token}'), headers}. "
        "Templates may use {path} and credential fields. After defining, use it like any protocol."
    ),
    input_schema={"type": "object", "properties": {
        "protocol": {"type": "string"}, "kind": {"type": "string", "enum": ["fsspec", "rest"]},
        "config": {"type": "object"}, "description": {"type": "string"},
    }, "required": ["protocol", "kind"]},
    tags=["internal", "transfer"],
)
async def transfer_define_connector(args: Dict[str, Any]) -> Any:
    from schemas.transfer import ConnectorDefCreate
    db, svc = _svc()
    try:
        d = svc.create_connector_def(ConnectorDefCreate(
            protocol=args["protocol"], kind=args["kind"], config=args.get("config"), description=args.get("description")))
        return {"status": "ok", "connector": d.model_dump()}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@internal_tool_registry.register(
    name="transfer_run_route",
    title="Run a transfer route now",
    description="Execute a route by id (reads FROM endpoints, writes to TO endpoints). Returns what was transferred or the error.",
    input_schema={"type": "object", "properties": {"record_id": {"type": "string"}}, "required": ["record_id"]},
    tags=["internal", "transfer"],
)
async def transfer_run_route(args: Dict[str, Any]) -> Any:
    from services.transfer import transfer_engine
    db, _ = _svc()
    try:
        return transfer_engine.run_record(db, args["record_id"])
    finally:
        db.close()
