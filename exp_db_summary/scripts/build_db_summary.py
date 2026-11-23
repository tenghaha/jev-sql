"""Build the offline database summary (table cards + FK graph) and the full schema.

Usage
-----
    python scripts/build_db_summary.py --config configs/bird_dev.yaml
    python scripts/build_db_summary.py --config configs/bird_dev.yaml --db california_schools
    python scripts/build_db_summary.py --config configs/bird_dev.yaml \
        --summarizer llm --db california_schools --limit-tables 3

Config (YAML, one file per dataset split):
    dataset            dataset name written into the output
    split              split name written into the output
    db_backend         sqlite_dir (BIRD) | sqlite_zip (Spider2 local)
    databases_dir      sqlite_dir: root directory holding <db>/<db>.sqlite
    db_pattern         sqlite_dir: glob under databases_dir
    db_zip             sqlite_zip: archive holding one .sqlite per database
    cache_dir          where extracted .sqlite files and the LLM cache live
    tables_json        optional BIRD *_tables.json; its column types are authoritative
    output_path        summary JSON to write
    schema_output_path full column-level schema JSON to write (same 1 pass)
    summarizer         template (deterministic) | llm (needs .env credentials)
    summary_max_chars  hard cap on one table summary
    summary_max_words  wording cap for the LLM prompt
    sample_rows        rows sampled per table (1-2)
    sample_values      sample values kept in the card
    llm_max_tables     safety cap on uncached LLM calls per run (0 = unlimited)

Output: two JSON documents per config, produced in the same pass over the
databases:
    <output_path>         table cards + connectivity graph (compressed view)
    <schema_output_path>  full column-level schema (same shape as
                          scripts/build_schema.py)
See docs/01-db-summary.md.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbprofile.build import build_all, write_json  # noqa: E402
from dbprofile.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build offline table summaries + FK connectivity graph.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", default=None, help="only this db_id")
    parser.add_argument("--summarizer", choices=("template", "llm"), default=None,
                        help="override the config's summarizer")
    parser.add_argument("--limit-tables", type=int, default=None,
                        help="cap on uncached LLM calls (0 = unlimited)")
    parser.add_argument("--output", default=None, help="override output_path")
    parser.add_argument("--schema-output", default=None,
                        help="override schema_output_path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.summarizer:
        cfg["summarizer"] = args.summarizer
    for attr, key in (("output", "output_path"), ("schema_output", "schema_output_path")):
        value = getattr(args, attr)
        if value:
            cfg[key] = value if os.path.isabs(value) \
                else os.path.normpath(os.path.join(cfg["exp_dir"], value))

    summary, schema = build_all(cfg, only_db=args.db, limit_tables=args.limit_tables)
    write_json(summary, cfg["output_path"])
    write_json(schema, cfg["schema_output_path"])

    stats = summary["stats"]
    sstats = schema["stats"]
    print(f"[saved] {cfg['output_path']}")
    print(f"[stats] databases={stats['databases']} tables={stats['tables']} "
          f"edges={stats['edges']} isolated={stats['isolated_tables']} "
          f"components={stats['components']} "
          f"summary_chars(mean)={stats['summary_chars_mean']} "
          f"llm_calls={stats['llm_calls']}")
    print(f"[saved] {cfg['schema_output_path']}")
    print(f"[schema] databases={sstats['databases']} tables={sstats['tables']} "
          f"columns={sstats['columns']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
