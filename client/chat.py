#!/usr/bin/env python3
"""Amazon Bedrock 上の Claude と対話する薄い CLI クライアント。

- 通常モード: Converse API (converse_stream)。モデル差を吸収する統一 API のため、
  会話履歴を維持したままモデルを切り替えられる。
- RAG モード (Phase 2): Knowledge Base の RetrieveAndGenerate API。
  自分のドキュメントを検索し、出典付きで回答する。sessionId により
  マルチターンの会話も KB 側で維持される。
- 既定モデルは日本国内完結 (東京/大阪ルーティング) の JP cross-region
  inference profile。
- 会話ログは JSONL 形式で logs/ 配下にローカル保存する。

使い方:
    python chat.py                        # 対話モード (デフォルト: sonnet)
    python chat.py --rag                  # RAG モードで起動 (KB は名前で自動検出)
    python chat.py --rag --once "質問文"  # RAG 単発実行
    python chat.py -m haiku               # モデル指定
    python chat.py --profile bedrock-lab  # AWS プロファイル指定

注意: 通常モードと RAG モードは会話文脈が別 (通常=ローカル履歴 / RAG=KB セッション)。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# 日本国内完結 (東京/大阪) の cross-region inference profile
MODEL_ALIASES: dict[str, str] = {
    "sonnet": "jp.anthropic.claude-sonnet-4-6",
    "opus": "jp.anthropic.claude-opus-4-8",
    "haiku": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet45": "jp.anthropic.claude-sonnet-4-5-20250929-v1:0",
}
DEFAULT_ALIAS = "sonnet"
DEFAULT_REGION = "ap-northeast-1"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_KB_NAME = "bedrock-lab-kb"
LOG_DIR = Path(__file__).resolve().parent / "logs"

HELP_TEXT = """\
チャット内コマンド:
  /model          現在のモデルとエイリアス一覧を表示
  /model <name>   モデル切替 (エイリアス or 推論プロファイルID)
  /rag            RAG モードの状態を表示
  /rag on|off     RAG モード切替 (on=KB検索+出典付き回答 / off=素のモデル)
                  ※通常モードと RAG モードの会話文脈は独立
  /clear          会話履歴をクリア (RAG セッションもリセット)
  /usage          このセッションの累計トークン数を表示 (通常モードのみ計上)
  /help           このヘルプ
  /quit           終了 (Ctrl-D でも可)
