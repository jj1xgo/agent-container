"""Shared kernel for agent-container brokers.

Modules here must not import broker-specific modules
(handover_*, egress_*, github_*, family_*) or agent_container.state.
"""
