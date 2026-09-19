"""合同费率双时态追溯 API。"""

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import logic
from .db import init_db
from .timeutil import iso


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时自动建表
    yield


app = FastAPI(
    title="合同费率双时态追溯 API",
    version="1.5.0",
    description="按业务时间（valid_at）与认知时间（known_at）双维度追溯合同费率，修订只追加版本、不覆盖历史；版本血缘以可审计的 supersedes / retained_from 边记录；结算期费率锁保证锁定窗口内费率不再被修订。",
    lifespan=lifespan,
)


# ---------- 请求 / 响应模型 ----------

class RateCreate(BaseModel):
    contract_no: str = Field(min_length=1, max_length=128, description="合同号")
    rate: float = Field(description="费率值")
    valid_from: datetime = Field(description="业务生效起点（含），ISO-8601")
    valid_to: Optional[datetime] = Field(default=None, description="业务生效终点（不含），缺省为无限期")
    recorded_at: Optional[datetime] = Field(default=None, description="系统记录时间，缺省为当前时间；同一合同内必须单调递增")
    reason: Optional[str] = Field(default=None, max_length=500, description="录入/修订说明")


class VersionView(BaseModel):
    rate_id: int
    contract_no: str
    rate: float
    version: int
    valid_from: str
    valid_to: Optional[str]
    recorded_at: str
    superseded_at: Optional[str]
    status: str  # current / superseded
    reason: Optional[str]


class RateCreated(BaseModel):
    created: VersionView
    superseded_rate_ids: list[int]
    retained_versions: list[VersionView] = Field(
        default_factory=list,
        description="部分重叠修订时，被覆盖旧版本在新区间之外的左/右补偿版本（原费率、原 reason），按 valid_from 排序",
    )


class QuoteResponse(BaseModel):
    contract_no: str
    rate: float
    valid_at: str
    known_at: str
    basis: VersionView = Field(description="命中费率的版本依据")


class TimelineResponse(BaseModel):
    contract_no: str
    versions: list[VersionView]


class LineageEdge(BaseModel):
    from_rate_id: int = Field(description="后继版本 id（新费率或补偿片段）")
    to_rate_id: int = Field(description="祖先版本 id（被取代版本或补偿片段来源版本）")
    relation: str = Field(description="supersedes（取代）/ retained_from（补偿片段来源）")


class LineageResponse(BaseModel):
    target_rate_id: int
    nodes: list[VersionView] = Field(
        description="目标及其全部祖先版本（去重 DAG），按距目标从近到远、version 升序排序"
    )
    edges: list[LineageEdge] = Field(description="血缘边（后继 -> 祖先），按端点距离稳定排序")


class CoverageSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    status: str = Field(description="covered / gap / conflict")
    basis: Optional[VersionView] = Field(default=None, description="covered 时命中版本的唯一依据")
    candidates: list[VersionView] = Field(default_factory=list, description="conflict 时的全部候选版本")


class CoverageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="查询窗口起点（含）")
    to: str = Field(description="查询窗口终点（不含）")
    known_at: str = Field(description="实际使用的认知时点")
    segments: list[CoverageSegment]


class ChangeSideView(BaseModel):
    status: str = Field(description="该认知时点下的覆盖状态：covered / gap / conflict")
    basis: Optional[VersionView] = Field(default=None, description="covered 时命中版本的唯一依据")
    candidates: list[VersionView] = Field(default_factory=list, description="conflict 时的全部候选版本")


class ChangeSegmentView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    change: str = Field(description="added / removed / unchanged / rate_changed / conflict")
    duration_us: int = Field(description="该片段的持续时长（微秒）")
    before: ChangeSideView = Field(description="before_known_at 时点下的覆盖状态")
    after: ChangeSideView = Field(description="after_known_at 时点下的覆盖状态")


class ChangeSummary(BaseModel):
    added_us: int = Field(description="gap→covered 片段的总时长（微秒）")
    removed_us: int = Field(description="covered→gap 片段的总时长（微秒）")
    unchanged_us: int = Field(description="无变化片段的总时长（微秒）")
    rate_changed_us: int = Field(description="费率变化片段的总时长（微秒）")
    conflict_us: int = Field(description="任一侧冲突片段的总时长（微秒）")


class ChangesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="查询窗口起点（含）")
    to: str = Field(description="查询窗口终点（不含）")
    before_known_at: str = Field(description="较早认知时点")
    after_known_at: str = Field(description="较晚认知时点")
    segments: list[ChangeSegmentView]
    summary: ChangeSummary = Field(description="各变更分类的持续微秒数汇总")


class ChargeLineIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from", description="账单行起点（含），ISO-8601")
    to: datetime = Field(description="账单行终点（不含），ISO-8601")
    base_amount: float = Field(description="账单行基准金额（须为非负有限数值），按区间时长比例折算")


class ChargePreviewRequest(BaseModel):
    known_at: Optional[datetime] = Field(default=None, description="认知时点，缺省为当前时间；不得晚于当前时间")
    lines: list[ChargeLineIn] = Field(min_length=1, description="账单行列表，至少一条")


class ChargeCandidate(BaseModel):
    rate_id: int
    rate: float
    version: int


class ChargeSegmentView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    status: str = Field(description="covered / gap / conflict")
    portion: float = Field(description="该段时长占账单行总时长的比例（6 位小数）")
    rate_id: Optional[int] = Field(default=None, description="covered 时命中的费率版本 id")
    rate: Optional[float] = Field(default=None, description="covered 时命中的费率值")
    charge: Optional[float] = Field(default=None, description="covered 时该段折算费用（6 位小数）")
    candidates: list[ChargeCandidate] = Field(default_factory=list, description="conflict 时的全部候选版本")


class ChargeLineView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="账单行起点（含）")
    to: str = Field(description="账单行终点（不含）")
    base_amount: float
    status: str = Field(description="ok / gap / conflict / mixed（同时含 gap 与 conflict 段）")
    line_total: Optional[float] = Field(default=None, description="行内费用合计；含 gap/conflict 段时为 null")
    segments: list[ChargeSegmentView]


class ChargePreviewResponse(BaseModel):
    contract_no: str
    known_at: str = Field(description="实际使用的认知时点")
    status: str = Field(description="ok / rejected（任一行含 gap/conflict 段，总额被拒绝）")
    total_amount: Optional[float] = Field(default=None, description="全部行费用合计；rejected 时为 null")
    lines: list[ChargeLineView]


class RateLockCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from", description="锁定结算期起点（含），ISO-8601")
    to: datetime = Field(description="锁定结算期终点（不含），ISO-8601")
    known_at: datetime = Field(description="锁定所依据的认知时点，ISO-8601")
    reason: Optional[str] = Field(default=None, max_length=500, description="锁定原因")


class RateLockView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    lock_id: int
    contract_no: str
    from_: str = Field(alias="from", description="锁定片段起点（含）")
    to: str = Field(description="锁定片段终点（不含）")
    known_at: str = Field(description="锁定所依据的认知时点")
    basis_rate_id: int = Field(description="该片段在 known_at 时点唯一可见的费率版本 id")
    reason: Optional[str]
    created_at: str = Field(description="锁记录写入时间（审计用）")


class RateLockCreated(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="锁定窗口起点（含）")
    to: str = Field(description="锁定窗口终点（不含）")
    known_at: str
    locks: list[RateLockView] = Field(
        description="按覆盖率切分得到的连续锁记录（每段附 basis rate_id），按片段起点排序"
    )


class RateLockListResponse(BaseModel):
    contract_no: str
    locks: list[RateLockView]


def to_view(row: sqlite3.Row) -> VersionView:
    return VersionView(
        rate_id=row["id"],
        contract_no=row["contract_no"],
        rate=row["rate"],
        version=row["version"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        superseded_at=row["superseded_at"],
        status="current" if row["superseded_at"] is None else "superseded",
        reason=row["reason"],
    )


def to_lock_view(row: sqlite3.Row) -> RateLockView:
    return RateLockView(
        lock_id=row["id"],
        contract_no=row["contract_no"],
        from_=row["valid_from"],
        to=row["valid_to"],
        known_at=row["known_at"],
        basis_rate_id=row["basis_rate_id"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


def to_coverage_segment(seg: dict) -> CoverageSegment:
    return CoverageSegment(
        from_=seg["from"],
        to=seg["to"],
        status=seg["status"],
        basis=to_view(seg["candidates"][0]) if seg["status"] == "covered" else None,
        candidates=[to_view(r) for r in seg["candidates"]] if seg["status"] == "conflict" else [],
    )


# ---------- 异常处理 ----------

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"code": "INVALID_REQUEST", "message": "请求参数不合法", "errors": jsonable_encoder(exc.errors())},
    )


# ---------- 路由 ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/rates", response_model=RateCreated, status_code=status.HTTP_201_CREATED)
def create_rate(payload: RateCreate):
    """录入费率。与既有当前版本区间重叠时自动修订：旧版本仅交集被替换，
    交集之外的左、右部分按原费率与 reason 追加为补偿版本，历史保留、窗口外报价不变。"""
    try:
        row, superseded_ids, retained_rows = logic.record_rate(
            contract_no=payload.contract_no,
            rate=payload.rate,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            recorded_at=payload.recorded_at,
            reason=payload.reason,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.NonMonotonicRecordTimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NON_MONOTONIC_RECORDED_AT", "message": str(exc)},
        )
    except logic.RateWindowLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RATE_WINDOW_LOCKED",
                "message": str(exc),
                "locks": [to_lock_view(c["lock"]).model_dump(by_alias=True) for c in exc.conflicts],
                "intersections": [c["intersection"] for c in exc.conflicts],
            },
        )
    return RateCreated(
        created=to_view(row),
        superseded_rate_ids=superseded_ids,
        retained_versions=[to_view(r) for r in retained_rows],
    )


