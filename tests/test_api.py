"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""

import os
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def post_rate(client, **kw):
    return client.post("/rates", json=kw)


# ---------- 录入与基本报价 ----------

def test_create_and_quote(client):
    r = post_rate(
        client,
        contract_no="C-1",
        rate=0.05,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-07-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"]["version"] == 1
    assert body["created"]["status"] == "current"
    assert body["superseded_rate_ids"] == []
    assert body["retained_versions"] == []

    q = client.get(
        "/quote",
        params={
            "contract_no": "C-1",
            "valid_at": "2026-03-01T00:00:00Z",
            "known_at": "2026-02-01T00:00:00Z",
        },
    )
    assert q.status_code == 200, q.text
    data = q.json()
    assert data["rate"] == 0.05
    assert data["basis"]["version"] == 1
    assert data["basis"]["recorded_at"] == "2026-01-01T00:00:00.000000+00:00"


def test_quote_boundary_left_closed_right_open(client):
    post_rate(
        client,
        contract_no="C-2",
        rate=0.1,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
        recorded_at="2025-12-01T00:00:00Z",
    )
    # valid_from 边界：含
    assert client.get("/quote", params={
        "contract_no": "C-2", "valid_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"}).status_code == 200
    # valid_to 边界：不含
    assert client.get("/quote", params={
        "contract_no": "C-2", "valid_at": "2026-02-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"}).status_code == 404


def test_open_ended_interval(client):
    post_rate(
        client,
        contract_no="C-3",
        rate=0.07,
        valid_from="2026-01-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    q = client.get("/quote", params={
        "contract_no": "C-3", "valid_at": "2099-06-01T00:00:00Z",
        "known_at": "2026-01-02T00:00:00Z"})
    assert q.status_code == 200
    assert q.json()["rate"] == 0.07


# ---------- 修订：追加版本，不覆盖历史 ----------

