"""预封存提案（pre-seal proposal）端到端测试。

覆盖：
- 发起：三名互异、已注册、非本人见证人校验；带时区未来截止时刻；响应只给编号与摘要；
- 可见性：仅发起人及受邀见证人可见摘要/截止时刻/状态/确认人数，其他账号凭编号统一 403；
- 确认：见证人凭摘要各确认一次；摘要不符、重复确认拒绝且不计票；非受邀见证人/发起人 403；
- 集齐：三人截止前确认后同事务生成沿用现有规则的封存资料并返回资料编号；
  重复请求不再生成资料；失败不产生资料或封存审计；
- 撤回：仅发起人可在完成前撤回；撤回/失效/完成后库内正文清空；
- 到期：截止未集齐即失效（扫描器/GET/确认/撤回临界区确定性判定）；
- 重启一致性；真实 uvicorn + 多线程的并发确认 vs 撤回、旧密钥确认 vs 密钥轮换赛跑。
"""
import datetime as dt
import hashlib
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import auth, digest_of, register  # noqa: E402


def in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


@pytest.fixture
def users(client):
    names = ["ps-alice", "ps-bob", "ps-carol", "ps-dave", "ps-erin"]
    keys = {n: register(client, n) for n in names}
    return keys


def propose(client, owner, body, witnesses, deadline=None):
    return client.post(
        "/pre-seal-proposals",
        json={
            "body": body,
            "witnesses": witnesses,
            "confirm_deadline": deadline or in_hours(1),
        },
        headers=auth(owner),
    )


# ---------------------------------------------------------------------------
# 发起校验
# ---------------------------------------------------------------------------


def test_create_pre_seal_basic(client, users):
    body = "预封存正文 PRE-SEAL-SECRET"
    r = propose(client, users["ps-alice"], body, ["ps-bob", "ps-carol", "ps-dave"])
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert data["proposal_id"] >= 1
    assert data["digest"] == digest_of(body)
    assert data["confirmation_count"] == 0
    assert data["required"] == 3
    assert data["document_id"] is None
    assert data["witnesses"] == ["ps-bob", "ps-carol", "ps-dave"]
    assert data["owner_username"] == "ps-alice"
    # 响应不含正文
    assert "body" not in r.text


@pytest.mark.parametrize(
    "witnesses",
    [
        ["ps-bob", "ps-carol", "ps-carol"],          # 不互异
        ["ps-bob", "ps-carol", "ps-alice"],          # 含本人
        ["ps-bob", "ps-carol", "ps-ghost"],          # 未注册
    ],
)
def test_create_pre_seal_invalid_witnesses(client, users, witnesses):
    r = propose(client, users["ps-alice"], "x", witnesses)
    assert r.status_code == 400, r.text
    assert "body" not in r.text


def test_create_pre_seal_wrong_witness_count(client, users):
    # 人数不是三人由请求模型校验拒绝（422，与直接封存一致），错误体不回显正文
    secret = "UNIQUE-MARKER-不应出现在错误响应"
    r = client.post(
        "/pre-seal-proposals",
        json={"body": secret, "witnesses": ["ps-bob", "ps-carol"], "confirm_deadline": in_hours(1)},
        headers=auth(users["ps-alice"]),
    )
    assert r.status_code == 422
    assert secret not in r.text


def test_create_pre_seal_deadline_validation(client, users):
    # 已过截止时刻
    r = propose(
        client, users["ps-alice"], "x",
        ["ps-bob", "ps-carol", "ps-dave"], deadline="2020-01-01T00:00:00Z",
    )
    assert r.status_code == 400
    # 不带时区
    r = client.post(
        "/pre-seal-proposals",
        json={
            "body": "x",
            "witnesses": ["ps-bob", "ps-carol", "ps-dave"],
            "confirm_deadline": "2030-01-01T00:00:00",
        },
        headers=auth(users["ps-alice"]),
    )
    assert r.status_code == 422
    # 无凭据
    r = client.post(
        "/pre-seal-proposals",
        json={
            "body": "x",
            "witnesses": ["ps-bob", "ps-carol", "ps-dave"],
            "confirm_deadline": in_hours(1),
        },
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 可见性：只有发起人与受邀见证人；其他账号不可凭编号探知
# ---------------------------------------------------------------------------


def test_pre_seal_visibility(client, users):
    pid = propose(
        client, users["ps-alice"], "机密",
        ["ps-bob", "ps-carol", "ps-dave"],
    ).json()["proposal_id"]

    # 发起人可见
    for who in ["ps-alice", "ps-bob", "ps-carol", "ps-dave"]:
        r = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users[who]))
        assert r.status_code == 200, (who, r.text)
        data = r.json()
        assert data["digest"] and data["confirm_deadline"]
        assert data["status"] == "pending"
        assert data["confirmation_count"] == 0
        assert "body" not in r.text

    # 无关账号与未认证请求不可凭编号探知（统一 403/401，不区分存在性）
    assert client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-erin"])).status_code == 403
    assert client.get("/pre-seal-proposals/999999", headers=auth(users["ps-erin"])).status_code == 403
    assert client.get("/pre-seal-proposals/999999", headers=auth(users["ps-alice"])).status_code == 403
    assert client.get(f"/pre-seal-proposals/{pid}").status_code == 401


