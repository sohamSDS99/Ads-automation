"use client";

import { FileText } from "lucide-react";

import { LintChip } from "@/components/creative/lint-chip";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { CreativeAssetItem, CreativeAssetKind, CreativeAssetStatus } from "@/lib/api/creative-runs";
import { errorMessage, useCreativeAssets } from "@/lib/queries";

const KIND: Record<CreativeAssetKind, string> = {
  headline: "Headline",
  long_headline: "Long headline",
  description: "Description",
  path: "Display path",
  sitelink: "Sitelink",
  callout: "Callout",
  structured_snippet: "Structured snippet",
  promotion: "Promotion",
  price: "Price",
  lead_form: "Lead form",
  business_name: "Business name",
  video_script: "Video script",
  image: "Image",
  video: "Video",
  logo: "Logo",
};

const STATUS: Record<CreativeAssetStatus, string> = {
  draft: "Draft",
  linted: "Linted",
  reserve: "Reserve",
  awaiting_review: "Awaiting review",
  approved: "Approved",
  rejected: "Rejected",
  dropped: "Dropped",
  awaiting_exception: "Awaiting exception",
  released: "Released",
};

/**
 * The Assets tab (Stage 04 PRD §15.4 C): what this node produced, each with
 * the lint verdict it was stored with.
 *
 * A table, not cards — a node writes dozens of strings and they are read
 * against each other (§15.2 rule 4). The run's assets are read once and
 * narrowed to the node here, which is display, not a decision.
 */
export function AssetsTab({
  runId,
  nodeId,
  model,
}: {
  runId: string;
  nodeId: string;
  /** The node's model, for the `AI-generated` chip's hover (§15.2 rule 3). */
  model: string | null;
}) {
  const assets = useCreativeAssets(runId);

  if (assets.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy>
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-9 w-full" />
      </div>
    );
  }
  if (assets.isError) {
    return (
      <Alert tone="error" title="The assets could not be loaded">
        {errorMessage(assets) ?? "Try the refresh button in the console header."}
      </Alert>
    );
  }

  const mine = assets.data.items.filter((asset) => asset.node_id === nodeId);
  if (mine.length === 0) {
    return (
      <EmptyState
        icon={FileText}
        title="No assets from this node yet"
        description={`What ${nodeId} writes appears here the moment it is created, each linted against the run's ruleset pin before it can leave draft.`}
      />
    );
  }

  return (
    // `min-w-0`, and the text column takes what is left: a headline wraps
    // rather than pushing Lint and Status off the panel.
    <Table label={`Assets written by ${nodeId}`} className="min-w-0">
      <thead>
        <tr>
          <Th>Asset</Th>
          <Th>Lint</Th>
          <Th>Status</Th>
        </tr>
      </thead>
      <tbody>
        {mine.map((asset) => (
          <AssetRow key={asset.id} asset={asset} model={model} />
        ))}
      </tbody>
    </Table>
  );
}

function AssetRow({ asset, model }: { asset: CreativeAssetItem; model: string | null }) {
  return (
    <Tr>
      <Td className="w-full align-top">
        <p className="text-sm text-fg">{asset.text ?? KIND[asset.kind]}</p>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-subtle">
          <span>{KIND[asset.kind]}</span>
          {asset.variant ? <span>Variant {asset.variant}</span> : null}
          {asset.ad_group_ref ? <span>{asset.ad_group_ref}</span> : null}
          {asset.generated_by_ai ? (
            <span title={model ? `Written by ${model}` : "Written by a model"}>
              <Badge>AI-generated</Badge>
            </span>
          ) : null}
        </p>
      </Td>
      <Td className="align-top whitespace-nowrap">
        <LintChip verdict={asset.lint_verdict} />
      </Td>
      <Td className="align-top whitespace-nowrap text-xs text-fg-muted">{STATUS[asset.status]}</Td>
    </Tr>
  );
}
