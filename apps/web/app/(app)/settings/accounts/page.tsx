"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, UserPlus } from "lucide-react";
import { useState } from "react";

import { NoAccess } from "@/components/auth/no-access";
import { InviteResult } from "@/components/settings/invite-result";
import { ReissueLinkButton } from "@/components/settings/reissue-link-button";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import {
  createAccount,
  reissueAccountInvite,
  updateAccount,
  type AccountSummary,
} from "@/lib/api/platform";
import type { InviteCreated } from "@/lib/api/users";
import type { WorkspaceSummary } from "@/lib/api/workspace";
import { relativeTime } from "@/lib/format";
import { ROLE_DESCRIPTION, ROLE_LABEL, type Role } from "@/lib/permissions";
import { errorMessage, keys, useAccounts, useWorkspaces } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * Everyone on the installation, and the two things that are true of a person
 * rather than of a membership.
 *
 * Kept apart from Team on purpose. Team is the membership list of the
 * workspace you are in, and a workspace admin owns it; this is the account
 * across every workspace, and only the system administrator sees it. Merging
 * the two would put a switch that reaches every company next to a role that
 * reaches one.
 */
export default function AccountsPage() {
  const { user, has } = useSession();
  const [adding, setAdding] = useState(false);
  const allowed = has("platform_admin");
  const accounts = useAccounts(allowed);
  const workspaces = useWorkspaces(false, allowed);
  const error = errorMessage(accounts);

  if (!allowed) {
    return <NoAccess permission="platform_admin" role={user.role} what="account administration" />;
  }

  const rows = accounts.data?.accounts ?? [];
  const admins = rows.filter((row) => row.is_superadmin && row.status === "active");

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Accounts could not be loaded">
          {error}
        </Alert>
      ) : null}

      <Card>
        <CardHeader
          title="Accounts"
          description="One account per person, whatever workspaces they work in. Disabling an account locks it out of all of them at once."
          actions={
            <Button
              size="sm"
              onClick={() => setAdding(true)}
              disabled={(workspaces.data?.workspaces.length ?? 0) === 0}
            >
              <UserPlus aria-hidden />
              Add account
            </Button>
          }
        />
        {accounts.isPending ? (
          <CardBody>
            <Skeleton className="h-40 w-full" />
          </CardBody>
        ) : (
          <Table label="Accounts on this installation">
            <thead>
              <tr>
                <Th>Person</Th>
                <Th>Workspaces</Th>
                <Th>Last sign-in</Th>
                <Th className="text-right">Actions</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((account) => (
                <AccountRow
                  key={account.id}
                  account={account}
                  lastSystemAdmin={
                    account.is_superadmin && admins.length === 1 && admins[0]?.id === account.id
                  }
                />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <AddAccountDialog
        open={adding}
        onOpenChange={setAdding}
        workspaces={workspaces.data?.workspaces ?? []}
      />
    </div>
  );
}

function AccountRow({
  account,
  lastSystemAdmin,
}: {
  account: AccountSummary;
  lastSystemAdmin: boolean;
}) {
  const queryClient = useQueryClient();
  const session = useSession();
  const [confirmingDisable, setConfirmingDisable] = useState(false);
  const isSelf = session.user.id === account.id;

  const patch = useMutation({
    mutationFn: (body: { is_superadmin?: boolean; status?: "active" | "disabled" }) =>
      updateAccount(account.id, body),
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({ queryKey: keys.accounts });
      await queryClient.invalidateQueries({ queryKey: keys.users });
      toast.success(`${updated.name} updated`);
    },
    onError: (error) =>
      toast.error("Not changed", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  // The one control that could leave the installation with no way back.
  const lockReason = lastSystemAdmin
    ? "This is the only system administrator. Promote someone else first."
    : null;

  return (
    <Tr>
      {/* Capped and truncating, for the reason the `Table` docstring gives:
          `min-w-max` sizes the table to its widest cell, so an unbounded
          email column lets one long address push the actions off the right
          edge of the card. It was measured at 1024 in a 1022 container — two
          pixels, entirely at the mercy of whoever signs up next. */}
      <Td>
        <div className="max-w-xs">
        <p className="flex items-center gap-2 font-medium text-fg">
          <span className="truncate">{account.name}</span>
          {isSelf ? <span className="shrink-0 text-xs text-fg-subtle">you</span> : null}
          {account.is_superadmin ? (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium text-fg-muted">
              <ShieldCheck className="size-3" aria-hidden />
              System admin
            </span>
          ) : null}
        </p>
        {/* `title` because the address is the identifier — truncating it
            must not make it unreadable, only stop it setting the layout. */}
        <p className="truncate text-xs text-fg-subtle" title={account.email}>
          {account.email}
          {account.status === "invited" ? " · invite not accepted" : ""}
          {account.status === "disabled" ? " · disabled" : ""}
        </p>
        </div>
      </Td>
      {/* Capped on the inner element, not the cell: `max-width` on a `td` is
          ignored under `table-layout: auto`, and `Table` is `min-w-max`, so an
          unbounded list of chips pushed the actions column off the right edge
          of the card at 1440 and a cap on the `<td>` changed nothing. */}
      <Td>
        {/* 16rem, not 24rem: the actions column gained a third control
            ("Get link") and the table went 17px past its card. The chips wrap
            onto another line happily; the buttons had nowhere to go. */}
        <ul className="flex max-w-64 flex-wrap gap-1.5">
          {account.workspaces.map((workspace) => (
            <li
              key={workspace.id}
              className="rounded-full border px-2 py-0.5 text-xs text-fg-muted"
            >
              {workspace.name}
              <span className="text-fg-subtle"> · {ROLE_LABEL[workspace.role]}</span>
            </li>
          ))}
          {/* Dashed, because an unaccepted invite is not access. Naming the
              workspace is the point: the row used to say "invited" and leave
              you to guess invited to what. */}
          {account.pending.map((workspace) => (
            <li
              key={workspace.id}
              className="rounded-full border border-dashed px-2 py-0.5 text-xs text-fg-subtle"
            >
              {workspace.name}
              <span> · invited as {ROLE_LABEL[workspace.role].toLowerCase()}</span>
            </li>
          ))}
          {account.workspaces.length === 0 && account.pending.length === 0 ? (
            <li className="text-xs text-fg-subtle">
              {account.is_superadmin ? "Every workspace" : "None — cannot sign in anywhere"}
            </li>
          ) : null}
        </ul>
      </Td>
      <Td className="whitespace-nowrap text-fg-muted">
        {account.last_login_at ? relativeTime(account.last_login_at) : "Never"}
      </Td>
      <Td className="text-right">
        {/* Fixed width for the same reason: inside a `min-w-max` table a
            flex row sizes to max-content and never wraps, so the wrap has to
            be given something to wrap against. */}
        <div className="ml-auto flex w-44 flex-wrap items-center justify-end gap-1.5">
          {/* One pending invite is the ordinary case, so its workspace is
              implied; several would need choosing, and this hands back the
              first rather than guessing — so it is only offered for one. */}
          {account.pending.length === 1 ? (
            <ReissueLinkButton
              name={account.name}
              showWorkspace
              reissue={() => reissueAccountInvite(account.id, account.pending[0]!.id)}
            />
          ) : null}
          <Tooltip content={lockReason} wrapDisabled={Boolean(lockReason)}>
            <Button
              size="sm"
              variant="ghost"
              disabled={Boolean(lockReason) || patch.isPending}
              onClick={() => patch.mutate({ is_superadmin: !account.is_superadmin })}
            >
              {account.is_superadmin ? "Revoke system admin" : "Make system admin"}
            </Button>
          </Tooltip>
          {account.status === "disabled" ? (
            <Button
              size="sm"
              variant="secondary"
              disabled={patch.isPending}
              onClick={() => patch.mutate({ status: "active" })}
            >
              Enable
            </Button>
          ) : (
            <Tooltip content={lockReason} wrapDisabled={Boolean(lockReason)}>
              <Button
                size="sm"
                variant="secondary"
                disabled={Boolean(lockReason) || patch.isPending}
                onClick={() => setConfirmingDisable(true)}
              >
                Disable
              </Button>
            </Tooltip>
          )}
        </div>

        <ConfirmDialog
          open={confirmingDisable}
          onOpenChange={setConfirmingDisable}
          title={`Disable ${account.name}?`}
          description="They are signed out of every workspace and cannot sign back in anywhere."
          body={
            <p className="text-sm text-fg-muted">
              This is the whole-system lock, not a change to any one workspace. Their memberships
              are left intact, so enabling the account later puts everything back.
              {isSelf ? " This is your own account: you will be signed out." : ""}
            </p>
          }
          confirmLabel="Disable account"
          destructive
          onConfirm={() => patch.mutateAsync({ status: "disabled" }).then(() => undefined)}
        />
      </Td>
    </Tr>
  );
}

const ROLES: Role[] = ["admin", "operator", "approver", "viewer"];

/**
 * Add somebody to any workspace, without going and standing in it first.
 *
 * The Team screen already invites into the workspace you are in. What was
 * missing — and what made this whole screen read-only — was a way to populate
 * one of the *other* workspaces, which used to mean switching, inviting, and
 * switching back to express a single intention.
 *
 * Deliberately the same two-state dialog as Team's, down to the shared
 * `InviteResult` panel: the two screens are one system, and an administrator
 * who has used one should recognise the other immediately. The only extra
 * field is the one that was implicit before — which workspace.
 */
function AddAccountDialog({
  open,
  onOpenChange,
  workspaces,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaces: WorkspaceSummary[];
}) {
  const queryClient = useQueryClient();
  const { user } = useSession();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const [error, setError] = useState<string | null>(null);

  // One workspace on the installation means there is nothing to choose.
  const only = workspaces.length === 1 ? workspaces[0] : undefined;
  // Defaulted to the workspace this session is in, and not left empty. An
  // unset `<select>` still *renders* its first option, so an empty value
  // showed a workspace while disabling the submit button — a control that
  // looks ready and refuses, for a reason the screen never gave.
  const chosen = workspaceId || only?.id || user.workspace_id || workspaces[0]?.id || "";

  const add = useMutation({
    mutationFn: () =>
      createAccount({
        email: email.trim(),
        // Omitted rather than sent empty: the API fills in a readable
        // placeholder from the address, and the person replaces it on accept.
        ...(name.trim() ? { name: name.trim() } : {}),
        workspace_id: chosen,
        role,
      }),
    onSuccess: async (result) => {
      setCreated(result);
      setError(null);
      await queryClient.invalidateQueries({ queryKey: keys.accounts });
      await queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      await queryClient.invalidateQueries({ queryKey: keys.users });
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The account could not be created."),
  });

  function close() {
    onOpenChange(false);
    setCreated(null);
    setEmail("");
    setName("");
    setWorkspaceId("");
    setRole("viewer");
    setError(null);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent
        title={created ? "Account created" : "Add an account"}
        description={
          created
            ? undefined
            : "An email address is all you need. They choose their own name and password from the link — there is no public signup, and nobody but them ever sets their password."
        }
      >
        {created ? (
          <>
            <DialogBody>
              <InviteResult invite={created} showWorkspace />
            </DialogBody>
            <DialogFooter>
              <Button onClick={close}>Done</Button>
            </DialogFooter>
          </>
        ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              add.mutate();
            }}
          >
            <DialogBody>
              <Field
                label="Email"
                type="email"
                value={email}
                autoFocus
                required
                onChange={(event) => setEmail(event.target.value)}
                placeholder="name@company.com"
                error={error ?? undefined}
              />
              <Field
                label="Name (optional)"
                value={name}
                maxLength={120}
                onChange={(event) => setName(event.target.value)}
                hint="Leave it blank and they fill it in themselves."
              />

              <div className="flex flex-col gap-1.5">
                <label htmlFor="add-workspace" className="text-sm font-medium text-fg">
                  Workspace
                </label>
                <Select
                  id="add-workspace"
                  value={chosen}
                  onChange={(event) => setWorkspaceId(event.target.value)}
                  disabled={Boolean(only)}
                >
                  {workspaces.map((workspace) => (
                    <option key={workspace.id} value={workspace.id}>
                      {workspace.name}
                    </option>
                  ))}
                </Select>
                <p className="min-h-4 text-xs text-fg-muted">
                  {only
                    ? "The only workspace on this installation."
                    : "Which workspace they get access to. It does not have to be the one you are in."}
                </p>
              </div>

              <div className="flex flex-col gap-1.5">
                <label htmlFor="add-role" className="text-sm font-medium text-fg">
                  Role in that workspace
                </label>
                <Select
                  id="add-role"
                  value={role}
                  onChange={(event) => setRole(event.target.value as Role)}
                >
                  {ROLES.map((item) => (
                    <option key={item} value={item}>
                      {ROLE_LABEL[item]}
                    </option>
                  ))}
                </Select>
                <p className="min-h-4 text-xs text-fg-muted">{ROLE_DESCRIPTION[role]}</p>
              </div>

              {/* Said here because the control is elsewhere, and somebody
                  looking for it on this form should be told where it went
                  rather than conclude it does not exist. */}
              <p className="text-xs text-fg-subtle">
                To make someone a system administrator, add them first and use{" "}
                <strong>Make system admin</strong> on their row. It reaches every workspace, so
                it is its own decision rather than a checkbox here.
              </p>
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" disabled={add.isPending || !email.trim() || !chosen}>
                {add.isPending ? <Spinner label="Adding" /> : null}
                Add account
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