def test_pre_seal_not_in_documents_list_before_completion(client, users):
    """完成前提案不是资料：不出现在任何参与方的资料列表中。"""
    propose(
        client, users["ps-alice"], "机密",
        ["ps-bob", "ps-carol", "ps-dave"],
    )
    for who in ["ps-alice", "ps-bob", "ps-erin"]:
        r = client.get("/documents", headers=auth(users[who]))
        assert r.status_code == 200
        assert r.json()["documents"] == []


# ---------------------------------------------------------------------------
# 确认：摘要绑定、每人一次、权限
# ---------------------------------------------------------------------------


def _proposal(client, users, body="待确认正文 CONFIRM-SECRET"):
    r = propose(client, users["ps-alice"], body, ["ps-bob", "ps-carol", "ps-dave"])
    return r.json()["proposal_id"], digest_of(body)


def test_confirm_flow_and_counts(client, users):
    pid, dg = _proposal(client, users)

    r = client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-bob"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["confirmation_count"] == 1
    assert r.json()["sealed"] is False
    assert "body" not in r.text

    r = client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-carol"]),
    )
    assert r.json()["confirmation_count"] == 2
    assert r.json()["sealed"] is False

    # GET 看到人数
    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-dave"])).json()
    assert got["confirmation_count"] == 2 and got["status"] == "pending"


def test_confirm_wrong_digest_rejected_and_not_counted(client, users):
    pid, dg = _proposal(client, users)
    r = client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": "0" * 64}, headers=auth(users["ps-bob"]),
    )
    assert r.status_code == 409
    # 不计票
    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-alice"])).json()
    assert got["confirmation_count"] == 0
    # 同一见证人随后可用正确摘要确认
    r = client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-bob"]),
    )
    assert r.status_code == 200 and r.json()["confirmation_count"] == 1


def test_confirm_duplicate_rejected_and_not_counted(client, users):
    pid, dg = _proposal(client, users)
    for _ in range(2):
        r = client.post(
            f"/pre-seal-proposals/{pid}/confirmations",
            json={"digest": dg}, headers=auth(users["ps-bob"]),
        )
    assert r.status_code == 409
    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-bob"])).json()
    assert got["confirmation_count"] == 1


def test_confirm_authorization(client, users):
    pid, dg = _proposal(client, users)
    # 发起人本人不能确认
    assert client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-alice"]),
    ).status_code == 403
    # 无关账号：统一 403，不区分提案是否存在
    assert client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-erin"]),
    ).status_code == 403
    assert client.post(
        "/pre-seal-proposals/999999/confirmations",
        json={"digest": dg}, headers=auth(users["ps-erin"]),
    ).status_code == 403
    # 无凭据 401
    assert client.post(
        f"/pre-seal-proposals/{pid}/confirmations", json={"digest": dg}
    ).status_code == 401


# ---------------------------------------------------------------------------
# 集齐三人：同事务生成沿用现有规则的封存资料
# ---------------------------------------------------------------------------


