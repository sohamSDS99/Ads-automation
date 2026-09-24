import { ratioValue, type RatioPlan } from "@/lib/api/media";
import { cn } from "@/lib/utils";

/** How each plan reads, said in words for the glyph's accessible name. */
export const RATIO_PLAN_LABEL: Record<RatioPlan, string> = {
  native: "native",
  relaid: "relaid",
  crop: "cropped",
  gap: "gap",
};

/**
 * One required ratio, drawn at its true proportions (PRD §15.4 B).
 *
 * The rectangle *is* the ratio — 1.91:1 is a wide bar, 9:16 a tall one — fit
 * into a `size`-pixel square so a row of them lines up. How it is filled says
 * how the chosen model makes it, by shape rather than colour (§15.2 rule 14):
 *
 * - **solid** — native: the model paints this ratio from the prompt;
 * - **outlined** — relaid (painted at this ratio from the master) or cropped
 *   (cut from a larger generation);
 * - **struck through** — a gap: nothing the model paints keeps enough of the
 *   frame, so no asset at this ratio is made.
 *
 * Geometry goes in `style`: it is a measurement of the data, not a design
 * value, and there is no token for "1.91 times as wide as it is tall".
 */
export function AspectGlyph({
  ratio,
  plan,
  size = 18,
  className,
}: {
  ratio: string;
  plan: RatioPlan;
  size?: number;
  className?: string;
}) {
  const value = ratioValue(ratio) ?? 1;
  const stroke = 1.5;
  const inset = stroke / 2;
  const span = size - stroke;
  const width = value >= 1 ? span : span * value;
  const height = value >= 1 ? span / value : span;
  const x = inset + (span - width) / 2;
  const y = inset + (span - height) / 2;

  return (
    <svg
      role="img"
      aria-label={`${ratio} ${RATIO_PLAN_LABEL[plan]}`}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className={cn("shrink-0", className)}
    >
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={1}
        strokeWidth={stroke}
        className={cn(
          "stroke-fg-muted",
          plan === "native" ? "fill-fg-muted" : "fill-none",
          plan === "gap" && "stroke-fg-subtle",
        )}
      />
      {plan === "gap" ? (
        <line
          x1={x - inset}
          y1={y + height + inset}
          x2={x + width + inset}
          y2={y - inset}
          strokeWidth={stroke}
          strokeLinecap="round"
          className="stroke-fg"
        />
      ) : null}
    </svg>
  );
}

/** The glyphs for every ratio a model was checked against, in the spec sheet's order. */
export function AspectGlyphRow({
  coverage,
  size,
}: {
  coverage: Record<string, RatioPlan>;
  size?: number;
}) {
  const entries = Object.entries(coverage);
  if (entries.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1" aria-label="Ratio coverage">
      {entries.map(([ratio, plan]) => (
        <span key={ratio} className="inline-flex items-center gap-0.5" title={`${ratio} · ${RATIO_PLAN_LABEL[plan]}`}>
          <AspectGlyph ratio={ratio} plan={plan} size={size} />
        </span>
      ))}
    </span>
  );
}
