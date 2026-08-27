"""Register DataSage as a read-only capability plugin for Hermes."""

from . import (
    contracts,
    entitlements,
    entities,
    schemas,
    tools,
    wire,
)


DATASAGE_EVIDENCE_BOUNDARIES = """datasage.answer-boundary/v1: references/answer-boundary.md
When answering from DataSage tool evidence:
- Preserve each result’s metric, unit, period, population, scope, typed state, truncation/has_more, and local failure. Do not widen partial or Top-N evidence to a total-population claim.
- Report cross-metric or cross-population facts separately unless returned evidence explicitly proves scope compatibility.
- Returned governed target_status authorizes only target status; without a compatible governed benchmark, do not make qualitative performance, health, or risk judgments.
- State cause, driver, contribution, or offset only from explicitly authorized, reconciled evidence; arithmetic relationships alone are not business mechanisms.
- Absolute receivable/overdue proximity does not establish equal risk. Do not infer profitability or overall health when those metrics are unavailable."""


def register(ctx) -> None:
    ctx.register_system_prompt_section(
        "datasage.evidence-boundaries",
        DATASAGE_EVIDENCE_BOUNDARIES,
        position="after_memory",
        max_chars=900,
    )
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
        name="datasage_query",
        toolset="datasage-query",
        schema=schemas.DATASAGE_QUERY,
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
