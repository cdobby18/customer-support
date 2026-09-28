"""Support agents and the domain logic they share.

One module per agent named in ``markdowns/AGENTS.md`` (intake, triage,
knowledge, response, escalation, agent assist, evaluation) plus the
cross-cutting services they depend on: the LLM gateway and prompt registry,
embeddings and semantic search, the guardrail/policy engine, outbound
helpdesk integrations, and shared helpers.
"""
