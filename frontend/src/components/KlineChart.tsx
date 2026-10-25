import { useEffect, useRef } from "react";
import { createChart, ColorType, CrosshairMode } from "lightweight-charts";
import { cssVar, useTheme, withAlpha } from "../theme";

interface Bar { time: string; open: number; high: number; low: number; close: number; volume?: number }

const MA_COLORS: Record<number, string> = { 5: "#f5a623", 20: "#7a4dff" };

function movingAverage(bars: Bar[], n: number) {
  const out: { time: string; value: number }[] = [];
  let sum = 0;
  bars.forEach((b, i) => {
    sum += b.close;
    if (i >= n) sum -= bars[i - n].close;
    if (i >= n - 1) out.push({ time: b.time, value: +(sum / n).toFixed(3) });
  });
  return out;
}

/** 日 K：红涨（空心）绿跌（实心）+ MA5 / MA20 + 成交量，配色随主题。均线只是已发生收盘价的平均，不含预测。 */
export default function KlineChart({ series }: { series: Bar[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useTheme();
  useEffect(() => {
    if (!ref.current) return;
    const up = cssVar("--up") || "#e93030";
    const down = cssVar("--down") || "#0e9f5a";
    const bg = cssVar("--chart-bg") || "#ffffff";
    const chart = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: bg }, textColor: cssVar("--chart-text"), fontSize: 11 },
      grid: { vertLines: { color: cssVar("--chart-grid") }, horzLines: { color: cssVar("--chart-grid") } },
      rightPriceScale: { borderColor: cssVar("--chart-border") },
      timeScale: { borderColor: cssVar("--chart-border") },
      crosshair: { mode: CrosshairMode.Normal },
      height: 260,
      autoSize: true,
    });
    const bars = series.filter((b) => b.open != null && b.close != null);
    const candles = chart.addCandlestickSeries({ upColor: bg, borderUpColor: up, wickUpColor: up, downColor: down, borderDownColor: down, wickDownColor: down, priceLineVisible: false });
    candles.setData(bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })) as any);
    for (const n of [5, 20]) {
      if (bars.length > n) {
        const line = chart.addLineSeries({ color: MA_COLORS[n], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: `MA${n}` });
        line.setData(movingAverage(bars, n) as any);
      }
    }
    if (bars.some((b) => b.volume != null)) {
      const vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", priceLineVisible: false, lastValueVisible: false });
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      vol.setData(bars.map((b) => ({ time: b.time, value: b.volume || 0, color: withAlpha(b.close >= b.open ? up : down, 0.45) })) as any);
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series, theme]);
  return <div ref={ref} className="kline" />;
}
