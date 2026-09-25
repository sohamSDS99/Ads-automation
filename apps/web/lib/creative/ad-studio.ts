/**
 * The Ad Studio's reading of a run (Stage 04 PRD §15.4 E): which ads there
 * are, what each carries, in which order, and what 4.2.3 found about each pair.
 *
 * Composition only — no verdict is made here. The asset rows are the truth for
 * what an ad carries now (`linted` = in the ad, `reserve` = kept for it) and
 * for every lint verdict; the node outputs add what only they hold: the order
 * 4.2.3 judged, the quotas 4.2.1 selected to, the pair flags and labels, and
 * variant B's distinctness and hypothesis. When a person has changed an asset
 * since, the pairs it is in are reported as checked before the change, never
 * as though the old check still held.
 */
import type {
  AdGroupBrief,
  ClaimBoundDescriptionsOutput,
  CombinationCoherenceOutput,
  CreativeAssetItem,
  DescriptionGroup,
  HeadlineGroup,
  HeadlineSpreadOutput,
  Pair,
  PairReport,
  VariantBGroup,
  VariantBOutput,
} from "@/lib/api/creative-runs";

export type Variant = "A" | "B";

/** One Search ad group of the brief, as 4.2.1 wrote for it. */
export type AdSlot = {
  key: string;
  campaign_ref: string;
  ad_group_ref: string;
  campaign_type: string;
  market: string;
  language: string;
  /** B only once 4.2.4 has written it. */
  variants: Variant[];
};

export type StudioAd = {
  slot: AdSlot;
  variant: Variant;
  headlineGroup: HeadlineGroup;
  descriptionGroup: DescriptionGroup | null;
  /** In the ad now, in the order 4.2.3 judged it; a swap takes its parent's place. */
  headlines: CreativeAssetItem[];
  /** Kept for it, best replacement first (4.2.1's order). */
  headlineReserves: CreativeAssetItem[];
  descriptions: CreativeAssetItem[];
  descriptionReserves: CreativeAssetItem[];
  /** 4.2.1's quota per category beside how many the ad carries now. */
  quota: { category: string; required: number; carried: number }[];
  report: PairReport | null;
  paths: [string | null, string | null];
  finalUrl: string | null;
  /** B's 4.2.4 record: distinctness, the floor it was held to, the hypothesis. */
  variantB: VariantBGroup | null;
};

export type PairState =
  | { state: "checked"; pair: Pair }
  /** 4.2.3 checked it, then a person rewrote one of the two. */
  | { state: "stale"; pair: Pair }
  /** A person swapped one of the two in: 4.2.3 never paired them. */
  | { state: "unchecked" };

export function slotKey(campaignRef: string, adGroupRef: string): string {
  return `${campaignRef}\u0000${adGroupRef}`;
}

export function pairKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

/** Every Search ad group 4.2.1 wrote headlines for, in its order. */
export function adSlots(spread: HeadlineSpreadOutput | null, variantB: VariantBOutput | null): AdSlot[] {
  const withB = new Set(
    (variantB?.ad_groups ?? []).map((group) => slotKey(group.campaign_ref, group.ad_group_ref)),
  );
  return (spread?.ad_groups ?? [])
    .filter((group) => group.variant === "A")
    .map((group) => {
      const key = slotKey(group.campaign_ref, group.ad_group_ref);
      return {
        key,
        campaign_ref: group.campaign_ref,
        ad_group_ref: group.ad_group_ref,
        campaign_type: group.campaign_type,
        market: group.market,
        language: group.language,
        variants: withB.has(key) ? ["A", "B"] : ["A"],
      };
    });
}

type Outputs = {
  spread: HeadlineSpreadOutput | null;
  descriptions: ClaimBoundDescriptionsOutput | null;
  coherence: CombinationCoherenceOutput | null;
  variantB: VariantBOutput | null;
};