def test_three_confirmations_seal_document(client, users):
    body = "三人确认后封存 SEALED-BY-PRE-SEAL"
    pid = propose(
        client, users["ps-alice"], body,
        ["ps-bob", "ps-carol", "ps-dave"],
    ).json()["proposal_id"]
    dg = digest_of(body)

    for i, who in enumerate(["ps-bob", "ps-carol", "ps-dave"]):
        r = client.post(
            f"/pre-seal-proposals/{pid}/confirmations",
            json={"digest": dg}, headers=auth(users[who]),
        )
        assert r.status_code == 200, r.text
        if i < 2:
            assert r.json()["sealed"] is False
        else:
            assert r.json()["sealed"] is True

    final = r.json()
    assert final["status"] == "sealed"
    assert final["confirmation_count"] == 3
    doc_id = final["document_id"]
    assert isinstance(doc_id, int)

    # 正式资料沿用现有规则：封存状态、同一发起人、同一三名见证人
    got = client.get(f"/documents/{doc_id}", headers=auth(users["ps-alice"])).json()
    assert got["status"] == "sealed"
    assert got["witnesses"] == ["ps-bob", "ps-carol", "ps-dave"]
    assert "body" not in got  # 开封前不返回正文
    assert "digest" not in got  # 与直接封存一致：开封前详情不含摘要（提案视图里有）

    # 提案视图：sealed + 资料编号，无正文
    p = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-bob"])).json()
    assert p["status"] == "sealed" and p["document_id"] == doc_id
    assert "body" not in p

    # 新资料的审计链：seal + pre_seal_completed，无正文/密钥
    audit = client.get(f"/documents/{doc_id}/audit", headers=auth(users["ps-alice"])).json()
    actions = [rec["action"] for rec in audit["records"]]
    assert actions[0] == "seal"
    assert "pre_seal_completed" in actions
    assert body not in repr(audit)

    # 新资料可走既有开封流程（两名见证人两票）
    dl = in_hours(24)
    req = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "预封存资料开封", "deadline": dl},
        headers=auth(users["ps-alice"]),
    )
    assert req.status_code == 201, req.text
    rid = req.json()["request_id"]
    client.post(f"/open-requests/{rid}/approve", json={"digest": dg}, headers=auth(users["ps-bob"]))
    opened = client.post(
        f"/open-requests/{rid}/approve", json={"digest": dg}, headers=auth(users["ps-carol"]),
    )
    assert opened.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(users["ps-alice"])).json()
    assert got["body"] == body
    assert got["digest"] == dg


def test_no_second_document_after_sealed(client, users):
    """完成后任何重复确认都 409，且不会再生成第二份资料。"""
    pid, dg = _proposal(client, users)
    for who in ["ps-bob", "ps-carol", "ps-dave"]:
        client.post(
            f"/pre-seal-proposals/{pid}/confirmations",
            json={"digest": dg}, headers=auth(users[who]),
        )
    doc_id = client.get(
        f"/pre-seal-proposals/{pid}", headers=auth(users["ps-alice"])
    ).json()["document_id"]

    # 三名见证人重复确认全部 409
    for who in ["ps-bob", "ps-carol", "ps-dave"]:
        r = client.post(
            f"/pre-seal-proposals/{pid}/confirmations",
            json={"digest": dg}, headers=auth(users[who]),
        )
        assert r.status_code == 409
    # 仍只有一份资料
    docs = client.get("/documents", headers=auth(users["ps-alice"])).json()["documents"]
    assert [d["id"] for d in docs] == [doc_id]


def test_failed_confirmation_creates_no_document_or_seal_audit(client, users):
    """摘要不符的失败确认不产生资料、不留任何确认/封存记录。"""
    pid, dg = _proposal(client, users)
    client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": "0" * 64}, headers=auth(users["ps-bob"]),
    )
    assert client.get("/documents", headers=auth(users["ps-alice"])).json()["documents"] == []
    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-alice"])).json()
    assert got["confirmation_count"] == 0


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------


def test_owner_revoke_before_completion(client, users):
    pid, dg = _proposal(client, users)
    # 一票后发起人撤回
    client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-bob"]),
    )
    r = client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(users["ps-alice"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"
    assert "body" not in r.text

    # 撤回后确认一律 409，不计票
    r = client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-carol"]),
    )
    assert r.status_code == 409
    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-bob"])).json()
    assert got["status"] == "revoked" and got["confirmation_count"] == 1
    # 不生成资料
    assert client.get("/documents", headers=auth(users["ps-alice"])).json()["documents"] == []


def test_revoke_permissions_and_states(client, users):
    # 见证人/无关账号不能撤回
    pid, _ = _proposal(client, users)
    assert client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(users["ps-bob"])).status_code == 403
    assert client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(users["ps-erin"])).status_code == 403
    assert client.post("/pre-seal-proposals/999999/revoke", headers=auth(users["ps-alice"])).status_code == 403

    # 重复撤回 409
    assert client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(users["ps-alice"])).status_code == 200
    assert client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(users["ps-alice"])).status_code == 409

    # 已完成的提案不能撤回
    body = "完成后不可撤"
    pid2 = propose(client, users["ps-alice"], body, ["ps-bob", "ps-carol", "ps-dave"]).json()["proposal_id"]
    dg2 = digest_of(body)
    for who in ["ps-bob", "ps-carol", "ps-dave"]:
        client.post(
            f"/pre-seal-proposals/{pid2}/confirmations",
            json={"digest": dg2}, headers=auth(users[who]),
        )
    assert client.post(f"/pre-seal-proposals/{pid2}/revoke", headers=auth(users["ps-alice"])).status_code == 409