@app.get("/quote", response_model=QuoteResponse)
def quote(
    contract_no: str = Query(min_length=1, description="合同号"),
    valid_at: datetime = Query(description="业务时点，ISO-8601"),
    known_at: Optional[datetime] = Query(default=None, description="认知时点，缺省为当前时间"),
):
    """报价：返回在 valid_at 有效、且在 known_at 时点可见的唯一费率及版本依据。"""
    rows, ka = logic.find_visible_rates(contract_no, valid_at, known_at)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NO_MATCH",
                "message": f"合同 '{contract_no}' 在业务时点 {valid_at}、认知时点 {ka} 下无可见且有效的费率",
            },
        )
    if len(rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "OVERLAP",
                "message": f"合同 '{contract_no}' 在该时点坐标下命中 {len(rows)} 条费率，数据存在重叠冲突",
                "candidates": [to_view(r).model_dump() for r in rows],
            },
        )
    row = rows[0]
    return QuoteResponse(
        contract_no=row["contract_no"],
        rate=row["rate"],
        valid_at=iso(valid_at),
        known_at=ka,
        basis=to_view(row),
    )


@app.get("/contracts/{contract_no}/timeline", response_model=TimelineResponse)
def timeline(contract_no: str):
    """版本时间线：列出合同全部版本及其业务/系统时间区间与状态。"""
    rows = logic.get_timeline(contract_no)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTRACT_NOT_FOUND", "message": f"合同 '{contract_no}' 无任何费率版本"},
        )
    return TimelineResponse(contract_no=contract_no, versions=[to_view(r) for r in rows])


@app.get("/rates/{rate_id}/lineage", response_model=LineageResponse)
def rate_lineage(rate_id: int):
    """版本血缘：从目标版本沿 supersedes / retained_from 边向全部祖先展开的去重 DAG。
    节点含版本全部现有字段，边含 relation；节点按距目标从近到远、版本号稳定排序。
    没有出边的历史行视为根（单独返回目标节点、无边）。血缘损坏（环或悬空边）
    返回 409 LINEAGE_CORRUPT。"""
    try:
        result = logic.get_lineage(rate_id)
    except logic.RateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RATE_NOT_FOUND", "message": str(exc)},
        )
    except logic.LineageCorruptError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "LINEAGE_CORRUPT", "message": str(exc)},
        )
    return LineageResponse(
        target_rate_id=result["target_id"],
        nodes=[to_view(r) for r in result["nodes"]],
        edges=[LineageEdge(**e) for e in result["edges"]],
    )


@app.get("/contracts/{contract_no}/coverage", response_model=CoverageResponse)
def coverage(
    contract_no: str,
    from_: datetime = Query(alias="from", description="查询窗口起点（含），ISO-8601"),
    to: datetime = Query(description="查询窗口终点（不含），ISO-8601"),
    known_at: Optional[datetime] = Query(default=None, description="认知时点，缺省为当前时间"),
):
    """覆盖率分析：以 known_at 可见版本的业务区间边界把 [from, to) 切为连续片段，
    逐段标记 covered（附唯一版本依据）/ gap / conflict（附全部候选），
    分类与候选完全相同的相邻段合并。"""
    try:
        segments, ka = logic.compute_coverage(contract_no, from_, to, known_at)
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    return CoverageResponse(
        contract_no=contract_no,
        from_=iso(from_),
        to=iso(to),
        known_at=ka,
        segments=[to_coverage_segment(seg) for seg in segments],
    )


