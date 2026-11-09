"""
FinLens · 财报透镜
==================
面向个人投资者的 AI 财报速读与风险预警 Agent（零依赖 Web 服务）。

三条不可让渡的约束，也是本文件的结构依据：
  1. 没有原文引文，不出结论（claims 里 evidence 为空的条目直接不生成）
  2. 数字不经模型（数值来自数据快照或 PDF 抽取；LLM 只做措辞，输出要过数字白名单）
  3. 异常 ≠ 结论（风险雷达只输出"需人工复核"，并列公司自述原因，不给评级/打分）

运行：python app.py        环境变量：PORT（默认 8000）
可选：pip install pypdf    安装后启用 PDF 上传解析，未安装则明确降级
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PORT = int(os.environ.get("PORT", "8000"))

try:  # 可选依赖：缺失时降级而非崩溃
    from pypdf import PdfReader

    HAS_PYPDF = True
except Exception:  # pragma: no cover
    HAS_PYPDF = False

DISCLAIMER = (
    "本内容基于公开定期报告自动生成，所有结论均附原文出处以便核对。仅供研究参考，不构成任何投资建议；"
    "风险信号仅表示「该指标偏离常规区间，建议人工复核」，不代表对公司经营或财务状况的负面判断。"
)

# 证券投资咨询需持牌：生成侧命中即拦截改写
ADVICE_WORDS = [
    "买入", "卖出", "增持", "减持", "清仓", "建仓", "加仓", "目标价", "目标市值",
    "评级", "强烈推荐", "建议关注", "值得布局", "抄底", "上车", "必涨", "翻倍", "稳赚",
]

# 风险规则阈值（"筛选值得看一眼"的经验阈值，非判定标准）
TH = {"R1": 20.0, "R2": 15.0, "R3": 0.5, "R4": 0.70, "R5": 30.0, "R6": 0.50, "R7": -1.0}

CORE = ["revenue", "np_parent", "np_deduct", "ocf", "inventory", "ar", "equity"]

LABELS = {
    "revenue": "营业收入", "np_parent": "归母净利润", "np_deduct": "扣非净利润",
    "ocf": "经营活动现金流净额", "inventory": "存货", "ar": "应收账款",
    "equity": "归母净资产", "goodwill": "商誉", "st_debt": "短期借款",
    "cash": "货币资金", "rd_exp": "研发费用", "sell_exp": "销售费用",
    "admin_exp": "管理费用", "nre": "非经常性损益合计",
}

FLOW_KEYS = {"revenue", "np_parent", "np_deduct", "ocf"}

_METRIC_PATTERNS = {
    "revenue": r"营业总收入|营业收入",
    "np_parent": r"归属于上市公司股东的净利润|归属于母公司股东的净利润|归母净利润",
    "np_deduct": r"扣除非经常性损益的净利润|扣非净利润",
    "ocf": r"经营活动产生的现金流量净额",
    "inventory": r"存货",
    "ar": r"应收账款",
    "equity": r"归属于上市公司股东的所有者权益|归属于母公司所有者权益",
    "goodwill": r"商誉",
    "st_debt": r"短期借款",
    "cash": r"货币资金",
    "rd_exp": r"研发费用",
    "sell_exp": r"销售费用",
    "admin_exp": r"管理费用",
}

_CELL_RE = re.compile(r"(-?[\d,]+\.\d+|—|-?[\d,]{3,})")


# ===========================================================================
# §1 数据适配层（真实接入点）
# ===========================================================================

def load_companies() -> dict:
    """读取数据快照。真实环境替换为 iFinD MCP / 扶摇 的取数实现即可，上层无感知：
        iFinD MCP → 财报三表、公告全文、研报   扶摇 → 行情、估值、板块成分
    """
    with open(ROOT / "data" / "companies.json", encoding="utf-8") as fh:
        return json.load(fh)["companies"]


COMPANIES = load_companies()


# ===========================================================================
# §2 PDF 解析与指标抽取
# ===========================================================================

class PDFParseError(Exception):
    """带错误码的解析异常，便于前端给出可操作提示。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def parse_pdf(data: bytes) -> dict:
    if not HAS_PYPDF:
        raise PDFParseError("NO_PDF_LIB", "当前部署未启用 PDF 解析（缺少 pypdf），可切换内置样例。")
    if not data or len(data) < 1024:
        raise PDFParseError("EMPTY", "文件为空或过小，请确认上传的是财报 PDF 本体。")
    if len(data) > 80 * 1024 * 1024:
        raise PDFParseError("TOO_LARGE", "文件超过 80MB，请仅上传所需报告期的那一份。")
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise PDFParseError("BROKEN", f"文件无法解析（{type(exc).__name__}），请确认是原始财报而非截图。")

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise PDFParseError("ENCRYPTED", "该 PDF 已加密，请先解除密码后再上传。")
    try:
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as exc:
        raise PDFParseError("EXTRACT_FAIL", f"文本层读取失败（{type(exc).__name__}），可能是扫描件。")

    text = "\n".join(pages).strip()
    if len(text) < 200:
        raise PDFParseError("NO_TEXT_LAYER", "该 PDF 疑似扫描件（无文本层）。本产品不做 OCR。")
    return {"text": text, "pages": len(pages)}


def _to_num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if s in ("—", "-", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_metrics(text: str) -> dict:
    """尽力抽取关键指标：抽到的带 quote（可核对），抽不到的标为未覆盖，不猜。"""
    found: dict = {}
    for key, pat in _METRIC_PATTERNS.items():
        m = re.search(pat, text)
        if not m:
            continue
        seg = text[m.start(): m.start() + 240]
        nums = [n for n in (_to_num(c) for c in _CELL_RE.findall(seg)) if n is not None]
        if not nums:
            continue
        first = nums[0]
        # 量级门禁：资产/损益类科目正常在百万以上，过小视为解析异常直接丢弃
        if key not in ("goodwill", "st_debt") and abs(first) < 1e6:
            continue
        if abs(first) < 1e4:
            continue

        def _is_pct(x: float | None) -> bool:
            # 必须排除年份：实测 2026 曾被当成同比
            return x is not None and abs(x) < 1000 and not (1900 <= abs(x) <= 2100)

        value, prev, yoy = first, None, None
        rest = nums[1:4]
        # 季报「主要会计数据」常为 [单季, 单季同比, 累计, 累计同比]，统一取累计口径
        if key in FLOW_KEYS and len(nums) >= 3 and abs(nums[2]) >= 1e6 and abs(nums[2]) >= abs(nums[0]) * 0.9:
            value = nums[2]
            if len(nums) > 3 and _is_pct(nums[3]):
                yoy = nums[3]
        elif rest:
            if abs(rest[0]) >= 1e5:      # 看起来是金额 → 上年同期
                prev = rest[0]
                if len(rest) > 1 and _is_pct(rest[1]):
                    yoy = rest[1]
            elif _is_pct(rest[0]):       # 看起来是百分比 → 同比
                yoy = rest[0]
        if yoy is None and prev:
            yoy = growth(value, prev)

        found[key] = {
            "label": LABELS.get(key, key), "unit": "元", "value": value, "prev": prev,
            "yoy": yoy, "quote": seg[:120].replace("\n", " ⏎ "), "offset": m.start(),
        }
    return found


_SUBJECT_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,14}?(?:股份有限公司|有限责任公司|有限公司))")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")