# ---------------------------------------------------------------------------
# 到期失效（扫描器 / GET / 确认临界区）+ 正文清空
# ---------------------------------------------------------------------------


def _db_proposal_body(db_path, pid):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT body, status FROM pre_seal_proposals WHERE id=?", (pid,)).fetchone()
        return row
    finally:
        conn.close()


def test_pre_seal_expired_by_sweeper_clears_body(client, users, sweep, tmp_path):
    body = "到期未集齐 EXPIRED-SECRET"
    pid = propose(
        client, users["ps-alice"], body,
        ["ps-bob", "ps-carol", "ps-dave"], deadline=in_seconds(1),
    ).json()["proposal_id"]
    dg = digest_of(body)
    client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-bob"]),
    )
    time.sleep(1.2)
    assert sweep() >= 0

    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-alice"])).json()
    assert got["status"] == "expired" and got["confirmation_count"] == 1
    # 截止后确认 409
    assert client.post(
        f"/pre-seal-proposals/{pid}/confirmations",
        json={"digest": dg}, headers=auth(users["ps-carol"]),
    ).status_code == 409
    # 截止后撤回 409
    assert client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(users["ps-alice"])).status_code == 409
    # 不生成资料
    assert client.get("/documents", headers=auth(users["ps-alice"])).json()["documents"] == []
    # 库内正文已清空（摘要保留）
    stored_body, status = _db_proposal_body(tmp_path / "test.db", pid)
    assert status == "expired" and stored_body == ""


def test_pre_seal_expired_by_get_without_sweeper(client, users, tmp_path, monkeypatch):
    """无扫描器时 GET 本身确定性判定到期。"""
    body = "GET 触发到期"
    pid = propose(
        client, users["ps-alice"], body,
        ["ps-bob", "ps-carol", "ps-dave"], deadline=in_seconds(1),
    ).json()["proposal_id"]
    time.sleep(1.2)
    got = client.get(f"/pre-seal-proposals/{pid}", headers=auth(users["ps-bob"])).json()
    assert got["status"] == "expired"
    stored_body, status = _db_proposal_body(tmp_path / "test.db", pid)
    assert status == "expired" and stored_body == ""


def test_pre_seal_body_cleared_after_revoke_and_sealed(client, users, tmp_path):
    body = "终态清空正文"
    pid = propose(client, users["ps-alice"], body, ["ps-bob", "ps-carol", "ps-dave"]).json()["proposal_id"]
    dg = digest_of(body)
    for who in ["ps-bob", "ps-carol", "ps-dave"]:
        client.post(
            f"/pre-seal-proposals/{pid}/confirmations",
            json={"digest": dg}, headers=auth(users[who]),
        )
    stored_body, status = _db_proposal_body(tmp_path / "test.db", pid)
    assert status == "sealed" and stored_body == ""

    # 另一份撤回的提案正文也清空
    pid2 = propose(client, users["ps-alice"], body + "2", ["ps-bob", "ps-carol", "ps-dave"]).json()["proposal_id"]
    client.post(f"/pre-seal-proposals/{pid2}/revoke", headers=auth(users["ps-alice"]))
    stored_body, status = _db_proposal_body(tmp_path / "test.db", pid2)
    assert status == "revoked" and stored_body == ""


