"""复现 README §8 的性能数字：python tests/bench.py

口径 = 用户真实体感：起一个临时端口，发 20 次 GET /api/analyze，
计时含 HTTP 往返与 JSON 序列化（不含浏览器渲染）。
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("BENCH_PORT", "4311"))
URL = f"http://127.0.0.1:{PORT}/api/analyze?code=002281&as_of=2026-11-05"
N = 20


def get() -> dict:
    with urllib.request.urlopen(URL, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT,
                            env={**os.environ, "PORT": str(PORT)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
                break
            except Exception:
                time.sleep(0.2)
        else:
            print("服务启动失败")
            return 2

        t0 = time.perf_counter()
        get()                                   # 首请求：含 JSON 快照反序列化与连接建立
        cold = (time.perf_counter() - t0) * 1000
        times, d = [], {}
        for _ in range(N):
            t0 = time.perf_counter()
            d = get()
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        print(f"样本 {N} 次　结论 {len(d['claims'])} 条　命中 "
              f"{[r['id'] for r in d['radar'] if r['status'] == 'HIT']}")
        print(f"p50 {statistics.median(times):.1f} ms　p95 {times[int(N * 0.95) - 1]:.1f} ms　"
              f"max {times[-1]:.1f} ms　首请求 {cold:.0f} ms")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
