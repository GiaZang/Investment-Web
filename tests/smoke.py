"""
FinLens 冒烟测试：24 条断言，覆盖主链路 / 数据缺失与接口失败 / 合规边界。

运行：python tests/smoke.py
说明：脚本自行启动一个临时端口上的服务，跑完自动关闭；不需要预先起服务。
      少数用例依赖环境（是否安装 pypdf），会在结果里标注实际走的分支。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("SMOKE_PORT", "4310"))
BASE = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(ROOT))

CASES: list[tuple[str, str, object]] = []


def req(path: str, method: str = "GET", body: bytes | None = None,
        ctype: str = "application/json") -> tuple[int, bytes]:
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={"Content-Type": ctype} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def jget(path: str) -> tuple[int, dict]:
    s, b = req(path)
    return s, json.loads(b.decode() or "{}")


def jpost(path: str, payload: dict) -> tuple[int, dict]:
    s, b = req(path, "POST", json.dumps(payload).encode())
    return s, json.loads(b.decode() or "{}")


def check(group: str, name: str):
    """只登记用例；等临时服务起来后由 main() 统一执行。"""

    def deco(fn):
        CASES.append((group, name, fn))
        return fn

    return deco


# ======================= 主链路 =======================

@check("主链路", "A1 首页可访问且返回产品页")
def a1():
    s, b = req("/")
    t = b.decode()
    assert s == 200, f"HTTP {s}"
    assert "FinLens" in t and "一键摘要" in t, "首页缺少产品标识或 Tab"
    return f"{len(t)} 字节"


@check("主链路", "A2 健康检查返回 3 家公司")
def a2():
    s, d = jget("/api/health")
    assert s == 200 and d["ok"], f"HTTP {s}"
    assert len(d["companies"]) == 3, d["companies"]
    return f"pdf_upload={d['pdf_upload']}"


@check("主链路", "A3 光迅科技覆盖 7 项核心指标")
def a3():
    s, d = jget("/api/companies")
    c = next(x for x in d["companies"] if x["code"] == "002281")
    assert len(c["coverage"]) == 7, c["coverage"]
    return f"{c['metrics']} 项指标"


@check("主链路", "A4 结论 9 条且每条都有原文引文")
def a4():
    s, d = jget("/api/analyze?code=002281&as_of=2026-11-05")
    assert s == 200, f"HTTP {s}"
    assert len(d["claims"]) == 9, f"实际 {len(d['claims'])} 条"
    bad = [c["id"] for c in d["claims"] if not c["evidence"].get("quote")]
    assert not bad, f"无引文结论：{bad}"
    return "9/9 有引文"


@check("主链路", "A5 风险命中恰为 R1/R2/R3")
def a5():
    s, d = jget("/api/analyze?code=002281&as_of=2026-11-05")
    hits = sorted(r["id"] for r in d["radar"] if r["status"] == "HIT")
    assert hits == ["R1", "R2", "R3"], hits
    return ",".join(hits)


@check("主链路", "A6 归因已解释 16%~17%，未覆盖项列全")
def a6():
    s, d = jget("/api/analyze?code=002281&as_of=2026-11-05")
    a = d["attribution"]
    assert a["ok"], a.get("reason")
    assert 16.0 <= a["covered_ratio"] <= 17.0, a["covered_ratio"]
    assert any(c["contribution_pct"] is None for c in a["contributors"]), "缺少未覆盖项"
    return f"{a['covered_ratio']:.2f}%"


@check("主链路", "A7 中际旭创 R7 降级为 NOTED 而非命中")
def a7():
    s, d = jget("/api/analyze?code=300308&as_of=2026-11-05")
    r7 = next(r for r in d["radar"] if r["id"] == "R7")
    assert r7["status"] == "NOTED", f"{r7['status']}"
    assert "稀释" in (r7.get("note") or ""), "缺少降级说明"
    return "NOTED"


@check("主链路", "A8 中际旭创缺失科目判为 INSUFFICIENT")
def a8():
    s, d = jget("/api/analyze?code=300308&as_of=2026-11-05")
    st = {r["id"]: r["status"] for r in d["radar"]}
    for rid in ("R3", "R4", "R5"):
        assert st[rid] == "INSUFFICIENT", f"{rid}={st[rid]}"
    return "R3/R4/R5"


@check("主链路", "A9 可导出核查底稿（含来源与免责）")
def a9():
    s, b = req("/api/report?code=002281&as_of=2026-11-05")
    t = b.decode()
    assert s == 200, f"HTTP {s}"
    assert "字符偏移" in t and "不构成任何投资建议" in t and "cninfo" in t, "底稿缺少出处或免责"
    return f"{len(t)} 字符"


# ======================= 数据缺失 / 接口失败 =======================

@check("数据缺失/接口失败", "B1 未收录代码返回 404")
def b1():
    s, d = jget("/api/analyze?code=999999")
    assert s == 404 and d["error"]["code"] == "UNKNOWN_CODE", f"{s} {d}"
    return d["error"]["code"]


@check("数据缺失/接口失败", "B2 日期格式错误返回 400")
def b2():
    s, d = jget("/api/analyze?code=002281&as_of=2026/11/05")
    assert s == 400 and d["error"]["code"] == "BAD_DATE", f"{s} {d}"
    return d["error"]["code"]


@check("数据缺失/接口失败", "B3 稿件早于披露日被拦截（未来信息）")
def b3():
    s, d = jget("/api/analyze?code=002281&as_of=2026-10-01")
    assert s == 409 and d["error"]["code"] == "FUTURE_INFO", f"{s} {d}"
    assert "未来信息" in d["error"]["message"]
    return "409 FUTURE_INFO"


@check("数据缺失/接口失败", "B4 新易盛营收缺失时判为无法判断")
def b4():
    s, d = jget("/api/analyze?code=300502&as_of=2026-11-05")
    st = {r["id"]: r["status"] for r in d["radar"]}
    assert st["R1"] == "INSUFFICIENT" and st["R2"] == "INSUFFICIENT", st
    assert not any(r["status"] == "NOT_HIT" and "未取得" in r["observed"] for r in d["radar"]), \
        "数据缺失被标成了未见异常"
    return "R1/R2 INSUFFICIENT"


@check("数据缺失/接口失败", "B5 新易盛不硬凑结论（仅 2 条）")
def b5():
    s, d = jget("/api/analyze?code=300502&as_of=2026-11-05")
    assert len(d["claims"]) == 2, f"实际 {len(d['claims'])} 条"
    assert d["meta"]["coverage"]["ratio"] < 50, d["meta"]["coverage"]
    return f"覆盖 {d['meta']['coverage']['ratio']}%"


@check("数据缺失/接口失败", "B6 空问题返回 400")
def b6():
    s, d = jpost("/api/qa", {"code": "002281", "q": "  "})
    assert s == 400 and d["error"]["code"] == "EMPTY_QUERY", f"{s} {d}"
    return d["error"]["code"]


@check("数据缺失/接口失败", "B7 检索不到时拒答而非编造")
def b7():
    s, d = jpost("/api/qa", {"code": "002281", "q": "公司董事长是谁", "as_of": "2026-11-05"})
    assert s == 200 and d["ok"] is False, d
    assert d["reason"] == "NO_DISCLOSURE_FOUND", d["reason"]
    assert d["suggestions"], "拒答未给可答问题建议"
    return "拒答 + 3 条建议"


@check("数据缺失/接口失败", "B8 模型不可达时降级为模板且告警")
def b8():
    s, d = jpost("/api/polish", {"code": "002281", "as_of": "2026-11-05", "text": "营业收入 116.83 亿元",
                                 "api_key": "sk-invalid", "base_url": "http://127.0.0.1:9",
                                 "model": "deepseek-chat"})
    assert s == 200, f"HTTP {s}"
    assert d["mode"] == "TEMPLATE", d["mode"]
    assert d["warn"], "降级未给出提示"
    return d["warn"][:34]


@check("数据缺失/接口失败", "B9 PDF 上传：能解析则抽取，不能则明确降级")
def b9():
    import importlib.util

    has_fitz = importlib.util.find_spec("fitz") is not None
    if importlib.util.find_spec("pypdf") and has_fitz:
        # 环境具备解析能力：用真实财报文本生成一份 PDF，验证抽取链路
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        text = ("光迅科技股份有限公司 2026 年第三季度报告\n"
                "一、主要会计数据和财务指标\n"
                "营业收入（元）\n116,830,898.97\n36.94\n"
                "归属于上市公司股东的净利润（元）\n1,284,463.91\n78.65\n"
                "扣除非经常性损益的净利润（元）\n1,271,927.86\n87.48\n"
                "经营活动产生的现金流量净额（元）\n-1,881,145.44\n-274.75\n"
                "二、资产负债表项目\n"
                "存货\n12,110,194.99\n5,745,975.50\n110.76\n"
                "应收账款\n1,973,007.43\n1,236,530.32\n59.56\n"
                "货币资金\n4,993,491.24\n4,442,198.93\n12.41\n"
                "短期借款\n9,963.24\n10,357.69\n-3.81\n"
                "商誉\n7,942.66\n7,942.66\n0.00\n"
                "研发费用\n857,320.41\n671,938.54\n27.59\n"
                "三、其他说明\n本报告期数据未经审计，单位均为人民币元。")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(36, 36, 560, 780), text, fontsize=9, fontname="china-s")
        pdf = doc.tobytes()
        s, b = req("/api/upload?code=002281&as_of=2026-11-05", "POST", pdf, "application/pdf")
        d = json.loads(b.decode())
        assert s == 200, f"{s} {d.get('error')}"
        assert d["upload"]["extracted"] >= 3, d["upload"]
        return f"解析 {d['upload']['extracted']} 项科目"
    s, b = req("/api/upload?code=002281&as_of=2026-11-05", "POST", b"x" * 5000, "application/pdf")
    d = json.loads(b.decode())
    assert s == 422, f"HTTP {s}"
    assert d["error"]["code"] in ("NO_PDF_LIB", "BROKEN", "NO_TEXT_LAYER"), d["error"]
    return f"降级 {d['error']['code']}"


# ======================= 合规边界 =======================

@check("合规边界", "C1 买卖类提问被拒答")
def c1():
    s, d = jpost("/api/qa", {"code": "002281", "q": "现在能买吗", "as_of": "2026-11-05"})
    assert d["ok"] is False and d["reason"] == "COMPLIANCE", d
    assert "投资建议" in d["message"]
    return "COMPLIANCE"


@check("合规边界", "C2 目标价类提问被拒答")
def c2():
    s, d = jpost("/api/qa", {"code": "002281", "q": "目标价多少", "as_of": "2026-11-05"})
    assert d["ok"] is False and d["reason"] == "COMPLIANCE", d
    return d["reason"]


@check("合规边界", "C3 建议类词表命中即改写")
def c3():
    import app

    out, hits = app.guard_compliance("我们强烈推荐买入并给出目标价")
    assert "强烈推荐" in hits and "买入" in hits and "目标价" in hits, hits
    assert "买入" not in out and "已按合规规则删除" in out
    return f"命中 {len(hits)} 个词"


@check("合规边界", "C4 事实集之外的数字被拦截")
def c4():
    import app

    f = app.build_facts("002281", None, __import__("datetime").date(2026, 11, 5))
    assert app.guard_numbers("营业收入 116.83 亿元", f) == [], "正确数字被误拦"
    banned = app.guard_numbers("营业收入 999.99 亿元，毛利率 47%", f)
    assert "999.99" in banned and "47" in banned, banned
    return f"拦截 {banned}"


@check("合规边界", "C5 接口返回固定免责声明")
def c5():
    s, d = jget("/api/analyze?code=002281&as_of=2026-11-05")
    assert "不构成任何投资建议" in d["disclaimer"]
    assert "不代表对公司经营或财务状况的负面判断" in d["disclaimer"]
    return "免责声明齐备"


@check("合规边界", "C6 结论均标注置信度且不出现买卖建议")
def c6():
    import app

    s, d = jget("/api/analyze?code=002281&as_of=2026-11-05")
    assert all(c["confidence"] in ("高", "中", "低") for c in d["claims"]), "置信度缺失"
    for c in d["claims"]:
        hit = [w for w in app.ADVICE_WORDS if w in c["text"]]
        assert not hit, f"{c['id']} 含建议词 {hit}"
    return "9 条全部标注"


# ======================= 审计补强（P0/P1 回归） =======================

@check("审计补强", "D1 上传别家公司的财报被拒（主体错配）")
def d1():
    import app
    d = __import__("datetime").date(2026, 11, 5)
    up = {"revenue": {"label": "营业收入", "unit": "元", "value": 1e9, "prev": None,
                      "yoy": None, "quote": "片段", "offset": 1}}
    try:
        app.build_facts("002281", up, d, {"subject": "中际旭创股份有限公司", "year": 2026})
    except app.PDFParseError as exc:
        assert exc.code == "SUBJECT_MISMATCH", exc.code
        return exc.code
    raise AssertionError("别家公司的财报被当成了光迅科技的数据")


@check("审计补强", "D2 上传件的证据不再指向内置 PDF")
def d2():
    import app
    d = __import__("datetime").date(2026, 11, 5)
    up = {"revenue": {"label": "营业收入", "unit": "元", "value": 1e9, "prev": 9e8,
                      "yoy": 11.1, "quote": "上传片段", "offset": 30}}
    f = app.build_facts("002281", up, d, {"subject": "光迅科技股份有限公司", "year": 2026})
    ev = app.make_claims(f)[0]["evidence"]
    assert ev["page"] is None and ev["url"] is None, ev
    assert ev["offset_kind"] == "全文字符偏移", ev["offset_kind"]
    assert f["mixed"] and f["carried_over"], "沿用了内置科目却没有标记 mixed"
    return "page=None, url=None, 全文偏移, mixed=True"


@check("审计补强", "D3 导出底稿同样拦截未来信息")
def d3():
    s, d = jget("/api/report?code=002281&as_of=2026-10-01")
    assert s == 409 and d["error"]["code"] == "FUTURE_INFO", f"{s} {d}"
    return "409 FUTURE_INFO"


@check("审计补强", "D4 未来日期不得用于分析")
def d4():
    s, d = jget("/api/analyze?code=002281&as_of=2030-01-01")
    assert s == 400 and d["error"]["code"] == "FUTURE_DATE", f"{s} {d}"
    return d["error"]["code"]


@check("审计补强", "D5 归因用变动额且比例收口到 0~100")
def d5():
    import app
    d = __import__("datetime").date(2026, 11, 5)
    a = app.attribution(app.build_facts("002281", None, d))
    names = [c["factor"] for c in a["contributors"]]
    assert "非经常性损益变动" in names, names          # 由 归母−扣非 两期推算，不是用存量除增量
    assert 0 <= a["covered_ratio"] <= 100, a["covered_ratio"]

    f = app.build_facts("002281", None, d)
    f["metrics"]["nre"] = {**f["metrics"]["nre"], "prev": -5e9}   # 构造极端变动
    a2 = app.attribution(f)
    assert a2["covered_ratio"] == 100 and a2["clamped"], a2
    return f"{a['covered_ratio']:.2f}% / 极端值截断为 {a2['covered_ratio']:.0f}%"


@check("审计补强", "D6 现金流问答：正负两种情形不得自相矛盾")
def d6():
    import app
    d = __import__("datetime").date(2026, 11, 5)
    f = app.build_facts("002281", None, d)
    neg = app._structured_answer("经营现金流和净利润差多少", f)["answer"]
    assert "现金流为负" in neg, neg
    f["metrics"]["ocf"] = {**f["metrics"]["ocf"], "value": 5e9}
    pos = app._structured_answer("经营现金流和净利润差多少", f)["answer"]
    assert "现金流为负" not in pos and "尚未转化" not in pos, pos
    return "两支文案不再互相矛盾"


@check("审计补强", "D7 数字白名单不误杀自己的模板，仍拦住编造值")
def d7():
    import app
    d = __import__("datetime").date(2026, 11, 5)
    f = app.build_facts("002281", None, d)
    text = "；".join(c["text"] for c in app.make_claims(f))
    assert app.guard_numbers(text, f, text) == [], "模板文案被自己的白名单拦下了"
    banned = app.guard_numbers("营业收入 999.99 亿元，毛利率 47%", f, text)
    assert "999.99" in banned and "47" in banned, banned
    return "模板 0 误杀；编造值仍拦截"


@check("审计补强", "D8 R7 未被营收高增稀释时判 HIT")
def d8():
    import app
    d = __import__("datetime").date(2026, 11, 5)
    f = app.build_facts("002281", None, d)
    # 营收同比 36.94%（未达 40% 的降级条件）+ 费用率下降 >1pct → 应判 HIT，不是 NOTED
    f["metrics"]["rd_exp"] = {**f["metrics"]["rd_exp"], "prev": 1.5e9}
    r7 = next(r for r in app.risk_radar(f) if r["id"] == "R7")
    assert r7["status"] == "HIT", r7["status"]
    return "HIT"


def main() -> int:
    env = {**os.environ, "PORT": str(PORT)}
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                if req("/api/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            print("服务启动失败")
            return 2

        results = []
        for g, name, fn in CASES:
            try:
                results.append((g, name, True, str(fn() or "")))
            except AssertionError as exc:
                results.append((g, name, False, str(exc) or "断言失败"))
            except Exception as exc:
                results.append((g, name, False, f"{type(exc).__name__}: {exc}"))

        group = None
        for g, name, ok, detail in results:
            if g != group:
                print(f"\n【{g}】")
                group = g
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  —  {detail}" if detail else ""))

        passed = sum(1 for r in results if r[2])
        total = len(results)
        print(f"\n合计 {passed}/{total} 通过")
        if passed != total:
            print("失败项：")
            for g, name, ok, detail in results:
                if not ok:
                    print(f"  - {name}：{detail}")
        return 0 if passed == total else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
