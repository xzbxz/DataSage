# DataSage source review export

`scripts/datasage_source_export.py` creates a local, controlled source review
package for the `datasage-canary-next` candidate. It is not a Hermes release,
installation, deployment, upgrade, or publishing mechanism. It never reads
the active Profile's `.env`, `auth.json`, database, sessions, memory, logs, or
report output, and it does not start a service or send a message.

The exporter uses an explicit, reviewed file manifest. Source Python modules,
contracts, the vendored PyMySQL runtime, DataSage Skill references, scripts,
tests, synthetic fixtures, `docs/maintenance.md`, and the root maintenance
documents are listed by name in the exporter. An unknown file is ignored. A
missing listed contract is an error, and any symlink anywhere under the source
root is rejected so an allowlisted path cannot redirect outside the Profile.

The archive contains a generated `config.yaml`. It preserves the candidate's
behavior switches, but replaces WeCom home-channel identity with
`${WECOM_HOME_CHANNEL}` and the absolute Pyright command with
`${PYRIGHT_LANGSERVER_COMMAND}`. `.env.EXAMPLE` is copied as an empty template
and receives a portable Pyright command example. The candidate's real
`config.yaml` is never modified.

## Usage

Run from the candidate Profile's parent with the official Hermes Python:

```text
python -B datasage-canary-next/scripts/datasage_source_export.py \
  datasage-canary-next \
  C:\path\outside\the\profile\datasage-canary-next-source.tar.gz
```

The destination must be new and outside the source Profile. A path ending in
`.tar.gz` or `.tgz` produces a deterministic archive; another path produces a
new directory. Existing destinations, path traversal, symlinked components,
and runtime paths are refused.

The package can be inspected or loaded in a second temporary Home. Loading
the plugin still requires the normal Hermes host and Python dependencies; an
archive does not contain credentials or a database account. The package is an
internal controlled source-review artifact. `entity-registry.yaml`, contracts,
and synthetic fixtures can contain business terminology or identifiers, so an
owner must review them before any public sharing. The exporter’s high-signal
credential scan and generated-config path check are evidence for packaging
hygiene, not a claim that all business or personal data has been independently
classified. Existing source-level Windows font or fixture path examples are
recorded in `EXPORT_MANIFEST.json` for separate owner review.

Before using an exported configuration in an authorized deployment, resolve
the channel identity and Pyright command for that deployment. Hermes' native
configuration reader expands the template variables; a literal reviewed
command path can also be set in the deployment's `config.yaml`. An unresolved
placeholder is not an installed runtime. The portable-config regression uses
a different temporary Home, synthetic channel identity, and a launcher path
containing spaces to verify native expansion and LSP spawn configuration
without starting a gateway or language server. The separate host compatibility
test performs the actual installed Pyright handshake. Neither test establishes
database, messaging, or production deployment readiness.

The Profile-level `.gitignore` is included for the remediation tests. Parent
repository metadata such as a repository-level `.gitignore` or `.gitattributes`
is outside this one-Profile package; tests that explicitly read that parent
metadata must run with the original repository checkout.

To include a new module or resource, review it first and add its concrete
relative path to `SOURCE_ALLOWLIST`; do not replace the manifest with a glob.
