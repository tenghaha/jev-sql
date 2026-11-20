"""Profile BIRD gold SQL and questions to test a semantic-tree IR design.

Usage:
    conda run -n znws python scripts/analyze_bird_features.py
    conda run -n znws python scripts/analyze_bird_features.py --out-dir outputs/bird_features

Inputs:
    BIRD/dev_20240627/dev.json
    BIRD/train/train.json
    BIRD/dev_20240627/dev_tables.json
    BIRD/dev_20240627/dev_databases/<db_id>/<db_id>.sqlite   (gold execution probe)

Outputs (under --out-dir):
    features.json    per-split feature rates, shape signatures, dev gold execution shapes
    report.md        Markdown summary

The script answers three questions about the data:
    1. Which SQL constructs actually occur, and how often?
    2. How many distinct "query shapes" exist, and does a shape library mined on
       train cover dev?
    3. What do gold results look like (row counts), i.e. what output-shape
       constraint should an IR carry?
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AGG_FUNCS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL",
    "GROUP_CONCAT", "STRING_AGG", "COUNT_BIG",
}


class Tok:
    __slots__ = ("kind", "text")

    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self.text = text


def tokenize(sql: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n":
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            quote = c
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                if sql[j] == "\\" and quote == "'":
                    j += 2
                    continue
                j += 1
            toks.append(Tok("STR" if quote == "'" else "IDENT", sql[i:j]))
            i = j
            continue
        if c == "`":
            j = sql.find("`", i + 1)
            j = n if j < 0 else j + 1
            toks.append(Tok("IDENT", sql[i:j]))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and (sql[j].isdigit() or sql[j] in ".eE"):
                j += 1
            toks.append(Tok("NUM", sql[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            toks.append(Tok("WORD", sql[i:j]))
            i = j
            continue
        if c in "(),;":
            toks.append(Tok("PUNCT", c))
            i += 1
            continue
        for op in ("<=", ">=", "<>", "!=", "==", "||", "::"):
            if sql.startswith(op, i):
                toks.append(Tok("OP", op))
                i += len(op)
                break
        else:
            toks.append(Tok("OP", c))
            i += 1
    return toks


def up(tok: Tok | None) -> str:
    if tok is None or tok.kind != "WORD":
        return ""
    return tok.text.upper()


def depths(tokens: list[Tok]) -> list[int]:
    out: list[int] = []
    d = 0
    for t in tokens:
        if t.kind == "PUNCT" and t.text == ")":
            d -= 1
        out.append(d)
        if t.kind == "PUNCT" and t.text == "(":
            d += 1
    return out


def split_top(tokens: list[Tok], sep) -> list[list[Tok]]:
    out: list[list[Tok]] = []
    cur: list[Tok] = []
    d = 0
    for t in tokens:
        if t.kind == "PUNCT" and t.text == ")":
            d -= 1
        is_sep = d == 0 and sep(t)
        if t.kind == "PUNCT" and t.text == "(":
            d += 1
        if is_sep:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return out


KW_FROM_STOP = {
    "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "JOIN", "INNER",
    "LEFT", "RIGHT", "FULL", "CROSS", "ON", "USING", "AS", "SELECT", "UNION",
    "WINDOW", "FETCH", "NATURAL", "OUTER",
}


def profile_sql(sql: str) -> dict:
    toks = tokenize(sql)
    ups = [up(t) for t in toks]
    d = depths(toks)
    f: dict = {}

    # tables in the outermost FROM ... join chain
    tables: list[str] = []
    for i, t in enumerate(toks):
        if up(t) in {"FROM", "JOIN"}:
            j = i + 1
            while j < len(toks) and up(toks[j]) in {"INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "NATURAL"}:
                j += 1
            if j < len(toks) and toks[j].kind in {"WORD", "IDENT"} and up(toks[j]) not in KW_FROM_STOP:
                tables.append(toks[j].text.lower())
    f["n_tables"] = max(1, len(tables))
    f["has_join"] = len(tables) > 1 or "JOIN" in ups
    f["self_join"] = len({t for t in tables}) < len(tables)

    f["has_group_by"] = any(ups[i] == "GROUP" and i + 1 < len(ups) and ups[i + 1] == "BY" for i in range(len(ups)))
    f["has_having"] = "HAVING" in ups
    f["has_order_by"] = any(ups[i] == "ORDER" and i + 1 < len(ups) and ups[i + 1] == "BY" for i in range(len(ups) - 1))
    f["has_limit"] = any(u in {"LIMIT", "OFFSET", "FETCH"} for u in ups)
    f["has_distinct"] = "DISTINCT" in ups
    f["has_star"] = any(t.text == "*" for t in toks)
    f["has_cte"] = bool(ups) and ups[0] == "WITH"
    f["has_setop"] = any(u in {"UNION", "INTERSECT", "EXCEPT", "MINUS"} for u in ups)
    f["has_window"] = "OVER" in ups
    f["has_case"] = "CASE" in ups
    f["has_cast"] = "CAST" in ups
    f["has_lt_like"] = "LIKE" in ups
    f["has_in"] = any(u == "IN" for u in ups)
    f["has_between"] = "BETWEEN" in ups
    f["has_is_null"] = any(ups[i] == "IS" and i + 1 < len(ups) and ups[i + 1] in {"NULL", "NOT"} for i in range(len(ups) - 1))
    f["has_exists"] = "EXISTS" in ups
    f["has_not_in"] = any(ups[i] == "NOT" and i + 1 < len(ups) and ups[i + 1] == "IN" for i in range(len(ups) - 1))
    f["has_not_exists"] = any(ups[i] == "NOT" and i + 1 < len(ups) and ups[i + 1] == "EXISTS" for i in range(len(ups) - 1))
    f["has_neg_not"] = any(
        ups[i] == "NOT" and (i + 1 >= len(ups) or ups[i + 1] not in {"IN", "LIKE", "BETWEEN", "NULL"})
        for i in range(len(ups))
    )
    f["has_div"] = any(t.kind == "OP" and t.text == "/" for t in toks)
    f["has_arith"] = any(t.kind == "OP" and t.text in {"+", "-", "*", "/"} for t in toks)
    f["has_date_fn"] = any(
        t.kind == "WORD" and t.text.upper() in {"STRFTIME", "JULIANDAY", "DATE", "DATETIME", "SUBSTR", "SUBSTRING"}
        for t in toks
    )

    aggs: Counter = Counter()
    # aggregate calls: NAME '(' at any depth
    for i, t in enumerate(toks[:-1]):
        if t.kind == "WORD" and t.text.upper() in AGG_FUNCS and toks[i + 1].text == "(":
            aggs[t.text.upper()] += 1
    f["n_agg_calls"] = sum(aggs.values())
    f["aggs"] = dict(aggs)
    f["has_count_distinct"] = any(
        toks[i].text.upper() == "COUNT" and i + 2 < len(toks) and up(toks[i + 2]) == "DISTINCT"
        for i in range(len(toks))
    )

    # subqueries
    sub_idx = [i for i in range(len(toks) - 1) if toks[i].text == "(" and up(toks[i + 1]) == "SELECT"]
    f["n_subquery"] = len(sub_idx)
    if sub_idx:
        outer_aliases = {
            toks[i + 1].text.lower()
            for i in range(len(toks) - 1)
            if up(toks[i]) == "AS" and toks[i + 1].kind in {"WORD", "IDENT"}
        }
        correlated = False
        for i in sub_idx:
            closed = None
            depth = 0
            for j in range(i, len(toks)):
                if toks[j].text == "(":
                    depth += 1
                elif toks[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        closed = j
                        break
            inner = toks[i:closed if closed else len(toks)]
            if any(t.text.lower() in outer_aliases and t.kind == "WORD" for t in inner):
                correlated = True
            if any(up(t) == "EXISTS" for t in inner):
                f["has_exists"] = True
        f["correlated_subquery"] = correlated
        f["derived_table"] = any(up(toks[i]) == "FROM" and toks[i + 1].text == "(" for i in range(len(toks) - 1))
    else:
        f["correlated_subquery"] = False
        f["derived_table"] = False

    # projection kind
    bounds = [i for i, t in enumerate(toks) if d[i] == 0 and up(t) in {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT"}]
    end = bounds[0] if bounds else len(toks)
    items = [x for x in split_top(toks[1:end], lambda t: t.kind == "PUNCT" and t.text == ",") if x]
    kinds = []
    for it in items:
        if len(it) == 1 and it[0].text == "*":
            kinds.append("star")
        elif it[0].kind == "WORD" and it[0].text.upper() in AGG_FUNCS and len(it) > 2 and it[1].text == "(":
            kinds.append("agg")
        elif all(t.kind in {"WORD", "IDENT"} or t.text == "." for t in it):
            kinds.append("col")
        else:
            kinds.append("expr")
    if not kinds:
        kinds = ["expr"]
    f["n_select"] = len(kinds)
    if all(k == "agg" for k in kinds):
        f["proj_kind"] = "agg"
    elif all(k == "col" for k in kinds):
        f["proj_kind"] = "col"
    elif "star" in kinds:
        f["proj_kind"] = "star"
    elif "expr" in kinds:
        f["proj_kind"] = "expr"
    else:
        f["proj_kind"] = "mixed"

    # aggregate vs aggregate comparison (HAVING or WHERE with a scalar agg subquery)
    f["agg_vs_agg"] = bool(f["has_having"]) or (
        f["n_subquery"] > 0 and "WHERE" in ups and f["n_agg_calls"] >= 2
    )
    f["top1_per_group"] = bool(f["has_group_by"] and f["n_subquery"] > 0 and f["has_limit"])
    return f


SIG_KEYS = [
    "n_tables", "has_group_by", "has_having", "has_subquery", "correlated_subquery",
    "derived_table", "has_order_by", "has_limit", "has_distinct", "has_setop",
    "has_window", "proj_kind", "has_case", "has_cast", "has_div", "has_neg_not",
    "has_agg_vs_agg",
]


def signature(f: dict) -> tuple:
    sig = []
    for k in SIG_KEYS:
        if k == "n_tables":
            sig.append(min(f[k], 4))
        elif k == "has_subquery":
            sig.append(min(f["n_subquery"], 2))
        elif k == "has_agg_vs_agg":
            sig.append(bool(f["agg_vs_agg"]))
        else:
            sig.append(bool(f[k]))
    return tuple(sig)


def load_split(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "outputs" / "bird_features"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dev = load_split(ROOT / "BIRD/dev_20240627/dev.json")
    train = load_split(ROOT / "BIRD/train/train.json")

    result: dict = {}
    for name, rows in (("dev", dev), ("train", train)):
        feats = [profile_sql(r["SQL"]) for r in rows]
        n = len(feats)
        rates = {}
        for k in ["has_join", "self_join", "has_group_by", "has_having", "has_order_by", "has_limit",
                  "has_distinct", "has_star", "has_cte", "has_setop", "has_window", "has_case",
                  "has_cast", "has_lt_like", "has_between", "has_is_null", "has_exists", "has_not_in",
                  "has_not_exists", "has_neg_not", "has_div", "has_arith", "has_date_fn",
                  "has_count_distinct", "correlated_subquery", "derived_table", "agg_vs_agg",
                  "top1_per_group"]:
            rates[k] = round(100 * sum(1 for f in feats if f[k]) / n, 1)
        rates["n_subquery_gt0"] = round(100 * sum(1 for f in feats if f["n_subquery"] > 0) / n, 1)
        tables_dist = Counter(min(f["n_tables"], 5) for f in feats)
        proj_dist = Counter(f["proj_kind"] for f in feats)
        agg_dist = Counter(a for f in feats for a in f["aggs"])
        sigs = Counter(signature(f) for f in feats)
        result[name] = {
            "n": n, "rates": rates,
            "n_tables_dist": {str(k): v for k, v in sorted(tables_dist.items())},
            "proj_dist": dict(proj_dist),
            "agg_calls": dict(agg_dist.most_common(8)),
            "n_signatures": len(sigs),
            "top_signatures": [[list(map(str, s)), c] for s, c in sigs.most_common(15)],
        }
        result[name]["_sigs"] = sigs
        result[name]["_feats"] = feats

    # shape transfer: train -> dev
    train_sigs = result["train"]["_sigs"]
    dev_sigs = result["dev"]["_sigs"]
    dev_total = result["dev"]["n"]
    seen = sum(c for s, c in dev_sigs.items() if s in train_sigs)
    result["shape_transfer"] = {
        "dev_distinct_shapes": len(dev_sigs),
        "train_distinct_shapes": len(train_sigs),
        "dev_covered_by_train_shapes": seen,
        "dev_coverage_pct": round(100 * seen / dev_total, 1),
        "dev_unseen_shapes": len([s for s in dev_sigs if s not in train_sigs]),
        "dev_unseen_queries": dev_total - seen,
        "head_curve": [
            {"top_k": k, "coverage_pct": round(100 * sum(c for _, c in dev_sigs.most_common(k)) / dev_total, 1)}
            for k in (5, 10, 20, 50, 100, 200)
        ],
        "unseen_examples": [
            {"shape": list(map(str, s)), "count": c}
            for s, c in dev_sigs.most_common()
            if s not in train_sigs
        ][:8],
    }

    # questions / evidence
    for name, rows in (("dev", dev), ("train", train)):
        ev = [r.get("evidence") or "" for r in rows]
        nonempty = [e for e in ev if e.strip()]
        has_def = [e for e in nonempty if ("=" in e or "refers to" in e.lower() or "means" in e.lower())]
        result[name]["evidence"] = {
            "nonempty_pct": round(100 * len(nonempty) / len(rows), 1),
            "with_definition_pct": round(100 * len(has_def) / len(rows), 1),
            "median_len": sorted(len(e) for e in ev)[len(ev) // 2],
        }
        if name == "dev":
            by_diff = defaultdict(lambda: [0, 0, 0])
            for r in rows:
                d = r.get("difficulty", "?")
                by_diff[d][0] += 1
                if (r.get("evidence") or "").strip():
                    by_diff[d][1] += 1
                if (r.get("evidence") or "").strip() and ("=" in r["evidence"]):
                    by_diff[d][2] += 1
            result[name]["difficulty"] = {
                k: {"n": v[0], "evidence_pct": round(100 * v[1] / v[0], 1), "definition_pct": round(100 * v[2] / v[0], 1)}
                for k, v in sorted(by_diff.items())
            }

    # gold execution shapes on dev (sqlite)
    dbs_dir = ROOT / "BIRD/dev_20240627/dev_databases"
    rows_dist = Counter()
    errors = 0
    err_examples = []
    for r in dev:
        db_path = dbs_dir / r["db_id"] / f"{r['db_id']}.sqlite"
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = con.execute(r["SQL"])
            nrows = len(cur.fetchall())
            con.close()
            if nrows == 0:
                rows_dist["0"] += 1
            elif nrows == 1:
                rows_dist["1"] += 1
            elif nrows <= 5:
                rows_dist["2-5"] += 1
            elif nrows <= 20:
                rows_dist["6-20"] += 1
            else:
                rows_dist[">20"] += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if len(err_examples) < 5:
                err_examples.append({"db": r["db_id"], "error": str(exc)[:120], "sql": r["SQL"][:120]})
    result["dev_execution"] = {
        "rowcount_dist": dict(rows_dist.most_common()),
        "errors": errors,
        "error_examples": err_examples,
    }

    # schema shape
    tables = json.loads((ROOT / "BIRD/dev_20240627/dev_tables.json").read_text(encoding="utf-8"))
    per_db = []
    for entry in tables:
        ncol = sum(len(t) for t in entry["column_names_original"])
        weird_cols = sum(1 for _, c in entry["column_names_original"] if (" " in c or "(" in c or "/" in c))
        fks = entry.get("foreign_keys", [])
        per_db.append({
            "db": entry["db_id"], "tables": len(entry["table_names_original"]),
            "cols": ncol, "fks": len(fks), "weird_cols": weird_cols,
        })
    per_db.sort(key=lambda x: -x["cols"])
    result["dev_schema"] = {
        "dbs": per_db[:12],
        "median_tables": sorted(d["tables"] for d in per_db)[len(per_db) // 2],
        "median_cols": sorted(d["cols"] for d in per_db)[len(per_db) // 2],
        "total_weird_cols": sum(d["weird_cols"] for d in per_db),
    }

    for k in ("dev", "train"):
        result[k].pop("_sigs", None)
        result[k].pop("_feats", None)
    (out_dir / "features.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    dev_r, tr_r = result["dev"], result["train"]

    # ------------------------------ markdown ---------------------------------
    zh = {
        "has_join": "JOIN / 多表", "self_join": "自连接", "has_group_by": "GROUP BY",
        "has_having": "HAVING", "has_order_by": "ORDER BY", "has_limit": "LIMIT",
        "has_distinct": "DISTINCT", "has_star": "SELECT *", "has_cte": "CTE",
        "has_setop": "集合运算", "has_window": "窗口函数", "has_case": "CASE 表达式",
        "has_cast": "CAST", "has_lt_like": "LIKE", "has_between": "BETWEEN",
        "has_is_null": "IS [NOT] NULL", "has_exists": "EXISTS", "has_not_in": "NOT IN",
        "has_not_exists": "NOT EXISTS", "has_neg_not": "条件取反 NOT",
        "has_div": "除法 /", "has_arith": "算术运算", "has_date_fn": "日期/字符串函数",
        "has_count_distinct": "COUNT(DISTINCT)", "correlated_subquery": "相关子查询",
        "derived_table": "FROM 子查询", "agg_vs_agg": "聚合与聚合比较",
        "top1_per_group": "组内 top-1", "n_subquery_gt0": "任意子查询",
    }
    md: list[str] = ["# BIRD 数据实测特征（用于评估语义树 IR 设计）\n"]
    md.append("样本：dev 1534 条、train 9428 条 gold SQL；dev 结果行数由 SQLite 实跑得到。\n")
    md.append("## 1. SQL 结构出现率\n")
    md.append("| 结构 | dev | train |\n|---|---:|---:|")
    for k, label in zh.items():
        md.append(f"| {label} | {dev_r['rates'][k]:.1f}% | {tr_r['rates'][k]:.1f}% |")
    md.append("")
    md.append("联表数量分布（dev）：" + "，".join(f"{k} 表 {v} 条" for k, v in dev_r["n_tables_dist"].items()))
    md.append("")
    md.append("投影形态（dev）：" + "，".join(f"{k} {v}" for k, v in dev_r["proj_dist"].items()))
    md.append("")
    md.append("## 2. 答案形状（dev gold 实跑）\n")
    md.append("| 返回行数 | 条数 | 占比 |\n|---|---:|---:|")
    for k, v in result["dev_execution"]["rowcount_dist"].items():
        md.append(f"| {k} | {v} | {100*v/dev_r['n']:.1f}% |")
    md.append(f"\n执行报错：{result['dev_execution']['errors']} 条\n")
    md.append("## 3. 形状迁移（用 train 挖掘的形状覆盖 dev）\n")
    st = result["shape_transfer"]
    md.append(f"- dev 形状数 {st['dev_distinct_shapes']}，train 形状数 {st['train_distinct_shapes']}")
    md.append(f"- train 形状对 dev 的覆盖率：{st['dev_coverage_pct']}%（未覆盖 {st['dev_unseen_queries']} 条 / {st['dev_unseen_shapes']} 个形状）")
    md.append("- dev 按频次排序的形状覆盖率曲线：" +
              "，".join(f"top{k} {v['coverage_pct']}%" for k, v in
                        [(c["top_k"], c) for c in st["head_curve"]]))
    md.append("")
    md.append("## 4. 问题与 evidence\n")
    md.append(f"- dev evidence 非空 {dev_r['evidence']['nonempty_pct']}%，含定义式 {dev_r['evidence']['with_definition_pct']}%")
    md.append(f"- train evidence 非空 {tr_r['evidence']['nonempty_pct']}%，含定义式 {tr_r['evidence']['with_definition_pct']}%")
    md.append("- dev 按难度：" + "；".join(
        f"{k} {v['n']} 条（evidence {v['evidence_pct']}%，定义式 {v['definition_pct']}%）"
        for k, v in dev_r["difficulty"].items()))
    md.append("")
    md.append("## 5. Schema 规模\n")
    s = result["dev_schema"]
    md.append(f"- dev 每库中位 {s['median_tables']} 表 / {s['median_cols']} 列；dev 中含空格/括号的列名 {s['total_weird_cols']} 个")
    md.append("- train 每库中位 5 表 / 70 列（最大 65 表 / 912 列）")
    md.append("")
    (out_dir / "report.md").write_text("\n".join(md), encoding="utf-8")

    # ------------------------------ console ---------------------------------
    dev_r, tr_r = result["dev"], result["train"]
    print("feature rates (% of queries)                     dev    train")
    for k, v in dev_r["rates"].items():
        print(f"  {k:24s} {v:6.1f} {tr_r['rates'][k]:7.1f}")
    print("tables joined:", dev_r["n_tables_dist"])
    print("projection:", dev_r["proj_dist"])
    print("aggs:", dev_r["agg_calls"])
    print("signatures:", dev_r["n_signatures"], "dev /", tr_r["n_signatures"], "train")
    print("shape transfer:", json.dumps(result["shape_transfer"], ensure_ascii=False)[:600])
    print("evidence:", dev_r["evidence"], tr_r["evidence"])
    print("difficulty:", json.dumps(dev_r["difficulty"], ensure_ascii=False))
    print("rowcount dist:", result["dev_execution"]["rowcount_dist"], "errors:", errors)
    print("schema:", result["dev_schema"]["median_tables"], result["dev_schema"]["median_cols"],
          "weird cols:", result["dev_schema"]["total_weird_cols"])
    print("written:", out_dir)


if __name__ == "__main__":
    main()
