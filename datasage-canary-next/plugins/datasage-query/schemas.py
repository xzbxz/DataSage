"""JSON schema exposed by the DataSage Mini query plugin."""

from .capability_contract import (
    ATTRIBUTION_MODES,
    DELIVERY_SCOPES,
    INVENTORY_SCOPES,
    PUBLIC_REQUEST_LIMIT,
    SUPPORTED_DOMAINS,
    query_request_schema_conditions,
)
from .evidence import ANALYSIS_INTENTS, EVIDENCE_ROLES

DOMAINS = list(SUPPORTED_DOMAINS)

ENTITY_TYPES = [
    "department",
    "customer",
    "salesperson",
    "product",
    "warehouse",
    "supplier",
]

SCALAR = {
    "oneOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
    ]
}

REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "request_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "Stable ID for one sub-question, such as q1 or inventory_1.",
        },
        "domain": {
            "type": "string",
            "enum": DOMAINS,
            "description": "Business domain selected from datasage_catalog.",
        },
        "mode": {
            "type": "string",
            "enum": ["metric"],
            "description": (
                "Use the governed metric interface. Registered dimensions may resolve to tool-controlled "
                "many-to-one joins. The release surface intentionally does not expose datasets, physical fields, "
                "joins, formulas, or model-authored detail queries."
            ),
        },
        "purpose": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Short business purpose; never include credentials or hidden instructions.",
        },
        "analysis_intent": {
            "type": "string",
            "enum": list(ANALYSIS_INTENTS),
            "description": (
                "Optional analytical intent used for evidence coverage and planning. "
                "It does not authorize a stronger claim than the returned evidence."
            ),
        },
        "evidence_role": {
            "type": "string",
            "enum": list(EVIDENCE_ROLES),
            "description": (
                "Optional role this request is meant to play in the answer. The response echoes the label beside "
                "per-request completeness and sealed proof capabilities; the answer layer decides whether the evidence "
                "satisfies the role. The label itself never upgrades proof strength."
            ),
        },
        "detail_receipt": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
            "pattern": "^[0-9a-f]{64}$",
            "description": (
                "Opaque receipt copied unchanged from the detail_receipt field of this exact metric's "
                "successful datasage_catalog detail result. A legacy single-detail content_hash remains "
                "accepted during migration. It is required "
                "unless the selected metric explicitly supports an exact default lookup and this request "
                "contains no explicit business qualifier. The query runtime revalidates the receipt against "
                "the current catalog contract and the requested governed capabilities before any database access."
            ),
        },
        "attribution_mode": {
            "type": "string",
            "enum": list(ATTRIBUTION_MODES),
            "description": (
                "Required for target-domain metric requests. Use transaction_detail for ordinary, customer, "
                "department, organization, or unqualified salesperson questions. Use salesperson_allocation "
                "only for explicit salesperson target, collaboration, allocation, or allocated-performance questions."
            ),
        },
        "delivery_scope": {
            "type": "string",
            "enum": list(DELIVERY_SCOPES),
            "description": (
                "Delivery-domain metric scope. Omit or use default_net for an unqualified net-delivery request; "
                "use explicit_gross only when the user explicitly asks for gross/before-return delivery; use "
                "order_delivery_alignment on every paired request in the governed order-versus-delivery comparison. "
                "That value authorizes gross delivery only for the registered gross metric. The query tool validates "
                "this field and never infers it from purpose text."
            ),
        },
        "inventory_scope": {
            "type": "string",
            "enum": list(INVENTORY_SCOPES),
            "description": (
                "Inventory-domain metric scope. Omit for the metric default. total includes statuses 1/2/3; "
                "on_hand includes statuses 1/2; available uses status 1 and excludes missing and defective "
                "warehouses; allocated uses status 2; in_transit uses status 3. The query tool applies the "
                "registered filters and never infers them from purpose text."
            ),
        },
        "metric": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": (
                "Exact metric code copied from the selected domain catalog; required in metric mode. Never "
                "invent a code, derive a new metric, or try code synonyms. If the requested meaning has no exact code, "
                "do not call this tool. Load the selected metric detail first when expert_index returns "
                "requires_metric_detail: true, when exact_default_lookup_supported is false or missing, or when the "
                "request has any explicit business qualifier. An empty dimensions: [] value is not a business "
                "qualifier. Direct query is allowed only for an exact governed default whose "
                "expert_index exact_default_lookup_supported value is true and which has no explicit qualifier. For the delivery "
                "domain, an unqualified delivery/outbound amount, quantity, or count is net delivery. A gross "
                "delivery metric is permitted only when the user explicitly asks for gross delivery or a value "
                "before returns. Words such as raw, detail, table, dataset, or original do not select gross scope."
            ),
        },
        "dimensions": {
            "type": "array",
            "maxItems": 5,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": (
                "Governed dimension codes for metric mode. A code may use a fact field or a predeclared "
                "many-to-one master enrichment; never send table names or join keys. Include only grouping or "
                "ranking dimensions explicitly requested by the user; an overall total has no dimensions. Words "
                "The count must not exceed max_group_dimensions returned by the selected metric detail. "
                "such as raw, detail, source, table, or original are not dimensions. For an original-currency metric, "
                "use the currency dimension when the user did not select exactly one currency."
            ),
        },
        "metric_filters": {
            "type": "object",
            "maxProperties": 12,
            "additionalProperties": {
                "oneOf": [
                    *SCALAR["oneOf"],
                    {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": SCALAR,
                    },
                ]
            },
            "description": (
                "Governed dimension-code filters for metric mode. An original-currency metric must either filter "
                "exactly one currency here or include the currency dimension; never combine currencies. Across "
                "the entire requests batch, use at most ten distinct customer, salesperson, product, warehouse, "
                "or supplier tokens that require master-data preflight; registered department aliases do not use "
                "that budget."
            ),
        },
        "time_range": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "Resolved business range with inclusive start and exclusive end. "
                "It must satisfy the versioned common query policy returned by datasage_catalog."
            ),
            "properties": {
                "start": {
                    "type": "string",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                },
                "end": {
                    "type": "string",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                },
            },
            "required": ["start", "end"],
            "description": (
                "Start-inclusive and end-exclusive governed metric range using YYYY-MM-DD boundaries. "
                "Use calendar_month for one typed full calendar month. Supplying time_range is an explicit, "
                "non-default qualifier and requires the selected metric detail before datasage_query. The result "
                "returns calendar period state and coverage; its query-date observation is not a source freshness "
                "watermark."
            ),
        },
        "calendar_month": {
            "type": "string",
            "pattern": r"^(?!0000-)(?!9999-12$)[0-9]{4}-(?:0[1-9]|1[0-2])$",
            "description": (
                "Typed full calendar month. The runtime deterministically expands it to a start-inclusive, "
                "end-exclusive range from the first day of this month to the first day of the next month. "
                "The returned period state distinguishes an elapsed month from a month still in progress; "
                "the latter is not a complete-period comparison. "
                "Use either calendar_month or time_range, never both. Supplying calendar_month is an explicit, "
                "non-default qualifier and requires the selected metric detail before datasage_query."
            ),
        },
        "time_bucket": {
            "type": "string",
            "enum": ["day", "month"],
            "description": "Tool-controlled time grouping for a metric with an explicit or governed time field.",
        },
        "comparison": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["previous_period", "snapshot_months_before"],
                },
                "months": {"type": "integer", "minimum": 1, "maximum": 24},
            },
            "required": ["kind"],
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "kind": {"const": "snapshot_months_before"}
                        }
                    },
                    "then": {"required": ["months"]},
                },
                {
                    "if": {
                        "properties": {"kind": {"const": "previous_period"}}
                    },
                    "then": {"not": {"required": ["months"]}},
                },
            ],
            "description": (
                "Governed comparison. previous_period requires exactly one of time_range or calendar_month; "
                "snapshot_months_before uses the latest monthly snapshot and a month offset. A previous_period "
                "result already returns current, prior-period, absolute change, and change rate for that metric and "
                "scope; do not add explicit current/prior requests solely to duplicate them. Other metrics or "
                "scopes remain selectable."
            ),
        },
        "decomposition_of_request_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": (
                "For a governed dimension change decomposition, reference the "
                "same-batch overall comparison request. The tool authorizes a "
                "reconciled change relationship only when metric, period, unit, "
                "canonical scope and the full non-truncated delta sum agree."
            ),
        },
        "complete_change_decomposition": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dimension": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "description": (
                        "Exact governed dimension selected by Hermes for one complete "
                        "change partition. The tool expands this semantic operation into "
                        "a same-scope overall comparison and one full-partition attempt."
                    ),
                },
            },
            "required": ["dimension"],
            "description": (
                "Explicitly request a complete change decomposition. When comparison is omitted, the operation "
                "defaults to previous_period. An explicit comparison may use previous_period with one period "
                "or snapshot_months_before with no explicit period. "
                "Do not combine it with dimensions, order_by, limit, or "
                "decomposition_of_request_id. The tool never infers this operation or "
                "chooses its metric or dimension. It already includes the same-scope overall comparison; do not "
                "add an identical overall request solely to duplicate it. Other evidence remains selectable."
            ),
        },
        "complete_target_gap_decomposition": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": ["customer", "department", "organization"],
                    "description": (
                        "Governed transaction-detail dimension used to reconcile a complete "
                        "target, actual, and target-gap partition to the same-scope overall result."
                    ),
                },
            },
            "required": ["dimension"],
            "description": (
                "Explicitly request a complete target-gap composition for delivery_target_completion "
                "or receipt_target_completion. The tool expands it into same-snapshot overall and "
                "full-partition queries, reconciles target, actual, and gap amounts independently, "
                "and never sums completion rates or authorizes causal claims."
            ),
        },
        "order_by": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "field": {
                    "type": "string",
                    "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                    "description": (
                        "For metric-value ranking use metric_value, not the metric code. A governed dimension "
                        "output or period may be used only when intentionally sorting that output."
                    ),
                },
                "direction": {"type": "string", "enum": ["asc", "desc"]},
            },
            "required": ["field", "direction"],
            "description": (
                "Ascending or descending output ordering. Metric codes are accepted as compatibility aliases "
                "and normalized by the tool to metric_value."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Maximum returned rows; the environment cap can reduce it further.",
        },
    },
    "required": [
        "request_id",
        "domain",
        "mode",
        "purpose",
    ],
    "allOf": [
        {
            "not": {
                "required": ["time_range", "calendar_month"],
            },
        },
        {
            "if": {"properties": {"mode": {"const": "metric"}}, "required": ["mode"]},
            "then": {
                "required": ["metric"],
            },
        },
        {
            "if": {
                "required": ["comparison"],
                "properties": {
                    "comparison": {
                        "required": ["kind"],
                        "properties": {"kind": {"const": "previous_period"}},
                    }
                },
            },
            "then": {
                "oneOf": [
                    {"required": ["time_range"]},
                    {"required": ["calendar_month"]},
                ],
            },
        },
        {
            "if": {
                "required": ["comparison"],
                "properties": {
                    "comparison": {
                        "required": ["kind"],
                        "properties": {
                            "kind": {"const": "snapshot_months_before"}
                        },
                    }
                },
            },
            "then": {
                "not": {
                    "anyOf": [
                        {"required": ["time_range"]},
                        {"required": ["calendar_month"]},
                    ],
                },
            },
        },
        {
            "if": {"required": ["complete_change_decomposition"]},
            "then": {
                "anyOf": [
                    {
                        "required": ["comparison"],
                        "properties": {
                            "comparison": {
                                "properties": {
                                    "kind": {"const": "snapshot_months_before"}
                                }
                            }
                        },
                    },
                    {
                        "oneOf": [
                            {"required": ["time_range"]},
                            {"required": ["calendar_month"]},
                        ]
                    },
                ],
                "not": {
                    "anyOf": [
                        {"required": ["dimensions"]},
                        {"required": ["order_by"]},
                        {"required": ["limit"]},
                        {"required": ["decomposition_of_request_id"]},
                    ]
                },
            },
        },
        {
            "if": {"required": ["complete_target_gap_decomposition"]},
            "then": {
                "properties": {
                    "domain": {"const": "target"},
                    "metric": {
                        "enum": [
                            "delivery_target_completion",
                            "receipt_target_completion",
                        ]
                    },
                    "attribution_mode": {"const": "transaction_detail"},
                },
                "required": ["attribution_mode"],
                "not": {
                    "anyOf": [
                        {"required": ["dimensions"]},
                        {"required": ["order_by"]},
                        {"required": ["limit"]},
                        {"required": ["comparison"]},
                        {"required": ["decomposition_of_request_id"]},
                        {"required": ["complete_change_decomposition"]},
                    ]
                },
            },
        },
        *query_request_schema_conditions(),
    ],
}

