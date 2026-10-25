"""投资 X Buddy 演示视频：全自动录制 + 字幕 + 快进 + 片头片尾，输出 1080p MP4。

依赖（不在后端 requirements 里，仅录制时需要）：
    pip install playwright imageio-ffmpeg && python -m playwright install chromium

用法：
    # 1) 用真实密钥（.env）和一个空数据目录启动后端，保证侧栏只有本次录制的线程
    cd backend && DATA_DIR=/tmp/xbuddy-demo python -m uvicorn app.main:app --port 8767
    # 2) 另开终端录制（约 4 分钟，产出约 2 分钟的 1080p MP4；会调用真实模型，约 0.05 美元）
    python scripts/demo_video/make_demo.py --base http://127.0.0.1:8767 --out xbuddy-demo.mp4

流程：
1. 预跑：通过 API 注入扶摇故障 → 跑一个研究（作为“故障降级”片段的素材）→ 拒绝其记忆写入 → 清除故障。
2. 录制：无头 Chromium（1920×1080，界面放大 1.25 倍）按分镜操作真实产品；CDP 截屏流逐帧送入 ffmpeg。
3. 等待模型与工具的片段按 4 倍速写入时间轴，画面右上角显示“×4 快进”。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import pathlib
import subprocess
import time
import urllib.request

import imageio_ffmpeg
from playwright.async_api import async_playwright

HERE = pathlib.Path(__file__).resolve().parent
OVERLAY_JS = (HERE / "overlay.js").read_text(encoding="utf-8")
FPS = 30
Z = 1.25
W, H = 1920, 1080
GOAL_MAIN = "研究贵州茅台(600519.SH)近3个月行情、估值与最新财务指标，并与白酒板块对比"
GOAL_FAULT = "研究招商银行(600036.SH)估值与近一年走势统计"
FIX_CSS = f".shell{{height:calc(100vh / {Z}) !important}} .modal{{max-height:calc(88vh / {Z}) !important}}"


# ------------------------------------------------------------------ API（预跑）
def api(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def prep_fault_thread(base: str) -> None:
    print("[prep] 注入扶摇故障并预跑降级线程 …")
    api(base, "POST", "/api/dev/faults", {"target": "fuyao", "mode": "error"})
    try:
        thr = api(base, "POST", "/api/threads", {"title": "新研究线程"})
        run = api(base, "POST", f"/api/threads/{thr['id']}/runs", {"goal": GOAL_FAULT, "budget": {"plan_approval": "never"}})
        t0 = time.time()
        while time.time() - t0 < 300:
            r = api(base, "GET", f"/api/runs/{run['id']}")
            if r["status"] in ("completed", "failed", "stopped"):
                break
            time.sleep(2)
        for a in r.get("approvals", []):
            if a["status"] == "pending":
                api(base, "POST", f"/api/approvals/{a['id']}/decide", {"approved": False, "reason": "数据源故障演示，不写入记忆"})
        print(f"[prep] 降级线程完成：{r['status']}，工具失败 {r['usage']['tool_errors']} 次")
    finally:
        api(base, "POST", "/api/dev/faults", {"target": "all", "mode": "off"})


# ------------------------------------------------------------------ 录制器：截屏流 → 恒定帧率 → ffmpeg
class Recorder:
    def __init__(self, out: str):
        self.proc = subprocess.Popen(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
             "-vf", f"scale={W}:{H}:flags=lanczos:in_range=pc:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-color_range", "tv", "-movflags", "+faststart", out],
            stdin=subprocess.PIPE,
        )
        self.speed = 1.0
        self.frame: bytes | None = None
        self.last_t: float | None = None
        self.out_t = 0.0
        self.emitted = 0

    def _advance(self, now: float) -> None:
        if self.last_t is not None and self.frame is not None:
            self.out_t += (now - self.last_t) / self.speed
            target = int(self.out_t * FPS)
            for _ in range(target - self.emitted):
                self.proc.stdin.write(self.frame)
            self.emitted = max(self.emitted, target)
        self.last_t = now

    def on_frame(self, data: bytes) -> None:
        self._advance(time.monotonic())
        self.frame = data

    def set_speed(self, s: float) -> None:
        self._advance(time.monotonic())
        self.speed = s

    def tick(self) -> None:
        self._advance(time.monotonic())

    @property
    def seconds(self) -> float:
        return self.out_t

    def close(self) -> None:
        self.tick()
        self.proc.stdin.close()
        self.proc.wait()


# ------------------------------------------------------------------ 导演：光标、字幕、点击
class Director:
    def __init__(self, page, rec: Recorder):
        self.page = page
        self.rec = rec
        self.x, self.y = W * 0.55, H * 0.5

    async def setup(self) -> None:
        await self.page.evaluate(f"document.documentElement.style.zoom = '{Z}'")
        await self.page.add_style_tag(content=FIX_CSS)
        await self.page.add_script_tag(content=OVERLAY_JS)
        await self.page.mouse.move(self.x, self.y)

    async def hold(self, s: float) -> None:
        end = time.monotonic() + s * self.rec.speed
        while time.monotonic() < end:
            self.rec.tick()
            await asyncio.sleep(0.05)

    async def caption(self, step: str, text: str, pos: str = "top") -> None:
        await self.page.evaluate("([s,t,p]) => window.__demo.caption(s,t,p)", [step, text, pos])
        await self.center_caption(pos)

    async def center_caption(self, pos: str) -> None:
        # 字幕居中于中间栏（弹窗时居中于全屏）
        await self.page.evaluate("""(pos) => {
          const cap = document.querySelector('#demo-overlay .d-cap');
          const main = document.querySelector('.main'); const modal = document.querySelector('.modal');
          let cx = innerWidth / 2;
          if (main && !modal) { const r = main.getBoundingClientRect(); cx = r.left + r.width / 2; }
          cap.style.left = cx + 'px';
          cap.style.maxWidth = (modal ? 1500 : Math.min(1240, (main ? main.getBoundingClientRect().width - 60 : 1240))) + 'px';
        }""", pos)

    async def badge(self, text: str) -> None:
        await self.page.evaluate("(t) => window.__demo.badge(t)", text)

    async def card(self, html: str) -> None:
        await self.page.evaluate("(h) => window.__demo.card(h)", html)

    async def move(self, x: float, y: float, dur: float = 0.55) -> None:
        n = max(6, int(dur * FPS))
        x0, y0 = self.x, self.y
        for i in range(1, n + 1):
            t = i / n
            e = 0.5 - 0.5 * math.cos(math.pi * t)  # ease-in-out
            await self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            self.rec.tick()
            await asyncio.sleep(dur / n)
        self.x, self.y = x, y

    async def point(self, locator, dur: float = 0.55, dx: float = 0.0, dy: float = 0.0) -> None:
        await locator.scroll_into_view_if_needed()
        bb = await locator.bounding_box()
        await self.move(bb["x"] + bb["width"] / 2 + dx, bb["y"] + bb["height"] / 2 + dy, dur)

    async def click(self, locator, dur: float = 0.55) -> None:
        await self.point(locator, dur)
        await self.hold(0.15)
        await self.page.mouse.down()
        await asyncio.sleep(0.08)
        await self.page.mouse.up()
        await self.hold(0.35)

    async def scroll_into(self, selector: str, block: str = "center") -> None:
        await self.page.evaluate("([s,b]) => { const e = document.querySelector(s); if (e) e.scrollIntoView({behavior:'smooth', block:b}); }", [selector, block])
        await self.hold(0.9)

    async def scroll_el(self, selector: str, dy: int, steps: int = 6, pause: float = 0.12) -> None:
        for _ in range(steps):
            await self.page.evaluate("([s,d]) => { const e = document.querySelector(s); if (e) e.scrollBy({top:d, behavior:'smooth'}); }", [selector, dy / steps])
            await self.hold(pause)


TITLE_CARD = """
<div class="logo">X</div>
<h1>投资 X Buddy</h1>
<div class="sub">面向投资研究者的个人 Agent 工作台</div>
<div class="line"></div>
<div class="meta">题目 13 · 产品演示 · 真实模型 + 扶摇实时数据 · 全程无剪辑拼接（等待段 4 倍速）</div>
"""
END_CARD = """
<div class="logo">X</div>
<h1>投资 X Buddy</h1>
<div class="line"></div>
<div class="links"><b>在线体验</b>https://xbuddy-c5m7.onrender.com<br/><b>源码</b>https://github.com/XXXSinkacion/xbuddy</div>
<div class="meta">内容仅为研究过程与数据整理记录，不构成任何投资建议</div>
"""


async def record(base: str, out: str) -> float:
    rec = Recorder(out)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, locale="zh-CN")
        page = await ctx.new_page()
        await page.goto(base + "/")
        await page.wait_for_selector(".welcome")
        d = Director(page, rec)
        await d.setup()
        cdp = await ctx.new_cdp_session(page)

        def on_frame(ev):
            rec.on_frame(base64.b64decode(ev["data"]))
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))

        cdp.on("Page.screencastFrame", on_frame)
        await d.card(TITLE_CARD)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": W, "maxHeight": H, "everyNthFrame": 1})
        await d.hold(3.2)
        await d.card("")

        # ① 工作台概览
        await d.caption("① 工作台", "给出一个研究目标：Agent 规划任务 → 调用真实金融数据工具 → 记录并核验证据 → 产出可回溯的研究报告")
        await d.point(page.locator(".welcome .steps"), 0.8)
        await d.hold(1.6)
        await d.point(page.locator(".sidebar .threads"), 0.6)
        await d.hold(0.8)
        await d.point(page.locator(".right .tabs"), 0.8)
        await d.hold(0.8)
        await d.point(page.locator(".appbar .status"), 0.6)
        await d.hold(1.4)

        # ② 发起研究
        await d.caption("② 发起研究", "把“计划确认”设为始终审批，再选择预置目标：贵州茅台行情、估值、财务，并与白酒板块对比")
        await d.click(page.locator(".presets button", has_text="预算与审批"))
        sel = page.locator(".settings label", has_text="计划确认").locator("select")
        await d.point(sel, 0.5)
        await sel.select_option("always")
        await d.hold(1.2)
        await d.click(page.locator(".presets button", has_text="预算与审批"), 0.4)
        await d.click(page.locator(".presets button").first, 0.6)
        await d.hold(1.0)
        await d.click(page.locator(".composer button.primary"), 0.6)

        # ③ 计划与审批（规划阶段等待模型 → 快进）
        await d.caption("③ 计划审批", "模型把目标拆成可验收的任务：每个任务写明要用的工具与验收标准")
        rec.set_speed(4.0)
        await d.badge("×4 快进")
        await page.wait_for_selector(".approval:not(.decided) button.primary", timeout=180_000)
        rec.set_speed(1.0)
        await d.badge("")
        await d.scroll_into(".plan", "start")
        n_tasks = await page.locator(".plan .task").count()
        for i in range(min(n_tasks, 3)):
            await d.point(page.locator(".plan .task").nth(i), 0.45)
            await d.hold(0.9)
        await d.caption("③ 计划审批", "关键决策由人确认：批准后才开始执行（拒绝则停止并输出部分报告）")
        await d.scroll_into(".approval:not(.decided)", "center")
        await d.click(page.locator(".approval:not(.decided) button.primary"), 0.6)

        # ④ 执行（快进）
        await d.caption("④ 执行", "每次工具调用都生成证据 E#：来源 · 数据时点 · 单位口径 · 原始字段；同轮取数并发执行，每一步写检查点")
        rec.set_speed(4.0)
        await d.badge("×4 快进")
        t_exec = time.monotonic()
        switched = False
        while True:
            done = await page.locator(".msg .artifact").count()
            if done:
                break
            if not switched and time.monotonic() - t_exec > 30:
                switched = True
                await d.click(page.locator(".tabs button").nth(1), 0.3)
                await d.caption("④ 执行", "右侧证据面板实时增加：成功、失败、缺失都会记录，不会被静默跳过")
            if time.monotonic() - t_exec > 360:
                raise TimeoutError("研究运行超时")
            await d.hold(0.25)
        await d.hold(1.2)
        rec.set_speed(1.0)
        await d.badge("")
        await d.click(page.locator(".tabs button").nth(0), 0.4)

        # ⑤ 动态 UI：K 线与指标
        await d.caption("⑤ 动态 UI", "行情指标卡、日 K 线（红涨绿跌，MA5 / MA20）、估值与财务表格，均标注证据编号与数据时点")
        await d.scroll_into(".kline", "center")
        await d.point(page.locator(".kline").first, 0.7)
        await d.hold(2.6)

        # ⑥ 证据回溯
        await d.caption("⑥ 证据回溯", "点击任意证据编号 [E#]，查看来源、数据时点、单位口径、request_id 与原始 JSON", "bottom")
        ev = page.locator("div:has(> .kline) > .block-title .evref").first
        await d.click(ev, 0.5)
        await page.wait_for_selector(".modal")
        await d.center_caption("bottom")
        await d.hold(1.8)
        await d.scroll_el(".modal", 520, steps=6)
        await d.hold(1.6)
        await d.click(page.locator(".modal button", has_text="关闭"), 0.5)

        # ⑦ 研究报告
        await d.caption("⑦ 研究报告", "结论分为【事实】【推断】【不确定】三层，每条都带证据引用", "bottom")
        await d.scroll_into(".msg .artifact", "center")
        await d.click(page.locator(".msg .artifact button.primary").last, 0.6)
        await page.wait_for_selector(".modal .md")
        await d.center_caption("bottom")
        await d.hold(1.2)
        await d.scroll_el(".modal", 260, steps=4)
        await d.hold(2.2)
        await d.caption("⑦ 研究报告", "数值核验：【事实】里的每个数字都回到所引证据的原始字段逐个核对，核不到的标“待核”", "bottom")
        await d.page.evaluate("() => { const m = document.querySelector('.modal'); const c = [...m.querySelectorAll('.callout')].find(x => x.innerText.includes('数值核验')); if (c) c.scrollIntoView({behavior:'smooth', block:'center'}); }")
        await d.hold(1.0)
        if await page.locator(".modal .callout", has_text="数值核验").count():
            await d.point(page.locator(".modal .callout", has_text="数值核验").first, 0.5)
        await d.hold(2.2)
        await d.caption("⑦ 研究报告", "接口失败、数据缺失如实列出，不生成“正常”结论；不做涨跌预测，不给买卖建议", "bottom")
        await d.page.evaluate("() => { const m = document.querySelector('.modal'); const h = [...m.querySelectorAll('h2')].find(x => x.innerText.includes('数据缺失')); if (h) h.scrollIntoView({behavior:'smooth', block:'start'}); }")
        await d.hold(3.0)
        await d.page.evaluate("() => { const m = document.querySelector('.modal'); const h = [...m.querySelectorAll('h2')].find(x => x.innerText.includes('免责声明')); if (h) h.scrollIntoView({behavior:'smooth', block:'center'}); }")
        await d.hold(1.8)
        await d.click(page.locator(".modal button", has_text="关闭"), 0.5)

        # ⑧ 长期记忆审批 + 可观测性
        await d.caption("⑧ 长期记忆", "研究要点写入长期记忆同样需要人工批准；记忆面板可查看、可删除")
        await d.scroll_into(".approval:not(.decided)", "center")
        await d.hold(1.0)
        await d.click(page.locator(".approval:not(.decided) button.primary"), 0.6)
        await d.click(page.locator(".tabs button").nth(4), 0.6)
        await d.hold(2.0)
        await d.caption("⑨ 可观测性", "观测页：模型与工具调用次数、token、成本、时延、停止规则余量与事件时间线")
        await d.click(page.locator(".tabs button").nth(3), 0.5)
        await d.hold(1.0)
        await d.point(page.locator(".right .panel .card").first, 0.5)
        await d.hold(2.0)
        await d.point(page.locator(".right .panel .card").nth(1), 0.5)
        await d.hold(1.6)

        # ⑩ 故障降级（录制前预跑的线程）
        await d.caption("⑩ 故障降级", "录制前预跑：注入扶摇数据源故障 → 重试、熔断 → 报告逐条写明哪些数据缺失，不编造任何数值")
        await d.click(page.locator(".tabs button").nth(0), 0.4)
        await d.click(page.locator(".thread-item", has_text="招商银行"), 0.6)
        await page.wait_for_selector(".msg .artifact")
        await d.hold(1.2)
        if await page.locator(".msg .callout.error").count():
            await d.scroll_into(".msg .callout.error", "center")
            await d.point(page.locator(".msg .callout.error").first, 0.5)
        await d.hold(2.2)
        await d.scroll_into(".msg .artifact", "start")
        await d.hold(2.6)

        # ⑪ 深色终端主题
        await d.caption("⑪ 主题", "浅色 / 深色终端两套主题，红涨绿跌")
        await d.click(page.locator(".thread-item", has_text="茅台"), 0.6)
        await page.wait_for_selector(".kline")
        await d.hold(0.6)
        await d.scroll_into(".kline", "center")
        await d.click(page.locator(".appbar .theme-toggle"), 0.7)
        await d.hold(2.6)
        await d.click(page.locator(".appbar .theme-toggle"), 0.4)
        await d.hold(0.8)
        await d.caption("", "")

        await d.card(END_CARD)
        await d.hold(4.5)
        await cdp.send("Page.stopScreencast")
        await browser.close()
    rec.close()
    return rec.seconds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8767")
    ap.add_argument("--out", default="xbuddy-demo.mp4")
    ap.add_argument("--skip-prep", action="store_true")
    a = ap.parse_args()
    if not a.skip_prep:
        prep_fault_thread(a.base)
    secs = asyncio.run(record(a.base, a.out))
    print(f"[done] {a.out}  时长约 {secs:.1f}s")


if __name__ == "__main__":
    main()
