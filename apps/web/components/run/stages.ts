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

  // 2.0 is the handshake pair S2-P0 shipped so `stage='plan'` had something to
  // execute. S2-P2 deletes it; the entry costs a line and stops today's rail
  // reading "2.0 Stage 2.0".
  "2.0": "Take in the accepted research",

  // Stage 02, the planning DAG (Stage 02 PRD §11). Same voice as above: what
  // the stage decides, in the words someone would use out loud, not the node
  // names. The rail shows the id beside it, so the title never repeats it.
  "2.1": "Set the goal and the numbers",
  "2.2": "Decide the budget",
  "2.3": "Choose the campaign types",
  "2.4": "Decide how the account is organised",
  "2.5": "Decide how we measure and what we test",
  "2.6": "Write the plan",
};

export function stageTitle(stage: string): string {
  return STAGE_TITLE[stage] ?? `Stage ${stage}`;
}