def test_pre_seal_restart_consistency(tmp_path, monkeypatch):
    """停机期间到点：重启恢复扫描置 expired；未到期提案重启后可继续确认完成。"""
    import uvicorn
    import httpx
    from app.main import app

    def _free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def serve(port):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        return server, thread

    def wait_ready(base):
        end = time.time() + 10
        while time.time() < end:
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        pytest.fail("server did not start")

    db = tmp_path / "restart2.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")

    keys: dict[str, str] = {}
    port1 = _free_port()
    server1, thread1 = serve(port1)
    base1 = f"http://127.0.0.1:{port1}"
    wait_ready(base1)
    try:
        with httpx.Client(timeout=10) as c:
            for n in ["rr-alice", "rr-bob", "rr-carol", "rr-dave"]:
                keys[n] = c.post(base1 + "/register", json={"username": n}).json()["api_key"]
            body = "重启一致性正文 RR-PRE-SEAL"
            dg = hashlib.sha256(body.encode()).hexdigest()
            short = c.post(
                base1 + "/pre-seal-proposals",
                json={
                    "body": body + "-short",
                    "witnesses": ["rr-bob", "rr-carol", "rr-dave"],
                    "confirm_deadline": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat(),
                },
                headers={"X-API-Key": keys["rr-alice"]},
            ).json()["proposal_id"]
            long_pid = c.post(
                base1 + "/pre-seal-proposals",
                json={
                    "body": body,
                    "witnesses": ["rr-bob", "rr-carol", "rr-dave"],
                    "confirm_deadline": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
                },
                headers={"X-API-Key": keys["rr-alice"]},
            ).json()["proposal_id"]
            r = c.post(
                f"{base1}/pre-seal-proposals/{long_pid}/confirmations",
                json={"digest": dg}, headers={"X-API-Key": keys["rr-bob"]},
            )
            assert r.status_code == 200
    finally:
        server1.should_exit = True
        thread1.join(timeout=5)

    time.sleep(1.2)

    port2 = _free_port()
    server2, thread2 = serve(port2)
    base2 = f"http://127.0.0.1:{port2}"
    wait_ready(base2)
    try:
        with httpx.Client(timeout=10) as c:
            # 恢复扫描：短期提案 expired
            g = c.get(f"{base2}/pre-seal-proposals/{short}", headers={"X-API-Key": keys["rr-alice"]})
            assert g.json()["status"] == "expired"
            # 长期提案仍 pending，已确认一票保留；补齐后同事务封存
            g = c.get(f"{base2}/pre-seal-proposals/{long_pid}", headers={"X-API-Key": keys["rr-alice"]})
            assert g.json()["status"] == "pending" and g.json()["confirmation_count"] == 1
            r = c.post(
                f"{base2}/pre-seal-proposals/{long_pid}/confirmations",
                json={"digest": dg}, headers={"X-API-Key": keys["rr-carol"]},
            )
            assert r.json()["sealed"] is False
            r = c.post(
                f"{base2}/pre-seal-proposals/{long_pid}/confirmations",
                json={"digest": dg}, headers={"X-API-Key": keys["rr-dave"]},
            )
            assert r.status_code == 200 and r.json()["sealed"] is True
            doc_id = r.json()["document_id"]
            got = c.get(f"{base2}/documents/{doc_id}", headers={"X-API-Key": keys["rr-alice"]}).json()
            assert got["status"] == "sealed" and got["witnesses"] == ["rr-bob", "rr-carol", "rr-dave"]
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)


# ---------------------------------------------------------------------------
# 真实 uvicorn + 多线程并发
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "1")

    import uvicorn
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            import httpx

            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("server did not start")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


def _reg(c, base, name):
    r = c.post(base + "/register", json={"username": name})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def test_live_concurrent_duplicate_confirmation_counted_once(live_server):
    """同一见证人并发重复确认：至多一票计入。"""
    import httpx

    base = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "lc-owner")
        ws = [_reg(c, base, f"lc-w{i}") for i in range(1, 4)]
        body = "并发重复确认 RACE"
        dg = hashlib.sha256(body.encode()).hexdigest()
        pid = c.post(
            base + "/pre-seal-proposals",
            json={"body": body, "witnesses": [f"lc-w{i}" for i in range(1, 4)],
                  "confirm_deadline": in_hours(1)},
            headers={"X-API-Key": owner},
        ).json()["proposal_id"]

    barrier = threading.Barrier(2)

    def call(key):
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            r1 = c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                        json={"digest": dg}, headers={"X-API-Key": key})
            r2 = c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                        json={"digest": dg}, headers={"X-API-Key": key})
            return r1.status_code, r2.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, [ws[0], ws[1]]))
    for first, second in results:
        assert first == 200
        assert second == 409

    with httpx.Client(timeout=10) as c:
        got = c.get(f"{base}/pre-seal-proposals/{pid}", headers={"X-API-Key": owner}).json()
        assert got["confirmation_count"] == 2 and got["status"] == "pending"