CALCULATION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "calculation_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "Unique ID for one governed arithmetic observation.",
        },
        "operation": {
            "type": "string",
            "enum": ["difference", "ratio", "share"],
            "description": (
                "Closed arithmetic operation over two successful, untruncated scalar request results. "
                "difference and ratio require the same registered metric, governed non-time scope, filters, "
                "and unit; their periods may differ, and ratio requires a nonzero denominator. Arithmetic remains "
                "visible when calendar coverage differs, but period_compatibility then prevents treating it as "
                "a formal period trend. share requires "
                "the same metric, period, and unit; left must be a proven strict additive-partition subset of "
                "right, right must be positive, and 0 <= left <= right. Free-form formulas are never accepted."
            ),
        },
        "left_request_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
        },
        "right_request_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
        },
    },
    "required": [
        "calculation_id",
        "operation",
        "left_request_id",
        "right_request_id",
    ],
}

DATASAGE_QUERY = {
    "name": "datasage_query",
    "description": (
        "Execute one to ten fresh, read-only governed metric queries. Load the relevant domains with "
        "datasage_catalog first and copy exact metric and dimension codes from that catalog. The caller may query "
        "directly only when the selected expert_index metric has exact_default_lookup_supported: "
        "true and the request has no explicit business qualifier. Empty dimensions: [] does not count as a qualifier. "
        "If that flag is false or missing, or if calendar_month, "
        "time_range, dimensions, filters, an entity, comparison, decomposition, or ranking is explicit, load the "
        "selected metric detail before calling datasage_query and copy that result's detail_receipt unchanged. "
        "A legacy single-detail content_hash remains accepted during migration. The "
        "runtime rejects a missing, stale, tampered, wrong-metric, or capability-incompatible receipt before any "
        "database access. The model-facing surface accepts no SQL, physical "
        "tables, columns, joins, or formulas. Registered entity tokens may be "
        "provided as metric filters and are resolved deterministically inside the query. The response returns "
        "structured values, applied scope, data state, and evidence metadata for Hermes to analyze and summarize. "
        "A non-empty answer_scope_line is a required final-answer scope statement: present it verbatim or faithfully "
        "without changing the actual range. Every sealed disclosure_ledger item with applies: true is validated "
        "internally and batch-deduplicated into the model-facing disclosures list; present every returned disclosure "
        "and never drop one through summarization. The compact response preserves facts, typed states, Top-N status, "
        "limitations, reconciliation, calculations, guardrails, and explicit answer_constraints without repeating "
        "row-level seals or scope envelopes. Raw JSON is not required. "
        "Unavailable data affects only this tool call and does not control the surrounding conversation."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": PUBLIC_REQUEST_LIMIT,
                "items": REQUEST,
                "description": (
                    "Independent governed requests or explicit semantic operations selected by Hermes for the "
                    "current answer. Batch the adaptively chosen evidence here in one tool call, preserve question "
                    "order, and give each input a unique request_id. A complete_change_decomposition or "
                    "complete_target_gap_decomposition operation expands to two physical requests that "
                    "count against the same ten-request execution budget."
                ),
            },
            "calculations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": CALCULATION,
                "description": (
                    "Optional governed difference, ratio, or share operations. Each operand must reference a "
                    "successful scalar request_id from this batch. Results are derived observations, never "
                    "registered KPIs, structural contributions, or causal evidence."
                ),
            },
        },
        "required": ["requests"],
    },
}