def test_revision_appends_version_and_keeps_history(client):
    post_rate(client, contract_no="C-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始费率")
    r2 = post_rate(client, contract_no="C-4", rate=0.08,
                   valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
                   recorded_at="2026-06-01T00:00:00Z", reason="费率更正")
    assert r2.status_code == 201
    body = r2.json()
    assert body["created"]["version"] == 2
    assert len(body["superseded_rate_ids"]) == 1

    # 修订前的认知时点：仍看到旧费率 0.05
    old = client.get("/quote", params={
        "contract_no": "C-4", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-03-01T00:00:00Z"})
    assert old.status_code == 200
    assert old.json()["rate"] == 0.05
    assert old.json()["basis"]["version"] == 1

    # 修订后的认知时点：看到新费率 0.08
    new = client.get("/quote", params={
        "contract_no": "C-4", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-06-02T00:00:00Z"})
    assert new.status_code == 200
    assert new.json()["rate"] == 0.08
    assert new.json()["basis"]["version"] == 2

    # 时间线保留两个版本，旧版本状态为 superseded
    tl = client.get("/contracts/C-4/timeline")
    assert tl.status_code == 200
    versions = tl.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["status"] == "superseded"
    assert versions[0]["superseded_at"] == "2026-06-01T00:00:00.000000+00:00"
    assert versions[1]["status"] == "current"


def test_partial_overlap_revision(client):
    """新区间完整覆盖多条相接的当前版本：全部取代，无补偿片段。"""
    post_rate(client, contract_no="C-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="C-5", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    # 新区间覆盖整个 2026 年 → 取代上面两个当前版本
    r = post_rate(client, contract_no="C-5", rate=0.09,
                  valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
                  recorded_at="2026-03-01T00:00:00Z")
    body = r.json()
    assert len(body["superseded_rate_ids"]) == 2
    assert body["created"]["version"] == 3
    assert body["retained_versions"] == []

    q = client.get("/quote", params={
        "contract_no": "C-5", "valid_at": "2026-08-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"})
    assert q.json()["rate"] == 0.09


# ---------- 部分重叠修订：只替换交集，窗口外保留原费率 ----------

def _quote(client, contract, valid_at, known_at):
    return client.get("/quote", params={
        "contract_no": contract, "valid_at": valid_at, "known_at": known_at})


def test_partial_overlap_keeps_left_and_right_fragments(client):
    """新区间落在旧版本中间：左右未覆盖部分按原费率与 reason 追加补偿版本。"""
    post_rate(client, contract_no="R-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="原费率")
    r = post_rate(client, contract_no="R-1", rate=0.09,
                  valid_from="2026-04-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
                  recorded_at="2026-03-01T00:00:00Z", reason="年中调整")
    body = r.json()
    assert body["created"]["version"] == 2
    assert body["superseded_rate_ids"] == [1]

    # 新费率先创建（version 最小），补偿片段按 valid_from 排序、version 严格递增
    retained = body["retained_versions"]
    assert [(v["version"], v["valid_from"], v["valid_to"], v["rate"], v["reason"])
            for v in retained] == [
        (3, "2026-01-01T00:00:00.000000+00:00", "2026-04-01T00:00:00.000000+00:00",
         0.05, "原费率"),
        (4, "2026-07-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00",
         0.05, "原费率"),
    ]
    assert all(v["status"] == "current" for v in retained)
    assert all(v["recorded_at"] == "2026-03-01T00:00:00.000000+00:00" for v in retained)

    # 修订后：窗口外报价保持原费率，窗口内为新费率，窗口外之外仍是 gap
    assert _quote(client, "R-1", "2026-02-01T00:00:00Z",
                  "2026-03-02T00:00:00Z").json()["rate"] == 0.05
    mid = _quote(client, "R-1", "2026-05-01T00:00:00Z", "2026-03-02T00:00:00Z").json()
    assert mid["rate"] == 0.09 and mid["basis"]["version"] == 2
    right = _quote(client, "R-1", "2026-10-01T00:00:00Z",
                   "2026-03-02T00:00:00Z").json()
    assert right["rate"] == 0.05 and right["basis"]["version"] == 4
    # 左闭右开边界：2027-01-01 原本就无费率，保留为 gap 而不是被错误填补
    assert _quote(client, "R-1", "2027-01-01T00:00:00Z",
                  "2026-03-02T00:00:00Z").status_code == 404

    # 历史 known_at 仍看到原版本（整条、原费率），补偿版本尚不可见
    old = _quote(client, "R-1", "2026-05-01T00:00:00Z",
                 "2026-02-01T00:00:00Z").json()
    assert old["rate"] == 0.05 and old["basis"]["version"] == 1
    assert old["basis"]["valid_to"] == "2027-01-01T00:00:00.000000+00:00"

    # 时间线：原版本 superseded，新版本与两个补偿版本 current
    versions = client.get("/contracts/R-1/timeline").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2, 3, 4]
    assert versions[0]["status"] == "superseded"
    assert versions[0]["superseded_at"] == "2026-03-01T00:00:00.000000+00:00"
    assert [v["status"] for v in versions[1:]] == ["current"] * 3


def test_partial_overlap_open_ended_old_keeps_open_right_fragment(client):
    """旧版本开放上界、修订区间有限：右补偿片段同样开放上界，远期报价不丢。"""
    post_rate(client, contract_no="R-2", rate=0.07,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    body = post_rate(client, contract_no="R-2", rate=0.08,
                     valid_from="2026-06-01T00:00:00Z",
                     valid_to="2026-09-01T00:00:00Z",
                     recorded_at="2026-03-01T00:00:00Z").json()
    retained = body["retained_versions"]
    assert [(v["version"], v["valid_from"], v["valid_to"], v["rate"])
            for v in retained] == [
        (3, "2026-01-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00", 0.07),
        (4, "2026-09-01T00:00:00.000000+00:00", None, 0.07),
    ]

    ka = "2026-03-02T00:00:00Z"
    assert _quote(client, "R-2", "2026-05-01T00:00:00Z", ka).json()["rate"] == 0.07
    assert _quote(client, "R-2", "2026-07-01T00:00:00Z", ka).json()["rate"] == 0.08
    far = _quote(client, "R-2", "2099-01-01T00:00:00Z", ka).json()
    assert far["rate"] == 0.07 and far["basis"]["version"] == 4
    assert far["basis"]["valid_to"] is None


def test_partial_overlap_open_ended_new_keeps_only_left_fragment(client):
    """修订区间开放上界且只压住旧版本右半：只生成左补偿，无右补偿。"""
    post_rate(client, contract_no="R-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="R-3", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2026-10-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    body = post_rate(client, contract_no="R-3", rate=0.09,
                     valid_from="2026-03-01T00:00:00Z",
                     recorded_at="2026-04-01T00:00:00Z").json()
    # 新版本号先占 3；两个旧版本均被取代，仅第一条旧版本留下左补偿 [01,03)
    assert body["created"]["version"] == 3
    assert body["superseded_rate_ids"] == [1, 2]
    (left,) = body["retained_versions"]
    assert (left["version"], left["valid_from"], left["valid_to"], left["rate"]) == (
        4, "2026-01-01T00:00:00.000000+00:00", "2026-03-01T00:00:00.000000+00:00", 0.05)

    ka = "2026-04-02T00:00:00Z"
    assert _quote(client, "R-3", "2026-02-01T00:00:00Z", ka).json()["rate"] == 0.05
    assert _quote(client, "R-3", "2026-07-01T00:00:00Z", ka).json()["rate"] == 0.09
    assert _quote(client, "R-3", "2099-01-01T00:00:00Z", ka).json()["rate"] == 0.09


def test_partial_overlap_spanning_multiple_old_versions(client):
    """修订区间跨越两条相接旧版本：各自只保留窗口外侧片段，中间不留 gap。"""
    post_rate(client, contract_no="R-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="上半年")
    post_rate(client, contract_no="R-4", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="下半年")
    body = post_rate(client, contract_no="R-4", rate=0.09,
                     valid_from="2026-04-01T00:00:00Z",
                     valid_to="2026-08-01T00:00:00Z",
                     recorded_at="2026-03-01T00:00:00Z", reason="窗口修订").json()
    assert body["created"]["version"] == 3
    assert body["superseded_rate_ids"] == [1, 2]
    retained = body["retained_versions"]
    assert [(v["version"], v["valid_from"], v["valid_to"], v["rate"], v["reason"])
            for v in retained] == [
        (4, "2026-01-01T00:00:00.000000+00:00", "2026-04-01T00:00:00.000000+00:00",
         0.05, "上半年"),
        (5, "2026-08-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00",
         0.06, "下半年"),
    ]

    # 修订后全年连续覆盖：左补偿 / 新窗口 / 右补偿，无 gap
    ka = "2026-03-02T00:00:00Z"
    assert _quote(client, "R-4", "2026-02-01T00:00:00Z", ka).json()["rate"] == 0.05
    assert _quote(client, "R-4", "2026-05-01T00:00:00Z", ka).json()["rate"] == 0.09
    assert _quote(client, "R-4", "2026-07-01T00:00:00Z", ka).json()["rate"] == 0.09
    assert _quote(client, "R-4", "2026-10-01T00:00:00Z", ka).json()["rate"] == 0.06

    # 历史 known_at 回放：仍是修订前两条旧版本
    old_ka = "2026-02-01T00:00:00Z"
    assert _quote(client, "R-4", "2026-05-01T00:00:00Z", old_ka).json()["basis"]["version"] == 1
    assert _quote(client, "R-4", "2026-07-01T00:00:00Z", old_ka).json()["basis"]["version"] == 2

    # 覆盖率：修订后窗口内全年 covered、无 gap
    segs = client.get("/contracts/R-4/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": ka}).json()["segments"]
    assert [(s["status"], s["basis"]["rate"] if s["basis"] else None) for s in segs] == [
        ("covered", 0.05), ("covered", 0.09), ("covered", 0.06),
    ]


def test_partial_overlap_adjacent_intervals_not_touched(client):
    """仅相接（不相交）的当前版本不被取代，也不产生补偿。"""
    post_rate(client, contract_no="R-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    body = post_rate(client,
                     # 左闭右开：从 2026-06-01 起的新版本与旧版本仅相接
                     contract_no="R-5", rate=0.08,
                     valid_from="2026-06-01T00:00:00Z",
                     valid_to="2026-09-01T00:00:00Z",
                     recorded_at="2026-02-01T00:00:00Z").json()
    assert body["superseded_rate_ids"] == []
    assert body["retained_versions"] == []
    ka = "2026-02-02T00:00:00Z"
    assert _quote(client, "R-5", "2026-05-31T00:00:00Z", ka).json()["rate"] == 0.05
    assert _quote(client, "R-5", "2026-06-01T00:00:00Z", ka).json()["rate"] == 0.08


def test_recorded_at_must_be_monotonic(client):
    post_rate(client, contract_no="C-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = post_rate(client, contract_no="C-6", rate=0.06,
                  valid_from="2026-01-01T00:00:00Z",
                  recorded_at="2026-05-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NON_MONOTONIC_RECORDED_AT"


def test_invalid_interval_rejected(client):
    r = post_rate(client, contract_no="C-7", rate=0.05,
                  valid_from="2026-06-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_malformed_request_422(client):
    # 缺少必填字段 / 非法时间格式 → 统一的 INVALID_REQUEST
    r = post_rate(client, rate=0.05, valid_from="2026-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = post_rate(client, contract_no="C-7", rate=0.05, valid_from="not-a-date")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


# ---------- 报价错误分支 ----------

def test_quote_no_match_404(client):
    q = client.get("/quote", params={
        "contract_no": "NOPE", "valid_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"})
    assert q.status_code == 404
    assert q.json()["detail"]["code"] == "NO_MATCH"


def test_quote_not_yet_recorded_404(client):
    """known_at 早于 recorded_at 时，费率尚不可见。"""
    post_rate(client, contract_no="C-8", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    q = client.get("/quote", params={
        "contract_no": "C-8", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-01-15T00:00:00Z"})
    assert q.status_code == 404


def test_quote_overlap_409(client, tmp_path, monkeypatch):
    """直接注入互相重叠的脏数据（绕过录入保护），报价应报 409 冲突。"""
    post_rate(client, contract_no="C-9", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('C-9', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    q = client.get("/quote", params={
        "contract_no": "C-9", "valid_at": "2026-07-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"})
    assert q.status_code == 409
    detail = q.json()["detail"]
    assert detail["code"] == "OVERLAP"
    assert len(detail["candidates"]) == 2


def test_quote_default_known_at_is_now(client):
    # recorded_at 缺省 = 当前时间；不指定 known_at 的报价应立即可见
    post_rate(client, contract_no="C-10", rate=0.03,
              valid_from="2020-01-01T00:00:00Z")
    q = client.get("/quote", params={
        "contract_no": "C-10", "valid_at": "2026-09-18T00:00:00Z"})
    assert q.status_code == 200
    assert q.json()["rate"] == 0.03


# ---------- 时间线 ----------

def test_timeline_unknown_contract_404(client):
    r = client.get("/contracts/GHOST/timeline")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CONTRACT_NOT_FOUND"


# ---------- 覆盖率 ----------

def test_coverage_covered_and_gap(client):
    post_rate(client, contract_no="CV-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = client.get("/contracts/CV-1/coverage", params={
        "from": "2026-03-01T00:00:00Z", "to": "2026-09-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "CV-1"
    assert data["from"] == "2026-03-01T00:00:00.000000+00:00"
    assert data["to"] == "2026-09-01T00:00:00.000000+00:00"
    assert data["known_at"] == "2026-02-01T00:00:00.000000+00:00"
    segs = data["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-03-01T00:00:00.000000+00:00", "2026-07-01T00:00:00.000000+00:00", "covered"),
        ("2026-07-01T00:00:00.000000+00:00", "2026-09-01T00:00:00.000000+00:00", "gap"),
    ]
    # covered 段附唯一版本依据；gap 段无依据、无候选
    assert segs[0]["basis"]["version"] == 1
    assert segs[0]["basis"]["rate"] == 0.05
    assert segs[0]["candidates"] == []
    assert segs[1]["basis"] is None
    assert segs[1]["candidates"] == []


def test_coverage_from_must_precede_to(client):
    for frm, to in [("2026-09-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = client.get("/contracts/CV-X/coverage", params={"from": frm, "to": to})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_coverage_missing_params_422(client):
    r = client.get("/contracts/CV-X/coverage")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_coverage_no_visible_versions_is_single_gap(client):
    # 合同不存在 → 覆盖整个查询窗口的 gap
    r = client.get("/contracts/GHOST/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200
    segs = r.json()["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00", "gap"),
    ]

    # 合同存在但 known_at 早于 recorded_at → 版本尚不可见，仍整窗 gap
    post_rate(client, contract_no="CV-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = client.get("/contracts/CV-2/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-01-15T00:00:00Z"})
    assert [s["status"] for s in r.json()["segments"]] == ["gap"]


def test_coverage_reflects_known_at(client):
    """同一业务窗口，修订前后的认知时点看到不同的覆盖结果。"""
    post_rate(client, contract_no="CV-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    revision = post_rate(client, contract_no="CV-3", rate=0.08,
                         valid_from="2026-06-01T00:00:00Z",
                         valid_to="2027-01-01T00:00:00Z",
                         recorded_at="2026-03-01T00:00:00Z").json()
    # 部分重叠修订：旧版本的上半年部分作为补偿版本（v3）保留，不留 gap
    assert revision["superseded_rate_ids"] == [1]
    (retained,) = revision["retained_versions"]
    assert retained["version"] == 3
    assert (retained["valid_from"], retained["valid_to"], retained["rate"]) == (
        "2026-01-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00", 0.05)

    # 修订前的认知时点：旧版本覆盖整个窗口
    old = client.get("/contracts/CV-3/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"}).json()
    assert [(s["status"], s["basis"]["version"] if s["basis"] else None)
            for s in old["segments"]] == [("covered", 1)]

    # 修订后的认知时点：上半年仍为原费率（补偿版本 v3），下半年为新版本 v2
    new = client.get("/contracts/CV-3/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"}).json()
    assert [(s["from"], s["status"]) for s in new["segments"]] == [
        ("2026-01-01T00:00:00.000000+00:00", "covered"),
        ("2026-06-01T00:00:00.000000+00:00", "covered"),
    ]
    assert new["segments"][0]["basis"]["version"] == 3
    assert new["segments"][0]["basis"]["rate"] == 0.05
    assert new["segments"][1]["basis"]["version"] == 2
    assert new["segments"][1]["basis"]["rate"] == 0.08


def test_coverage_multiple_versions_segmentation(client):
    """相接的两个当前版本在边界处切分；相邻 covered 段候选不同，不合并。"""
    post_rate(client, contract_no="CV-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="CV-4", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = client.get("/contracts/CV-4/coverage", params={
        "from": "2025-06-01T00:00:00Z", "to": "2027-06-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"}).json()
    segs = data["segments"]
    assert [(s["status"], s["basis"]["rate"] if s["basis"] else None) for s in segs] == [
        ("gap", None), ("covered", 0.05), ("covered", 0.06), ("gap", None),
    ]
    # 片段连续铺满整个窗口
    assert segs[0]["from"] == "2025-06-01T00:00:00.000000+00:00"
    assert segs[-1]["to"] == "2027-06-01T00:00:00.000000+00:00"
    for prev, nxt in zip(segs, segs[1:]):
        assert prev["to"] == nxt["from"]
    assert segs[1]["to"] == "2026-06-01T00:00:00.000000+00:00"


def test_coverage_conflict(client):
    """直接注入互相重叠的脏数据 → 重叠区段标记 conflict 并附全部候选。"""
    post_rate(client, contract_no="CV-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('CV-5', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    data = client.get("/contracts/CV-5/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"}).json()
    segs = data["segments"]
    assert [s["status"] for s in segs] == ["covered", "conflict", "covered"]
    conflict = segs[1]
    assert conflict["from"] == "2026-06-01T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-09-01T00:00:00.000000+00:00"
    assert conflict["basis"] is None
    assert [c["version"] for c in conflict["candidates"]] == [1, 2]


def test_coverage_open_ended_and_default_known_at(client):
    """无限期区间裁剪到窗口终点；known_at 缺省为当前时间即可见。"""
    post_rate(client, contract_no="CV-6", rate=0.07,
              valid_from="2026-01-01T00:00:00Z")
    data = client.get("/contracts/CV-6/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2099-01-01T00:00:00Z"}).json()
    segs = data["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2099-01-01T00:00:00.000000+00:00", "covered"),
    ]


# ---------- 计费预览 ----------

def post_preview(client, contract, **kw):
    return client.post(f"/contracts/{contract}/charge-preview", json=kw)


def test_charge_preview_single_version_full_cover(client):
    post_rate(client, contract_no="P-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = post_preview(client, "P-1", known_at="2026-06-01T00:00:00Z", lines=[
        {"from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z", "base_amount": 1000.0},
    ])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "P-1"
    assert data["known_at"] == "2026-06-01T00:00:00.000000+00:00"
    assert data["status"] == "ok"
    (line,) = data["lines"]
    assert line["status"] == "ok"
    assert line["from"] == "2026-02-01T00:00:00.000000+00:00"
    assert line["to"] == "2026-03-01T00:00:00.000000+00:00"
    assert line["base_amount"] == 1000.0
    (seg,) = line["segments"]
    assert seg["status"] == "covered"
    assert seg["portion"] == 1.0
    assert seg["rate_id"] == 1
    assert seg["rate"] == 0.05
    assert seg["charge"] == 50.0
    assert seg["candidates"] == []
    assert line["line_total"] == 50.0
    assert data["total_amount"] == 50.0


def test_charge_preview_splits_at_version_boundaries(client):
    """账单行被两个相接版本的边界切开，各段按时长比例折算。"""
    post_rate(client, contract_no="P-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-03T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="P-2", rate=0.08,
              valid_from="2026-01-03T00:00:00Z", valid_to="2026-01-05T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-2", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z", "base_amount": 1000.0},
    ]).json()
    (line,) = data["lines"]
    segs = line["segments"]
    assert [(s["status"], s["rate"], s["portion"]) for s in segs] == [
        ("covered", 0.05, 0.5),
        ("covered", 0.08, 0.5),
    ]
    assert [s["rate_id"] for s in segs] == [1, 2]
    assert [s["charge"] for s in segs] == [25.0, 40.0]
    assert line["line_total"] == 65.0
    assert data["status"] == "ok"
    assert data["total_amount"] == 65.0


def test_charge_preview_prorates_by_duration(client):
    """非对半的时长占比：1/3 与 2/3，费用按 6 位小数取舍。"""
    post_rate(client, contract_no="P-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-02T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="P-3", rate=0.08,
              valid_from="2026-01-02T00:00:00Z", valid_to="2026-01-04T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-3", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-04T00:00:00Z", "base_amount": 300.0},
    ]).json()
    (line,) = data["lines"]
    segs = line["segments"]
    assert [s["portion"] for s in segs] == [0.333333, 0.666667]
    assert [s["charge"] for s in segs] == [5.0, 16.0]
    assert line["line_total"] == 21.0
    assert data["total_amount"] == 21.0


def test_charge_preview_respects_known_at(client):
    """同一账单行，修订前后的认知时点命中不同费率版本。"""
    post_rate(client, contract_no="P-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="P-4", rate=0.08,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    line = {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 100.0}
    old = post_preview(client, "P-4", known_at="2026-03-01T00:00:00Z", lines=[line]).json()
    assert old["lines"][0]["segments"][0]["rate"] == 0.05
    assert old["lines"][0]["segments"][0]["rate_id"] == 1
    new = post_preview(client, "P-4", known_at="2026-06-02T00:00:00Z", lines=[line]).json()
    assert new["lines"][0]["segments"][0]["rate"] == 0.08
    assert new["lines"][0]["segments"][0]["rate_id"] == 2


def test_charge_preview_gap_rejects_total(client):
    """部分区间无费率 → 该行标记 gap、行合计为 null，整体拒绝总额；正常行不受影响。"""
    post_rate(client, contract_no="P-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-02-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-5", known_at="2026-03-01T00:00:00Z", lines=[
        {"from": "2026-01-15T00:00:00Z", "to": "2026-03-01T00:00:00Z", "base_amount": 900.0},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 100.0},
    ]).json()
    assert data["status"] == "rejected"
    assert data["total_amount"] is None
    gap_line, ok_line = data["lines"]
    assert gap_line["status"] == "gap"
    assert gap_line["line_total"] is None
    assert [s["status"] for s in gap_line["segments"]] == ["covered", "gap"]
    gap_seg = gap_line["segments"][1]
    assert gap_seg["from"] == "2026-02-01T00:00:00.000000+00:00"
    assert gap_seg["rate_id"] is None
    assert gap_seg["rate"] is None
    assert gap_seg["charge"] is None
    assert gap_seg["candidates"] == []
    # 未受影响的行仍正常计价
    assert ok_line["status"] == "ok"
    assert ok_line["line_total"] == 5.0


def test_charge_preview_conflict_rejects_total(client):
    """注入重叠脏数据 → 重叠区段标记 conflict 并附候选，行与总额均被拒绝。"""
    post_rate(client, contract_no="P-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('P-6', 0.99, '2026-01-02T00:00:00.000000+00:00', "
            "        '2026-01-04T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    data = post_preview(client, "P-6", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z", "base_amount": 400.0},
    ]).json()
    (line,) = data["lines"]
    assert line["status"] == "conflict"
    assert line["line_total"] is None
    assert data["status"] == "rejected"
    assert data["total_amount"] is None
    assert [s["status"] for s in line["segments"]] == ["covered", "conflict", "covered"]
    conflict = line["segments"][1]
    assert conflict["from"] == "2026-01-02T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-01-04T00:00:00.000000+00:00"
    assert conflict["portion"] == 0.5
    assert conflict["charge"] is None
    assert [(c["rate_id"], c["rate"], c["version"]) for c in conflict["candidates"]] == [
        (1, 0.05, 1), (2, 0.99, 2),
    ]


def test_charge_preview_mixed_gap_and_conflict(client):
    """同一行同时出现 gap 与 conflict 段 → 行状态 mixed。"""
    post_rate(client, contract_no="P-7", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-03T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('P-7', 0.99, '2026-01-02T00:00:00.000000+00:00', "
            "        '2026-01-03T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    data = post_preview(client, "P-7", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z", "base_amount": 400.0},
    ]).json()
    (line,) = data["lines"]
    assert [s["status"] for s in line["segments"]] == ["covered", "conflict", "gap"]
    assert line["status"] == "mixed"
    assert line["line_total"] is None
    assert data["status"] == "rejected"
    assert data["total_amount"] is None


def test_charge_preview_unknown_contract_is_all_gap(client):
    """合同不存在（或该认知时点无可见版本）→ 整行 gap，拒绝总额，不报 404。"""
    r = post_preview(client, "GHOST", known_at="2026-06-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z", "base_amount": 100.0},
    ])
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "rejected"
    assert data["total_amount"] is None
    (line,) = data["lines"]
    assert line["status"] == "gap"
    assert [s["status"] for s in line["segments"]] == ["gap"]


def test_charge_preview_multiple_lines_sum(client):
    post_rate(client, contract_no="P-8", rate=0.5,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-8", known_at="2026-06-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 100.0},
        {"from": "2026-02-01T00:00:00Z", "to": "2026-02-03T00:00:00Z", "base_amount": 200.0},
    ]).json()
    assert data["status"] == "ok"
    assert [line["line_total"] for line in data["lines"]] == [50.0, 100.0]
    assert data["total_amount"] == 150.0


def test_charge_preview_default_known_at_is_now(client):
    # recorded_at 缺省 = 当前时间；不指定 known_at 的预览应立即可见
    post_rate(client, contract_no="P-9", rate=0.1, valid_from="2020-01-01T00:00:00Z")
    data = post_preview(client, "P-9", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 10.0},
    ]).json()
    assert data["status"] == "ok"
    assert data["total_amount"] == 1.0


def test_charge_preview_invalid_interval(client):
    for frm, to in [("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = post_preview(client, "P-10", lines=[{"from": frm, "to": to, "base_amount": 100.0}])
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_charge_preview_invalid_amount(client):
    r = post_preview(client, "P-11", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z", "base_amount": -1.0},
    ])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_AMOUNT"


def test_charge_preview_future_known_at_rejected(client):
    r = post_preview(client, "P-12", known_at="2999-01-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z", "base_amount": 100.0},
    ])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_KNOWN_AT"


