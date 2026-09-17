"""Register DataSage as a read-only capability plugin for Hermes."""

from . import (
    contract_store,
    contracts,
    entitlements,
    entities,
    schemas,
    settings,
    tools,
    wire,
)


def register(ctx) -> None:
    contract_store.pin_contract_snapshot()
    settings.bind_config_reader(ctx.get_config)
    ctx.register_tool(
        name="datasage_catalog",
        toolset="datasage-query",
        schema=schemas.model_tool_schema(schemas.DATASAGE_CATALOG),
        handler=entitlements.guard(
            "datasage_catalog",
            wire.bounded_json_handler("datasage_catalog", contracts.datasage_catalog),
        ),
        description=schemas.DATASAGE_CATALOG["description"],
    )
    ctx.register_tool(
        name="datasage_entity_resolve",
        toolset="datasage-query",
        schema=schemas.model_tool_schema(schemas.DATASAGE_ENTITY_RESOLVE),
        handler=wire.bounded_json_handler(
            "datasage_entity_resolve",
            entitlements.guard("datasage_entity_resolve", tools.datasage_entity_resolve),
        ),
        description=schemas.DATASAGE_ENTITY_RESOLVE["description"],
    )
    ctx.register_tool(
        name="datasage_query",
        toolset="datasage-query",
        schema=schemas.model_tool_schema(schemas.DATASAGE_QUERY),
        handler=wire.bounded_json_handler(
            "datasage_query",
            tools.entitlement_guarded_datasage_query,
        ),
        requires_env=[
            "DATA_QUERY_MYSQL_HOST",
            "DATA_QUERY_MYSQL_DATABASE",
            "DATA_QUERY_MYSQL_USER",
            "DATA_QUERY_MYSQL_PASSWORD",
        ],
        description=schemas.DATASAGE_QUERY["description"],
    )
    from .runtime_health import record_plugin_initialization
    record_plugin_initialization(register.__code__)
