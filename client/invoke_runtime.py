#!/usr/bin/env python3
"""AgentCore Runtime 上の運用エージェントを呼び出すクライアント (Phase 3 方式①)。

ローカル版 (agent_cli.py) との違い:
  - エージェントループはクラウド側 (Runtime) で回る。手元は質問を送るだけ
  - 会話文脈は runtimeSessionId 単位。同じ ID で送り続ければ
    Runtime 側のプロセスメモリにある履歴が使われる

使い方:
    python invoke_runtime.py --profile bedrock-lab            # 対話
    python invoke_runtime.py --profile bedrock-lab --once "今月のコストは?"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_REGION = "ap-northeast-1"
RUNTIME_NAME = "bedrock_lab_ops_agent"


def resolve_runtime_arn(session) -> str | None:
    try:
        client = session.client("bedrock-agentcore-control")
        resp = client.list_agent_runtimes(maxResults=50)
        for rt in resp.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == RUNTIME_NAME:
                return rt["agentRuntimeArn"]
    except (ClientError, BotoCoreError) as err:
        print(f"Runtime 検索エラー: {err}", file=sys.stderr)
    return None


def invoke(client, arn: str, session_id: str, prompt: str, verbose: bool) -> None:
    t0 = time.perf_counter()
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode(),
        )
        body = resp["response"].read()
    except (ClientError, BotoCoreError) as err:
        print(f"呼び出しエラー: {err}", file=sys.stderr)
        return
    elapsed = time.perf_counter() - t0

    try:
        data = json.loads(body)
        print(data.get("result") or data.get("error") or data)
    except json.JSONDecodeError:
        print(body.decode(errors="replace"))
    if verbose:
        print(f"\n--- round-trip: {elapsed:.1f}s / session: {session_id} ---")


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentCore Runtime 呼び出しクライアント")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--arn", default=None, help="Runtime ARN (省略時は名前から自動検出)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--once", metavar="PROMPT")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=300)
    client = session.client("bedrock-agentcore", config=cfg)

    arn = args.arn or resolve_runtime_arn(session)
    if not arn:
        print("Runtime が見つかりません (--arn で指定可)", file=sys.stderr)
        return 1

    # runtimeSessionId は 33 文字以上が必要。uuid4 の文字列表現 (36字) を使う
    session_id = str(uuid.uuid4())

    if args.once:
        invoke(client, arn, session_id, args.once, args.verbose)
        return 0

    print(f"AgentCore Runtime 対話モード (runtime: {RUNTIME_NAME})")
    print(f"セッション: {session_id} (同一セッションで会話文脈が維持されるか観察できる)")
    print("/quit または Ctrl-D で終了、/new で新しいセッション")
    while True:
        try:
            query = input("\nremote-agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.startswith("/"):
            if query in ("/quit", "/exit"):
                break
            if query == "/new":
                session_id = str(uuid.uuid4())
                print(f"新しいセッション: {session_id}")
                continue
            print("コマンドは /quit, /new のみです")
            continue
        invoke(client, arn, session_id, query, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
