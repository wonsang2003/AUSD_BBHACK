/**
 * Sparkline: a small inline-SVG area chart of a price series (no chart library).
 *
 * The update moment (`markT`, unix seconds) is drawn as a vertical amber line; the last price
 * and the % change over the window are labelled top-right. Line/area are green when the window
 * is up, red when it is down.
 */

"use client";

import type { PricePoint } from "@/lib/history";

const GREEN = "#3ddc84";
const RED = "#ff4d4d";
const AMBER = "#ffa028";
const MUTED = "#8a8a8a";

interface SparklineProps {
  points: PricePoint[];
  markT?: number | null;
  width?: number;
  height?: number;
  label?: string;
}

export function formatPrice(price: number): string {
  const digits = price >= 1000 ? 0 : price >= 1 ? 2 : 4;
  return price.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function Sparkline({ points, markT, width = 280, height = 90, label }: SparklineProps) {
  if (points.length < 2) {
    return (
      <div
        className="flex items-center justify-center border border-[#333] text-[11px] uppercase text-[#8a8a8a]"
        style={{ width, height }}
      >
        no price history
      </div>
    );
  }
  const padTop = 16;
  const padBottom = 4;
  const t0 = points[0].t;
  const t1 = Math.max(points[points.length - 1].t, markT ?? 0);
  const span = Math.max(t1 - t0, 1);
  const prices = points.map((point) => point.p);
  const lo = Math.min(...prices);
  const hi = Math.max(...prices);
  const range = hi - lo || hi * 0.001 || 1;
  const x = (t: number) => ((t - t0) / span) * (width - 2) + 1;
  const y = (p: number) => padTop + (1 - (p - lo) / range) * (height - padTop - padBottom);

  const line = points.map((point, i) => `${i === 0 ? "M" : "L"}${x(point.t).toFixed(1)},${y(point.p).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points[points.length - 1].t).toFixed(1)},${height} L${x(t0).toFixed(1)},${height} Z`;
  const first = points[0].p;
  const last = points[points.length - 1].p;
  const changePct = ((last - first) / first) * 100;
  const color = changePct >= 0 ? GREEN : RED;
  const markX = markT != null && markT >= t0 ? x(Math.min(markT, t1)) : null;
  const gradientId = `spark-${Math.round(t0)}-${Math.round(last * 100)}`;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="price chart" className="block">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect x="0.5" y="0.5" width={width - 1} height={height - 1} fill="#050505" stroke="#262626" />
      <path d={area} fill={`url(#${gradientId})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
      {markX !== null && (
        <g>
          <line x1={markX} x2={markX} y1={padTop - 2} y2={height} stroke={AMBER} strokeWidth="1.25" strokeDasharray="3 2" />
          <circle cx={markX} cy={y(last)} r="2.5" fill={AMBER} />
        </g>
      )}
      <text x="4" y="11" fontSize="9" fill={MUTED} fontFamily="monospace">
        {label ?? ""}
      </text>
      <text x={width - 4} y="11" fontSize="10" textAnchor="end" fontFamily="monospace" style={{ fontVariantNumeric: "tabular-nums" }}>
        <tspan fill="#e6e6e6">{formatPrice(last)}</tspan>
        <tspan fill={color}>{`  ${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`}</tspan>
      </text>
    </svg>
  );
}
