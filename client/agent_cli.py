#!/usr/bin/env python3
"""自前 tool use ループ — AWS 運用エージェント (Phase 3 方式②)。

Converse API の toolConfig / toolUse / toolResult をローカルで回す学習用 CLI。
エージェントループ (ツール選択 → 実行 → 観察 → 再計画 → 停止) の中身を
フレームワークなしで理解するのが目的。

ツールはすべて read-only (照会系のみ):
  get_cost       : Cost Explorer でコスト照会 (注意: $0.01/リクエスト)
  search_logs    : invocation logging のロググループを検索
  list_resources : Resource Groups Tagging API でリソース棚卸し
  search_kb      : Phase 2 の Knowledge Base を検索 (Retrieve API)

使い方:
    python agent_cli.py --profile bedrock-lab --verbose   # 対話
    python agent_cli.py --profile bedrock-lab --once "今月のコストは?"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

MODEL_ID = "jp.anthropic.claude-sonnet-4-6"
DEFAULT_REGION = "ap-northeast-1"
DEFAULT_KB_NAME = "bedrock-lab-kb"
LOG_GROUP = "/aws/bedrock/bedrock-lab/model-invocations"
MAX_STEPS = 8  # 暴走 (ツール連打) 対策。CE 課金 ($0.01/call) の上限にもなる
LOG_DIR = Path(__file__).resolve().parent / "logs"

SYSTEM_PROMPT = """\
あなたは個人 AWS アカウント (学習用ホームラボ) の運用アシスタントです。