def extract_meta(text: str) -> dict:
    """抽取文件主体与报告期年份，用于拦截"上传了别家公司财报"这类主体错配。"""
    head = text[:800]
    m = _SUBJECT_RE.search(head)
    subject = m.group(1) if m else None
    y = _YEAR_RE.search(head)
    return {"subject": subject, "year": int(y.group(1)) if y else None}


def build_facts(code: str, uploaded: dict | None, as_of: date,
                uploaded_meta: dict | None = None) -> dict:
    """以内置快照为基线，用上传文件抽到的指标覆盖同名项，并记录来源。"""
    base = COMPANIES[code]
    f = {
        "code": base["code"], "name": base["name"], "period": base["period"],
        "doc_title": base["doc_title"], "published_at": base["published_at"],
        "pdf_url": base["pdf_url"], "page": base["page"], "offset_kind": "页内字符偏移",
        "explanations": dict(base.get("explanations", {})),
        "metrics": {k: dict(v, source="builtin") for k, v in base["metrics"].items()},
        "uploaded_count": 0, "disclosure_unknown": False, "mixed": False,
        "carried_over": [], "subject_unverified": False,
    }
    if uploaded:
        # 主体校验：上传的是别家公司的财报 → 拒绝，不把两家数字混在一份结论里
        if uploaded_meta and uploaded_meta.get("subject"):
            subj = uploaded_meta["subject"]
            if base["name"] not in subj and subj not in base["name"]:
                raise PDFParseError(
                    "SUBJECT_MISMATCH",
                    f"该文件的主体是「{subj}」，与当前选择的「{base['name']}」不一致。"
                    f"请先在左侧切换到对应公司再上传，避免把两家公司的数字混在一起。")
        if uploaded_meta and uploaded_meta.get("year"):
            by = int(re.match(r"\s*(20\d{2})", base["period"]).group(1)) \
                if re.match(r"\s*(20\d{2})", base["period"]) else None
            if by and uploaded_meta["year"] != by:
                raise PDFParseError(
                    "PERIOD_MISMATCH",
                    f"该文件的报告期年份是 {uploaded_meta['year']} 年，"
                    f"与当前选择的 {base['period']} 不一致。请选择对应报告期后再上传。")
        if uploaded_meta is not None and not uploaded_meta.get("subject"):
            f["subject_unverified"] = True

        for k, v in uploaded.items():
            if k in f["metrics"]:
                merged = dict(f["metrics"][k])
                merged.update({kk: vv for kk, vv in v.items() if kk != "label"})
                merged["label"] = f["metrics"][k]["label"]
            else:
                merged = dict(v)
            merged["source"] = "uploaded"
            f["metrics"][k] = merged
        f["uploaded_count"] = len(uploaded)
        # 漏掉的科目仍来自内置快照 → 口径混用，必须显式提示并列出沿用了哪几项
        f["carried_over"] = [k for k in base["metrics"] if k not in uploaded]
        f["mixed"] = bool(f["carried_over"])
        f["doc_title"] = "上传文件"
        # 上传件的真实披露日无法可靠取得：按"稿件日期当时已可得"处理并标注未知，
        # 避免把一份正常财报误判成"使用未来信息"。
        f["published_at"] = as_of.isoformat()
        f["disclosure_unknown"] = True
        # 上传件没有稳定的页码/页内偏移，也没有可回链的原始 URL：
        # 证据改为「全文偏移」，且不外链到内置快照的 PDF（否则等于指错原文）
        f["page"] = None
        f["pdf_url"] = None
        f["offset_kind"] = "全文字符偏移"
    return f


# ===========================================================================
# §3 格式化与计算层（纯函数 —— "数字不经模型"的落点）
# ===========================================================================

def money(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:,.2f} 亿元"
    if a >= 1e4:
        return f"{v / 1e4:,.2f} 万元"
    return f"{v:,.2f} 元"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def growth(cur: float | None, prev: float | None) -> float | None:
    if not cur or not prev:
        return None
    return (cur / prev - 1) * 100


def evidence(m: dict, f: dict) -> dict:
    return {
        "doc": f["doc_title"], "published_at": f["published_at"], "page": f.get("page"),
        "quote": m.get("quote", ""), "offset": m.get("offset"),
        "offset_kind": f.get("offset_kind", "页内字符偏移"),
        "source": m.get("source", "builtin"), "url": f.get("pdf_url"),
    }


