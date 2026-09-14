"""Explicit domain plugin list - no dynamic loading, no discovery magic.

To add a domain: implement models.py / service.py / tools.py / schema.sql under
domains/<name>/ and append ONE line to DOMAINS below.

IMPORTANT: runtime enable/disable is an OPERATOR action backed by the plugin table.
It is never exposed as an LLM-callable tool (self-authorization risk, same as switch_role).
"""
