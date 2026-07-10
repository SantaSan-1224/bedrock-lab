#!/usr/bin/env python3
"""AgentCore Gateway (MCP) テストクライアント (Phase 3 M6)。

Gateway が公開する MCP エンドポイントに、SigV4 署名付きの JSON-RPC を送る。
MCP ライブラリを使わず素の HTTP で叩くことで、プロトコルの中身
(initialize → tools/list → tools/call) を観察するのが目的。

使い方:
    URL=$(cd ../terraform && terraform output -raw gateway_mcp_url)
    python mcp_gateway_client.py --profile bedrock-lab --url "$URL" --list
    python mcp_gateway_client.py --profile bedrock-lab --url "$URL" \
        --call ops___search_kb --args '{"query": "2048 bytes エラー"}'
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SERVICE = "bedrock-agentcore"
PROTOCOL_VERSION = "2025-03-26"


def rpc(url: str, region: str, creds, method: str, params: dict | None = None,
        rpc_id: int = 1, verbose: bool = False) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload, ensure_ascii=False).encode()

    req = AWSRequest(
        method="POST", url=url, data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    SigV4Auth(creds, SERVICE, region).add_auth(req)

    if verbose:
        print(f">>> {method} {json.dumps(params, ensure_ascii=False)[:120] if params else ''}",
              file=sys.stderr)
    http_req = urllib.request.Request(url, data=body, headers=dict(req.headers), method="POST")
    try:
        with urllib.request.urlopen(http_req, timeout=120) as r:
            raw = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        print(f"HTTP {err.code}: {err.read().decode()[:500]}", file=sys.stderr)
        raise SystemExit(1)

    if "text/event-stream" in ctype:
        # SSE 形式: data: 行の JSON を取り出す
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise ValueError(f"SSE に data が見つからない: {raw[:200]}")
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gateway MCP テストクライアント")
    parser.add_argument("--url", required=True, help="MCP エンドポイント (terraform output gateway_mcp_url)")
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--list", action="store_true", help="ツール一覧 (tools/list)")
    parser.add_argument("--call", metavar="TOOL", help="ツール呼び出し (tools/call)")
    parser.add_argument("--args", default="{}", help="ツール引数 (JSON)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    creds = session.get_credentials()

    # MCP の作法どおり initialize から入る (Gateway はステートレスだが観察のため)
    init = rpc(args.url, args.region, creds, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "bedrock-lab-mcp-client", "version": "0.1"},
    }, rpc_id=1, verbose=args.verbose)
    if args.verbose:
        print(f"<<< initialize: {json.dumps(init.get('result', init), ensure_ascii=False)[:200]}",
              file=sys.stderr)

    if args.list:
        resp = rpc(args.url, args.region, creds, "tools/list", {}, rpc_id=2,
                   verbose=args.verbose)
        tools = resp.get("result", {}).get("tools", [])
        print(f"ツール {len(tools)} 件:")
        for t in tools:
            print(f"  - {t['name']}: {t.get('description', '')[:80]}")
        return 0

    if args.call:
        resp = rpc(args.url, args.region, creds, "tools/call", {
            "name": args.call,
            "arguments": json.loads(args.args),
        }, rpc_id=2, verbose=args.verbose)
        result = resp.get("result", resp)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