def make_claims(f: dict) -> list[dict]:
    """生成结论。每一条都必须带 evidence，否则不进入列表。"""
    m = f["metrics"]
    claims: list[dict] = []

    def add(cid, ctype, text, value, unit, ev, conf="高", caveat=None):
        if not ev or not ev.get("quote"):
            return  # 无原文 → 不出结论（硬约束）
        claims.append(dict(id=cid, type=ctype, text=text, value=value, unit=unit,
                           evidence=ev, confidence=conf, caveat=caveat))

    rev, npp, npd, ocf = m.get("revenue"), m.get("np_parent"), m.get("np_deduct"), m.get("ocf")

    if rev:
        add("C1", "成长", f"营业收入 {money(rev['value'])}，同比 {pct(rev.get('yoy'))}",
            rev["value"], "元", evidence(rev, f))
    if rev and npp and rev.get("yoy") is not None and npp.get("yoy") is not None:
        diff = npp["yoy"] - rev["yoy"]
        # 差值必须大于 0 才叫"快于"：否则会输出"增速快于营收 -31.94pct"这种自相矛盾的话
        if diff > 0.5:
            rel = f"增速快于营收 {diff:+.2f}pct"
        elif diff < -0.5:
            rel = f"增速低于营收 {abs(diff):.2f}pct"
        else:
            rel = f"增速与营收基本持平（差值 {diff:+.2f}pct）"
        add("C2", "成长", f"归母净利润 {money(npp['value'])}，同比 {pct(npp['yoy'])}，{rel}",
            npp["value"], "元", evidence(npp, f))
    if npp and npd and npp["value"]:
        ratio = npd["value"] / npp["value"] * 100
        # 定性措辞必须由比例决定，不能写死
        judge = ("主业盈利与表观利润接近" if ratio >= 90 else
                 "主业盈利占比有所下降" if ratio >= 70 else
                 "表观利润对非经常性损益依赖较高")
        add("C3", "质量", f"扣非净利润 {money(npd['value'])}，占归母净利润 "
                          f"{ratio:.2f}%，{judge}",
            npd["value"], "元", evidence(npd, f), conf="高" if ratio >= 90 else "中")
    if npp and m.get("nre") and npp["value"]:
        nre = m["nre"]
        share = nre["value"] / npp["value"] * 100
        if nre["value"] >= 0:
            judge = (f"占归母净利润 {share:.2f}%，利润质量不依赖一次性收益" if abs(share) < 5
                     else f"占归母净利润 {share:.2f}%，占比较高，需结合一次性收益的可持续性看")
            head = "非经常性损益"
        else:
            judge = f"为净损失，相当于归母净利润的 {abs(share):.2f}%，表观利润低于主业盈利"
            head = "非经常性损益"
        add("C3b", "质量", f"{head} {money(nre['value'])}，{judge}",
            nre["value"], "元", evidence(nre, f), conf="中" if nre["value"] >= 0 else "高")
    if ocf and npp and npp["value"]:
        mult = ocf["value"] / npp["value"]
        # 负数不能直接说"为…的 -1.46 倍"
        mult_txt = (f"净流出额相当于归母净利润的 {abs(mult):.2f} 倍" if mult < 0
                    else f"为归母净利润的 {mult:.2f} 倍")
        add("C4", "现金流", f"经营活动现金流净额 {money(ocf['value'])}，同比 {pct(ocf.get('yoy'))}，{mult_txt}",
            ocf["value"], "元", evidence(ocf, f),
            caveat="现金流为负不等于经营恶化，需结合备货与账期看（见风险雷达 R1/R2）")
    for key, cid, name in (("inventory", "C5", "存货"), ("ar", "C5b", "应收账款")):
        it = m.get(key)
        if it and it.get("prev"):
            add(cid, "资产结构", f"{name} {money(it['value'])}，较上年末 "
                                f"{pct(growth(it['value'], it['prev']))}",
                it["value"], "元", evidence(it, f))
    eq = m.get("equity")
    if eq and eq.get("prev"):
        add("C6", "资产结构", f"归母净资产 {money(eq['value'])}，较上年末 "
                              f"{pct(growth(eq['value'], eq['prev']))}",
            eq["value"], "元", evidence(eq, f))
    rd = m.get("rd_exp")
    if rd and rev and rd.get("prev") and rev.get("prev"):
        r_now = rd["value"] / rev["value"] * 100
        r_prev = rd["prev"] / rev["prev"] * 100
        add("C7", "投入", f"研发费用 {money(rd['value'])}，同比 {pct(growth(rd['value'], rd['prev']))}；"
                          f"研发费用率 {r_now:.2f}%，同比 {r_now - r_prev:+.2f}pct",
            rd["value"], "元", evidence(rd, f), conf="中",
            caveat="费用率同时受分子与分母影响，收入高增时会自然摊薄，不能直接读作投入收缩")
    return claims


def attribution(f: dict) -> dict:
    """利润归因：只用两期原值，不用同比反推基期（实测反推会引入数万元误差）。"""
    m = f["metrics"]
    rev, npp = m.get("revenue"), m.get("np_parent")
    out = {"ok": False, "contributors": [], "covered_ratio": None, "uncovered": []}
    if not (rev and npp and rev.get("prev") and npp.get("prev")):
        missing = [n for n, x in (("营收上年同期", rev), ("归母上年同期", npp)) if not (x and x.get("prev"))]
        out["reason"] = ("缺少两期原值（" + "、".join(missing) + "），无法计算归因。"
                         "本产品不使用「本期÷(1+同比)」反推基期——实测该做法会引入数万元级误差。")
        return out

    gap = (npp.get("yoy") or growth(npp["value"], npp["prev"])) - (rev.get("yoy") or growth(rev["value"], rev["prev"]))
    inc = npp["value"] - npp["prev"]
    exp_keys = [k for k in ("rd_exp", "sell_exp", "admin_exp")
                if m.get(k) and m[k].get("prev") is not None and m[k]["prev"]]
    covered, contributors, uncovered = 0.0, [], []

    if len(exp_keys) == 3 and inc:
        exp_now = sum(m[k]["value"] for k in exp_keys)
        exp_prev = sum(m[k]["prev"] for k in exp_keys)
        r_now, r_prev = exp_now / rev["value"] * 100, exp_prev / rev["prev"] * 100
        delta = r_now - r_prev
        gain = -delta / 100 * rev["prev"]      # 费用率下降 → 利润增加
        share = gain / inc * 100
        covered += share
        contributors.append({
            "factor": "三费+研发费用率变动", "contribution_pct": share, "basis": "computed",
            "detail": f"费用率 {r_prev:.2f}% → {r_now:.2f}%（{delta:+.2f}pct），"
                      f"对应利润影响 {money(gain)} ÷ 利润增量 {money(inc)}",
        })
    else:
        uncovered.append("三费+研发费用率变动")
        contributors.append({
            "factor": "三费+研发费用率变动", "contribution_pct": None, "basis": "uncovered",
            "detail": "销售/管理/研发费用中至少一项缺少上年同期，算不出费用率变动比例",
        })

    # 非经常性损益必须用「变动额 ÷ 利润增量」。用存量（本期金额）相除在数学上不成立：
    # 实测同一份数据能算出 500% 以上的"贡献"，纯属口径错误。
    nre = m.get("nre")
    delta_nre = None
    if nre:
        if nre.get("prev") is not None:
            delta_nre = nre["value"] - nre["prev"]
        elif m.get("np_parent") and m.get("np_deduct") \
                and m["np_parent"].get("prev") and m["np_deduct"].get("prev"):
            # 归母 − 扣非 = 非经常性损益，两期都能算出变动额，不需要额外数据
            delta_nre = ((m["np_parent"]["value"] - m["np_deduct"]["value"])
                         - (m["np_parent"]["prev"] - m["np_deduct"]["prev"]))
    if delta_nre is not None and inc:
        share = delta_nre / inc * 100
        covered += share
        contributors.append({
            "factor": "非经常性损益变动", "contribution_pct": share, "basis": "computed",
            "detail": f"非经常性损益变动 {money(delta_nre)} ÷ 利润增量 {money(inc)}",
        })
    else:
        uncovered.append("非经常性损益变动")
        contributors.append({
            "factor": "非经常性损益变动", "contribution_pct": None, "basis": "uncovered",
            "detail": "缺少上年同期非经常性损益（或归母/扣非的上年同期），不出比例",
        })
    contributors.append({"factor": "毛利率变动", "contribution_pct": None, "basis": "uncovered",
                         "detail": "需从「营业成本」科目自行核算，当前未建卡 → 不出结论"})
    uncovered.append("毛利率变动")
    raw = covered
    covered = max(0.0, min(100.0, raw))          # 比例必须在 [0,100]，否则界面会出现"未解释 -451%"
    out.update({"ok": True, "gap": gap, "profit_growth": npp.get("yoy"),
                "revenue_growth": rev.get("yoy"), "profit_increment": inc,
                "contributors": contributors, "covered_ratio": covered,
                "raw_covered_ratio": round(raw, 2), "clamped": abs(raw - covered) > 0.05,
                "uncovered": uncovered})
    return out


