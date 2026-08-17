"""Register DataSage as a read-only capability plugin for Hermes."""

from . import (
    contracts,
    entitlements,
    entities,
    references,
    runtime_health,
    schemas,
    skill_prompt,
    tools,
    wire,
)


def register(ctx) -> None:
    runtime_health.record_startup_health()
    ctx.register_hook("pre_llm_call", skill_prompt.frozen_wecom_skill_hook())
    ctx.register_tool(
        name="datasage_catalog",
        toolset="datasage-query",
        schema=schemas.DATASAGE_CATALOG,
        handler=entitlements.guard(
            "datasage_catalog",
            wire.bounded_json_handler("datasage_catalog", contracts.datasage_catalog),
        ),
        description=schemas.DATASAGE_CATALOG["description"],
    )
    ctx.register_tool(
        name="datasage_entity_resolve",
        toolset="datasage-query",
        schema=schemas.DATASAGE_ENTITY_RESOLVE,
        handler=entitlements.guard(
            "datasage_entity_resolve",
            wire.bounded_json_handler(
                "datasage_entity_resolve",
                entities.datasage_entity_resolve,
            ),
        ),
        description=schemas.DATASAGE_ENTITY_RESOLVE["description"],
    )
    ctx.register_tool(
        name="datasage_reference",
        toolset="datasage-query",
        schema=schemas.DATASAGE_REFERENCE,
        handler=entitlements.guard(
            "datasage_reference",
            wire.bounded_json_handler(
                "datasage_reference",
                references.datasage_reference,
            ),
        ),
        description=schemas.DATASAGE_REFERENCE["description"],
    )
    ctx.register_tool(
        name="datasage_query",
        toolset="datasage-query",
        schema=schemas.DATASAGE_QUERY,
        handler=entitlements.guard(
            "datasage_query",
            wire.bounded_json_handler(
                "datasage_query",
                tools.runtime_guarded_datasage_query,
            ),
        ),
        requires_env=[
            "DATA_QUERY_MYSQL_HOST",
            "DATA_QUERY_MYSQL_DATABASE",
            "DATA_QUERY_MYSQL_USER",
            "DATA_QUERY_MYSQL_PASSWORD",
        ],
        description=schemas.DATASAGE_QUERY["description"],
    )