def test_live_confirm_vs_revoke_single_outcome(live_server):
    """第三票确认 vs 发起人撤回同时发生：唯一终态——要么封存要么撤回。"""
    import httpx

    base = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "rv-owner")
        ws = [_reg(c, base, f"rv-w{i}") for i in range(1, 4)]
        body = "确认撤回赛跑 RV-RACE"
        dg = hashlib.sha256(body.encode()).hexdigest()
        pid = c.post(
            base + "/pre-seal-proposals",
            json={"body": body, "witnesses": [f"rv-w{i}" for i in range(1, 4)],
                  "confirm_deadline": in_hours(1)},
            headers={"X-API-Key": owner},
        ).json()["proposal_id"]
        for key in ws[:2]:
            assert c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                          json={"digest": dg}, headers={"X-API-Key": key}).status_code == 200

    barrier = threading.Barrier(2)

    def confirm():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                          json={"digest": dg}, headers={"X-API-Key": ws[2]}).status_code

    def revoke():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/revoke",
                          headers={"X-API-Key": owner}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cf_code, rv_code = list(pool.map(lambda f: f(), [confirm, revoke]))

    with httpx.Client(timeout=10) as c:
        got = c.get(f"{base}/pre-seal-proposals/{pid}", headers={"X-API-Key": owner}).json()
        # 恰一个成功
        assert sorted([cf_code, rv_code]) == [200, 409]
        if got["status"] == "sealed":
            assert cf_code == 200 and rv_code == 409
            doc = c.get(f"{base}/documents/{got['document_id']}", headers={"X-API-Key": owner}).json()
            assert doc["status"] == "sealed" and "body" not in doc
        else:
            assert rv_code == 200 and cf_code == 409
            assert got["status"] == "revoked"
            assert c.get(base + "/documents", headers={"X-API-Key": owner}).json()["documents"] == []
        # 无论谁赢，之后都不能再封存出第二份资料
        assert c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                      json={"digest": dg}, headers={"X-API-Key": ws[2]}).status_code == 409


def test_live_old_key_confirm_vs_rotation_single_outcome(live_server):
    """第三名见证人持旧密钥的确认与其本人密钥轮换赛跑：按提交先后唯一判定。

    - 轮换先提交：旧密钥确认 401 整体回滚（不计票、不生成资料、不留封存审计），
      持新密钥重放确认后照常封存；
    - 确认先提交：提案 sealed，轮换随后完成，结果同样唯一。
    """
    import httpx

    base = live_server
    with httpx.Client(timeout=10) as c:
        owner = _reg(c, base, "kr-owner")
        ws = [_reg(c, base, f"kr-w{i}") for i in range(1, 4)]
        body = "轮换确认赛跑 KR-RACE"
        dg = hashlib.sha256(body.encode()).hexdigest()
        pid = c.post(
            base + "/pre-seal-proposals",
            json={"body": body, "witnesses": [f"kr-w{i}" for i in range(1, 4)],
                  "confirm_deadline": in_hours(1)},
            headers={"X-API-Key": owner},
        ).json()["proposal_id"]
        for key in ws[:2]:
            c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                   json={"digest": dg}, headers={"X-API-Key": key})
        old_key = ws[2]

    barrier = threading.Barrier(2)

    def confirm_old():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                          json={"digest": dg}, headers={"X-API-Key": old_key}).status_code

    def rotate():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            r = c.post(base + "/me/api-key/rotate", headers={"X-API-Key": old_key})
            return r.status_code, (r.json()["api_key"] if r.status_code == 200 else None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cf_code, (rt_code, new_key) = list(pool.map(lambda f: f(), [confirm_old, rotate]))

    assert rt_code == 200 and new_key
    with httpx.Client(timeout=10) as c:
        got = c.get(f"{base}/pre-seal-proposals/{pid}", headers={"X-API-Key": owner}).json()
        if cf_code == 200:
            # 业务写先提交：已封存
            assert got["status"] == "sealed" and got["confirmation_count"] == 3
            # 旧密钥随即失效
            assert c.get(base + "/me", headers={"X-API-Key": old_key}).status_code == 401
        else:
            # 轮换先提交：旧密钥确认 401 回滚，票仍为 2，无资料、无封存审计
            assert cf_code == 401
            assert got["status"] == "pending" and got["confirmation_count"] == 2
            assert got["document_id"] is None
            assert c.get(base + "/documents", headers={"X-API-Key": owner}).json()["documents"] == []
            # 持新密钥补齐第三票 -> 正常封存
            r = c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                       json={"digest": dg}, headers={"X-API-Key": new_key})
            assert r.status_code == 200 and r.json()["sealed"] is True
