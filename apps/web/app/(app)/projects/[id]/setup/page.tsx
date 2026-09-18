import { redirect } from "next/navigation";

/**
 * Where setup used to live.
 *
 * Kept as a redirect rather than deleted: this path is in bookmarks, in the
 * browser checks under `scripts/`, and in the toast that fires after a project
 * is created. A 404 for a link that worked last week is a worse answer than
 * landing on the tab that now owns the same fields.
 */
export default async function ProjectSetupRedirect({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/settings/context?project=${id}`);
}
