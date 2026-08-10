"""Register DataSage as a read-only capability plugin for Hermes."""

from . import (
    contracts,
    entities,
    runtime_health,
    schemas,
    tools,
)


def register(ctx) -> None:
    runtime_health.record_startup_health()
    ctx.register_tool(
        name="datasage_catalog",
        toolset="datasage-query",
        schema=schemas.DATASAGE_CATALOG,
        handler=contracts.datasage_catalog,
        description=schemas.DATASAGE_CATALOG["description"],
    )
    ctx.register_tool(
        name="datasage_entity_resolve",
        toolset="datasage-query",
        schema=schemas.DATASAGE_ENTITY_RESOLVE,
        handler=entities.datasage_entity_resolve,
        description=schemas.DATASAGE_ENTITY_RESOLVE["description"],
    )
    ctx.register_tool(
        name="datasage_query",
        toolset="datasage-query",
        schema=schemas.DATASAGE_QUERY,
        handler=tools.runtime_guarded_datasage_query,
        requires_env=[
            "DATA_QUERY_MYSQL_HOST",
            "DATA_QUERY_MYSQL_DATABASE",
            "DATA_QUERY_MYSQL_USER",
            "DATA_QUERY_MYSQL_PASSWORD",
        ],
        description=schemas.DATASAGE_QUERY["description"],
    )
