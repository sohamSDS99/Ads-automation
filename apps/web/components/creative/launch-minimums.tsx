"use client";

import { CircleCheck, CircleMinus, CircleX } from "lucide-react";

import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { CampaignCreative } from "@/lib/api/creative-packages";
import { cn } from "@/lib/utils";

/**
 * `LaunchMinimums` — per campaign, what the pin's spec sheet requires before
 * the campaign may launch and what the package carries (Stage 04 PRD §15.4 K,
 * L). The counts and `met` are 4.7.1's (`package.launch_minimums`), read from
 * the final pin; `present` is the fewest any one container carries — each ad
 * for RSA text, each asset group for asset-group text. Nothing is counted here.
 */
export function LaunchMinimums({ campaigns }: { campaigns: CampaignCreative[] }) {
  return (
    <div className="flex flex-col gap-4" data-testid="launch-minimums">
      {campaigns.map((campaign) => {
        const check = campaign.launch_minimums;
        return (
          <div key={campaign.campaign_ref} className="flex flex-col gap-2" data-testid="launch-minimum" data-met={String(check.met)}>
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              {check.met ? (
                <CircleCheck className="size-4 text-status-success" aria-hidden />
              ) : (
                <CircleX className="size-4 text-status-failed-ink" aria-hidden />
              )}
              <span className="font-medium text-fg">{campaign.campaign_ref}</span>
              <span className="text-fg-muted">
                {check.campaign_type.replace(/_/g, " ")} · {check.met ? "meets its launch minimum" : "short of its launch minimum"}
              </span>
            </p>
            {check.no_stated_minimum ? (
              <p className="text-sm text-fg-muted">
                The pin’s specs for {check.campaign_type.replace(/_/g, " ")} name no minimum, so there is nothing to count.
              </p>
            ) : (
              <div className="rounded-token border">
                <Table label={`Launch minimum for ${campaign.campaign_ref}`} className="min-w-0">
                  <thead>
                    <Tr>
                      <Th className="w-full">Asset type</Th>
                      <Th className="text-right">Carries</Th>
                      <Th className="text-right">Requires</Th>
                      <Th>Status</Th>
                    </Tr>
                  </thead>
                  <tbody>
                    {check.required.map((line) => (
                      <Tr key={line.asset_type} data-met={String(line.met)}>
                        <Td>{line.asset_type.replace(/_/g, " ")}</Td>
                        <Td className={cn("text-right tabular-nums", !line.met && "font-medium")}>{line.present}</Td>
                        <Td className="text-right tabular-nums text-fg-muted">{line.required}</Td>
                        <Td>
                          <span className="inline-flex items-center gap-1">
                            {line.met ? (
                              <CircleCheck className="size-3.5 text-status-success" aria-hidden />
                            ) : (
                              <CircleMinus className="size-3.5 text-status-failed-ink" aria-hidden />
                            )}
                            {line.met ? "Met" : `${line.required - line.present} short`}
                          </span>
                        </Td>
                      </Tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
