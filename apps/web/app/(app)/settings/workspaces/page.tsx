"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArchiveRestore, Building2, Check, Pencil, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { NoAccess } from "@/components/auth/no-access";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import {
  archiveWorkspace,
  createWorkspace,
  renameWorkspace,
  restoreWorkspace,
  type WorkspaceSummary,
} from "@/lib/api/workspace";
import { shortDate } from "@/lib/format";
import { errorMessage, keys, useSwitchWorkspace, useWorkspaces } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * Every workspace on the installation — one company, or one business function
 * inside one — and the controls that only the system administrator holds.
 *
 * This tab is the counterpart to the Workspace tab, not a duplicate of it.
 * That one configures the workspace you are in and belongs to its own admins;
 * this one creates, renames and closes workspaces across the whole
 * installation, which is a different job held by a different account.
 */
export default function WorkspacesPage() {
  const { user, has } = useSession();
  const params = useSearchParams();
  const [creating, setCreating] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const allowed = has("platform_admin");
  const workspaces = useWorkspaces(showArchived, allowed);
  const error = errorMessage(workspaces);

  // The switcher's "New workspace" lands here with the dialog already open,
  // so that path is one click rather than three.
  useEffect(() => {
    if (params.get("new") === "1") setCreating(true);
  }, [params]);

  if (!allowed) {
    return <NoAccess permission="platform_admin" role={user.role} what="workspace administration" />;
  }

  const rows = workspaces.data?.workspaces ?? [];
  const live = rows.filter((row) => row.archived_at === null);

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Workspaces could not be loaded">
          {error}
        </Alert>
      ) : null}

      <Card>
        <CardHeader
          title="Workspaces"
          description="One per company, or one per business function. Everything inside a workspace — projects, runs, evidence, people — is visible only from within it."
          actions={
            <>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowArchived((value) => !value)}
                aria-pressed={showArchived}
              >
                {showArchived ? "Hide archived" : "Show archived"}
              </Button>
              <Button size="sm" onClick={() => setCreating(true)}>
                <Building2 aria-hidden />
                New workspace
              </Button>
            </>
          }
        />
        {workspaces.isPending ? (
          <CardBody>
            <Skeleton className="h-40 w-full" />
          </CardBody>
        ) : (
          <Table label="Workspaces on this installation">
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Members</Th>
                <Th>Created</Th>
                <Th className="text-right">Actions</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((workspace) => (
                <WorkspaceRow
                  key={workspace.id}
                  workspace={workspace}
                  lastLive={live.length === 1 && workspace.archived_at === null}
                />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <CreateWorkspaceDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}

function WorkspaceRow({
  workspace,
  lastLive,
}: {
  workspace: WorkspaceSummary;
  lastLive: boolean;
}) {
  const queryClient = useQueryClient();
  const switcher = useSwitchWorkspace();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(workspace.name);
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  const archived = workspace.archived_at !== null;

  function invalidate() {
    return queryClient.invalidateQueries({ queryKey: ["workspaces"] });
  }

  const rename = useMutation({
    mutationFn: () => renameWorkspace(workspace.id, name.trim()),
    onSuccess: async (updated) => {
      setRenaming(false);
      await invalidate();
      await queryClient.invalidateQueries({ queryKey: keys.workspace });
      toast.success(`Renamed to ${updated.name}`);
    },
    onError: (error) =>
      toast.error("Not renamed", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const archive = useMutation({
    mutationFn: () => archiveWorkspace(workspace.id),
    onSuccess: async (result) => {
      await invalidate();
      toast.success(`${result.name} archived`, {
        description:
          result.sessions_ended > 0
            ? `${result.sessions_ended} open session${result.sessions_ended === 1 ? "" : "s"} ended. Nothing inside was deleted.`
            : "Nothing inside was deleted — restore it any time.",
      });
    },
    onError: (error) =>
      toast.error("Not archived", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const restore = useMutation({
    mutationFn: () => restoreWorkspace(workspace.id),
    onSuccess: async () => {
      await invalidate();
      toast.success(`${workspace.name} restored`);
    },
    onError: (error) =>
      toast.error("Not restored", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const busy = rename.isPending || archive.isPending || restore.isPending;
  const lastReason = lastLive
    ? "This is the only workspace left. Create another one before archiving it."
    : null;

  return (
    <Tr className={archived ? "text-fg-subtle" : undefined}>
      <Td>
        {renaming ? (
          <form
            className="flex items-center gap-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              rename.mutate();
            }}
          >
            <Input
              value={name}
              autoFocus
              maxLength={120}
              aria-label={`New name for ${workspace.name}`}
              onChange={(event) => setName(event.target.value)}
              className="h-8 w-52"
            />
            <Button
              type="submit"
              size="sm"
              variant="ghost"
              aria-label="Save name"
              disabled={busy || !name.trim()}
            >
              <Check aria-hidden />
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              aria-label="Cancel rename"
              onClick={() => {
                setName(workspace.name);
                setRenaming(false);
              }}
            >
              <X aria-hidden />
            </Button>
          </form>
        ) : (
          <>
            <p className="font-medium text-fg">
              {workspace.name}
              {workspace.current ? (
                <span className="ml-2 text-xs text-fg-subtle">you are here</span>
              ) : null}
              {archived ? <span className="ml-2 text-xs text-fg-subtle">archived</span> : null}
            </p>
            <p className="text-xs text-fg-subtle">
              {workspace.is_member
                ? `You are a member (${workspace.role})`
                : "You reach this as the system administrator"}
            </p>
          </>
        )}
      </Td>
      <Td data-numeric className="whitespace-nowrap">
        {workspace.member_count ?? "—"}
      </Td>
      <Td className="whitespace-nowrap text-fg-muted">{shortDate(workspace.created_at)}</Td>
      <Td className="whitespace-nowrap text-right">
        <div className="inline-flex flex-wrap items-center justify-end gap-1.5">
          {archived ? (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => restore.mutate()}
            >
              {restore.isPending ? <Spinner label="Restoring" /> : <ArchiveRestore aria-hidden />}
              Restore
            </Button>
          ) : (
            <>
              {!workspace.current ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={switcher.isPending}
                  onClick={() => switcher.mutate(workspace.id)}
                >
                  Open
                </Button>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Rename ${workspace.name}`}
                disabled={busy || renaming}
                onClick={() => setRenaming(true)}
              >
                <Pencil aria-hidden />
              </Button>
              <Tooltip content={lastReason} wrapDisabled={Boolean(lastReason)}>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy || Boolean(lastReason)}
                  onClick={() => setConfirmingArchive(true)}
                >
                  Archive
                </Button>
              </Tooltip>
            </>
          )}
        </div>

        <ConfirmDialog
          open={confirmingArchive}
          onOpenChange={setConfirmingArchive}
          title={`Archive ${workspace.name}?`}
          description="Nobody can open it until it is restored. Nothing inside is deleted."
          body={
            <p className="text-sm text-fg-muted">
              Its projects, runs, reports and audit log all stay exactly where they are, and
              restoring puts every member back. Anyone signed into it right now is signed out on
              their next request.
            </p>
          }
          confirmLabel="Archive workspace"
          destructive
          onConfirm={() => archive.mutateAsync().then(() => undefined)}
        />
      </Td>
    </Tr>
  );
}

/**
 * Create a workspace, and optionally hand it to someone.
 *
 * The admin email is optional and does the obvious thing: the new workspace
 * starts with someone able to run it, invited the ordinary way — a link, and
 * a password they choose. Leaving it blank makes a workspace only the system
 * administrator can reach, which is a fine place to start and a strange place
 * to stay, so the field says so.
 */
function CreateWorkspaceDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [error, setError] = useState<string | null>(null);

  function close() {
    onOpenChange(false);
    setName("");
    setAdminEmail("");
    setError(null);
  }

  const create = useMutation({
    mutationFn: () =>
      createWorkspace({
        name: name.trim(),
        ...(adminEmail.trim() ? { admin_email: adminEmail.trim() } : {}),
      }),
    onSuccess: async (workspace) => {
      await queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      if (workspace.admin_invited === false) {
        toast.warning(`${workspace.name} created, but the invite failed`, {
          description: `Open it and invite ${adminEmail.trim()} from the Team tab.`,
        });
      } else {
        toast.success(`${workspace.name} created`, {
          description: workspace.admin_invited
            ? `An invite is on its way to ${adminEmail.trim()}.`
            : "Open it to invite its first people.",
        });
      }
      close();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The workspace could not be created."),
  });

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent
        title="New workspace"
        description="A separate company, or a separate business function. Nothing is shared with any other workspace."
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setError(null);
            create.mutate();
          }}
        >
          <DialogBody>
            <Field
              label="Name"
              value={name}
              autoFocus
              required
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
              placeholder="Paid Search"
              error={error ?? undefined}
              hint="What the people working in it will call it."
            />
            <Field
              label="First admin (optional)"
              type="email"
              value={adminEmail}
              onChange={(event) => setAdminEmail(event.target.value)}
              placeholder="lead@company.com"
              hint="They get an invite link and set their own password. Leave blank and only you can reach it."
            />
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !name.trim()}>
              {create.isPending ? <Spinner label="Creating" /> : null}
              Create workspace
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
