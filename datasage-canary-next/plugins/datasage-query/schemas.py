"""JSON schema exposed by the DataSage Mini query plugin."""

from .evidence import EVIDENCE_ROLES
from .references import SECTION_IDS, SOURCE_IDS

DOMAINS = [
    "delivery",
    "receipt",
    "receivable",
    "target",
    "customer_risk",
    "inventory",
]

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
        "evidence_role": {
            "type": "string",
            "enum": list(EVIDENCE_ROLES),
            "description": (
                "Optional role this request is meant to play in the answer. The response echoes the label beside "
                "per-request completeness and sealed proof capabilities; the answer layer decides whether the evidence "
                "satisfies the role. The label itself never upgrades proof strength."
            ),
        },
        "attribution_mode": {
            "type": "string",
            "enum": ["transaction_detail", "salesperson_allocation"],
            "description": (
                "Required for target-domain metric requests. Use transaction_detail for ordinary, customer, "
                "department, organization, or unqualified salesperson questions. Use salesperson_allocation "
                "only for explicit salesperson target, collaboration, allocation, or allocated-performance questions."
            ),
        },
        "delivery_scope": {
            "type": "string",
            "enum": ["default_net", "explicit_gross", "order_delivery_alignment"],
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
            "enum": ["total", "on_hand", "available", "allocated", "in_transit"],
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
                "Exact metric code copied from the selected domain planner metrics; required in metric mode. Never "
                "invent a code, derive a new metric, or try code synonyms. If the requested meaning has no exact code, "
                "do not call this tool. For the delivery "
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
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["start", "end"],
            "oneOf": [
                {
                    "properties": {
                        "start": {"pattern": r"^\d{4}-\d{2}$"},
                        "end": {"pattern": r"^\d{4}-\d{2}$"},
                    }
                },
                {
                    "properties": {
                        "start": {"pattern": r"^\d{4}-\d{2}-\d{2}$"},
                        "end": {"pattern": r"^\d{4}-\d{2}-\d{2}$"},
                    }
                },
            ],
            "description": "Start-inclusive and end-exclusive governed metric range.",
        },
        "calendar_month": {
            "type": "string",
            "pattern": r"^(?!0000-)(?!9999-12$)[0-9]{4}-(?:0[1-9]|1[0-2])$",
            "description": (
                "Typed full calendar month. The runtime deterministically expands it to a start-inclusive, "
                "end-exclusive range from the first day of this month to the first day of the next month. "
                "Use either calendar_month or time_range, never both."
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
                "Explicitly request a complete previous-period change decomposition. "
                "When comparison is omitted, selecting this optional operation defaults its comparison semantics to previous_period. "
                "An explicitly provided comparison must also be previous_period. "
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
                "properties": {
                    "comparison": {
                        "properties": {"kind": {"const": "previous_period"}},
                    },
                },
                "oneOf": [
                    {"required": ["time_range"]},
                    {"required": ["calendar_month"]},
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
                "and unit; their periods may differ, and ratio requires a nonzero denominator. share requires "
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
        "datasage_catalog first and copy exact metric and dimension codes from that catalog. The model-facing "
        "surface accepts no SQL, physical tables, columns, joins, or formulas. Registered entity tokens may be "
        "provided as metric filters and are resolved deterministically inside the query. The response returns "
        "structured values, applied scope, data state, and evidence metadata for Hermes to analyze and summarize. "
        "Unavailable data affects only this tool call and does not control the surrounding conversation."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
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
        "then request details only for the selected metric. Full summaries and metric details include bounded planning_guidance "
        "loaded from the versioned planner contract, plus non-binding analysis affordances describing proof capabilities, "
        "boundaries, adaptive follow-up, and stopping guidance. Metric detail also returns max_group_dimensions; "
        "they neither prescribe a fixed metric count nor authorize execution. Physical datasets, fields, filters, "
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
                        "domain": {"type": "string", "enum": DOMAINS},
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
                            "enum": ["expert_index"],
                            "description": (
                                "Optional compact discovery view. Use expert_index before loading detail for only "
                                "the selected metric. Omit to preserve the full legacy domain summary."
                            ),
                        },
                    },
                    "required": ["domain"],
                    "not": {"required": ["metric", "view"]},
                },
                "description": (
                    "One request per relevant domain. Use view=expert_index for compact discovery; include one exact "
                    "metric code for detail; omit both metric and view only for the full legacy summary."
                ),
            },
        },
        "required": ["requests"],
    },
}


DATASAGE_REFERENCE = {
    "name": "datasage_reference",
    "description": (
        "Read a small, approved DataSage expert-planning reference when a complex diagnosis, claim boundary, "
        "entity ambiguity, or domain-specific analysis recipe requires guidance not present in the metric catalog. "
        "Use mode=index only to discover fixed source_id/section_id pairs, then mode=read for at most three exact "
        "sections. This tool is non-authorizing and read-only: it cannot accept paths, filenames, URLs, globs, "
        "offsets, SQL, or user documents; it never reads plugin-private execution contracts and never accesses a "
        "database or network. Returned source and section hashes make the content auditable."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["index", "read"]},
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "source_id": {"type": "string", "enum": list(SOURCE_IDS)},
                        "section_id": {"type": "string", "enum": list(SECTION_IDS)},
                    },
                    "required": ["source_id", "section_id"],
                },
            },
        },
        "required": ["mode"],
        "allOf": [
            {
                "if": {"properties": {"mode": {"const": "read"}}},
                "then": {"required": ["requests"]},
            },
            {
                "if": {"properties": {"mode": {"const": "index"}}},
                "then": {"not": {"required": ["requests"]}},
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
                "enum": ["transaction_detail", "salesperson_allocation"],
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
