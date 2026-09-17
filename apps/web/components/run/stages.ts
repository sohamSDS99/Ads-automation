/**
 * What each stage of the research DAG is for, in the words of PRD §10.
 *
 * A lookup with a fallback rather than a fixed list: the registry is what
 * decides which stages exist, and it grows a phase at a time. A rail that
 * hard-coded 1.1 → 1.6 would show empty stages today and miss new ones later.
 */
export const STAGE_TITLE: Record<string, string> = {
  "1.1": "Understand our own business",
  "1.2": "Learn from what we already ran",
  "1.3": "Study the competition",
  "1.4": "Find the demand",
  "1.5": "Check we are actually ready",
  "1.6": "Write it up",
};

export function stageTitle(stage: string): string {
  return STAGE_TITLE[stage] ?? `Stage ${stage}`;
}