def risk_radar(f: dict) -> list[dict]:
    """7 条规则。只输出信号与「需人工复核」，不输出风险等级/分数。"""
    m, ex, rows = f["metrics"], f.get("explanations", {}), []

    def push(rid, name, formula, threshold, observed, status, hit, note=None, ev_key=None):
        rows.append({"id": rid, "name": name, "formula": formula, "threshold": threshold,
                     "observed": observed, "status": status, "hit": hit,
                     # explain 只放公司自述原因，note 放规则口径提示，两者不能混
                     "explain": ex.get(rid), "note": note,
                     "evidence": evidence(m[ev_key], f) if ev_key and m.get(ev_key) else None})

    inv, rev = m.get("inventory"), m.get("revenue")
    if inv and inv.get("prev") and rev and rev.get("yoy") is not None:
        g = growth(inv["value"], inv["prev"])
        d = g - rev["yoy"]
        push("R1", "存货增速显著高于营收增速", "(存货期末/期初−1) − 营收同比", f"> {TH['R1']:.0f}pct",
             f"存货 {g:+.2f}%，营收 {rev['yoy']:+.2f}%，差值 {d:+.2f}pct",
             "HIT" if d > TH["R1"] else "NOT_HIT", d > TH["R1"],
             note="季报基期与营收同比不完全对齐，仅作提示", ev_key="inventory")
    else:
        push("R1", "存货增速显著高于营收增速", "(存货期末/期初−1) − 营收同比", f"> {TH['R1']:.0f}pct",
             "存货或营收数据未取得", "INSUFFICIENT", False,
             note="缺少营收同比或存货期初值，无法判断")

    ar = m.get("ar")
    if ar and ar.get("prev") and rev and rev.get("yoy") is not None:
        g = growth(ar["value"], ar["prev"])
        d = g - rev["yoy"]
        push("R2", "应收增速显著高于营收增速", "(应收期末/期初−1) − 营收同比", f"> {TH['R2']:.0f}pct",
             f"应收 {g:+.2f}%，营收 {rev['yoy']:+.2f}%，差值 {d:+.2f}pct",
             "HIT" if d > TH["R2"] else "NOT_HIT", d > TH["R2"],
             note="季报基期与营收同比不完全对齐，仅作提示", ev_key="ar")
    else:
        push("R2", "应收增速显著高于营收增速", "(应收期末/期初−1) − 营收同比", f"> {TH['R2']:.0f}pct",
             "应收或营收数据未取得", "INSUFFICIENT", False)

    ocf, npp = m.get("ocf"), m.get("np_parent")
    if ocf and npp and npp["value"]:
        r = ocf["value"] / npp["value"]
        push("R3", "净利润未转化为现金", "经营现金流净额 ÷ 归母净利润", f"< {TH['R3']}",
             f"{money(ocf['value'])} ÷ {money(npp['value'])} = {r:.2f}",
             "HIT" if r < TH["R3"] else "NOT_HIT", r < TH["R3"], ev_key="ocf")
    else:
        push("R3", "净利润未转化为现金", "经营现金流净额 ÷ 归母净利润", f"< {TH['R3']}",
             "现金流或归母净利润未取得", "INSUFFICIENT", False)

    npd = m.get("np_deduct")
    if npd and npp and npp["value"]:
        r = npd["value"] / npp["value"]
        push("R4", "利润依赖非经常性损益", "扣非净利润 ÷ 归母净利润", f"< {TH['R4']}",
             f"{money(npd['value'])} ÷ {money(npp['value'])} = {r:.4f}",
             "HIT" if r < TH["R4"] else "NOT_HIT", r < TH["R4"], ev_key="np_deduct")
    else:
        push("R4", "利润依赖非经常性损益", "扣非净利润 ÷ 归母净利润", f"< {TH['R4']}",
             "扣非净利润未取得", "INSUFFICIENT", False)

    gw, eq = m.get("goodwill"), m.get("equity")
    if gw and eq and eq["value"]:
        r = gw["value"] / eq["value"] * 100
        push("R5", "商誉占净资产过高", "商誉 ÷ 归母净资产", f"> {TH['R5']:.0f}%",
             f"{money(gw['value'])} ÷ {money(eq['value'])} = {r:.3f}%",
             "HIT" if r > TH["R5"] else "NOT_HIT", r > TH["R5"], ev_key="goodwill")
    else:
        push("R5", "商誉占净资产过高", "商誉 ÷ 归母净资产", f"> {TH['R5']:.0f}%",
             "商誉或净资产未取得", "INSUFFICIENT", False)

    stl, cash = m.get("st_debt"), m.get("cash")
    if stl and cash and cash["value"]:
        r = stl["value"] / cash["value"]
        push("R6", "短期偿债压力", "短期借款 ÷ 货币资金", f"> {TH['R6']}",
             f"{money(stl['value'])} ÷ {money(cash['value'])} = {r:.3f}",
             "HIT" if r > TH["R6"] else "NOT_HIT", r > TH["R6"], ev_key="st_debt")
    else:
        push("R6", "短期偿债压力", "短期借款 ÷ 货币资金", f"> {TH['R6']}",
             "短期借款为 0 或未披露", "INSUFFICIENT", False)

    rd = m.get("rd_exp")
    if rd and rd.get("prev") and rev and rev.get("prev"):
        r_now = rd["value"] / rev["value"] * 100
        r_prev = rd["prev"] / rev["prev"] * 100
        d = r_now - r_prev
        hit = d < TH["R7"]
        # 营收高增会稀释费用率：规则字面成立但含义相反（研发绝对额可能在大幅增加），降级为提示
        downgraded = bool(hit and rev.get("yoy") and rev["yoy"] > 40)
        note = None
        if downgraded:
            note = (f"营收同比 {rev['yoy']:+.2f}% 稀释了费用率，而研发费用绝对额同比 "
                    f"{growth(rd['value'], rd['prev']):+.2f}% —— 规则字面成立但含义相反，已降级为提示")
        push("R7", "研发投入强度下降", "研发费用率(本期 − 上年同期)", f"< {TH['R7']:.1f}pct",
             f"{r_now:.2f}% vs {r_prev:.2f}%，变动 {d:+.2f}pct",
             "NOTED" if downgraded else ("HIT" if hit else "NOT_HIT"), hit, note=note, ev_key="rd_exp")
    else:
        push("R7", "研发投入强度下降", "研发费用率(本期 − 上年同期)", f"< {TH['R7']:.1f}pct",
             "研发费用或营收两期值不全", "INSUFFICIENT", False)
    return rows


