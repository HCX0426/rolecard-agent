"""Explicit domain plugin list - no dynamic loading, no discovery magic.

Two different things, deliberately kept apart:

  * **REGISTERED** - which domains exist, i.e. which code is present. This file. Changing it
    is a commit.
  * **ENABLED** - which registered domains are currently switched on. The `plugin` table.
    Changing it is an operator action.

`scripts/init_db.py` applies the schema of every REGISTERED domain, so a table always exists
and re-enabling a plugin never needs DDL.

To add a domain: implement models.py / service.py / tools.py / schema.sql under
domains/<name>/ and append its id to DOMAINS below. M3 adds the tool registry on top of this;
today it is the id list only.

IMPORTANT: runtime enable/disable is an OPERATOR action backed by the plugin table.
It is never exposed as an LLM-callable tool (self-authorization risk, same as switch_role).
"""

# Registered domain ids. Each MUST match a directory under domains/ and the plugin.plugin_id
# in core/schema.sql. This is the single source of truth - previously scripts/init_db.py kept
# its own hardcoded copy, which is how two lists drift apart (技术评审与决策.md §9 A3).
DOMAINS: tuple[str, ...] = ("health",)
