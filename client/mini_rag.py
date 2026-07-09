#!/usr/bin/env python3
"""自前ミニ RAG — KB の RetrieveAndGenerate を使わず、RAG の中身を1ステップずつ実行する学習用 CLI。

方式 (--search で切替):
  kb      : 方式② 検索は KB の Retrieve API (managed)、プロンプト合成と会話管理は自前
  vectors : 方式③ 埋め込み生成 (Titan V2) → S3 Vectors QueryVectors → 合成、の全工程を自前

chat.py の RAG モード (方式① RetrieveAndGenerate) との違い:
  - 会話履歴を自分で管理するため、フォローアップ (「〜だけ詳しく」) に追従できる
  - 検索資料は送信時のみプロンプトに注入し、履歴には質問と回答だけを残す
    (資料を履歴に積むと入力トークンが毎ターン膨張するため)

使い方:
    python mini_rag.py --search vectors --verbose          # 対話 (中間結果表示)
    python mini_rag.py --search kb --once "質問"           # 単発
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

MODEL_ID = "jp.anthropic.claude-sonnet-4-6"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSIONS = 1024
DEFAULT_REGION = "ap-northeast-1"
DEFAULT_KB_NAME = "bedrock-lab-kb"
DEFAULT_VECTOR_BUCKET = "bedrock-lab-vectors"
DEFAULT_VECTOR_INDEX = "bedrock-lab-kb-index"
DEFAULT_TOP_K = 5
LOG_DIR = Path(__file__).resolve().parent / "logs"

RAG_SYSTEM_PROMPT = """\
あなたは個人の技術ナレッジベース (本人が書いた記事・メモ) を参照して回答するアシスタントです。

- 各質問には「参考資料」が添付されます。回答は原則として資料の内容に基づいてください
- 資料に無い内容で補足する場合は「(資料外の一般知識)」と明示してください
- 会話の文脈を踏まえ、直前の回答と重複する説明は繰り返さないでください。
  「〜だけ詳しく」のような絞り込みの指示には、該当部分のみを深掘りして答えてください