# ===========================================================================
# §4 LLM 层（可选；失败可降级，输出过数字白名单）
# ===========================================================================

SYSTEM_PROMPT = """你是 FinLens 的财报分析助手。唯一任务是把给定的结构化事实改写成通顺的中文。
硬约束（违反即视为失败）：
1. 只能使用输入材料中的事实与数字，禁止补充外部信息。
2. 禁止生成输入材料中没有的数字。
3. 禁止投资建议类表述（买入/卖出/增持/减持/目标价/评级/建议关注等）。
4. 禁止预测股价或下一期业绩。5. 禁止对财务合规性下结论（不得说造假/舞弊/虚增）。
6. 只输出正文，不要标题、不要 markdown、不要解释你在做什么。"""


def cfg_from(body: dict) -> dict:
    key = (body.get("api_key") or "").strip()
    return {"enabled": bool(key), "api_key": key,
            "base_url": (body.get("base_url") or "https://api.deepseek.com").strip().rstrip("/"),
            "model": (body.get("model") or "deepseek-chat").strip(), "timeout": 20}


def llm_chat(cfg: dict, system: str, user: str, max_tokens: int = 400) -> tuple[str | None, str | None]:
    """返回 (内容, 错误信息)。错误信息给用户看，不抛异常。"""
    payload = json.dumps({"model": cfg["model"], "temperature": 0.2, "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 20)) as resp:
            data = json.loads(resp.read().decode())
        return (data["choices"][0]["message"]["content"] or "").strip(), None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return None, "API Key 无效或已过期（401），已回退模板结论"
        if exc.code == 429:
            return None, "触发限流（429），已回退模板结论"
        return None, f"模型服务返回 {exc.code}，已回退模板结论"
    except socket.timeout:
        return None, "请求超时，已回退模板结论"
    except urllib.error.URLError as exc:
        reason = "无法连接模型服务，请检查网络或 base_url" if "resolve" in str(exc) or "refused" in str(exc) else f"网络错误：{exc.reason}"
        return None, reason + "，已回退模板结论"
    except Exception as exc:  # 响应结构异常
        return None, f"调用失败：{type(exc).__name__}，已回退模板结论"


def _numbers_in(text: str) -> list[str]:
    """先归一化千分位再取数字：1,253.61 与 1253.61 必须被当成同一个数。"""
    t = re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    return re.findall(r"\d+(?:\.\d+)?", t)


def allowed_numbers(f: dict, *materials: str) -> set[str]:
    """白名单 = 事实集里的原值/亿/万缩放 + 输入材料中已出现的数字（含四舍五入变体）。"""
    allowed: set[str] = set()

    def put(x) -> None:
        if not isinstance(x, (int, float)) or x != x:
            return
        for y in (x, abs(x), x / 1e8, abs(x) / 1e8, x / 1e4, abs(x) / 1e4):
            for spec in ("{:.2f}", "{:.1f}", "{:.0f}"):
                s = spec.format(y)
                allowed.add(s)
                allowed.add(s.rstrip("0").rstrip("."))

    for m in f["metrics"].values():
        for k in ("value", "prev", "yoy"):
            put(m.get(k))
    for text in materials:
        for tok in _numbers_in(text):
            allowed.add(tok)
            try:
                allowed.add(f"{float(tok):.1f}")   # 允许模型做一位小数取整
            except ValueError:
                pass
    return {a for a in allowed if a}


def guard_numbers(text: str, f: dict, *materials: str) -> list[str]:
    """返回越界数字列表；空列表 = 通过。materials 是允许引用的输入材料，不含待检文本本身。"""
    allowed = allowed_numbers(f, *materials)
    banned = []
    for token in _numbers_in(text):
        if token in ("2024", "2025", "2026", "2027"):   # 年份免检
            continue
        if token in allowed:
            continue
        if token.endswith(".0") and token[:-2] in allowed:
            continue
        banned.append(token)
    return banned


def guard_compliance(text: str) -> tuple[str, list[str]]:
    hits = [w for w in ADVICE_WORDS if w in text]
    for w in hits:
        text = text.replace(w, "〔已按合规规则删除〕")
    return text, hits


def polish_with_llm(text: str, f: dict, cfg: dict) -> dict:
    if not cfg.get("enabled"):
        return {"text": text, "mode": "TEMPLATE", "warn": None}
    out, err = llm_chat(cfg, SYSTEM_PROMPT, f"请改写下面这段结论，保持数字完全一致：\n\n{text}", 320)
    if err or not out:
        return {"text": text, "mode": "TEMPLATE", "warn": err}
    banned = guard_numbers(out, f, text)   # 允许引用的只有"待改写原文"里已有的数字
    if banned:
        return {"text": text, "mode": "REJECTED",
                "warn": f"AI 表述出现无出处数字 {banned[:3]}，已丢弃并改用模板结论"}
    out, hits = guard_compliance(out)
    if hits:
        return {"text": out, "mode": "REWRITTEN", "warn": f"已按合规规则改写：{hits}"}
    return {"text": out, "mode": "LLM", "warn": None}


# ===========================================================================
# §5 检索与问答（轻量 RAG，不依赖向量库）
# ===========================================================================

def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i: i + 2] for i in range(len(s) - 1)}


