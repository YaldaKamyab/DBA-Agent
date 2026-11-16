"""Workflow stages. Each stage: run(session, llm, prompts, **inputs) -> None.

A stage reads its input from the session, writes its output files and
structured data back into the session, and advances `current_stage`.
"""