/** The ad `variant` of `slot`, or null when its headlines have not been written. */
export function studioAd(
  slot: AdSlot,
  variant: Variant,
  outputs: Outputs,
  assets: CreativeAssetItem[],
  briefGroups: AdGroupBrief[],
): StudioAd | null {
  const same = (item: { campaign_ref: string; ad_group_ref: string }) =>
    item.campaign_ref === slot.campaign_ref && item.ad_group_ref === slot.ad_group_ref;
  const b = variant === "B" ? (outputs.variantB?.ad_groups.find(same) ?? null) : null;
  const headlineGroup =
    variant === "B" ? (b?.headlines ?? null) : (outputs.spread?.ad_groups.find((g) => same(g) && g.variant === "A") ?? null);
  if (headlineGroup === null) return null;
  const descriptionGroup =
    variant === "B"
      ? (b?.descriptions ?? null)
      : (outputs.descriptions?.ad_groups.find((g) => same(g) && g.variant === "A") ?? null);
  const report =
    variant === "B"
      ? (b?.ad_b.pair_report ?? null)
      : (outputs.coherence?.ads.find((ad) => same(ad) && ad.variant === "A") ?? null);

  const mine = assets.filter((asset) => same({ campaign_ref: asset.campaign_ref, ad_group_ref: asset.ad_group_ref ?? "" }) && asset.variant === variant);
  const headlineRows = mine.filter((asset) => asset.kind === "headline");
  const descriptionRows = mine.filter((asset) => asset.kind === "description");

  const headlines = judgedOrder(headlineRows, report?.headlines ?? headlineGroup.selected);
  const descriptions = judgedOrder(
    descriptionRows,
    report?.descriptions ?? (descriptionGroup?.descriptions.map((d) => d.asset_id) ?? []),
  );
  const carried = new Map<string, number>();
  for (const asset of headlines) {
    const category = asset.category ?? "";
    carried.set(category, (carried.get(category) ?? 0) + 1);
  }

  return {
    slot,
    variant,
    headlineGroup,
    descriptionGroup,
    headlines,
    headlineReserves: reserveOrder(headlineRows, headlineGroup.reserve),
    descriptions,
    descriptionReserves: reserveOrder(
      descriptionRows,
      (descriptionGroup?.reserve ?? []).map((item) => item.asset_id),
    ),
    quota: headlineGroup.quota_report.lines.map((line) => ({
      category: line.category,
      required: line.required,
      carried: carried.get(line.category) ?? 0,
    })),
    report,
    paths:
      variant === "B"
        ? (b?.ad_b.paths ?? [null, null])
        : (descriptionGroup?.paths ?? [null, null]),
    finalUrl:
      variant === "B"
        ? (b?.ad_b.final_url ?? null)
        : (briefGroups.find((group) => same(group))?.landing_url ?? null),
    variantB: b,
  };
}

/**
 * The assets in the ad now, in the order 4.2.3 judged: each judged asset, or
 * whatever a person swapped in for it (followed through `lineage.parent_id`),
 * keeps its row; anything else the ad carries follows in written order.
 */
function judgedOrder(rows: CreativeAssetItem[], judged: string[]): CreativeAssetItem[] {
  const carried = rows.filter((row) => row.status === "linted");
  const inAd = new Set(carried.map((row) => row.id));
  const children = new Map<string, CreativeAssetItem[]>();
  for (const row of rows) {
    const parent = row.lineage?.["parent_id"];
    if (typeof parent === "string") children.set(parent, [...(children.get(parent) ?? []), row]);
  }
  const taken = new Set<string>();
  const ordered: CreativeAssetItem[] = [];
  for (const id of judged) {
    const queue = [id];
    const seen = new Set<string>();
    while (queue.length > 0) {
      const current = queue.shift() as string;
      if (seen.has(current)) continue;
      seen.add(current);
      if (inAd.has(current) && !taken.has(current)) {
        taken.add(current);
        ordered.push(carried.find((row) => row.id === current) as CreativeAssetItem);
        break;
      }
      for (const child of children.get(current) ?? []) queue.push(child.id);
    }
  }
  for (const row of carried) if (!taken.has(row.id)) ordered.push(row);
  return ordered;
}

