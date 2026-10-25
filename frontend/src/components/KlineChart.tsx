import { useEffect, useRef } from "react";
import { createChart, ColorType, type IChartApi } from "lightweight-charts";

interface Bar { time: string; open: number; high: number; low: number; close: number; volume?: number }

export default function KlineChart({ series }: { series: Bar[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#8b97ab" },
      grid: { vertLines: { color: "#1b2435" }, horzLines: { color: "#1b2435" } },
      rightPriceScale: { borderColor: "#22304a" },
      timeScale: { borderColor: "#22304a" },
      height: 260,
      autoSize: true,
    });
    chartRef.current = chart;
    const candles = chart.addCandlestickSeries({ upColor: "#ef4444", downColor: "#22c55e", borderUpColor: "#ef4444", borderDownColor: "#22c55e", wickUpColor: "#ef4444", wickDownColor: "#22c55e" });
    const data = series.filter((b) => b.open != null && b.close != null).map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }));
    candles.setData(data as any);
    if (series.some((b) => b.volume != null)) {
      const vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      vol.setData(series.map((b) => ({ time: b.time, value: b.volume || 0, color: b.close >= b.open ? "rgba(239,68,68,.45)" : "rgba(34,197,94,.45)" })) as any);
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series]);
  return <div ref={ref} className="kline" />;
}
