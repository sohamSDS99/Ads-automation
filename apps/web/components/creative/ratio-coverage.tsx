import { CircleSlash, Crop, Layers, Square } from "lucide-react";

import { AspectGlyph } from "@/components/creative/aspect-glyph";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { RatioPlanEntry } from "@/lib/api/creative";
import type { MediaModality, RatioPlan } from "@/lib/api/media";

const PLAN_CHIP: Record<RatioPlan, { label: string; icon: typeof Square }> = {
  native: { label: "native", icon: Square },
  relaid: { label: "relaid", icon: Layers },
  crop: { label: "crop", icon: Crop },
  gap: { label: "gap", icon: CircleSlash },
};

function percent(retained: number | undefined): string {
  return retained === undefined ? "" : `${Math.round(retained * 100)}%`;
}

/**
 * The consequence of one plan, in a sentence. The plan, the source ratio and
 * the share of frame kept are the server's (`media.ratio_plan_v1`); this only
 * says what they mean for the asset — and, for a gap, what fixes it
 * (§15.2 rule 9).
 */
function consequence(modality: MediaModality, ratio: string, entry: RatioPlanEntry): string {
  switch (entry.plan) {
    case "native":
      return `Painted at ${ratio} from the prompt.`;
    case "relaid":
      return modality === "video"
        ? `Generated at ${ratio} with the master as its first frame.`
        : `Painted at ${ratio} from the master, image to image, so it matches the concept.`;
    case "crop":
      return entry.from
        ? `Cropped from the ${entry.from} generation, keeping ${percent(entry.retained)} of the frame.`
        : `Cropped from a larger generation.`;
    case "gap":
      return entry.from
        ? `No ${ratio} ${modality} is made: the best crop, from ${entry.from}, keeps only ${percent(entry.retained)} of the frame. Choose a model that paints ${ratio} to fill it.`
        : `No ${ratio} ${modality} is made: this model paints nothing close. Choose a model that paints ${ratio} to fill it.`;
  }
}

/**
 * Ratio → plan → consequence, for every ratio the spec sheet requires (PRD
 * §9.2, §15.4 B): the cost of a model choice, seen before anything is spent.
 * A table, because it is data (§15.2 rule 4); the plan is a chip with an icon
 * and a word, so it never rests on colour.
 */
export function RatioCoverageTable({
  plan,
}: {
  plan: Partial<Record<MediaModality, Record<string, RatioPlanEntry>>>;
}) {
  const rows = (["image", "video"] as const).flatMap((modality) =>
    Object.entries(plan[modality] ?? {}).map(([ratio, entry]) => ({ modality, ratio, entry })),
  );
  if (rows.length === 0) return null;
  const gaps = rows.filter((row) => row.entry.plan === "gap").length;

  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-fg-muted">
        {gaps === 0
          ? `All ${rows.length} required ratios can be made with these models.`
          : `${gaps} of ${rows.length} required ratios ${gaps === 1 ? "is a gap" : "are gaps"} with these models.`}
      </p>
      {/* `min-w-0` over the primitive's `min-w-max`: the consequence is a
          sentence and has to wrap inside the sheet, not scroll it sideways. */}
      <Table label="Ratio coverage" className="min-w-0">
        <thead>
          <Tr>
            <Th>Ratio</Th>
            <Th>Plan</Th>
            <Th className="hidden sm:table-cell">Consequence</Th>
          </Tr>
        </thead>
        <tbody>
          {rows.map(({ modality, ratio, entry }) => {
            const chip = PLAN_CHIP[entry.plan];
            const Icon = chip.icon;
            return (
              <Tr key={`${modality}-${ratio}`}>
                <Td>
                  <span className="inline-flex items-center gap-2 whitespace-nowrap">
                    <AspectGlyph ratio={ratio} plan={entry.plan} />
                    <span className="font-mono text-xs text-fg">{ratio}</span>
                    <span className="text-xs text-fg-subtle">{modality}</span>
                  </span>
                </Td>
                <Td>
                  <span className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium text-fg-muted">
                    <Icon className="size-3" aria-hidden />
                    {chip.label}
                  </span>
                  {/* On a phone the consequence column folds under the chip. */}
                  <p className="mt-1 text-xs text-fg-muted sm:hidden">{consequence(modality, ratio, entry)}</p>
                </Td>
                <Td className="hidden text-fg-muted sm:table-cell">{consequence(modality, ratio, entry)}</Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}
