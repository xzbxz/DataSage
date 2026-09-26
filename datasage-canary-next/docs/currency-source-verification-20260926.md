# Currency basis and source coverage

The currency selector uses registered counterparts and the complete governed
source scope. Exact metric IDs retain their declared basis. Single-currency
automatic requests can select original amounts; cross-currency requests use
the registered RMB facts. Currency filters accept nonblank source text only.

This revision adds thirteen fixed metric variants:

| Domain | Added capability |
| --- | --- |
| Target | Eight original-currency target, allocated-actual and completion variants |
| Inventory | Original turnover denominator and cost/DDP turnover |
| Receivable | Original formal DSO |
| Pattern matching | Two RMB linked/attributed amount variants |

Original receivables use transaction currency. Inventory cost and DDP retain
their paired book currency; purchase currency is a separate basis. The monthly
customer-debt contract now includes currency in its declared primary key.
Target datasets register the observed currency fields needed by these paths;
no new exchange-rate conversion is introduced.

Inventory turnover still determines accounting readiness from the global RMB
cost records before evaluating the selected currency's inputs. Formal DSO
still requires continuous N+1 month-end snapshots and uses average net debt
divided by same-period positive **gross** delivery, multiplied by natural days.
Signed debt/inventory values remain signed. Original DSO seals its basis,
scope, components and units separately from the existing RMB attestation.

Receipt allocation retains the split ledger. Transaction details provide
coverage diagnostics only. Unrepresented internal records make completion
and gap unavailable; missing allocation identities are not inferred from the
transaction salesperson. Diagnostic-only groups remain visible. Coverage
counts for salesperson views are shared non-salesperson-scope observations,
not amounts or per-salesperson attribution.

Missing currency and missing monetary operands remain unknown. An incomplete
aging subtotal no longer exposes a zero-filled side value as source-value
evidence. Pattern RMB values come from the same verified sale detail and keep
the existing linkage, timing, deduplication and partial-coverage conditions.

Profit/fabric paths without complete approved original inputs retain their RMB
restriction. Sales quote RMB fields are not activated: quotes remain original
currency, and missing historical RMB observations are not reconstructed from
current quotes. Complete target-gap decomposition remains registered for the
existing RMB transaction-ledger metrics.

The regression coverage includes real generated SQL on isolated synthetic
data, unknown and signed inputs, one-sided currency scopes, snapshot/period
coverage, split-source gaps, and DSO attestation tampering. Source inspection
and controlled read-only reconciliation evidence is retained separately from
the source package; private business rows and connection material are not
part of this document or the added fixtures. Availability/readiness and release
owner decisions remain distinct from these code and data checks.
