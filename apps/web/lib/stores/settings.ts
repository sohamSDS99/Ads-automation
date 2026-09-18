/**
 * Which project the project-scoped settings tabs are looking at.
 *
 * In a store rather than in each page's state because it has to survive a tab
 * change: business context and approvers are two routes, and being asked which
 * project you meant twice in a row is exactly the friction that made the old
 * wizard feel like work.
 *
 * Mirrored into `?project=` so a link to this screen opens on the same project
 * someone else was looking at. Written with `history.replaceState` and read in
 * an effect rather than through `useSearchParams`, so selecting a project does
 * not push a history entry and this does not drag a Suspense boundary onto
 * every settings screen.
 */
"use client";

import { useEffect } from "react";
import { create } from "zustand";

import type { ProjectSummary } from "@/lib/api/projects";

const PARAM = "project";

type SettingsState = {
  projectId: string | null;
  select: (id: string) => void;
};

export const useSettingsProjectStore = create<SettingsState>((set) => ({
  projectId: null,
  select: (id) => {
    set({ projectId: id });
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    params.set(PARAM, id);
    window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
  },
}));

/**
 * The project these tabs are editing, once the list has loaded.
 *
 * `?project=` wins whenever it names a project that exists, so a link from a
 * project's own page opens on that project even if this browser was last
 * looking at another one. Otherwise whatever is already selected is kept, and
 * failing that the first project — which is the whole decision for a workspace
 * that only has one.
 *
 * The chosen id is written back into the URL on every tab, including the tabs
 * reached by a `<Link>` that carried no query, so the address bar is always
 * shareable. That write is idempotent: `select` stores the same string, the
 * selector returns the same string, and nothing re-renders.
 *
 * Returns null while the list is still loading and when the selection names
 * nothing — a project deleted in another tab, an id typed in by hand. The
 * caller renders its empty state for both, because both mean the same thing:
 * there is nothing here to edit.
 */
export function useSelectedProject(projects: ProjectSummary[] | undefined) {
  const projectId = useSettingsProjectStore((state) => state.projectId);
  const select = useSettingsProjectStore((state) => state.select);
  const known = (id: string | null) => Boolean(id) && (projects ?? []).some((p) => p.id === id);

  useEffect(() => {
    const first = projects?.[0];
    if (!first) return;
    const inUrl = new URLSearchParams(window.location.search).get(PARAM);
    const isKnown = (id: string | null) => Boolean(id) && projects.some((p) => p.id === id);
    const desired = isKnown(inUrl) ? (inUrl as string) : isKnown(projectId) ? (projectId as string) : first.id;
    if (desired !== projectId || inUrl !== desired) select(desired);
  }, [projects, projectId, select]);

  return { projectId: known(projectId) ? projectId : null, select };
}