@app.get("/contracts/{contract_no}/changes", response_model=ChangesResponse)
def changes(
    contract_no: str,
    from_: datetime = Query(alias="from", description="查询窗口起点（含），ISO-8601"),
    to: datetime = Query(description="查询窗口终点（不含），ISO-8601"),
    before_known_at: datetime = Query(description="较早认知时点，ISO-8601"),
    after_known_at: datetime = Query(description="较晚认知时点，ISO-8601，须晚于 before_known_at"),
):
    """变更对比：分别按 before_known_at / after_known_at 两个认知时点筛选可见费率，
    以两侧业务区间边界的并集把 [from, to) 切为连续片段，逐段返回 before/after
    状态及版本依据并分类（added / removed / unchanged / rate_changed / conflict；
    分类与两侧候选完全一致的相邻段合并），并汇总各分类的持续微秒数。"""
    try:
        result = logic.compute_changes(contract_no, from_, to, before_known_at, after_known_at)
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.InvalidKnownAtOrderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_KNOWN_AT_ORDER", "message": str(exc)},
        )

    def side_view(side: dict) -> ChangeSideView:
        return ChangeSideView(
            status=side["status"],
            basis=to_view(side["candidates"][0]) if side["status"] == "covered" else None,
            candidates=[to_view(r) for r in side["candidates"]] if side["status"] == "conflict" else [],
        )

    return ChangesResponse(
        contract_no=contract_no,
        from_=iso(from_),
        to=iso(to),
        before_known_at=result["before_known_at"],
        after_known_at=result["after_known_at"],
        segments=[
            ChangeSegmentView(
                from_=seg["from"],
                to=seg["to"],
                change=seg["change"],
                duration_us=seg["duration_us"],
                before=side_view(seg["before"]),
                after=side_view(seg["after"]),
            )
            for seg in result["segments"]
        ],
        summary=ChangeSummary(**result["summary"]),
    )


@app.post("/contracts/{contract_no}/charge-preview", response_model=ChargePreviewResponse)
def charge_preview(contract_no: str, payload: ChargePreviewRequest):
    """计费预览：把每条账单行按 known_at 可见费率的业务边界拆分，
    按时长比例折算各版本费用并汇总；遇 gap/conflict 段时标记并拒绝总额。"""
    try:
        result = logic.compute_charge_preview(
            contract_no,
            [(line.from_, line.to, line.base_amount) for line in payload.lines],
            payload.known_at,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.InvalidAmountError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_AMOUNT", "message": str(exc)},
        )
    except logic.FutureKnownAtError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_KNOWN_AT", "message": str(exc)},
        )
    return ChargePreviewResponse(
        contract_no=contract_no,
        known_at=result["known_at"],
        status="ok" if result["ok"] else "rejected",
        total_amount=result["total_amount"],
        lines=result["lines"],
    )


@app.post(
    "/contracts/{contract_no}/rate-locks",
    response_model=RateLockCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_rate_lock(contract_no: str, payload: RateLockCreate):
    """锁定结算期费率：复用覆盖率切分规则校验 [from, to) 在 known_at 时点全部 covered。

    仅当整个窗口均为 covered 时，才在单一事务内按连续片段保存锁记录及各段的
    basis rate_id；存在 gap/conflict 返回 409 RATE_LOCK_UNSAFE 且不落库。
    """
    try:
        lock_rows, ka = logic.create_rate_lock(
            contract_no,
            payload.from_,
            payload.to,
            payload.known_at,
            payload.reason,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.RateLockUnsafeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RATE_LOCK_UNSAFE",
                "message": str(exc),
                "segments": [
                    to_coverage_segment(seg).model_dump(by_alias=True)
                    for seg in exc.segments
                ],
            },
        )
    return RateLockCreated(
        contract_no=contract_no,
        from_=iso(payload.from_),
        to=iso(payload.to),
        known_at=ka,
        locks=[to_lock_view(r) for r in lock_rows],
    )


@app.get("/contracts/{contract_no}/rate-locks", response_model=RateLockListResponse)
def list_rate_locks(contract_no: str):
    """合同的结算期费率锁记录：按锁定片段起点（写入顺序）列出全部锁及其 basis rate_id。"""
    if not logic.get_timeline(contract_no):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTRACT_NOT_FOUND", "message": f"合同 '{contract_no}' 无任何费率版本"},
        )
    return RateLockListResponse(
        contract_no=contract_no,
        locks=[to_lock_view(r) for r in logic.get_rate_locks(contract_no)],
    )