DATASAGE_CATALOG = {
    "name": "datasage_catalog",
    "description": (
        "Load the trusted DataSage metric catalog needed to plan an internal business-data query. "
        "Hermes chooses whether data is needed and which domains match the user's request; DataSage does not "
        "classify or control ordinary conversation. Request expert_index for the smallest metric-discovery surface, "
        "then request detail for a selected metric when required. Every expert-index metric declares "
        "requires_metric_detail. Direct "
        "query is allowed only when exact_default_lookup_supported is true and no explicit business qualifier is "
        "present; empty dimensions: [] does not count as a qualifier. "
        "Otherwise request detail only for the selected metric before query. The default model projection is compact; "
        "full/audit are explicit compatibility views. The performance_scorecard view returns a governed operating "
        "set of candidate lenses while declaring unavailable capabilities. Hermes selects and orders the material "
        "subset. Metric detail returns capability facts and max_group_dimensions; these facts do not prescribe a "
        "metric count, call sequence, interpretation, or conclusion. Physical datasets, fields, filters, "
        "joins and formulas remain plugin-private and are never returned to Hermes. This loader invokes no second model."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "domain": {
                            "type": "string",
                            "enum": DOMAINS,
                            "description": "Governed business domain selected by Hermes.",
                        },
                        "metric": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                            "description": (
                                "Exact metric code from a prior expert index or domain summary. "
                                "Do not combine metric with view."
                            ),
                        },
                        "view": {
                            "type": "string",
                            "enum": ["expert_index", "full", "audit", "performance_scorecard"],
                            "description": (
                                "Use expert_index for compact discovery. Omit view for the default compact "
                                "model projection. Use full or audit only for explicit compatibility/audit "
                                "inspection. Use performance_scorecard without domain or metric for the "
                                "governed cross-domain candidate lenses."
                            ),
                        },
                    },
                    "oneOf": [
                        {
                            "required": ["domain"],
                            "not": {"required": ["metric", "view"]},
                            "properties": {
                                "view": {"enum": ["expert_index", "full", "audit"]},
                            },
                        },
                        {
                            "properties": {
                                "view": {"const": "performance_scorecard"},
                            },
                            "required": ["view"],
                            "not": {
                                "anyOf": [
                                    {"required": ["domain"]},
                                    {"required": ["metric"]},
                                ]
                            },
                        },
                    ]
                },
                "description": (
                    "One request per relevant domain, or one cross-domain view=performance_scorecard request. "
                    "Use expert_index for discovery, include one exact metric code for detail, and use explicit "
                    "full/audit only when the legacy summary is genuinely required. The scorecard returns a "
                    "set of candidate lenses plus independently sealed metric-detail receipts."
                ),
            },
        },
        "required": ["requests"],
        "oneOf": [
            {
                "properties": {
                    "requests": {
                        "maxItems": 1,
                        "items": {
                            "properties": {
                                "view": {"const": "performance_scorecard"},
                            },
                            "required": ["view"],
                        },
                    },
                },
            },
            {
                "properties": {
                    "requests": {
                        "items": {
                            "not": {
                                "properties": {
                                    "view": {"const": "performance_scorecard"},
                                },
                                "required": ["view"],
                            },
                        },
                    },
                },
            },
        ],
    },
}


