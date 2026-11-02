"""Build structured JSON schema for a BIRD split.

Usage:
    conda run -n znws python scripts/build_schema.py --config configs/schema_minidev.yaml

Config fields (relative paths are resolved from the project root):
    dataset       数据集名称，写入输出顶层
    split         数据划分名称，写入输出顶层
    tables_json   BIRD 元数据文件，如 dev_tables.json / train_tables.json
    databases_dir SQLite 数据库根目录
    output_path   输出 JSON 路径
    db_ids        仅处理指定数据库；留空表示全部
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.schema.bird_json import build_dataset, load_config, write_schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Build structured JSON schema for a BIRD split.")
    parser.add_argument("--config", required=True, help="Path to the YAML experiment config.")
    args = parser.parse_args()

    config = load_config(args.config)
    schema = build_dataset(config)
    write_schema(schema, config["output_path"])
    print(f"[saved] {len(schema['databases'])} databases -> {config['output_path']}")


if __name__ == "__main__":
    main()
