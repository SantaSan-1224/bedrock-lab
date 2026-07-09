#!/usr/bin/env python3
"""KB 投入用ドキュメントの生成スクリプト。

Qiita API から公開記事 (全件・本文込み) を取得し、bedrock-lab のドキュメントと
あわせて「本文 .md + メタデータサイドカー .metadata.json」の形式で出力する。

使い方:
    python3 scripts/build_kb_docs.py            # ./kb_docs/ に生成
    aws s3 sync ./kb_docs/ s3://<kb_source_bucket>/
    aws bedrock-agent start-ingestion-job --knowledge-base-id <id> --data-source-id <id>

メタデータサイドカーは KB のメタデータフィルタ・出典表示に使われる。
S3 Vectors 利用時はカスタムメタデータ合計 1KB/ベクトルの制限に注意。
"""

import json
import pathlib
import sys
import urllib.request

QIITA_USER = "kazu_techlog"
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "kb_docs"
LAB_ROOT = pathlib.Path(__file__).resolve().parent.parent


def write_doc(dirpath: pathlib.Path, fname: str, body: str, meta: dict) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / fname).write_text(body, encoding="utf-8")
    sidecar = {"metadataAttributes": meta}
    (dirpath / (fname + ".metadata.json")).write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )


def fetch_qiita_items() -> list[dict]:
    items = []
    for page in range(1, 5):
        url = f"https://qiita.com/api/v2/users/{QIITA_USER}/items?per_page=50&page={page}"
        with urllib.request.urlopen(url) as r:
            batch = json.load(r)
        items.extend(batch)
        if len(batch) < 50:
            break
    return items


def main() -> int:
    qiita_dir = OUT_DIR / "qiita"
    lab_dir = OUT_DIR / "lab-docs"

    items = fetch_qiita_items()
    for it in items:
        date = it["created_at"][:10]
        fname = f"{date}_{it['id']}.md"
        tags = ",".join(t["name"] for t in it["tags"])
        header = f"# {it['title']}\n\n公開日: {date} / 媒体: Qiita / URL: {it['url']}\n\n"
        write_doc(
            qiita_dir, fname, header + it["body"],
            {"title": it["title"], "source_url": it["url"],
             "published": date, "tags": tags, "media": "qiita"},
        )

    for src, title in [
        (LAB_ROOT / "README.md", "bedrock-lab README"),
        (LAB_ROOT / "docs/plan_phase1.md", "bedrock-lab Phase1 計画書+実装補正"),
        (LAB_ROOT / "docs/plan_phase2.md", "bedrock-lab Phase2 計画書"),
        (LAB_ROOT / "docs/rag_comparison.md", "bedrock-lab RAG 3方式比較メモ"),
    ]:
        if not src.exists():
            continue
        write_doc(
            lab_dir, src.name, src.read_text(encoding="utf-8"),
            {"title": title, "published": "2026-07-09", "media": "bedrock-lab",
             "source_url": "https://github.com/SantaSan-1224/bedrock-lab"},
        )

    n = len(list(OUT_DIR.rglob("*.md")))
    print(f"生成完了: {OUT_DIR} (本文 {n} 件 + サイドカー)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