function reserveOrder(rows: CreativeAssetItem[], preferred: string[]): CreativeAssetItem[] {
  const reserves = rows.filter((row) => row.status === "reserve");
  const rank = new Map(preferred.map((id, index) => [id, index]));
  return [...reserves].sort(
    (a, b) => (rank.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (rank.get(b.id) ?? Number.MAX_SAFE_INTEGER),
  );
}

/** What 4.2.3 found about two assets of the ad, and whether it still describes them. */
export function pairState(ad: StudioAd, a: CreativeAssetItem, b: CreativeAssetItem): PairState {
  const pair = ad.report?.pairs.find((p) => pairKey(p.a, p.b) === pairKey(a.id, b.id));
  if (pair === undefined) return { state: "unchecked" };
  const edited = [a, b].some((asset) => asset.lineage?.["origin"] === "human_edit");
  return edited ? { state: "stale", pair } : { state: "checked", pair };
}

export type Combination = {
  headlines: CreativeAssetItem[];
  descriptions: CreativeAssetItem[];
  /** False when pins leave no combination Google serves with every focus asset in it. */
  servable: boolean;
};

const HEADLINE_SLOTS = ["H1", "H2", "H3"];
const DESCRIPTION_SLOTS = ["D1", "D2"];

/**
 * One combination Google could serve: three headline slots and two
 * description slots, a pinned asset only in its pinned slot, the `focus`
 * assets (a pair from the heatmap) placed first wherever their pins allow.
 *
 * When the pins leave no room for the whole pair — H1 and H2 pinned, and two
 * unpinned headlines asked for — the pair is still shown, first, so it can be
 * read, and `servable` says Google never shows it that way.
 */
export function combination(ad: StudioAd, focus: string[] = []): Combination {
  const headlines = fill(ad.headlines, HEADLINE_SLOTS, focus);
  const descriptions = fill(ad.descriptions, DESCRIPTION_SLOTS, focus);
  const placed = new Set([...headlines, ...descriptions].map((asset) => asset.id));
  if (focus.every((id) => placed.has(id))) return { headlines, descriptions, servable: true };
  return {
    headlines: first(ad.headlines, HEADLINE_SLOTS, focus),
    descriptions: first(ad.descriptions, DESCRIPTION_SLOTS, focus),
    servable: false,
  };
}

/** The focus assets first, whatever their pins; the slots left filled as usual. */
function first(assets: CreativeAssetItem[], positions: string[], focus: string[]): CreativeAssetItem[] {
  const chosen = focus
    .map((id) => assets.find((asset) => asset.id === id))
    .filter((asset): asset is CreativeAssetItem => asset !== undefined);
  const rest = fill(
    assets.filter((asset) => !focus.includes(asset.id)),
    positions.slice(chosen.length),
    [],
  );
  return [...chosen, ...rest].slice(0, positions.length);
}

function fill(assets: CreativeAssetItem[], positions: string[], focus: string[]): CreativeAssetItem[] {
  const first = [
    ...focus.map((id) => assets.find((asset) => asset.id === id)).filter((a): a is CreativeAssetItem => !!a),
    ...assets.filter((asset) => !focus.includes(asset.id)),
  ];
  const used = new Set<string>();
  const slots: CreativeAssetItem[] = [];
  for (const position of positions) {
    const pick =
      first.find((asset) => !used.has(asset.id) && asset.pin_position === position) ??
      first.find((asset) => !used.has(asset.id) && asset.pin_position === null);
    if (pick === undefined) continue;
    used.add(pick.id);
    slots.push(pick);
  }
  return slots;
}

/** A market as a person reads it: `*` is the plan's "no market named". */
export function marketLabel(market: string): string {
  return market === "*" ? "All markets" : market;
}

/** "H4", "D2": where an asset sits in its ad's list, as a person refers to it. */
export function position(ad: StudioAd, asset: CreativeAssetItem): string {
  const headline = ad.headlines.findIndex((row) => row.id === asset.id);
  if (headline >= 0) return `H${headline + 1}`;
  const description = ad.descriptions.findIndex((row) => row.id === asset.id);
  return description >= 0 ? `D${description + 1}` : "Reserve";
}