"""


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


def explain_client_error(err: ClientError) -> str:
    """代表的なエラーに日本語のヒントを付ける。"""
    code = err.response.get("Error", {}).get("Code", "")
    hints = {
        "AccessDeniedException": (
            "モデルアクセスが未有効化、または IAM 権限不足の可能性。\n"
            "  - モデル利用開始手続き (use case フォーム + アグリーメント) が済んでいるか確認\n"
            "  - assume しているロールに bedrock:InvokeModel* / bedrock:Retrieve* 権限があるか確認"
        ),
        "ResourceNotFoundException": (
            "モデル ID / 推論プロファイル ID / Knowledge Base ID がこのリージョンに存在しない。\n"
            "  - リージョン (--region) と ID の組み合わせを確認"
        ),
        "ThrottlingException": "スロットリング発生。時間をおいて再実行。",
        "ValidationException": "リクエスト形式エラー。モデル ID や max-tokens を確認。",
        "ExpiredTokenException": "一時クレデンシャルの期限切れ。SSO ログイン / assume-role をやり直す。",
    }
    hint = hints.get(code, "")
    return f"[{code}] {err.response.get('Error', {}).get('Message', str(err))}" + (
        f"\nヒント: {hint}" if hint else ""
    )


class ChatSession:
    def __init__(
        self,
        clients: dict,
        model_id: str,
        system_prompt: str | None,
        max_tokens: int,
        log_path: Path,
        region: str,
        account_id: str,
        kb_id: str | None = None,
        rag_enabled: bool = False,
    ) -> None:
        self.clients = clients
        self.model_id = model_id
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.log_path = log_path
        self.region = region
        self.account_id = account_id
        self.kb_id = kb_id
        self.rag_enabled = rag_enabled
        self.rag_session_id: str | None = None
        self.messages: list[dict] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # ---- モデル ARN (RAG の modelArn は inference profile の ARN を渡す) ----
    @property
    def model_arn(self) -> str:
        if self.model_id.startswith("arn:"):
            return self.model_id
        return (
            f"arn:aws:bedrock:{self.region}:{self.account_id}"
            f":inference-profile/{self.model_id}"
        )

    # ---- 送信 (モードで分岐) ----
    def send(self, user_text: str) -> None:
        if self.rag_enabled:
            self._send_rag(user_text)
        else:
            self._send_converse(user_text)

    # ---- 通常モード: Converse API ----
    def _send_converse(self, user_text: str) -> None:
        self.messages.append({"role": "user", "content": [{"text": user_text}]})

        kwargs: dict = {
            "modelId": self.model_id,
            "messages": self.messages,
            "inferenceConfig": {"maxTokens": self.max_tokens},
        }
        if self.system_prompt:
            kwargs["system"] = [{"text": self.system_prompt}]

        parts: list[str] = []
        usage: dict = {}
        stop_reason = ""
        try:
            response = self.clients["runtime"].converse_stream(**kwargs)
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"].get("text", "")
                    print(delta, end="", flush=True)
                    parts.append(delta)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
            print()
        except KeyboardInterrupt:
            print("\n(中断しました。このターンは履歴に含めません)")
            self.messages.pop()
            return
        except (ClientError, BotoCoreError) as err:
            self.messages.pop()
            self._print_error(err)
            return

        assistant_text = "".join(parts)
        self.messages.append(
            {"role": "assistant", "content": [{"text": assistant_text}]}
        )
        self.total_input_tokens += usage.get("inputTokens", 0)
        self.total_output_tokens += usage.get("outputTokens", 0)
        self._write_log("converse", user_text, assistant_text,
                        usage=usage, stop_reason=stop_reason)

    # ---- RAG モード: RetrieveAndGenerate API ----
    def _send_rag(self, user_text: str) -> None:
        kwargs: dict = {
            "input": {"text": user_text},
            "retrieveAndGenerateConfiguration": {
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": self.kb_id,
                    "modelArn": self.model_arn,
                },
            },
        }
        # sessionId を渡すと KB 側で会話文脈が維持される (マルチターン)
        if self.rag_session_id:
            kwargs["sessionId"] = self.rag_session_id

        try:
            resp = self.clients["agent"].retrieve_and_generate(**kwargs)
        except (ClientError, BotoCoreError) as err:
            self._print_error(err)
            return

        self.rag_session_id = resp.get("sessionId")
        answer = resp["output"]["text"]
        print(answer)

        # 出典の集約表示 (チャンク単位の引用をドキュメント単位に重複排除)
        sources: dict[str, dict] = {}
        for citation in resp.get("citations", []):
            for ref in citation.get("retrievedReferences", []):
                md = ref.get("metadata", {})
                key = md.get("source_url") or md.get(
                    "x-amz-bedrock-kb-source-uri", "?"
                )
                sources[key] = {
                    "title": md.get("title", key),
                    "published": md.get("published", "?"),
                    "url": md.get("source_url", ""),
                }
        if sources:
            print("\n--- 出典 ---")
            for s in sources.values():
                line = f"・{s['title']} ({s['published']})"
                if s["url"]:
                    line += f" {s['url']}"
                print(line)

        self._write_log("rag", user_text, answer,
                        sources=list(sources.values()))

    # ---- ローカル会話ログ (JSONL) ----
    def _write_log(self, mode: str, user_text: str, assistant_text: str,
                   usage: dict | None = None, stop_reason: str = "",
                   sources: list | None = None) -> None:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "mode": mode,
            "model": self.model_id,
            "user": user_text,
            "assistant": assistant_text,
        }
        if usage is not None:
            record["usage"] = usage
        if stop_reason:
            record["stop_reason"] = stop_reason
        if sources:
            record["sources"] = sources
        if mode == "rag":
            record["kb_id"] = self.kb_id
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _print_error(self, err: Exception) -> None:
        if isinstance(err, ClientError):
            print(f"\nエラー: {explain_client_error(err)}", file=sys.stderr)
        else:
            print(f"\n接続エラー: {err}", file=sys.stderr)

    # ---- チャット内コマンド ----
    def show_usage(self) -> None:
        print(
            f"累計トークン (通常モード分): input={self.total_input_tokens} "
            f"output={self.total_output_tokens}"
        )

    def switch_model(self, name: str) -> None:
        self.model_id = resolve_model(name)
        print(f"モデルを切り替えました: {self.model_id}")

    def show_model(self) -> None:
        print(f"現在のモデル: {self.model_id}")
        print("エイリアス一覧:")
        for alias, model_id in MODEL_ALIASES.items():
            print(f"  {alias:10s} -> {model_id}")

    def toggle_rag(self, arg: str) -> None:
        if arg == "on":
            if not self.kb_id:
                print("Knowledge Base が見つからないため RAG モードにできません。")
                return
            self.rag_enabled = True
            print(f"RAG モード ON (KB: {self.kb_id})。出典付きで回答します。")
        elif arg == "off":
            self.rag_enabled = False
            print("RAG モード OFF。素のモデルと対話します。")
        else:
            state = f"ON (KB: {self.kb_id})" if self.rag_enabled else "OFF"
            print(f"RAG モード: {state}")

    def clear(self) -> None:
        self.messages = []
        self.rag_session_id = None
        print("会話履歴をクリアしました (RAG セッションもリセット)。")


def build_session(region: str, profile: str | None):
    session = boto3.Session(profile_name=profile, region_name=region)
    cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=300)
    clients = {
        "runtime": session.client("bedrock-runtime", config=cfg),
        "agent": session.client("bedrock-agent-runtime", config=cfg),
    }
    account_id = session.client("sts").get_caller_identity()["Account"]
    return session, clients, account_id


def resolve_kb_id(session, explicit_id: str | None) -> str | None:
    """--kb-id 指定があればそれを、なければ名前で自動検出する。"""
    if explicit_id:
        return explicit_id
    try:
        client = session.client("bedrock-agent")
        resp = client.list_knowledge_bases(maxResults=50)
        for kb in resp.get("knowledgeBaseSummaries", []):
            if kb["name"] == DEFAULT_KB_NAME:
                return kb["knowledgeBaseId"]
    except (ClientError, BotoCoreError):
        pass
    return None


def interactive_loop(chat: ChatSession) -> None:
    mode = f"RAG (KB: {chat.kb_id})" if chat.rag_enabled else "通常"
    print(f"Bedrock Claude チャット (model: {chat.model_id} / モード: {mode})")
    print("/help でコマンド一覧、/quit で終了")
    while True:
        try:
            prompt = "rag> " if chat.rag_enabled else "you> "
            user_input = input(f"\n{prompt}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd, _, arg = user_input.partition(" ")
            arg = arg.strip()
            if cmd in ("/quit", "/exit"):
                break
            elif cmd == "/help":
                print(HELP_TEXT)
            elif cmd == "/model":
                chat.switch_model(arg) if arg else chat.show_model()
            elif cmd == "/rag":
                chat.toggle_rag(arg)
            elif cmd == "/clear":
                chat.clear()
            elif cmd == "/usage":
                chat.show_usage()
            else:
                print(f"不明なコマンド: {cmd} (/help 参照)")
            continue

        chat.send(user_input)

    chat.show_usage()
    print(f"会話ログ: {chat.log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Amazon Bedrock 上の Claude と対話する CLI (Converse / RAG)"
    )
    parser.add_argument(
        "-m",
        "--model",
        default=DEFAULT_ALIAS,
        help=f"モデルエイリアス or 推論プロファイルID (default: {DEFAULT_ALIAS})",
    )
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None, help="AWS CLI プロファイル名")
    parser.add_argument("--system", default=None, help="システムプロンプト (通常モードのみ)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--once", metavar="PROMPT", help="単発実行して終了")
    parser.add_argument(
        "--rag", action="store_true",
        help="RAG モードで起動 (Knowledge Base 検索 + 出典付き回答)",
    )
    parser.add_argument(
        "--kb-id", default=None,
        help=f"Knowledge Base ID (省略時は名前 '{DEFAULT_KB_NAME}' で自動検出)",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=LOG_DIR, help="会話ログの出力先"
    )
    args = parser.parse_args()

    model_id = resolve_model(args.model)
    log_path = (
        args.log_dir
        / f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )

    try:
        session, clients, account_id = build_session(args.region, args.profile)
    except (ClientError, BotoCoreError) as err:
        print(f"AWS クライアント初期化に失敗: {err}", file=sys.stderr)
        return 1

    kb_id = resolve_kb_id(session, args.kb_id)
    if args.rag and not kb_id:
        print(
            f"Knowledge Base が見つかりません (名前 '{DEFAULT_KB_NAME}' の自動検出に失敗)。\n"
            "--kb-id で明示指定するか、terraform output kb_id を確認してください。",
            file=sys.stderr,
        )
        return 1

    chat = ChatSession(
        clients,
        model_id,
        args.system,
        args.max_tokens,
        log_path,
        region=args.region,
        account_id=account_id,
        kb_id=kb_id,
        rag_enabled=args.rag,
    )

    if args.once:
        chat.send(args.once)
        return 0

    interactive_loop(chat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