def build_corpus(f: dict) -> list[dict]:
    """语料 = 各指标原文引文 + 公司自述原因（无上传也能问答）。"""
    chunks = []
    for key, m in f["metrics"].items():
        if m.get("quote"):
            chunks.append({"id": f"m-{key}", "text": f"{m['label']}：{m['quote']}",
                           "source": f"{f['doc_title']} · {m['label']}"})
    for rid, txt in f.get("explanations", {}).items():
        chunks.append({"id": f"ex-{rid}", "text": f"{rid} 相关变动原因：{txt}",
                       "source": f"{f['doc_title']} · 变动原因"})
    return chunks


def retrieve(query: str, chunks: list[dict], top_k: int = 3) -> list[dict]:
    q = _bigrams(query)
    if not q:
        return []
    scored = []
    for c in chunks:
        cg = _bigrams(c["text"])
        if cg:
            scored.append({**c, "score": len(q & cg) / max(1, len(q))})
    scored.sort(key=lambda x: -x["score"])
    return [c for c in scored[:top_k] if c["score"] > 0]


def _cite(f: dict, keys: list[str]) -> list[dict]:
    out = []
    for k in keys:
        m = f["metrics"].get(k)
        if m and m.get("quote"):
            out.append({"id": f"m-{k}", "text": f"{m['label']}：{m['quote']}",
                        "source": f"{f['doc_title']} · {m['label']}", "score": 1.0})
    return out


def _structured_answer(q: str, f: dict) -> dict | None:
    """常见问题用确定性计算直接回答，比检索更准、更可核。"""
    m = f["metrics"]
    if ("现金流" in q or "现金" in q) and ("净利" in q or "利润" in q or "差" in q):
        ocf, npp = m.get("ocf"), m.get("np_parent")
        if ocf and npp and npp["value"]:
            r = ocf["value"] / npp["value"]
            if r < 0:
                lead = (f"经营活动现金流净额 {money(ocf['value'])}，归母净利润 {money(npp['value'])}——"
                        f"现金流为净流出，净流出额相当于净利润的 {abs(r):.2f} 倍。[1][2] ")
                # 因果句只在公司自述了存货/应收变动原因时才补，否则不替公司解释
                if f.get("explanations", {}).get("R1") or f.get("explanations", {}).get("R2"):
                    tail = "净利润为正而经营现金流为负，差额的去向可对照下方引文里存货与应收的变动原因。"
                else:
                    tail = "净利润为正而经营现金流为负，说明本期利润尚未转化为现金；差额去向需结合存货与应收科目自行核对。"
            else:
                lead = (f"经营活动现金流净额 {money(ocf['value'])}，归母净利润 {money(npp['value'])}，"
                        f"前者是后者的 {r:.2f} 倍。[1][2] ")
                tail = "经营现金流不低于净利润，说明本期利润已基本转化为现金。"
            return {"ok": True, "mode": "STRUCTURED", "warn": None,
                    "answer": lead + tail,
                    "citations": _cite(f, ["ocf", "np_parent"])}
    for key, kw, rid in (("inventory", "存货", "R1"), ("ar", "应收", "R2")):
        if kw in q:
            it = m.get(key)
            if it:
                g = growth(it["value"], it["prev"]) if it.get("prev") else None
                ans = f"{it['label']} {money(it['value'])}"
                if g is not None:
                    ans += f"，较上年末 {g:+.2f}%"
                cites = _cite(f, [key])
                ex = f.get("explanations", {}).get(rid)
                if ex:
                    ans += f"。[1] 公司自述原因：{ex}。[2]"
                    cites.append({"id": f"ex-{rid}", "text": f"变动原因：{ex}",
                                  "source": f"{f['doc_title']} · 变动原因", "score": 1.0})
                else:
                    ans += "。[1]"
                return {"ok": True, "mode": "STRUCTURED", "warn": None, "answer": ans, "citations": cites}
    if "研发" in q:
        rd, rev = m.get("rd_exp"), m.get("revenue")
        if rd:
            ans = f"研发费用 {money(rd['value'])}"
            if rd.get("prev"):
                ans += f"，同比 {growth(rd['value'], rd['prev']):+.2f}%"
            if rev and rev.get("prev"):
                r_now, r_prev = rd["value"] / rev["value"] * 100, rd["prev"] / rev["prev"] * 100
                ans += f"；研发费用率 {r_now:.2f}%，同比 {r_now - r_prev:+.2f}pct"
            return {"ok": True, "mode": "STRUCTURED", "warn": None,
                    "answer": ans + "。[1] 费用率同时受分子分母影响，收入高增时会自然摊薄，不宜直接读作投入收缩。",
                    "citations": _cite(f, ["rd_exp", "revenue"])}
    if any(k in q for k in ("营收", "收入", "营业额")):
        rev = m.get("revenue")
        if rev:
            return {"ok": True, "mode": "STRUCTURED", "warn": None,
                    "answer": f"营业收入 {money(rev['value'])}，同比 {pct(rev.get('yoy'))}。[1]",
                    "citations": _cite(f, ["revenue"])}
    return None


def _extractive(q: str, top: list[dict]) -> str:
    qg = _bigrams(q)
    sents = [s.strip() for s in re.split(r"[。；\n]", top[0]["text"]) if len(s.strip()) > 6]
    if not sents:
        return top[0]["text"][:300]
    sents.sort(key=lambda s: -len(qg & _bigrams(s)))
    picked = [s for s in sents[:2] if qg & _bigrams(s)] or sents[:1]
    return "。".join(picked) + "。[1]"


REFUSE_SUGGESTIONS = ["改问：存货较上年末变化多少、公司怎么解释", "改问：经营现金流与归母净利润的差额",
                      "改问：研发费用同比与研发费用率变化"]

ADVICE_Q = ["买入", "卖出", "增持", "减持", "清仓", "加仓", "减仓", "目标价", "评级",
            "推荐", "抄底", "上车", "能买", "该不该", "会涨", "能涨", "止损", "仓位"]