def test_charge_preview_malformed_request_422(client):
    # 空账单行列表 / 缺少必填字段 → 统一的 INVALID_REQUEST
    r = post_preview(client, "P-13", lines=[])
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = post_preview(client, "P-13", lines=[{"from": "2026-01-01T00:00:00Z",
                                             "to": "2026-02-01T00:00:00Z"}])
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------- 变更对比 ----------

def get_changes(client, contract, **params):
    return client.get(f"/contracts/{contract}/changes", params=params)


def us_between(frm: str, to: str) -> int:
    """两个 ISO-8601 时间串之间的时长（微秒）。"""
    delta = (datetime.fromisoformat(to.replace("Z", "+00:00"))
             - datetime.fromisoformat(frm.replace("Z", "+00:00")))
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def test_changes_rate_changed_and_unchanged(client):
    """修订落在两个认知时点之间：交集内费率变化，窗口外费率不变（同费率不同版本）。"""
    post_rate(client, contract_no="CH-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始费率")
    post_rate(client, contract_no="CH-1", rate=0.08,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-03-01T00:00:00Z", reason="年中调整")
    r = get_changes(client, "CH-1",
                    **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                    before_known_at="2026-02-01T00:00:00Z",
                    after_known_at="2026-03-02T00:00:00Z")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "CH-1"
    assert data["from"] == "2026-01-01T00:00:00.000000+00:00"
    assert data["to"] == "2027-01-01T00:00:00.000000+00:00"
    assert data["before_known_at"] == "2026-02-01T00:00:00.000000+00:00"
    assert data["after_known_at"] == "2026-03-02T00:00:00.000000+00:00"
    segs = data["segments"]
    assert [(s["from"], s["to"], s["change"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00", "unchanged"),
        ("2026-06-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00", "rate_changed"),
    ]
    # 上半年：before 为原版本 v1，after 为同费率的补偿版本 v3 → unchanged
    assert segs[0]["before"]["status"] == "covered"
    assert segs[0]["before"]["basis"]["version"] == 1
    assert segs[0]["before"]["basis"]["rate"] == 0.05
    assert segs[0]["after"]["status"] == "covered"
    assert segs[0]["after"]["basis"]["version"] == 3
    assert segs[0]["after"]["basis"]["rate"] == 0.05
    assert segs[0]["before"]["candidates"] == []
    assert segs[0]["after"]["candidates"] == []
    # 下半年：0.05 → 0.08
    assert segs[1]["before"]["basis"]["version"] == 1
    assert segs[1]["after"]["basis"]["version"] == 2
    assert segs[1]["after"]["basis"]["rate"] == 0.08
    # 逐段时长与分类汇总（微秒）
    jan, jun, jan2 = ("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert segs[0]["duration_us"] == us_between(jan, jun)
    assert segs[1]["duration_us"] == us_between(jun, jan2)
    assert data["summary"] == {
        "added_us": 0,
        "removed_us": 0,
        "unchanged_us": us_between(jan, jun),
        "rate_changed_us": us_between(jun, jan2),
        "conflict_us": 0,
    }


def test_changes_added_and_removed(client):
    """added：新费率在两个认知时点之间录入（gap→covered）；
    removed：版本被取代且无后继覆盖（covered→gap，脏数据）。"""
    # added：recorded_at 落在两个认知时点之间，before 侧尚不可见
    post_rate(client, contract_no="CH-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
              recorded_at="2026-03-01T00:00:00Z")
    data = get_changes(client, "CH-2",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-04-01T00:00:00Z").json()
    segs = data["segments"]
    assert [(s["change"], s["before"]["status"], s["after"]["status"]) for s in segs] == [
        ("added", "gap", "covered"),
        ("unchanged", "gap", "gap"),
    ]
    assert segs[0]["before"]["basis"] is None
    assert segs[0]["after"]["basis"]["version"] == 1
    assert segs[0]["after"]["basis"]["rate"] == 0.05
    jan, jul, sep = ("2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-09-01T00:00:00Z")
    assert data["summary"]["added_us"] == us_between(jan, jul)
    assert data["summary"]["unchanged_us"] == us_between(jul, sep)

    # removed：直接注入取代标记（无后继版本覆盖该业务区间）
    post_rate(client, contract_no="CH-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE rate_versions SET superseded_at = '2026-03-01T00:00:00.000000+00:00' "
            "WHERE contract_no = 'CH-3'"
        )
    data = get_changes(client, "CH-3",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-04-01T00:00:00Z").json()
    segs = data["segments"]
    assert [(s["change"], s["before"]["status"], s["after"]["status"]) for s in segs] == [
        ("removed", "covered", "gap"),
        ("unchanged", "gap", "gap"),
    ]
    assert segs[0]["before"]["basis"]["version"] == 1
    assert segs[0]["before"]["basis"]["rate"] == 0.05
    assert segs[0]["after"]["basis"] is None
    assert data["summary"]["removed_us"] == us_between(jan, jul)
    assert data["summary"]["unchanged_us"] == us_between(jul, sep)


def test_changes_conflict_on_either_side(client):
    """任一侧存在重叠冲突（脏数据）→ 该段分类为 conflict，并附两侧状态与候选。"""
    # after 侧冲突：重叠脏版本在两个认知时点之间录入
    post_rate(client, contract_no="CH-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('CH-4', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-03-01T00:00:00.000000+00:00', 2)"
        )
    data = get_changes(client, "CH-4",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-04-01T00:00:00Z").json()
    segs = data["segments"]
    assert [s["change"] for s in segs] == ["unchanged", "conflict", "unchanged"]
    conflict = segs[1]
    assert conflict["from"] == "2026-06-01T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-09-01T00:00:00.000000+00:00"
    assert conflict["before"]["status"] == "covered"
    assert conflict["before"]["basis"]["version"] == 1
    assert conflict["after"]["status"] == "conflict"
    assert conflict["after"]["basis"] is None
    assert [c["version"] for c in conflict["after"]["candidates"]] == [1, 2]
    jun, sep = "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z"
    assert conflict["duration_us"] == us_between(jun, sep)
    assert data["summary"]["conflict_us"] == us_between(jun, sep)

    # before 侧冲突：脏版本随后被取代，after 侧恢复唯一覆盖
    post_rate(client, contract_no="CH-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('CH-5', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
        conn.execute(
            "UPDATE rate_versions SET superseded_at = '2026-03-01T00:00:00.000000+00:00' "
            "WHERE contract_no = 'CH-5' AND version = 2"
        )
    data = get_changes(client, "CH-5",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-04-01T00:00:00Z").json()
    segs = data["segments"]
    assert [s["change"] for s in segs] == ["unchanged", "conflict", "unchanged"]
    conflict = segs[1]
    assert conflict["before"]["status"] == "conflict"
    assert [c["version"] for c in conflict["before"]["candidates"]] == [1, 2]
    assert conflict["after"]["status"] == "covered"
    assert conflict["after"]["basis"]["version"] == 1
    assert data["summary"]["conflict_us"] == us_between(jun, sep)


def test_changes_splits_on_union_of_boundaries(client):
    """两侧边界不同 → 按并集切分；分类相同但两侧候选不同的相邻段不得合并。"""
    post_rate(client, contract_no="CH-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="CH-6", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    revision = post_rate(client, contract_no="CH-6", rate=0.07,
                         valid_from="2026-03-01T00:00:00Z",
                         valid_to="2026-09-01T00:00:00Z",
                         recorded_at="2026-02-01T00:00:00Z").json()
    assert revision["superseded_rate_ids"] == [1, 2]
    assert [v["version"] for v in revision["retained_versions"]] == [4, 5]

    data = get_changes(client, "CH-6",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                       before_known_at="2026-01-15T00:00:00Z",
                       after_known_at="2026-02-02T00:00:00Z").json()
    segs = data["segments"]
    # 并集边界：01-01 / 03-01（after 侧）/ 06-01（before 侧）/ 09-01（after 侧）/ 次年 01-01
    assert [(s["from"], s["to"], s["change"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2026-03-01T00:00:00.000000+00:00", "unchanged"),
        ("2026-03-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00", "rate_changed"),
        ("2026-06-01T00:00:00.000000+00:00", "2026-09-01T00:00:00.000000+00:00", "rate_changed"),
        ("2026-09-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00", "unchanged"),
    ]
    # 相邻两段同为 rate_changed，但 before 候选不同（v1 vs v2），不得合并
    assert segs[1]["before"]["basis"]["version"] == 1
    assert segs[1]["after"]["basis"]["version"] == 3
    assert segs[2]["before"]["basis"]["version"] == 2
    assert segs[2]["after"]["basis"]["version"] == 3
    # 片段连续铺满整个窗口，逐段时长与分类汇总均等于窗口总长
    for prev, nxt in zip(segs, segs[1:]):
        assert prev["to"] == nxt["from"]
    total = us_between("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert sum(s["duration_us"] for s in segs) == total
    summary = data["summary"]
    assert sum(summary.values()) == total
    assert summary["rate_changed_us"] == us_between("2026-03-01T00:00:00Z", "2026-09-01T00:00:00Z")
    assert summary["unchanged_us"] == total - summary["rate_changed_us"]


def test_changes_both_sides_gap_is_unchanged(client):
    # 合同不存在 → 整窗 unchanged（两侧均 gap），不报 404
    data = get_changes(client, "GHOST",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-03-01T00:00:00Z").json()
    (seg,) = data["segments"]
    assert seg["change"] == "unchanged"
    assert seg["before"]["status"] == "gap"
    assert seg["after"]["status"] == "gap"
    assert seg["before"]["basis"] is None
    assert seg["after"]["candidates"] == []
    total = us_between("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert seg["duration_us"] == total
    assert data["summary"] == {
        "added_us": 0, "removed_us": 0, "unchanged_us": total,
        "rate_changed_us": 0, "conflict_us": 0,
    }

    # 合同存在但两个认知时点都早于 recorded_at → 版本尚不可见，同样整窗 unchanged
    post_rate(client, contract_no="CH-7", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    data = get_changes(client, "CH-7",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-03-01T00:00:00Z").json()
    assert [s["change"] for s in data["segments"]] == ["unchanged"]


def test_changes_no_revision_is_single_unchanged(client):
    """两个认知时点之间无任何修订：整窗 unchanged，两侧依据同一版本。"""
    post_rate(client, contract_no="CH-8", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = get_changes(client, "CH-8",
                       **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                       before_known_at="2026-02-01T00:00:00Z",
                       after_known_at="2026-06-01T00:00:00Z").json()
    (seg,) = data["segments"]
    assert seg["change"] == "unchanged"
    assert seg["before"]["basis"]["version"] == 1
    assert seg["after"]["basis"]["version"] == 1
    assert seg["duration_us"] == us_between("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")


def test_changes_invalid_window_422(client):
    for frm, to in [("2026-09-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = get_changes(client, "CH-X", **{"from": frm, "to": to},
                        before_known_at="2026-01-01T00:00:00Z",
                        after_known_at="2026-02-01T00:00:00Z")
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_changes_known_at_order_422(client):
    """before_known_at 必须严格早于 after_known_at（相等或倒置均拒绝）。"""
    for before, after in [("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                          ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = get_changes(client, "CH-X",
                        **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                        before_known_at=before, after_known_at=after)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_KNOWN_AT_ORDER"


def test_changes_missing_or_malformed_params_422(client):
    # 缺少必填的 before_known_at / after_known_at → 统一的 INVALID_REQUEST
    r = client.get("/contracts/CH-X/changes", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = get_changes(client, "CH-X",
                    **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"},
                    before_known_at="not-a-date",
                    after_known_at="2026-02-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


# ---------- 版本血缘 ----------

def get_lineage(client, rate_id):
    return client.get(f"/rates/{rate_id}/lineage")


def test_lineage_first_version_is_root_with_no_edges(client):
    """没有任何出边的版本（含初始录入）视为血缘根：单节点、无边。"""
    post_rate(client, contract_no="L-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始费率")
    r = get_lineage(client, 1)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["target_rate_id"] == 1
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["version"] == 1
    assert data["nodes"][0]["rate"] == 0.05
    assert data["edges"] == []


def test_lineage_full_overlap_chain_supersedes(client):
    """连续完整覆盖修订：新版本 -> 被取代版本的 supersedes 链。"""
    post_rate(client, contract_no="L-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="L-2", rate=0.08,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-03-01T00:00:00Z")
    post_rate(client, contract_no="L-2", rate=0.10,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    data = get_lineage(client, 3).json()
    # 节点按距目标从近到远、版本号排序：v3, v2, v1
    assert [n["rate_id"] for n in data["nodes"]] == [3, 2, 1]
    assert [n["version"] for n in data["nodes"]] == [3, 2, 1]
    assert {(e["from_rate_id"], e["to_rate_id"], e["relation"]) for e in data["edges"]} == {
        (3, 2, "supersedes"), (2, 1, "supersedes"),
    }

    # 从中间版本展开：只含它自己的祖先
    data = get_lineage(client, 2).json()
    assert [n["rate_id"] for n in data["nodes"]] == [2, 1]
    assert [(e["from_rate_id"], e["to_rate_id"], e["relation"]) for e in data["edges"]] == [
        (2, 1, "supersedes"),
    ]
    # 最早版本是根
    assert get_lineage(client, 1).json()["edges"] == []


def test_lineage_partial_overlap_retained_from_edges(client):
    """部分重叠修订：新版本 supersedes 旧版本；左右补偿片段 retained_from 旧版本。"""
    post_rate(client, contract_no="L-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="原费率")
    body = post_rate(client, contract_no="L-3", rate=0.09,
                     valid_from="2026-04-01T00:00:00Z",
                     valid_to="2026-07-01T00:00:00Z",
                     recorded_at="2026-03-01T00:00:00Z",
                     reason="年中调整").json()
    new_id = body["created"]["rate_id"]
    left_id, right_id = [v["rate_id"] for v in body["retained_versions"]]

    # 新版本的血缘：它 supersedes 旧版本
    data = get_lineage(client, new_id).json()
    assert [n["rate_id"] for n in data["nodes"]] == [new_id, 1]
    assert data["edges"] == [
        {"from_rate_id": new_id, "to_rate_id": 1, "relation": "supersedes"}]

    # 补偿片段的血缘：retained_from 旧版本
    for retained_id in (left_id, right_id):
        data = get_lineage(client, retained_id).json()
        assert [n["rate_id"] for n in data["nodes"]] == [retained_id, 1]
        assert data["edges"] == [
            {"from_rate_id": retained_id, "to_rate_id": 1, "relation": "retained_from"}]
        # 节点含现有版本全部字段
        node = data["nodes"][0]
        assert node["rate"] == 0.05 and node["reason"] == "原费率"
        assert set(node) == {
            "rate_id", "contract_no", "rate", "version", "valid_from", "valid_to",
            "recorded_at", "superseded_at", "status", "reason",
        }


def test_lineage_diamond_dag_dedup_and_order(client):
    """多源修订形成菱形 DAG：同一祖先经多条路径到达，节点去重且距离取最短，
    节点按 (距离, version) 排序、边按端点距离稳定排序。"""
    # v1 上半年 / v2 下半年（相接）
    post_rate(client, contract_no="L-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="上半年")
    post_rate(client, contract_no="L-4", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="下半年")
    # v3 压住 3–9 月：supersedes v1,v2；补偿 v4（1–3 月，源自 v1）、v5（9 月至年底，源自 v2）
    post_rate(client, contract_no="L-4", rate=0.07,
              valid_from="2026-03-01T00:00:00Z", valid_to="2026-09-01T00:00:00Z",
              recorded_at="2026-02-01T00:00:00Z", reason="第一次窗口修订")
    # v6 压住 2–12 月：supersedes 当前的 v4,v3,v5；补偿 v7（1–2 月源自 v4）、v8（12 月源自 v5）
    post_rate(client, contract_no="L-4", rate=0.08,
              valid_from="2026-02-01T00:00:00Z", valid_to="2026-12-01T00:00:00Z",
              recorded_at="2026-04-01T00:00:00Z", reason="第二次窗口修订")

    data = get_lineage(client, 6).json()
    # 距离：v6=0；v3/v4/v5=1；v1/v2=2（v1 同时经 v4 与 v3 到达，只出现一次）
    assert [n["rate_id"] for n in data["nodes"]] == [6, 3, 4, 5, 1, 2]
    assert [n["version"] for n in data["nodes"]] == [6, 3, 4, 5, 1, 2]
    assert [(e["from_rate_id"], e["to_rate_id"], e["relation"]) for e in data["edges"]] == [
        (6, 3, "supersedes"),
        (6, 4, "supersedes"),
        (6, 5, "supersedes"),
        (3, 1, "supersedes"),
        (3, 2, "supersedes"),
        (4, 1, "retained_from"),
        (5, 2, "retained_from"),
    ]

    # 从补偿片段 v7 展开：v7 -> v4 -> v1（同费率链）
    data = get_lineage(client, 7).json()
    assert [n["rate_id"] for n in data["nodes"]] == [7, 4, 1]
    assert [(e["from_rate_id"], e["to_rate_id"], e["relation"]) for e in data["edges"]] == [
        (7, 4, "retained_from"),
        (4, 1, "retained_from"),
    ]


def test_lineage_unknown_rate_404(client):
    r = get_lineage(client, 9999)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_NOT_FOUND"


def test_lineage_cycle_returns_409(client):
    """直接注入自环脏边：血缘出现环，返回 409 LINEAGE_CORRUPT 且不无限递归。"""
    post_rate(client, contract_no="L-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_version_edges "
            "(from_rate_id, to_rate_id, relation, recorded_at) "
            "VALUES (1, 1, 'supersedes', '2026-02-01T00:00:00.000000+00:00')"
        )
    r = get_lineage(client, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "LINEAGE_CORRUPT"


def test_lineage_dangling_edge_returns_409(client):
    """直接注入指向不存在版本的悬空边：返回 409 LINEAGE_CORRUPT。"""
    post_rate(client, contract_no="L-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_version_edges "
            "(from_rate_id, to_rate_id, relation, recorded_at) "
            "VALUES (1, 9999, 'supersedes', '2026-02-01T00:00:00.000000+00:00')"
        )
    r = get_lineage(client, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "LINEAGE_CORRUPT"


# ---------- 结算期费率锁 ----------

def post_lock(client, contract, **kw):
    return client.post(f"/contracts/{contract}/rate-locks", json=kw)


def get_locks(client, contract):
    return client.get(f"/contracts/{contract}/rate-locks")


def _seed_split_year(client, contract="K-1"):
    """全年费率，年中部分修订：v1 全年 -> v2（4–6 月）+ v3（1–4 月）+ v4（7 月至年底）。"""
    post_rate(client, contract_no=contract, rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="原费率")
    post_rate(client, contract_no=contract, rate=0.09,
              valid_from="2026-04-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
              recorded_at="2026-03-01T00:00:00Z", reason="年中调整")


def test_rate_lock_single_segment(client):
    """窗口完全落在单一可见版本内：单条锁记录，basis 为该版本。"""
    post_rate(client, contract_no="K-0", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始")
    r = post_lock(client, "K-0", **{
        "from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z", "reason": "2月结算期"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["contract_no"] == "K-0"
    assert body["from"] == "2026-02-01T00:00:00.000000+00:00"
    assert body["to"] == "2026-03-01T00:00:00.000000+00:00"
    assert body["known_at"] == "2026-02-01T00:00:00.000000+00:00"
    (lock,) = body["locks"]
    assert lock["from"] == body["from"] and lock["to"] == body["to"]
    assert lock["basis_rate_id"] == 1
    assert lock["known_at"] == body["known_at"]
    assert lock["reason"] == "2月结算期"
    assert set(lock) == {"lock_id", "contract_no", "from", "to", "known_at",
                         "basis_rate_id", "reason", "created_at"}


def test_rate_lock_splits_into_continuous_segments_with_basis(client):
    """锁定窗口跨越多个可见版本：复用覆盖切分，按连续片段逐段落库并记各自 basis rate_id。"""
    _seed_split_year(client)
    r = post_lock(client, "K-1", **{
        "from": "2026-02-01T00:00:00Z", "to": "2026-10-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z", "reason": "结算期锁定"})
    assert r.status_code == 201, r.text
    assert [(l["from"], l["to"], l["basis_rate_id"], l["reason"]) for l in r.json()["locks"]] == [
        ("2026-02-01T00:00:00.000000+00:00", "2026-04-01T00:00:00.000000+00:00", 3, "结算期锁定"),
        ("2026-04-01T00:00:00.000000+00:00", "2026-07-01T00:00:00.000000+00:00", 2, "结算期锁定"),
        ("2026-07-01T00:00:00.000000+00:00", "2026-10-01T00:00:00.000000+00:00", 4, "结算期锁定"),
    ]


def test_rate_lock_uses_known_at_visibility(client):
    """known_at 在修订之前：看到未切分的原版本，整窗单段 basis=v1。"""
    _seed_split_year(client, "K-2")
    r = post_lock(client, "K-2", **{
        "from": "2026-02-01T00:00:00Z", "to": "2026-10-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"})
    assert r.status_code == 201
    (lock,) = r.json()["locks"]
    assert lock["basis_rate_id"] == 1


def test_rate_lock_gap_returns_409_and_persists_nothing(client):
    """窗口含 gap：409 RATE_LOCK_UNSAFE，segments 标出 gap，且不落任何锁。"""
    _seed_split_year(client, "K-3")
    r = post_lock(client, "K-3", **{
        "from": "2026-06-01T00:00:00Z", "to": "2027-06-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "RATE_LOCK_UNSAFE"
    statuses = [(s["status"], s["basis"]["rate_id"] if s["basis"] else None)
                for s in detail["segments"]]
    assert statuses[0][0] == "covered"
    assert statuses[-1] == ("gap", None)
    # 失败后无任何锁
    assert get_locks(client, "K-3").json()["locks"] == []


def test_rate_lock_unknown_contract_is_gap_409(client):
    """合同不存在（该认知时点无可见版本）→ 整窗 gap → 409，而非创建锁。"""
    r = post_lock(client, "GHOST", **{
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-06-01T00:00:00Z"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "RATE_LOCK_UNSAFE"
    (seg,) = detail["segments"]
    assert seg["status"] == "gap"


def test_rate_lock_conflict_returns_409_with_candidates(client):
    """窗口内含重叠冲突（脏数据）：409 且 conflict 段附全部候选，锁不落库。"""
    _seed_split_year(client, "K-4")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('K-4', 0.99, '2026-05-01T00:00:00.000000+00:00', "
            "        '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-03-02T00:00:00.000000+00:00', 99)"
        )
    r = post_lock(client, "K-4", **{
        "from": "2026-02-01T00:00:00Z", "to": "2026-08-01T00:00:00Z",
        "known_at": "2026-03-03T00:00:00Z"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "RATE_LOCK_UNSAFE"
    (conflict,) = [s for s in detail["segments"] if s["status"] == "conflict"]
    assert conflict["from"] == "2026-05-01T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-06-01T00:00:00.000000+00:00"
    assert sorted(c["rate"] for c in conflict["candidates"]) == [0.09, 0.99]
    assert get_locks(client, "K-4").json()["locks"] == []


def test_rate_lock_invalid_interval_422(client):
    for frm, to in [("2026-09-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = post_lock(client, "K-X", **{"from": frm, "to": to,
                                       "known_at": "2026-03-02T00:00:00Z"})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_rate_lock_malformed_request_422(client):
    r = post_lock(client, "K-X", **{"to": "2026-06-01T00:00:00Z",
                                   "known_at": "2026-03-02T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = post_lock(client, "K-X", **{"from": "2026-01-01T00:00:00Z",
                                   "to": "bad", "known_at": "2026-03-02T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


# ---------- POST /rates 与锁的交互 ----------

def _lock_q2(client, contract="K-5", **over):
    payload = {
        "from": "2026-04-01T00:00:00Z", "to": "2026-07-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z", "reason": "Q2锁定",
    }
    payload.update(over)
    return post_lock(client, contract, **payload)


def test_rate_revision_intersecting_lock_409(client):
    """拟议区间与锁片段相交：409 RATE_WINDOW_LOCKED，列出锁及交集，不写版本/补偿/边。"""
    _seed_split_year(client, "K-5")
    lr = _lock_q2(client)
    assert lr.status_code == 201

    r = post_rate(client, contract_no="K-5", rate=0.2,
                  valid_from="2026-05-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
                  recorded_at="2026-08-01T00:00:00Z", reason="锁定窗口内修订")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "RATE_WINDOW_LOCKED"
    (entry_lock,), (entry_ix,) = detail["locks"], detail["intersections"]
    assert entry_lock["basis_rate_id"] == 2
    assert entry_lock["reason"] == "Q2锁定"
    assert entry_ix == {
        "from": "2026-05-01T00:00:00.000000+00:00",
        "to": "2026-06-01T00:00:00.000000+00:00",
    }

    # 没有新版本：时间线仍是 v1..v4
    versions = client.get("/contracts/K-5/timeline").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2, 3, 4]
    assert all(v["status"] == "current" for v in versions if v["version"] in (2, 3, 4))
    # 没有新增血缘边：v2 仍只有 supersedes v1
    edges = client.get("/rates/2/lineage").json()["edges"]
    assert [(e["from_rate_id"], e["to_rate_id"], e["relation"]) for e in edges] == [
        (2, 1, "supersedes")]


def test_rate_revision_partial_overlap_lock_lists_clip_and_segments(client):
    """拟议区间部分压住锁：交集裁剪为重叠部分；跨多段锁时逐段列出。"""
    _seed_split_year(client, "K-6")
    # 锁 2–10 月，三段 basis（v3/v2/v4）
    post_lock(client, "K-6", **{
        "from": "2026-02-01T00:00:00Z", "to": "2026-10-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"})
    # 拟议 3–8 月，与三段锁均相交；交集裁剪到 [03,04)/[04,07)/[07,08)
    r = post_rate(client, contract_no="K-6", rate=0.3,
                  valid_from="2026-03-01T00:00:00Z", valid_to="2026-08-01T00:00:00Z",
                  recorded_at="2026-09-01T00:00:00Z")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "RATE_WINDOW_LOCKED"
    assert [l["basis_rate_id"] for l in detail["locks"]] == [3, 2, 4]
    assert [(i["from"], i["to"]) for i in detail["intersections"]] == [
        ("2026-03-01T00:00:00.000000+00:00", "2026-04-01T00:00:00.000000+00:00"),
        ("2026-04-01T00:00:00.000000+00:00", "2026-07-01T00:00:00.000000+00:00"),
        ("2026-07-01T00:00:00.000000+00:00", "2026-08-01T00:00:00.000000+00:00"),
    ]


def test_rate_revision_touching_lock_boundary_allowed(client):
    """仅相接（不相交）的拟议区间不被拒绝；开放上界旧版本仍照常补偿修订。"""
    _seed_split_year(client, "K-7")
    _lock_q2(client, "K-7")
    # 从 2026-07-01（锁终点，不含）起的区间与锁仅相接
    r = post_rate(client, contract_no="K-7", rate=0.11,
                  valid_from="2026-07-01T00:00:00Z", valid_to="2026-08-01T00:00:00Z",
                  recorded_at="2026-08-01T00:00:00Z")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"]["version"] == 5
    assert body["superseded_rate_ids"] == [4]
    # 右补偿片段（8 月至年底，原费率 0.05）
    (right,) = body["retained_versions"]
    assert right["rate"] == 0.05
    assert (right["valid_from"], right["valid_to"]) == (
        "2026-08-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00")

    # 终点恰好为锁起点（不含）同样允许
    r2 = post_rate(client, contract_no="K-7", rate=0.12,
                   valid_from="2026-02-01T00:00:00Z", valid_to="2026-04-01T00:00:00Z",
                   recorded_at="2026-09-01T00:00:00Z")
    assert r2.status_code == 201, r2.text


def test_rate_revision_open_ended_interval_vs_lock(client):
    """开放上界拟议区间与锁相交：仍被拒绝，交集上界取锁终点。"""
    _seed_split_year(client, "K-8")
    _lock_q2(client, "K-8")
    r = post_rate(client, contract_no="K-8", rate=0.4,
                  valid_from="2026-06-15T00:00:00Z",
                  recorded_at="2026-08-01T00:00:00Z")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "RATE_WINDOW_LOCKED"
    assert detail["intersections"][0] == {
        "from": "2026-06-15T00:00:00.000000+00:00",
        "to": "2026-07-01T00:00:00.000000+00:00",
    }
    # 锁外的开放上界修订（锁终点之后）允许
    r2 = post_rate(client, contract_no="K-8", rate=0.4,
                   valid_from="2027-07-01T00:00:00Z",
                   recorded_at="2026-11-01T00:00:00Z")
    assert r2.status_code == 201, r2.text


def test_lock_check_runs_before_monotonic_check(client):
    """相交锁的拒绝优先于 recorded_at 单调性校验，且同样不落库。"""
    _seed_split_year(client, "K-9")
    _lock_q2(client, "K-9")
    r = post_rate(client, contract_no="K-9", rate=0.2,
                  valid_from="2026-05-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
                  recorded_at="2026-01-02T00:00:00Z")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RATE_WINDOW_LOCKED"


# ---------- 锁记录查询 ----------

def test_list_rate_locks(client):
    """查询返回合同全部锁（含多次锁定），按片段起点排序；无锁合同返回空列表。"""
    _seed_split_year(client, "K-10")
    post_lock(client, "K-10", **{
        "from": "2026-05-01T00:00:00Z", "to": "2026-06-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z", "reason": "5月"})
    post_lock(client, "K-10", **{
        "from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z", "reason": "2月"})
    r = get_locks(client, "K-10")
    assert r.status_code == 200
    body = r.json()
    assert body["contract_no"] == "K-10"
    assert [(l["from"], l["basis_rate_id"], l["reason"]) for l in body["locks"]] == [
        ("2026-02-01T00:00:00.000000+00:00", 3, "2月"),
        ("2026-05-01T00:00:00.000000+00:00", 2, "5月"),
    ]

    post_rate(client, contract_no="K-11", rate=0.1,
              valid_from="2026-01-01T00:00:00Z", recorded_at="2026-01-01T00:00:00Z")
    empty = get_locks(client, "K-11")
    assert empty.status_code == 200
    assert empty.json() == {"contract_no": "K-11", "locks": []}


def test_list_rate_locks_unknown_contract_404(client):
    r = get_locks(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CONTRACT_NOT_FOUND"
