"""Register DataSage as a read-only capability plugin for Hermes."""

from . import (
    answer_guard,
    contracts,
    entitlements,
    entities,
    references,
    schemas,
    tools,
    wire,
)


def register(ctx) -> None:
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
    # Keep the final-answer gate scoped to turns that actually executed a
    # DataSage query.  The post-tool hook stores only compact evidence
    # constraints; the final transform removes unsupported clauses before
    # local WeCom delivery. Hermes persists the raw draft before this hook, so
    # the bounded next-turn correction is durable compensation, not a rewrite
    # of canonical session history.
    ctx.register_hook("post_tool_call", answer_guard.capture_query_evidence)
    ctx.register_hook("transform_llm_output", answer_guard.transform_guarded_output)
    ctx.register_hook("pre_llm_call", answer_guard.inject_previous_guard_context)
    ctx.register_hook("on_session_end", answer_guard.clear_pending_turn)
    ctx.register_hook("on_session_finalize", answer_guard.clear_session)
    ctx.register_hook("on_session_reset", answer_guard.clear_session)
