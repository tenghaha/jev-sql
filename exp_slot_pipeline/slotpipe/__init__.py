"""Step 1 ablation: LLM writes schema-free slot-AST templates, JEV fills slots, SQL is rendered.

Pipeline per question (see ``slotpipe.pipeline``):

1. **template** - one zero-shot LLM call sees the database's table cards (stage-1
   summaries, no column lists) plus the question, and returns two candidate
   slot-AST templates.  Concrete values are returned in a *separate* ``values``
   map keyed by slot id, so the template itself stays schema- and value-free.
2. **table slots** - JEV ``noul`` question per candidate table, state = table cards.
3. **column slots** - JEV ``noul`` question per column, one request per selected
   table, state = that table's column-level schema.
4. **render** - bind the filled slots and unparse the AST to a SQLite statement.

Entry point: ``scripts/run_step1.py``; design notes in ``docs/01-step1.md``.
"""

__all__ = ["config", "fill", "llm", "pipeline", "render", "template"]
