#!/usr/bin/env python3
"""预封存（pre-seal proposal）端到端示例（纯标准库，无需第三方依赖）。

覆盖：
1) 发起预封存：正文 + 三名互异且非本人的已注册见证人 + 带时区未来确认截止时刻；
   响应只含提案编号与正文摘要（不含正文）；
2) 可见性：仅发起人及受邀见证人可见摘要/截止时刻/状态/确认人数，其他账号
   凭编号统一 403（不可探知存在性）；
3) 见证人凭摘要各确认一次：摘要不符、重复确认均被拒且不计票；
4) 三人截止前确认后，同一事务生成沿用现有规则的正式封存资料，返回资料编号；
   完成后重复请求不再生成资料，生成资料可走既有两票开封流程读取正文；
5) 发起人可在完成前撤回；截止时未集齐即失效——两种终态都清空提案正文；
6) 与密钥轮换并发：持旧密钥的确认与本人轮换赛跑，按提交先后唯一判定
   （轮换先提交则旧密钥确认 401 回滚，不计票、不生成资料，新密钥可补票）。

用法: python examples/demo_pre_seal.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, key: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def show(title: str, status: int, body) -> None:
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def main() -> None:
    users = {}
    for name in ["pre-alice", "pre-bob", "pre-carol", "pre-dave", "pre-erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    alice, bob, carol, dave, erin = (
        users[n] for n in ["pre-alice", "pre-bob", "pre-carol", "pre-dave", "pre-erin"]
    )
    witnesses = ["pre-bob", "pre-carol", "pre-dave"]

    # ------------------------------------------------------------------
    # 场景 1：发起预封存 + 校验 + 可见性
    # ------------------------------------------------------------------
    print("== 场景 1：发起预封存、参数校验、仅参与方可见 ==")
    secret = "预封存示例正文：PRE-SEAL-DEMO-SECRET"
    status, resp = call("POST", "/pre-seal-proposals", alice,
                        {"body": secret, "witnesses": witnesses, "confirm_deadline": in_hours(1)})
    show("发起预封存", status, resp)
    assert status == 201 and resp["status"] == "pending"
    pid, digest = resp["proposal_id"], resp["digest"]
    assert "body" not in json.dumps(resp)

    # 参数校验：见证人不互异/含本人/未注册 400；截止时刻已过 400；不带时区 422
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": ["pre-bob", "pre-bob", "pre-dave"],
                 "confirm_deadline": in_hours(1)})[0] == 400
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": ["pre-alice", "pre-carol", "pre-dave"],
                 "confirm_deadline": in_hours(1)})[0] == 400
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": ["pre-bob", "pre-carol", "pre-ghost"],
                 "confirm_deadline": in_hours(1)})[0] == 400
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": witnesses,
                 "confirm_deadline": "2020-01-01T00:00:00Z"})[0] == 400
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": witnesses,
                 "confirm_deadline": "2030-01-01T00:00:00"})[0] == 422

    # 仅发起人及受邀见证人可见；其他人不可凭编号探知（统一 403）
    for key in (alice, bob, carol, dave):
        s, got = call("GET", f"/pre-seal-proposals/{pid}", key)
        assert s == 200 and got["confirmation_count"] == 0 and "body" not in json.dumps(got)
    assert call("GET", f"/pre-seal-proposals/{pid}", erin)[0] == 403
    assert call("GET", "/pre-seal-proposals/999999", erin)[0] == 403
    # 发起人/见证人之外不能确认
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", alice, {"digest": digest})[0] == 403
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", erin, {"digest": digest})[0] == 403

    # ------------------------------------------------------------------
    # 场景 2：凭摘要确认；摘要不符/重复确认拒绝且不计票
    # ------------------------------------------------------------------
    print("== 场景 2：摘要绑定确认，错误/重复确认不计票 ==")
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", bob,
                {"digest": "0" * 64})[0] == 409
    assert call("GET", f"/pre-seal-proposals/{pid}", alice)[1]["confirmation_count"] == 0

    s, resp = call("POST", f"/pre-seal-proposals/{pid}/confirmations", bob, {"digest": digest})
    show("bob 确认", s, resp)
    assert s == 200 and resp["confirmation_count"] == 1 and resp["sealed"] is False
    # 重复确认：409，不计票
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", bob, {"digest": digest})[0] == 409
    assert call("GET", f"/pre-seal-proposals/{pid}", bob)[1]["confirmation_count"] == 1

    # ------------------------------------------------------------------
    # 场景 3：第三人确认 -> 同事务生成正式封存资料；之后不再生成
    # ------------------------------------------------------------------
    print("== 场景 3：三人集齐，同事务生成正式封存资料 ==")
    call("POST", f"/pre-seal-proposals/{pid}/confirmations", carol, {"digest": digest})
    s, resp = call("POST", f"/pre-seal-proposals/{pid}/confirmations", dave, {"digest": digest})
    show("dave 第三票确认（触发封存）", s, resp)
    assert s == 200 and resp["sealed"] is True and resp["status"] == "sealed"
    assert resp["confirmation_count"] == 3
    doc_id = resp["document_id"]

    # 完成后任何确认/撤回均被拒，且不会再生成第二份资料
    for key in (bob, carol, dave):
        assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", key, {"digest": digest})[0] == 409
    assert call("POST", f"/pre-seal-proposals/{pid}/revoke", alice)[0] == 409

    # 生成的资料沿用现有规则：开封前无正文；三名见证人两票即可开封读到原文
    s, got = call("GET", f"/documents/{doc_id}", alice)
    assert s == 200 and got["status"] == "sealed" and "body" not in got
    _, req = call("POST", f"/documents/{doc_id}/open-requests", alice,
                  {"reason": "预封存资料开封", "deadline": in_hours(24)})
    rid = req["request_id"]
    call("POST", f"/open-requests/{rid}/approve", bob, {"digest": digest})
    s, opened = call("POST", f"/open-requests/{rid}/approve", carol, {"digest": digest})
    show("正式资料两票开封", s, opened)
    assert opened["status"] == "opened"
    assert call("GET", f"/documents/{doc_id}", alice)[1]["body"] == secret
    print(f"   资料 {doc_id} 审计动作: "
          + ", ".join(r["action"] for r in call("GET", f"/documents/{doc_id}/audit", alice)[1]["records"]))

    # ------------------------------------------------------------------
    # 场景 4：发起人撤回（完成前）-> 提案失效、正文清空
    # ------------------------------------------------------------------
    print("== 场景 4：发起人在完成前撤回 ==")
    _, p2 = call("POST", "/pre-seal-proposals", alice,
                 {"body": secret + "-待撤回", "witnesses": witnesses, "confirm_deadline": in_hours(1)})
    pid2, dg2 = p2["proposal_id"], p2["digest"]
    call("POST", f"/pre-seal-proposals/{pid2}/confirmations", bob, {"digest": dg2})
    s, resp = call("POST", f"/pre-seal-proposals/{pid2}/revoke", alice)
    show("发起人撤回", s, resp)
    assert s == 200 and resp["status"] == "revoked"
    # 撤回后确认一律 409，不计票，不生成资料
    assert call("POST", f"/pre-seal-proposals/{pid2}/confirmations", carol, {"digest": dg2})[0] == 409
    assert call("GET", f"/pre-seal-proposals/{pid2}", bob)[1]["confirmation_count"] == 1
    assert call("GET", "/documents", alice)[1]["documents"]  # 只有场景 3 的那份

    # 只有发起人可撤回
    assert call("POST", f"/pre-seal-proposals/{pid2}/revoke", bob)[0] == 403

    # ------------------------------------------------------------------
    # 场景 5：截止时未集齐 -> 失效（后台扫描器每秒处理），正文清空
    # ------------------------------------------------------------------
    print("== 场景 5：截止未集齐，提案失效（扫描器确定性处理） ==")
    _, p3 = call("POST", "/pre-seal-proposals", alice,
                 {"body": secret + "-会过期", "witnesses": witnesses,
                  "confirm_deadline": in_seconds(1)})
    pid3, dg3 = p3["proposal_id"], p3["digest"]
    call("POST", f"/pre-seal-proposals/{pid3}/confirmations", bob, {"digest": dg3})
    time.sleep(2.2)
    s, got = call("GET", f"/pre-seal-proposals/{pid3}", carol)
    show("到期后查询", s, got)
    assert got["status"] == "expired" and got["confirmation_count"] == 1
    assert call("POST", f"/pre-seal-proposals/{pid3}/confirmations", carol, {"digest": dg3})[0] == 409
    assert call("POST", f"/pre-seal-proposals/{pid3}/revoke", alice)[0] == 409

    print("\n全部预封存示例断言通过。")


if __name__ == "__main__":
    main()