def answer_question(q: str, f: dict, cfg: dict) -> dict:
    hit = [w for w in ADVICE_Q if w in q]
    if hit:
        return {"ok": False, "reason": "COMPLIANCE", "citations": [], "suggestions": REFUSE_SUGGESTIONS,
                "message": f"该提问涉及投资建议或涨跌判断（命中：{hit}），本产品不提供此类内容。"}
    structured = _structured_answer(q, f)
    if structured:
        return structured
    top = retrieve(q, build_corpus(f), 3)
    if not top or top[0]["score"] < 0.18:
        return {"ok": False, "reason": "NO_DISCLOSURE_FOUND", "citations": [],
                "suggestions": REFUSE_SUGGESTIONS,
                "message": "未在披露文件中找到对应信息。本产品只回答这份财报里能核到原文的问题。"}
    if cfg.get("enabled"):
        prompt = (f"仅根据以下材料回答问题，每句结论后用 [编号] 标注来源；材料里没有的信息直接说"
                  f"「披露文件中未提及」。\n\n材料：\n"
                  + "\n\n".join(f"[{i + 1}] {c['text']}" for i, c in enumerate(top))
                  + f"\n\n问题：{q}")
        out, err = llm_chat(cfg, SYSTEM_PROMPT, prompt, 400)
        if err or not out:
            return {"ok": True, "answer": _extractive(q, top), "citations": top, "mode": "EXTRACTIVE", "warn": err}
        banned = guard_numbers(out, f, "\n".join(c["text"] for c in top))
        if banned:
            return {"ok": True, "answer": _extractive(q, top), "citations": top, "mode": "EXTRACTIVE",
                    "warn": f"AI 答案出现无出处数字 {banned[:3]}，已改用原文摘录"}
        out, hits = guard_compliance(out)
        return {"ok": True, "answer": out, "citations": top, "mode": "LLM",
                "warn": f"已按合规规则改写：{hits}" if hits else None}
    return {"ok": True, "answer": _extractive(q, top), "citations": top, "mode": "EXTRACTIVE", "warn": None}


# ===========================================================================
# §6 组装与导出
# ===========================================================================

def payload(f: dict, claims: list, attr: dict, radar: list, as_of: date) -> dict:
    present = [k for k in CORE if k in f["metrics"]]
    metrics = {}
    for k, m in f["metrics"].items():
        metrics[k] = {**m, "display": {"value": money(m.get("value")), "prev": money(m.get("prev")),
                                       "yoy": pct(m.get("yoy"))}}
    return {
        "meta": {"code": f["code"], "name": f["name"], "period": f["period"],
                 "doc_title": f["doc_title"], "published_at": f["published_at"],
                 "pdf_url": f["pdf_url"], "page": f.get("page"),
                 "offset_kind": f.get("offset_kind", "页内字符偏移"), "as_of": as_of.isoformat(),
                 "source": "uploaded" if f["uploaded_count"] else "builtin",
                 "uploaded_count": f["uploaded_count"], "mixed": f.get("mixed", False),
                 "carried_over": f.get("carried_over", []),
                 "subject_unverified": f.get("subject_unverified", False),
                 "disclosure_unknown": f.get("disclosure_unknown", False),
                 "coverage": {"present": present, "missing": [k for k in CORE if k not in f["metrics"]],
                              "ratio": round(len(present) / len(CORE) * 100, 1)}},
        "metrics": metrics, "claims": claims, "attribution": attr, "radar": radar,
        "disclaimer": DISCLAIMER,
    }


