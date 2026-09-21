"use client";

import { ForecastLine, ScenarioComparison } from "@/components/plan/forecast-chart";
import { asDemandForecast, asScenariosOutput } from "@/lib/api/plan";

/**
 * The figure a plan node's output deserves, above the JSON it came from.
 *
 * Two nodes in Stage 2.2 produce something with a shape: 2.2.1 is a series over
 * months and 2.2.3 is three alternatives to compare. Everything else is prose
 * and lists, which `JsonTree` already renders better than a chart would.
 *
 * The JSON stays underneath in every case. The chart is the reading; the tree
 * is the record, and an approver auditing a figure needs the record.
 */
export function PlanNodeFigure({
  nodeId,
  output,
}: {
  nodeId: string;
  output: Record<string, unknown> | null;
}) {
  if (output === null) return null;

  if (nodeId === "2.2.1") {
    const forecast = asDemandForecast(output);
    return forecast ? <ForecastLine forecast={forecast} className="mb-3" /> : null;
  }

  if (nodeId === "2.2.3") {
    const scenarios = asScenariosOutput(output);
    return scenarios ? (
      <ScenarioComparison
        scenarios={scenarios.scenarios}
        recommended={scenarios.recommended}
        reason={scenarios.recommendation_reason}
        className="mb-3"
      />
    ) : null;
  }

  return null;
}
