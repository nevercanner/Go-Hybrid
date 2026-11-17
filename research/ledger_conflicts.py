#!/usr/bin/env python3
"""ledger_conflicts.py — 把 MOONSHOT 账本里"哪两条在翻同一行 GUARANTEES"算出来。

覆盖口径：默认只读账本正文里的行号（下界）；加 --expand 才去读每条提案 issue 的正文，
覆盖率会从 5/27 升到 19/27，相交对从 3 组升到 27 组。两份数字都要报，不能只报一个。

动机：MOONSHOT.md 的裁决里反复出现 "dup-credit / merge judgment at the window"
"closest competitor W-022 which also flips Row 10" 这类人工判断。人工判断的问题
不是不准，是不可复算——两个人看同一份账本会得出不同的合并结论。

本脚本只做一件事：给定 MOONSHOT.md（账本表）与 guarantees.json（行 id），
输出每一对"翻转行集合相交"的提案，并按机制家族分成两类：

  merge     同家族 + 同行      → 应该合并计一次 credit
  conflict  不同家族 + 同行    → 两条对同一行的保证给出不同机制，必须裁决谁对

用法：
    python3 ledger_conflicts.py --moonshot MOONSHOT.md --table guarantees.json
        # 只读账本正文里的行号（口径一，结果=下界）
    python3 ledger_conflicts.py --moonshot MOONSHOT.md --table guarantees.json --expand
        # 追加读每条提案 issue 的正文（口径二，需要 gh CLI 且有网络）
    python3 ledger_conflicts.py --json        # 机器可读

退出码：0 = 正常；2 = 输入缺失。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LEDGER_ROW = re.compile(r"^\|\s*(W-\d{3})\s*\|.*$", re.M)
ROW_FLIP = re.compile(r"\brows?\s+(\d+(?:\s*(?:,|&|and)\s*\d+)*)", re.I)
# 只有出现在 GUARANTEES 语境里的行号才算数：
#   "corpus rows 21-30"（issue #193）是夹具编号，不是保证表的行，不能被当成 Row 21。
GUARANTEES_CTX = re.compile(r"guarantee|claims table", re.I)
NOT_GUARANTEES_CTX = re.compile(
    r"corpus|fixture|test case|test-case|\brows?\s+\d+\s*[\u2013\u2014-]\s*\d+", re.I)


def rows_in(text: str) -> set[int]:
    """抽取 GUARANTEES 行号：逐句判断语境，排除 corpus / fixture / 区间编号。"""
    found: set[int] = set()
    for sentence in re.split(r"(?<=[.!?;\u3002\uff1b])\s+|\n", text):
        if not ROW_FLIP.search(sentence):
            continue
        if NOT_GUARANTEES_CTX.search(sentence) or not GUARANTEES_CTX.search(sentence):
            continue
        for m in ROW_FLIP.finditer(sentence):
            found |= {int(x) for x in re.split(r"[^\d]+", m.group(1)) if x}
    return found

# 机制家族：同家族的提案在翻同一行时是"同一件事的不同写法"，不同家族才是真冲突。
FAMILIES = {
    "capability": ("capabilit", "linear", "own[", "share[", "view[", "affine", "region", "slab",
                   "take-and-detach", "detach", "move lowering", "slot", "backedge", "loan"),
    "escape-site": ("go-escape", "escape", "check-site", "capture", "foreign-call", "foreign call",
                    "a5"),
    "cancellation": ("cancel", "nursery", "settlement", "teardown", "drain", "settle"),
    "emit": ("emit", "witness", "asm", "libhyrt", "abi", "codegen", "translation validation"),
    "boundary": ("wasm", "wire boundary", "isolation", "message isolation", "clone"),
    "analysis-driver": ("in-process", "analysis driver", "gopls", "vet", "self-host", "toolexec"),
    "protocol": ("session type", "protocol", "deadlock", "wait-for", "epoch", "closed channel"),
}


def family_of(text: str) -> list[str]:
    """返回命中的机制家族集合（可能多个）。"""
    low = text.lower()
    hits = [fam for fam, keys in FAMILIES.items() if any(k in low for k in keys)]
    return sorted(hits) if hits else ["unclassified"]


def parse_ledger(moonshot: Path) -> list[dict]:
    text = moonshot.read_text(encoding="utf-8")
    # 只取 Ledger 小节，避免读到后面的 Review windows 表
    start = text.find("## Ledger")
    end = text.find("## Review windows")
    body = text[start:end] if start != -1 and end != -1 else text
    out = []
    for line in body.splitlines():
        if not line.startswith("| W-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        wid, summary, source = cells[0], cells[1], cells[3]
        rows = rows_in(summary)
        # Source 列才是提案 issue；摘要里的 #NNN 往往是 spike PR
        issue = re.match(r"#(\d{2,5})", source)
        out.append({
            "id": wid,
            "summary": summary[:120],
            "rows": sorted(rows),
            "issue": f"#{issue.group(1)}" if issue else None,
            "source": source[:40],
            "family": family_of(summary),
        })
    return out


def known_row_ids(table: Path) -> set[int]:
    data = json.loads(table.read_text(encoding="utf-8"))
    return {int(r["id"]) for r in data.get("rows", []) if str(r.get("id", "")).isdigit()}


def expand_from_issues(entries: list[dict]) -> tuple[int, int]:
    """口径二：读每条提案 issue 的正文补行号。返回 (补到几条, 尝试几条)。"""
    import subprocess
    tried = added = 0
    for e in entries:
        if e["rows"] or not e["issue"]:
            continue
        tried += 1
        p = subprocess.run(
            ["gh", "issue", "view", e["issue"].lstrip("#"), "-R", "limitlessprojects/Go-Hybrid",
             "--json", "body", "--jq", ".body"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        body = p.stdout or ""
        rows = rows_in(body)
        if rows:
            e["rows"] = sorted(rows)
            added += 1
    return added, tried


def conflicts(entries: list[dict]) -> list[dict]:
    pairs = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            hit = set(a["rows"]) & set(b["rows"])
            if not hit:
                continue
            # 同族交集非空 = 同一机制的不同写法（merge）；完全无交集 = 真冲突
            shared = set(a["family"]) & set(b["family"]) - {"unclassified"}
            # 一边归类不出来 ≠ 家族不同：这类对不判冲突，降级为人工裁决。
            unknown = "unclassified" in a["family"] or "unclassified" in b["family"]
            same = bool(shared)
            pairs.append({
                "a": a["id"], "b": b["id"],
                "rows": sorted(hit),
                "kind": "manual-review" if unknown and not same else ("merge" if same else "conflict"),
                "family_a": "+".join(a["family"]), "family_b": "+".join(b["family"]),
                "shared": sorted(set(a["family"]) & set(b["family"])),
            })
    return sorted(pairs, key=lambda p: (-len(p["rows"]), p["a"], p["b"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--moonshot", default="MOONSHOT.md")
    ap.add_argument("--table", default="guarantees.json")
    ap.add_argument("--expand", action="store_true",
                    help="追加读 issue 正文补行号（需要 gh CLI 与网络）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ms, tb = Path(args.moonshot), Path(args.table)
    if not ms.is_file():
        print(f"missing {ms}", file=sys.stderr)
        return 2

    entries = parse_ledger(ms)
    expanded = None
    if args.expand:
        expanded = expand_from_issues(entries)
    pairs = conflicts(entries)
    known = known_row_ids(tb) if tb.is_file() else set()

    if args.json:
        summary = {
            "entries": len(entries),
            "with_rows": sum(1 for e in entries if e["rows"]),
            "expanded": bool(args.expand),
            "merge": sum(1 for p in pairs if p["kind"] == "merge"),
            "conflict": sum(1 for p in pairs if p["kind"] == "conflict"),
            "manual_review": sum(1 for p in pairs if p["kind"] == "manual-review"),
            "unclassified_entries": [e["id"] for e in entries if "unclassified" in e["family"]],
        }
        print(json.dumps({"summary": summary, "entries": entries, "pairs": pairs},
                         ensure_ascii=False, indent=2))
        return 0

    cov = sum(1 for e in entries if e["rows"])
    print(f"账本条目 {len(entries)} 条｜有翻转行记录的 {cov} 条（口径：{'正文+issue' if args.expand else '仅正文'}）")
    if expanded:
        print(f"expand：尝试 {expanded[1]} 条 issue 正文，补到 {expanded[0]} 条")
    unknown = sorted({r for e in entries for r in e["rows"]} - known) if known else []
    if unknown:
        print(f"警告：账本提到但 guarantees.json 里没有的行号 {unknown}")
    merges = [p for p in pairs if p["kind"] == "merge"]
    conflicts_ = [p for p in pairs if p["kind"] == "conflict"]
    print(f"相交对 {len(pairs)} 组：merge {len(merges)} · conflict {len(conflicts_)}\n")

    print(f"{'A':6s} {'B':6s} {'行':8s} {'类型':9s} {'家族'}")
    for p in pairs:
        fam = p["family_a"] if p["kind"] == "merge" else f"{p['family_a']} vs {p['family_b']}"
        print(f"{p['a']:6s} {p['b']:6s} {','.join(map(str,p['rows'])):8s} {p['kind']:9s} {fam}")

    if not pairs:
        print("冲突矩阵为空 —— 判据被证伪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