def build_report(f: dict, claims: list, attr: dict, radar: list) -> str:
    source = f"{f['doc_title']} {f['pdf_url']}" if f.get("pdf_url") else \
        f"{f['doc_title']}（上传件，无外部链接，字符偏移基于上传文本全文）"
    lines = [f"# {f['name']}（{f['code']}）财报速读", f"- 报告期：{f['period']}",
             f"- 披露日：{f['published_at']}", f"- 来源：{source}",
             f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", "", "## 结论"]
    for c in claims:
        ev = c["evidence"]
        loc = f"第 {ev['page']} 页 · " if ev.get("page") else ""
        lines += [f"- [{c['confidence']}] {c['text']}",
                  f"  - 出处：{ev['doc']} · {loc}{ev.get('offset_kind', '页内字符偏移')} {ev.get('offset')}",
                  f"  - 原文：{ev['quote'][:90]}".replace("\n", " ")]
        if c.get("caveat"):
            lines.append(f"  - 口径提示：{c['caveat']}")
    if attr.get("ok"):
        lines += ["", "## 利润归因",
                  f"- 营收增速 {attr['revenue_growth']:+.2f}%，归母增速 {attr['profit_growth']:+.2f}%，"
                  f"增速差 {attr['gap']:+.2f}pct"]
        for c in attr["contributors"]:
            v = "未覆盖" if c["contribution_pct"] is None else f"{c['contribution_pct']:+.1f}%"
            lines.append(f"- {c['factor']}：{v}（{c['detail']}）")
        lines.append(f"- 已解释比例：{attr['covered_ratio']:.1f}%（其余明确列为未覆盖）")
    else:
        lines += ["", "## 利润归因", f"- 无法计算：{attr.get('reason')}"]
    lines += ["", "## 风险信号（仅提示复核，非结论）"]
    for r in radar:
        lines.append(f"- {r['id']} {r['name']}：{r['status']}　观测值 {r['observed']}")
        if r.get("explain"):
            lines.append(f"  - 公司自述：{r['explain']}")
        if r.get("note"):
            lines.append(f"  - 规则提示：{r['note']}")
    lines += ["", "---", DISCLAIMER]
    return "\n".join(lines)


# ===========================================================================
# §7 HTTP 层（标准库，零依赖）
# ===========================================================================

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def parse_date(s: str | None) -> date:
    try:
        return date.fromisoformat(s) if s else date.today()
    except ValueError:
        raise ApiError(400, "BAD_DATE", f"日期格式应为 YYYY-MM-DD，收到：{s}")


def resolve_facts(code: str | None, as_of_s: str | None, uploaded: dict | None = None,
                  uploaded_meta: dict | None = None) -> tuple[dict, date]:
    """所有接口共用的取数与可用性校验入口：四个接口的拦截规则必须一致。"""
    if not code:
        raise ApiError(400, "MISSING_CODE", f"缺少 code 参数。可用：{', '.join(COMPANIES)}")
    if code not in COMPANIES:
        raise ApiError(404, "UNKNOWN_CODE", f"未收录该代码：{code}。可用：{', '.join(COMPANIES)}")
    as_of = parse_date(as_of_s)
    if as_of > date.today():
        raise ApiError(400, "FUTURE_DATE",
                       f"稿件日期 {as_of.isoformat()} 晚于今天 {date.today().isoformat()}，"
                       f"不能用尚未发生的日期分析。")
    f = build_facts(code, uploaded, as_of, uploaded_meta)
    # 硬约束：稿件日期早于披露日 → 使用了当时还不存在的信息，直接拦截，不生成结论
    if not uploaded and as_of.isoformat() < f["published_at"]:
        raise ApiError(409, "FUTURE_INFO",
                       f"稿件日期 {as_of.isoformat()} 早于该报告披露日 {f['published_at']}，"
                       f"此时引用报告数据属于使用未来信息，已拦截。")
    return f, as_of


def do_analyze(code: str | None, as_of_s: str | None, uploaded: dict | None = None,
               uploaded_meta: dict | None = None) -> dict:
    f, as_of = resolve_facts(code, as_of_s, uploaded, uploaded_meta)
    return payload(f, make_claims(f), attribution(f), risk_radar(f), as_of)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音逐请求日志
        pass

    # ---- 工具 ----
    def send(self, status: int, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self, raw: bytes) -> dict:
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")

    def guard(self, fn):
        """统一异常出口：任何失败都返回结构化错误码，不静默、不返回 200 正常结论。"""
        try:
            return fn()
        except ApiError as exc:
            return self.send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except PDFParseError as exc:
            return self.send(422, {"error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:
            return self.send(500, {"error": {"code": "INTERNAL", "message": f"服务异常：{type(exc).__name__}"}})

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path

        if path == "/api/health":
            return self.send(200, {"ok": True, "companies": list(COMPANIES), "pdf_upload": HAS_PYPDF,
                                   "llm": "optional", "built_at": "2026-11-09"})
        if path == "/api/companies":
            return self.send(200, {"companies": [
                {"code": c["code"], "name": c["name"], "period": c["period"],
                 "published_at": c["published_at"], "metrics": len(c["metrics"]),
                 "coverage": [k for k in CORE if k in c["metrics"]]} for c in COMPANIES.values()]})
        if path == "/api/analyze":
            return self.guard(lambda: self.send(200, do_analyze(qs.get("code"), qs.get("as_of"))))
        if path == "/api/report":
            def report():
                f, _ = resolve_facts(qs.get("code"), qs.get("as_of"))
                md = build_report(f, make_claims(f), attribution(f), risk_radar(f))
                return self.send(200, md.encode(), "text/markdown; charset=utf-8")
            return self.guard(report)

        # 静态文件（含目录穿越防护）
        rel = "/index.html" if path == "/" else path
        target = (STATIC / rel.lstrip("/")).resolve()
        if not target.is_relative_to(STATIC.resolve()):
            return self.send(403, "forbidden", "text/plain; charset=utf-8")
        if target.is_file():
            return self.send(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))
        return self.send(404, "not found", "text/plain; charset=utf-8")

    def do_HEAD(self):
        """只回响应头，供链接预览、监控探活与部分客户端预检使用。

        HEAD 不执行业务逻辑（避免探活请求产生副作用），浏览器正常访问走 GET。
        没有这个方法时，网关会把 HEAD 请求回成 501，部分预览器会误判链接不可用。
        """
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.head_only(200, "application/json; charset=utf-8", 0)
        target = (STATIC / ("/index.html" if path == "/" else path).lstrip("/")).resolve()
        if not target.is_relative_to(STATIC.resolve()):
            return self.head_only(403, "text/plain; charset=utf-8", 0)
        if target.is_file():
            return self.head_only(200, MIME.get(target.suffix, "application/octet-stream"),
                                  target.stat().st_size)
        return self.head_only(404, "text/plain; charset=utf-8", 0)

    def head_only(self, status: int, ctype: str, length: int):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        # 先按声明长度拦截超大文件，避免把内存打满再报错
        if u.path == "/api/upload" and length > 80 * 1024 * 1024:
            return self.send(413, {"error": {"code": "TOO_LARGE",
                                             "message": "文件超过 80MB，请仅上传所需报告期的那一份。"}})
        raw = self.rfile.read(length) if length else b""

        if u.path == "/api/analyze":
            return self.send(405, {"error": {"code": "METHOD_NOT_ALLOWED",
                                             "message": "分析请使用 GET /api/analyze?code=&as_of="}},
                             "application/json; charset=utf-8")

        if u.path == "/api/upload":
            def upload():
                parsed = parse_pdf(raw)
                metrics = extract_metrics(parsed["text"])
                if not metrics:
                    raise ApiError(422, "NO_METRIC",
                                   "未能从该文件中抽取到可核对指标。可能原因：扫描件、非财报、或版式差异过大。")
                meta = extract_meta(parsed["text"])
                res = do_analyze(qs.get("code"), qs.get("as_of"), metrics, meta)
                res["upload"] = {"pages": parsed["pages"], "chars": len(parsed["text"]),
                                 "extracted": len(metrics), "subject": meta.get("subject"),
                                 "note": "披露日无法从 PDF 元数据可靠取得，已按稿件日期处理并标注未知；"
                                         "未能抽取的科目沿用内置快照，已在 mixed 标记。"}
                return self.send(200, res)
            return self.guard(upload)

        if u.path == "/api/qa":
            def qa():
                body = self.body_json(raw)
                q = (body.get("q") or "").strip()
                if not q:
                    raise ApiError(400, "EMPTY_QUERY", "请输入问题")
                f, _ = resolve_facts(body.get("code"), body.get("as_of"),
                                     body.get("upload_metrics") or None)
                res = answer_question(q, f, cfg_from(body))
                res["disclaimer"] = DISCLAIMER
                return self.send(200, res)
            return self.guard(qa)

        if u.path == "/api/polish":
            def polish():
                body = self.body_json(raw)
                text = (body.get("text") or "").strip()
                if not text:
                    raise ApiError(400, "EMPTY_TEXT", "缺少待改写文本")
                f, _ = resolve_facts(body.get("code"), body.get("as_of"),
                                     body.get("upload_metrics") or None)
                return self.send(200, polish_with_llm(text, f, cfg_from(body)))
            return self.guard(polish)

        return self.send(404, {"error": {"code": "NOT_FOUND", "message": "未知接口"}})


def main() -> None:
    print(f"FinLens listening on 0.0.0.0:{PORT}  (pdf_upload={HAS_PYPDF})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