- 回答の最後に、実際に参照した資料のタイトルを「出典:」として列挙してください
"""


# ---------- 検索 (方式②: KB Retrieve) ----------
def search_kb(clients, kb_id: str, query: str, top_k: int) -> list[dict]:
    resp = clients["agent"].retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": top_k}
        },
    )
    chunks = []
    for r in resp.get("retrievalResults", []):
        md = r.get("metadata", {})
        chunks.append(
            {
                "score": r.get("score", 0.0),
                "title": md.get("title", "?"),
                "published": md.get("published", "?"),
                "url": md.get("source_url", ""),
                "text": r.get("content", {}).get("text", ""),
            }
        )
    return chunks


# ---------- 検索 (方式③: 埋め込み + S3 Vectors 直叩き) ----------
def embed_text(clients, text: str) -> list[float]:
    body = json.dumps(
        {"inputText": text, "dimensions": EMBED_DIMENSIONS, "normalize": True}
    )
    resp = clients["runtime"].invoke_model(modelId=EMBED_MODEL_ID, body=body)
    return json.loads(resp["body"].read())["embedding"]


def search_vectors(clients, bucket: str, index: str, query: str, top_k: int,
                   timings: dict) -> list[dict]:
    t0 = time.perf_counter()
    vector = embed_text(clients, query)
    timings["embed_ms"] = int((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    resp = clients["s3vectors"].query_vectors(
        vectorBucketName=bucket,
        indexName=index,
        queryVector={"float32": vector},
        topK=top_k,
        returnMetadata=True,
        returnDistance=True,
    )
    timings["query_ms"] = int((time.perf_counter() - t0) * 1000)

    chunks = []
    for v in resp.get("vectors", []):
        md = v.get("metadata", {})
        chunks.append(
            {
                # S3 Vectors は distance を返す (cosine: 小さいほど近い)。
                # KB Retrieve の score (大きいほど近い) と向きが逆な点に注意
                "score": 1.0 - v.get("distance", 1.0),
                "title": md.get("title", "?"),
                "published": md.get("published", "?"),
                "url": md.get("source_url", ""),
                "text": md.get("AMAZON_BEDROCK_TEXT", ""),
            }
        )
    return chunks


# ---------- プロンプト合成 ----------
def build_user_message(query: str, chunks: list[dict]) -> str:
    parts = ["# 参考資料\n"]
    for i, c in enumerate(chunks, 1):
        parts.append(
            f"## 資料{i}: {c['title']} (公開日: {c['published']})\n{c['text']}\n"
        )
    parts.append(f"\n# 質問\n{query}")
    return "\n".join(parts)


# ---------- 会話 ----------
class MiniRagSession:
    def __init__(self, clients, search_mode: str, kb_id: str | None,
                 bucket: str, index: str, top_k: int, verbose: bool,
                 log_path: Path):
        self.clients = clients
        self.search_mode = search_mode
        self.kb_id = kb_id
        self.bucket = bucket
        self.index = index
        self.top_k = top_k
        self.verbose = verbose
        self.log_path = log_path
        # 履歴には「生の質問」と「回答」だけを残す (資料は毎ターン差し替え)
        self.history: list[dict] = []

    def ask(self, query: str) -> None:
        timings: dict = {}

        # --- 検索 ---
        t0 = time.perf_counter()
        try:
            if self.search_mode == "kb":
                chunks = search_kb(self.clients, self.kb_id, query, self.top_k)
            else:
                chunks = search_vectors(self.clients, self.bucket, self.index,
                                        query, self.top_k, timings)
        except (ClientError, BotoCoreError) as err:
            print(f"検索エラー: {err}", file=sys.stderr)
            return
        timings["search_total_ms"] = int((time.perf_counter() - t0) * 1000)

        if self.verbose:
            print(f"--- 検索ヒット ({self.search_mode}, {timings['search_total_ms']}ms) ---")
            for c in chunks:
                head = c["text"][:80].replace("\n", " ")
                print(f"  score={c['score']:.4f} | {c['title']} | {head}...")
            print()

        # --- 生成 (履歴 + 今回だけ資料付きメッセージ) ---
        messages = list(self.history)
        messages.append(
            {"role": "user", "content": [{"text": build_user_message(query, chunks)}]}
        )

        t0 = time.perf_counter()
        parts: list[str] = []
        usage: dict = {}
        try:
            resp = self.clients["runtime"].converse_stream(
                modelId=MODEL_ID,
                messages=messages,
                system=[{"text": RAG_SYSTEM_PROMPT}],
                inferenceConfig={"maxTokens": 4096},
            )
            for event in resp["stream"]:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"].get("text", "")
                    print(delta, end="", flush=True)
                    parts.append(delta)
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
            print()
        except (ClientError, BotoCoreError) as err:
            print(f"\n生成エラー: {err}", file=sys.stderr)
            return
        timings["generate_ms"] = int((time.perf_counter() - t0) * 1000)

        answer = "".join(parts)
        # 履歴には資料抜きの質問を積む
        self.history.append({"role": "user", "content": [{"text": query}]})
        self.history.append({"role": "assistant", "content": [{"text": answer}]})

        if self.verbose:
            print(f"\n--- 計測: {timings} / usage: in={usage.get('inputTokens')} "
                  f"out={usage.get('outputTokens')} ---")

        self._write_log(query, answer, chunks, timings, usage)

    def _write_log(self, query, answer, chunks, timings, usage) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": f"mini_rag/{self.search_mode}",
            "model": MODEL_ID,
            "user": query,
            "assistant": answer,
            "hits": [{"score": c["score"], "title": c["title"]} for c in chunks],
            "timings": timings,
            "usage": usage,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def resolve_kb_id(session) -> str | None:
    try:
        resp = session.client("bedrock-agent").list_knowledge_bases(maxResults=50)
        for kb in resp.get("knowledgeBaseSummaries", []):
            if kb["name"] == DEFAULT_KB_NAME:
                return kb["knowledgeBaseId"]
    except (ClientError, BotoCoreError):
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="自前ミニ RAG (学習用)")
    parser.add_argument("--search", choices=["kb", "vectors"], default="vectors",
                        help="kb=方式② KB Retrieve / vectors=方式③ S3 Vectors 直叩き")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--kb-id", default=None)
    parser.add_argument("--vector-bucket", default=DEFAULT_VECTOR_BUCKET)
    parser.add_argument("--vector-index", default=DEFAULT_VECTOR_INDEX)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--verbose", action="store_true",
                        help="検索ヒット・レイテンシ・トークン数を表示")
    parser.add_argument("--once", metavar="PROMPT")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=300)
    clients = {
        "runtime": session.client("bedrock-runtime", config=cfg),
        "agent": session.client("bedrock-agent-runtime", config=cfg),
        "s3vectors": session.client("s3vectors", config=cfg),
    }

    kb_id = args.kb_id or (resolve_kb_id(session) if args.search == "kb" else None)
    if args.search == "kb" and not kb_id:
        print("Knowledge Base が見つかりません (--kb-id で指定可)", file=sys.stderr)
        return 1

    log_path = LOG_DIR / f"minirag_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    rag = MiniRagSession(clients, args.search, kb_id, args.vector_bucket,
                         args.vector_index, args.top_k, args.verbose, log_path)

    if args.once:
        rag.ask(args.once)
        return 0

    mode_label = "② KB Retrieve + 自前合成" if args.search == "kb" else "③ フル自前 (S3 Vectors 直叩き)"
    print(f"ミニ RAG 対話モード (方式{mode_label} / model: {MODEL_ID})")
    print("Ctrl-D で終了")
    while True:
        try:
            query = input("\nmini-rag> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        rag.ask(query)

    print(f"ログ: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
