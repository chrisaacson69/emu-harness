"""Build: the agent-facing build tool over the library. The phase table (phase_table.toml) and its manual
(PHASES.md) live here; each project keeps its own state in re-roadmap.toml. Verbs return records
(--json) rather than prose. The build tool reads state and drives no emulator."""
