"use client";

import { Film, ImageIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type {
  CampaignCreative,
  PackageMediaAsset,
  PackageRsa,
  PackageTextAsset,
} from "@/lib/api/creative-packages";
import { mediaContentUrl } from "@/lib/api/media-library";

const EXTENSION_KIND: Record<string, string> = {
  sitelinks: "Sitelinks",
  callouts: "Callouts",
  snippets: "Structured snippets",
  promotions: "Promotions",
  prices: "Prices",
};

/** An asset's text as it ships, or its fields when it is structured (a snippet, a price). */
export function assetText(asset: PackageTextAsset | undefined): string {
  if (!asset) return "—";
  if (asset.text) return asset.text;
  const header = asset.fields["header"];
  const values = asset.fields["values"];
  if (typeof header === "string" && Array.isArray(values)) return `${header}: ${values.join(", ")}`;
  return Object.entries(asset.fields)
    .filter(([key]) => key !== "url_check")
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(" · ");
}

function AdTable({ ad, byId }: { ad: PackageRsa; byId: Map<string, PackageTextAsset> }) {
  const rows: { role: string; asset: PackageTextAsset | undefined }[] = [
    ...ad.headlines.map((id, index) => ({ role: `H${index + 1}`, asset: byId.get(id) })),
    ...ad.descriptions.map((id, index) => ({ role: `D${index + 1}`, asset: byId.get(id) })),
  ];
  const [p1, p2] = ad.paths;
  return (
    <div className="flex flex-col gap-2" data-testid="package-rsa" data-ad-ref={ad.ad_ref}>
      <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm">
        <span className="font-medium text-fg">
          {ad.ad_group_ref} · variant {ad.variant}
        </span>
        <span className="text-fg-muted">{ad.angle}</span>
      </p>
      <p className="break-all font-mono text-xs text-fg-muted">
        {ad.final_url}
        {p1 ? ` › ${p1}` : ""}
        {p2 ? ` › ${p2}` : ""}
      </p>
      {ad.hypothesis ? <p className="text-sm text-fg-muted">Hypothesis: {ad.hypothesis}</p> : null}
      <div className="rounded-token border">
        <Table label={`${ad.ad_ref} — headlines and descriptions`} className="min-w-0">
          <thead>
            <Tr>
              <Th>Slot</Th>
              <Th className="w-full">Text</Th>
              <Th className="text-right">Chars</Th>
              <Th className="hidden sm:table-cell">Pin</Th>
            </Tr>
          </thead>
          <tbody>
            {rows.map(({ role, asset }, index) => (
              <Tr key={`${role}-${index}`}>
                <Td className="text-fg-muted tabular-nums">{role}</Td>
                <Td className="max-w-0 whitespace-normal">{assetText(asset)}</Td>
                <Td className="text-right tabular-nums text-fg-muted">{asset?.text ? [...asset.text].length : "—"}</Td>
                <Td className="hidden text-fg-muted sm:table-cell">{asset?.pin_position ?? "—"}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </div>
    </div>
  );
}

/** One media asset: its first rendition at its own aspect ratio, and what else ships with it. */
export function MediaThumb({ asset }: { asset: PackageMediaAsset }) {
  const first = asset.renditions[0];
  const video = asset.modality === "video";
  return (
    <figure className="flex w-48 flex-col gap-1.5" data-testid="package-media">
      {first ? (
        // eslint-disable-next-line @next/next/no-img-element -- a stored rendition streamed by api
        <img
          src={mediaContentUrl(first.media_id, video ? "poster" : "preview")}
          alt={`${asset.modality} ${asset.concept_id ?? ""} at ${first.aspect_ratio}`}
          width={first.width}
          height={first.height}
          loading="lazy"
          className="h-auto w-full rounded-token border bg-surface object-contain"
        />
      ) : (
        <div className="flex aspect-square items-center justify-center rounded-token border border-dashed text-xs text-fg-muted">
          No rendition
        </div>
      )}
      <figcaption className="flex flex-wrap items-center gap-1 text-xs text-fg-muted tabular-nums">
        {video ? <Film className="size-3" aria-hidden /> : <ImageIcon className="size-3" aria-hidden />}
        {asset.renditions.map((r) => r.aspect_ratio).join(" · ") || asset.modality}
        {asset.generated_by_ai ? (
          <Badge className="ml-auto" data-testid="ai-generated">
            <span title={asset.provenance.model_id ?? undefined}>AI-generated</span>
          </Badge>
        ) : null}
      </figcaption>
    </figure>
  );
}

/**
 * What a package ships, campaign by campaign — the RSAs with their text, the
 * extensions and the media — as the package records it (§12.3). Read-only.
 */
export function PackageContents({ campaigns }: { campaigns: CampaignCreative[] }) {
  return (
    <div className="flex flex-col gap-8">
      {campaigns.map((campaign) => {
        const byId = new Map(campaign.text_assets.map((asset) => [asset.asset_id, asset]));
        const media = [...campaign.media, ...campaign.logos];
        const extensions = Object.entries(EXTENSION_KIND)
          .map(([key, label]) => ({
            label,
            items: (campaign.extensions[key as keyof typeof campaign.extensions] as string[] | null) ?? [],
          }))
          .filter((group) => group.items.length > 0);
        const leadForm = campaign.extensions.lead_form ? byId.get(campaign.extensions.lead_form) : undefined;
        return (
          <section key={campaign.campaign_ref} className="flex flex-col gap-4" data-testid="package-campaign">
            <h3 className="flex flex-wrap items-baseline gap-2 text-sm font-medium text-fg">
              {campaign.campaign_ref}
              <span className="font-normal text-fg-muted">
                {campaign.campaign_type.replace(/_/g, " ")} · {campaign.ads.length} {campaign.ads.length === 1 ? "RSA" : "RSAs"} ·{" "}
                {campaign.text_assets.length} text {campaign.text_assets.length === 1 ? "asset" : "assets"} · {media.length} media
              </span>
            </h3>
            {campaign.ads.map((ad) => (
              <AdTable key={ad.ad_ref} ad={ad} byId={byId} />
            ))}
            {extensions.length > 0 || leadForm ? (
              <div className="flex flex-col gap-3">
                {extensions.map((group) => (
                  <div key={group.label} className="flex flex-col gap-1">
                    <p className="text-xs font-medium text-fg-subtle">{group.label}</p>
                    <ul className="flex flex-wrap gap-2">
                      {group.items.map((id) => (
                        <li key={id} className="rounded-token border px-2.5 py-1 text-sm text-fg">
                          {assetText(byId.get(id))}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
                {leadForm ? (
                  <div className="flex flex-col gap-1">
                    <p className="text-xs font-medium text-fg-subtle">Lead form</p>
                    <p className="text-sm text-fg">{assetText(leadForm)}</p>
                  </div>
                ) : null}
              </div>
            ) : null}
            {media.length > 0 ? (
              <div className="flex flex-wrap gap-4">
                {media.map((asset) => (
                  <MediaThumb key={asset.asset_id} asset={asset} />
                ))}
              </div>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}
