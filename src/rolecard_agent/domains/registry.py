"""Explicit domain plugin list - no dynamic loading, no discovery magic.

To add a domain: implement models.py / service.py / tools.py / schema.sql under
domains/<name>/ and append ONE line to DOMAINS below.

IMPORTANT: runtime enable/disable is an OPERATOR action backed by the plugin table.
It is never exposed as an LLM-callable tool (self-authorization risk, same as switch_role).
"""

# Populated in M3. Each entry is the domain's own module-level spec, and its domain_id MUST
# match the plugin.plugin_id in core/schema.sql. Shape:
#     DOMAINS: tuple[DomainSpec, ...] = (health.DOMAIN,)
# A DomainSpec carries: domain_id, display_name, tools (callables to register),
# schema_paths, service_factory.
DOMAINS: tuple = ()
