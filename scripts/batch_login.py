#!/usr/bin/env python3
"""批量创建登录会话（不发 webhook）。

用法:
  # CSV：第 1 列 username，第 2 列 password（支持表头）
  uv run python scripts/batch_login.py --csv accounts.csv

  # 假账号
  uv run python scripts/batch_login.py -n 20
  uv run python scripts/batch_login.py -n 5 --silent
"""

from __future__ import annotations

import argparse
import csv
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def fake_account(index: int) -> tuple[str, str]:
    """生成唯一假账号（避免同 username 409）。"""
    suffix = uuid.uuid4().hex[:8]
    username = f"fake_{index:04d}_{suffix}@example.com"
    password = f"Pw_{secrets.token_hex(6)}"
    return username, password


def load_csv_accounts(path: Path) -> list[tuple[str, str]]:
    """读取 CSV：第 1 列 username，第 2 列 password；首行若是表头则跳过。"""
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    accounts: list[tuple[str, str]] = []
    for i, row in enumerate(rows):
        if not row or all(not str(c).strip() for c in row):
            continue
        if len(row) < 2:
            raise ValueError(f"{path}:{i + 1}: 至少需要两列 username,password")
        user = str(row[0]).strip()
        pwd = str(row[1]).strip()
        if i == 0 and user.lower() in {"username", "user", "账号", "用户名"}:
            continue
        if not user or not pwd:
            raise ValueError(f"{path}:{i + 1}: username/password 不能为空")
        accounts.append((user, pwd))
    if not accounts:
        raise ValueError(f"{path}: 没有有效账号行")
    return accounts


def submit_one(
    client: httpx.Client,
    *,
    base: str,
    headers: dict[str, str],
    index: int,
    total: int,
    username: str,
    password: str,
    priority: int,
    silent: bool,
) -> bool:
    body = {
        "username": username,
        "password": password,
        "priority": priority,
        "silent": silent,
        "client_ref": f"batch-{index}",
    }
    try:
        resp = client.post(f"{base}/v1/sessions", headers=headers, json=body)
        if resp.status_code == 202:
            data = resp.json()
            print(
                f"[{index}/{total}] ok id={data.get('id')} "
                f"status={data.get('status')} "
                f"pos={data.get('queue_position')} user={username}"
            )
            return True
        print(
            f"[{index}/{total}] fail HTTP {resp.status_code} "
            f"user={username} body={resp.text[:300]}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[{index}/{total}] error user={username}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="批量提交登录（无 webhook）")
    parser.add_argument(
        "--csv",
        type=Path,
        help="账号 CSV：第 1 列 username，第 2 列 password",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=10,
        help="假账号数量（未指定 --csv 时生效，默认 10）",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("BATCH_BASE_URL", "http://127.0.0.1:8056"),
        help="API 地址",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("API_TOKEN", "dev-token-change-me"),
        help="Bearer Token（默认读 API_TOKEN）",
    )
    parser.add_argument("--priority", type=int, default=1, help="优先级，越大越先")
    parser.add_argument(
        "--silent",
        action="store_true",
        help="静默模式：遇验证码/扫码直接失败",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.05,
        help="每次请求间隔秒数（默认 0.05）",
    )
    args = parser.parse_args()

    if args.csv is not None:
        try:
            accounts = load_csv_accounts(args.csv.expanduser().resolve())
        except (OSError, ValueError) as exc:
            print(f"读取 CSV 失败: {exc}", file=sys.stderr)
            return 2
        source = f"csv={args.csv}"
    else:
        if args.count < 1:
            print("count 必须 >= 1", file=sys.stderr)
            return 2
        accounts = [fake_account(i) for i in range(1, args.count + 1)]
        source = f"fake count={args.count}"

    base = args.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }
    total = len(accounts)
    ok = 0
    fail = 0

    print(
        f"批量入队 {source} total={total} base={base} "
        f"priority={args.priority} silent={args.silent} webhook=无"
    )

    with httpx.Client(timeout=30.0) as client:
        for i, (username, password) in enumerate(accounts, start=1):
            if submit_one(
                client,
                base=base,
                headers=headers,
                index=i,
                total=total,
                username=username,
                password=password,
                priority=args.priority,
                silent=args.silent,
            ):
                ok += 1
            else:
                fail += 1
            if args.interval > 0 and i < total:
                time.sleep(args.interval)

    print(f"完成 ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