- 質問に答えるために必要なツールだけを選んで使ってください
- get_cost は 1 リクエストごとに $0.01 かかります。必要な期間・粒度をまとめて1回で取得してください
- あなたの権限は照会 (read-only) のみです。変更・削除を求められたら、照会専用である旨を説明してください
- 回答は日本語で。ツールから得た事実と、あなたの解釈を区別して示してください
- ツール結果が空・エラーのときは、その旨を正直に伝えてください
"""

TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_cost",
                "description": (
                    "AWS のコストを Cost Explorer で照会する。期間指定がなければ今月分。"
                    "サービス別の内訳も取得できる。呼び出しごとに $0.01 かかるので必要時のみ使う。"
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "start_date": {"type": "string", "description": "開始日 YYYY-MM-DD (含む)。省略時は今月1日"},
                            "end_date": {"type": "string", "description": "終了日 YYYY-MM-DD (含まない)。省略時は明日"},
                            "group_by_service": {"type": "boolean", "description": "サービス別に分けるか。既定 true"},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "search_logs",
                "description": (
                    "Bedrock invocation logging の CloudWatch Logs を検索する。"
                    "モデル呼び出しの履歴・エラーの調査に使う。"
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "filter_pattern": {"type": "string", "description": "CloudWatch Logs のフィルタパターン。省略時は全件"},
                            "hours": {"type": "integer", "description": "何時間前まで遡るか。既定 24"},
                            "max_events": {"type": "integer", "description": "取得する最大イベント数。既定 10"},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "list_resources",
                "description": (
                    "アカウント内のリソースを Resource Groups Tagging API で棚卸しする。"
                    "サービス別の件数と ARN の一覧を返す。"
                ),
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            }
        },
        {
            "toolSpec": {
                "name": "search_kb",
                "description": (
                    "個人の技術ナレッジベース (本人が書いた記事・構築メモ) を検索する。"
                    "AWS のエラー対処・過去の検証内容・設計判断の記録を調べるときに使う。"
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "検索クエリ (日本語可)"},
                            "top_k": {"type": "integer", "description": "取得件数。既定 3"},
                        },
                        "required": ["query"],
                    }
                },
            }
        },
    ]
}


# ---------- ツール実装 (すべて read-only) ----------
def tool_get_cost(clients, inp: dict) -> dict:
    today = date.today()
    start = inp.get("start_date") or today.replace(day=1).isoformat()
    end = inp.get("end_date") or (today + timedelta(days=1)).isoformat()
    params = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
    }
    if inp.get("group_by_service", True):
        params["GroupBy"] = [{"Type": "DIMENSION", "Key": "SERVICE"}]
    resp = clients["ce"].get_cost_and_usage(**params)

    out = {"period": {"start": start, "end": end}, "results": []}
    for r in resp.get("ResultsByTime", []):
        entry = {"time": r["TimePeriod"]["Start"]}
        if r.get("Groups"):
            services = {}
            for g in r["Groups"]:
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                if amount >= 0.000001:
                    services[g["Keys"][0]] = round(amount, 6)
            entry["by_service_usd"] = dict(
                sorted(services.items(), key=lambda kv: kv[1], reverse=True)
            )
            entry["total_usd"] = round(sum(services.values()), 6)
        else:
            entry["total_usd"] = round(float(r["Total"]["UnblendedCost"]["Amount"]), 6)
        out["results"].append(entry)
    return out


def tool_search_logs(clients, inp: dict) -> dict:
    hours = inp.get("hours", 24)
    max_events = min(inp.get("max_events", 10), 50)
    start_ms = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
    params = {
        "logGroupName": LOG_GROUP,
        "startTime": start_ms,
        "limit": max_events,
    }
    if inp.get("filter_pattern"):
        params["filterPattern"] = inp["filter_pattern"]
    try:
        resp = clients["logs"].filter_log_events(**params)
    except clients["logs"].exceptions.ResourceNotFoundException:
        return {"error": f"ロググループ {LOG_GROUP} が存在しない (まだ呼び出しが無い可能性)"}

    events = []
    for e in resp.get("events", []):
        ts = datetime.fromtimestamp(e["timestamp"] / 1000).isoformat(timespec="seconds")
        # invocation ログは巨大な JSON。トークン膨張を防ぐため先頭のみ返す
        events.append({"time": ts, "message_head": e["message"][:500]})
    return {"log_group": LOG_GROUP, "window_hours": hours, "hit_count": len(events),
            "events": events}


def tool_list_resources(clients, inp: dict) -> dict:
    paginator = clients["tagging"].get_paginator("get_resources")
    arns = []
    for page in paginator.paginate(ResourcesPerPage=100):
        arns.extend(r["ResourceARN"] for r in page.get("ResourceTagMappingList", []))
        if len(arns) >= 200:
            break
    by_service: dict[str, int] = {}
    for arn in arns:
        svc = arn.split(":")[2]
        by_service[svc] = by_service.get(svc, 0) + 1
    return {"total": len(arns),
            "by_service": dict(sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)),
            "resource_arns": arns[:50]}


def tool_search_kb(clients, inp: dict, kb_id: str | None) -> dict:
    if not kb_id:
        return {"error": "Knowledge Base が見つからない"}
    resp = clients["agent"].retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": inp["query"]},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": inp.get("top_k", 3)}
        },
    )
    hits = []
    for r in resp.get("retrievalResults", []):
        md = r.get("metadata", {})
        hits.append({
            "score": round(r.get("score", 0.0), 4),
            "title": md.get("title", "?"),
            "url": md.get("source_url", ""),
            # KB は結合チャンク (最大8千字級) を返すため、ツール結果としては切り詰める
            "excerpt": r.get("content", {}).get("text", "")[:600],
        })
    return {"query": inp["query"], "hits": hits}


# ---------- エージェントループ ----------
class AgentSession:
    def __init__(self, clients, kb_id: str | None, verbose: bool, log_path: Path):
        self.clients = clients
        self.kb_id = kb_id
        self.verbose = verbose
        self.log_path = log_path
        self.history: list[dict] = []

    def _run_tool(self, name: str, inp: dict) -> dict:
        try:
            if name == "get_cost":
                return tool_get_cost(self.clients, inp)
            if name == "search_logs":
                return tool_search_logs(self.clients, inp)
            if name == "list_resources":
                return tool_list_resources(self.clients, inp)
            if name == "search_kb":
                return tool_search_kb(self.clients, inp, self.kb_id)
            return {"error": f"未知のツール: {name}"}
        except (ClientError, BotoCoreError) as err:
            # エラーもツール結果として返し、エージェント自身にリカバリさせる
            return {"error": str(err)}

    def ask(self, query: str) -> str | None:
        """1 つの質問についてエージェントループを回し、最終回答を返す。

        CLI (方式②) と AgentCore Runtime (方式①) の両方から呼ばれる。
        """
        messages = list(self.history)
        messages.append({"role": "user", "content": [{"text": query}]})
        steps: list[dict] = []
        total_usage = {"inputTokens": 0, "outputTokens": 0}
        answer = ""

        for step in range(1, MAX_STEPS + 1):
            t0 = time.perf_counter()
            try:
                resp = self.clients["runtime"].converse(
                    modelId=MODEL_ID,
                    messages=messages,
                    system=[{"text": SYSTEM_PROMPT}],
                    toolConfig=TOOL_CONFIG,
                    inferenceConfig={"maxTokens": 4096},
                )
            except (ClientError, BotoCoreError) as err:
                print(f"モデル呼び出しエラー: {err}", file=sys.stderr)
                return None
            llm_ms = int((time.perf_counter() - t0) * 1000)
            usage = resp.get("usage", {})
            total_usage["inputTokens"] += usage.get("inputTokens", 0)
            total_usage["outputTokens"] += usage.get("outputTokens", 0)

            out_msg = resp["output"]["message"]
            messages.append(out_msg)
            stop = resp.get("stopReason")

            if stop != "tool_use":
                answer = "".join(c.get("text", "") for c in out_msg["content"])
                print(answer)
                break

            # --- ツール実行フェーズ ---
            tool_results = []
            for block in out_msg["content"]:
                if "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                t1 = time.perf_counter()
                result = self._run_tool(tu["name"], tu.get("input", {}))
                tool_ms = int((time.perf_counter() - t1) * 1000)
                steps.append({"step": step, "tool": tu["name"], "input": tu.get("input", {}),
                              "llm_ms": llm_ms, "tool_ms": tool_ms,
                              "is_error": "error" in result})
                if self.verbose:
                    print(f"  [step {step}] {tu['name']}({json.dumps(tu.get('input', {}), ensure_ascii=False)}) "
                          f"→ {tool_ms}ms {'(error)' if 'error' in result else ''}")
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"json": result}],
                        **({"status": "error"} if "error" in result else {}),
                    }
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            answer = f"(ステップ上限 {MAX_STEPS} に到達したため打ち切りました)"
            print(answer)

        # 履歴にはユーザー質問と最終回答のみ残す (ツール往復は毎回捨てる。
        # 残すと入力トークンが急膨張するため — Phase 2 の履歴設計と同じ判断)
        self.history.append({"role": "user", "content": [{"text": query}]})
        self.history.append({"role": "assistant", "content": [{"text": answer}]})

        if self.verbose:
            print(f"\n--- steps: {len(steps)} / usage: in={total_usage['inputTokens']} "
                  f"out={total_usage['outputTokens']} ---")
        self._write_log(query, answer, steps, total_usage)
        return answer

    def _write_log(self, query, answer, steps, usage) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": "agent_cli/local",
            "model": MODEL_ID,
            "user": query,
            "assistant": answer,
            "steps": steps,
            "usage": usage,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_clients(session: boto3.Session) -> dict:
    cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=300)
    return {
        "runtime": session.client("bedrock-runtime", config=cfg),
        "agent": session.client("bedrock-agent-runtime", config=cfg),
        # Cost Explorer はグローバルサービス (エンドポイントは us-east-1 固定)。
        # 管理系 API のためデータ所在方針 (投入データの国内限定) とは別扱い
        "ce": session.client("ce", config=cfg),
        "logs": session.client("logs", config=cfg),
        "tagging": session.client("resourcegroupstaggingapi", config=cfg),
    }


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
    parser = argparse.ArgumentParser(description="AWS 運用エージェント (自前 tool use ループ)")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--kb-id", default=None)
    parser.add_argument("--verbose", action="store_true",
                        help="ツール呼び出し・レイテンシ・トークン数を表示")
    parser.add_argument("--once", metavar="PROMPT")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    clients = build_clients(session)
    kb_id = args.kb_id or resolve_kb_id(session)

    log_path = LOG_DIR / f"agent_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    agent = AgentSession(clients, kb_id, args.verbose, log_path)

    if args.once:
        agent.ask(args.once)
        return 0

    print(f"AWS 運用エージェント (自前 tool use ループ / model: {MODEL_ID})")
    print(f"ツール: get_cost / search_logs / list_resources / search_kb (KB: {kb_id or '未検出'})")
    print("/quit または Ctrl-D で終了")
    while True:
        try:
            query = input("\nagent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.startswith("/"):
            if query in ("/quit", "/exit"):
                break
            print("コマンドは /quit のみです (Ctrl-D でも終了できます)")
            continue
        agent.ask(query)

    print(f"ログ: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
