"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState } from "react";

import { NoAccess } from "@/components/auth/no-access";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import { updateAccount, type AccountSummary } from "@/lib/api/platform";
import { relativeTime } from "@/lib/format";
import { ROLE_LABEL } from "@/lib/permissions";
import { errorMessage, keys, useAccounts } from "@/lib/queries";
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
  const allowed = has("platform_admin");
  const accounts = useAccounts(allowed);
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
      <Td>
        <p className="flex items-center gap-2 font-medium text-fg">
          {account.name}
          {isSelf ? <span className="text-xs text-fg-subtle">you</span> : null}
          {account.is_superadmin ? (
            <span className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium text-fg-muted">
              <ShieldCheck className="size-3" aria-hidden />
              System admin
            </span>
          ) : null}
        </p>
        <p className="text-xs text-fg-subtle">
          {account.email}
          {account.status === "invited" ? " · invite not accepted" : ""}
          {account.status === "disabled" ? " · disabled" : ""}
        </p>
      </Td>
      {/* Capped on the inner element, not the cell: `max-width` on a `td` is
          ignored under `table-layout: auto`, and `Table` is `min-w-max`, so an
          unbounded list of chips pushed the actions column off the right edge
          of the card at 1440 and a cap on the `<td>` changed nothing. */}
      <Td>
        {account.workspaces.length === 0 ? (
          <span className="text-xs text-fg-subtle">
            {account.is_superadmin ? "Every workspace" : "None — cannot sign in anywhere"}
          </span>
        ) : (
          <ul className="flex max-w-sm flex-wrap gap-1.5">
            {account.workspaces.map((workspace) => (
              <li
                key={workspace.id}
                className="rounded-full border px-2 py-0.5 text-xs text-fg-muted"
              >
                {workspace.name}
                <span className="text-fg-subtle"> · {ROLE_LABEL[workspace.role]}</span>
              </li>
            ))}
          </ul>
        )}
      </Td>
      <Td className="whitespace-nowrap text-fg-muted">
        {account.last_login_at ? relativeTime(account.last_login_at) : "Never"}
      </Td>
      <Td className="text-right">
        {/* Fixed width for the same reason: inside a `min-w-max` table a
            flex row sizes to max-content and never wraps, so the wrap has to
            be given something to wrap against. */}
        <div className="ml-auto flex w-44 flex-wrap items-center justify-end gap-1.5">
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
