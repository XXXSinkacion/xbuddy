// 演示视频叠加层：假光标、点击波纹、字幕条、快进角标、片头片尾卡片。
// 页面根元素设置了 zoom，叠加层用反向 zoom 抵消，从而按真实 1920×1080 像素排版。
(() => {
  if (window.__demo) return;
  const Z = parseFloat(document.documentElement.style.zoom || "1") || 1;
  const root = document.createElement("div");
  root.id = "demo-overlay";
  root.style.cssText = `position:fixed;left:0;top:0;width:100vw;height:100vh;pointer-events:none;z-index:2147483647;zoom:${1 / Z};font-family:"PingFang SC","Microsoft YaHei",sans-serif;`;
  root.innerHTML = `
  <style>
    #demo-overlay * { box-sizing: border-box; }
    .d-cursor { position:absolute; left:0; top:0; width:28px; height:28px; transform:translate(-3px,-2px); transition: opacity .2s; filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
    .d-ripple { position:absolute; width:44px; height:44px; margin:-22px 0 0 -22px; border-radius:50%; border:3px solid #e93030; background:rgba(233,48,48,.18); animation: d-rip .55s ease-out forwards; }
    @keyframes d-rip { from { transform: scale(.3); opacity: 1 } to { transform: scale(1.4); opacity: 0 } }
    .d-cap { position:absolute; left:50%; transform:translateX(-50%); max-width:1240px; min-width:520px; background:rgba(20,20,22,.88); color:#fff; border-radius:12px; padding:14px 22px; font-size:24px; line-height:1.5; display:flex; gap:14px; align-items:flex-start; box-shadow:0 8px 24px rgba(0,0,0,.25); transition: opacity .25s; }
    .d-cap.top { top: 128px; } .d-cap.bottom { bottom: 36px; }
    .d-cap .d-step { flex:none; background:#e93030; color:#fff; font-size:18px; font-weight:700; border-radius:6px; padding:3px 10px; margin-top:3px; white-space:nowrap; }
    .d-badge { position:absolute; right:40px; top:128px; background:#e93030; color:#fff; font:800 30px/1 "DIN Alternate",Bahnschrift,Arial,sans-serif; padding:10px 16px; border-radius:10px; box-shadow:0 6px 18px rgba(233,48,48,.4); display:none; }
    .d-card { position:absolute; inset:0; background:#252525; color:#fff; display:none; flex-direction:column; align-items:center; justify-content:center; gap:18px; text-align:center; }
    .d-card .logo { width:96px; height:96px; border-radius:22px; background:#fff; color:#e93030; display:grid; place-items:center; font:800 60px/1 "DIN Alternate",Bahnschrift,Arial,sans-serif; }
    .d-card h1 { font-size:64px; margin:0; letter-spacing:1px; }
    .d-card .sub { font-size:30px; color:rgba(255,255,255,.78); }
    .d-card .line { width:120px; height:4px; background:#e93030; border-radius:2px; }
    .d-card .meta { font-size:22px; color:rgba(255,255,255,.6); }
    .d-card .links { font-size:28px; line-height:1.9; color:#fff; }
    .d-card .links b { color:#ff6b6b; font-weight:600; margin-right:14px; }
  </style>
  <svg class="d-cursor" viewBox="0 0 28 28"><path d="M3 2 L3 22 L8.5 17 L12.5 26 L16 24.5 L12 15.5 L19.5 15.5 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>
  <div class="d-cap top" style="opacity:0"><span class="d-step"></span><span class="d-text"></span></div>
  <div class="d-badge">×4 快进</div>
  <div class="d-card"></div>`;
  document.documentElement.appendChild(root);
  const cur = root.querySelector(".d-cursor");
  const cap = root.querySelector(".d-cap");
  const badge = root.querySelector(".d-badge");
  const card = root.querySelector(".d-card");
  // 实测：该 Chromium 在根元素 zoom 下 clientX / getBoundingClientRect 已是真实像素，叠加层（等效 zoom=1）直接使用
  window.addEventListener("mousemove", (e) => { cur.style.left = e.clientX + "px"; cur.style.top = e.clientY + "px"; }, true);
  window.addEventListener("mousedown", (e) => {
    const r = document.createElement("div");
    r.className = "d-ripple";
    r.style.left = e.clientX + "px"; r.style.top = e.clientY + "px";
    root.appendChild(r); setTimeout(() => r.remove(), 700);
  }, true);
  window.__demo = {
    caption(step, text, pos) {
      cap.className = "d-cap " + (pos || "top");
      cap.querySelector(".d-step").textContent = step || "";
      cap.querySelector(".d-step").style.display = step ? "" : "none";
      cap.querySelector(".d-text").textContent = text || "";
      cap.style.opacity = text ? "1" : "0";
    },
    badge(text) { badge.textContent = text || ""; badge.style.display = text ? "block" : "none"; },
    card(html) { card.innerHTML = html || ""; card.style.display = html ? "flex" : "none"; cur.style.opacity = html ? "0" : "1"; },
    cursorPos() { return [parseFloat(cur.style.left) || 0, parseFloat(cur.style.top) || 0]; },
  };
})();