DATASAGE_ENTITY_RESOLVE = {
    "name": "datasage_entity_resolve",
    "description": (
        "Search or clarify one business-entity token only after datasage_catalog has confirmed that the exact "
        "requested business quantity and operation are supported. Never call this resolver to explore an unsupported "
        "quantity or before capability coverage; an entity match cannot create metric capability. "
        "The resolver itself invokes no second model or embedding "
        "service, while the main Hermes Agent normally continues after receiving its result. This is not a mandatory "
        "pre-query hop: ordinary explicitly labeled metric filters continue through exact query preflight inside "
        "datasage_query. After the catalog has confirmed a metric, a type-neutral pre-query clarification is also "
        "allowed when the user's token is truly unlabeled and multiple metric-compatible entity types or roles remain "
        "reasonable. For that bounded case, omit entity_types, include the selected domain and metric, and resolve "
        "before a business query rather than guessing customer or another type. Registered exact aliases and codes "
        "resolve deterministically; otherwise the tool returns at most ten bounded master-data candidates. Call it "
        "only for that type-neutral clarification, an entity-only/search question, or after query preflight reports "
        "an unresolved or ambiguous identity. Do not repeat the same resolution with unchanged evidence. Resolve "
        "again only after material new information changes the token, selected metric or domain, possible role, or "
        "candidate scope and the result could change the next action. "
        "A unique exact match may be used only with one returned filter role. A metric-incompatible entity fails "
        "closed; multiple exact matches, multiple possible roles, or any prefix/contains matches require user "
        "clarification and must not trigger a business query. Entity-only questions stop after this result."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "token": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "The unresolved name, code, alias, or short token exactly as the user supplied it.",
            },
            "entity_types": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ENTITY_TYPES},
                "description": (
                    "Use only an explicit user-labeled entity type; never guess one from the token. Omit entity_types "
                    "for the bounded type-neutral clarification when the token is truly unlabeled."
                ),
            },
            "domain": {
                "type": "string",
                "enum": DOMAINS,
                "description": "Known business domain, used only to return valid semantic filter roles.",
            },
            "metric": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "Optional selected metric code; requires domain and narrows the valid filter role.",
            },
            "attribution_mode": {
                "type": "string",
                "enum": list(ATTRIBUTION_MODES),
                "description": "Optional governed ledger path for path-dependent metrics; requires metric and narrows the valid filter role.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
        },
        "required": ["token"],
    },
}
