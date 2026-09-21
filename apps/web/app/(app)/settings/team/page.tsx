"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UserMinus, UserPlus } from "lucide-react";
import { useState } from "react";

import { InviteResult } from "@/components/settings/invite-result";
import { ReissueLinkButton } from "@/components/settings/reissue-link-button";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError, type UserSummary } from "@/lib/api";
import {
  inviteUser,
  isLastActiveAdmin,
  reissueInvite,
  removeMember,
  updateUser,
  type InviteCreated,
} from "@/lib/api/users";
import { relativeTime } from "@/lib/format";
import { ROLE_DESCRIPTION, ROLE_LABEL, type Role } from "@/lib/permissions";
import { errorMessage, keys, useUsers } from "@/lib/queries";
import { useSession } from "@/lib/session";

const ROLES: Role[] = ["admin", "operator", "approver", "viewer"];

export default function TeamPage() {
  const users = useUsers();
  const error = errorMessage(users);
  const [inviting, setInviting] = useState(false);

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="The team could not be loaded">
          {error}
        </Alert>
      ) : null}

      <Card>
        <CardHeader
          title="Team"
          description="Everyone with access to this workspace. Roles are per workspace: the same person can be an admin here and a viewer elsewhere."
          actions={
            <Button size="sm" onClick={() => setInviting(true)}>
              <UserPlus aria-hidden />
              Invite
            </Button>
          }
        />
        {users.isPending ? (
          <CardBody>
            <Skeleton className="h-40 w-full" />
          </CardBody>
        ) : (
          <Table label="Workspace members">
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Role</Th>
                <Th>Status</Th>
                <Th>Last sign-in</Th>
                <Th className="text-right">Actions</Th>
              </tr>
            </thead>
            <tbody>
              {(users.data?.users ?? []).map((user) => (
                <MemberRow
                  key={user.id}
                  user={user}
                  lastAdmin={isLastActiveAdmin(users.data?.users ?? [], user.id)}
                />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <InviteDialog open={inviting} onOpenChange={setInviting} />
    </div>
  );
}

function MemberRow({ user, lastAdmin }: { user: UserSummary; lastAdmin: boolean }) {
  const queryClient = useQueryClient();
  const session = useSession();
  const [confirmingDisable, setConfirmingDisable] = useState(false);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const isSelf = session.user.id === user.id;

  const patch = useMutation({
    mutationFn: (body: { role?: Role; status?: "active" | "disabled" }) =>
      updateUser(user.id, body),
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({ queryKey: keys.users });
      toast.success(`${updated.name} updated`);
    },
    onError: (error) =>
      toast.error("Not changed", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const remove = useMutation({
    mutationFn: () => removeMember(user.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.users });
      toast.success(`${user.name} removed from this workspace`, {
        description: "Their account and everything they did here are untouched.",
      });
    },
    onError: (error) =>
      toast.error("Not removed", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  // Two controls could lock everyone out of the workspace. Both are disabled
  // with the reason attached rather than allowed and then refused.
  const lockReason = lastAdmin
    ? "This is the only active admin. Promote someone else first."
    : null;

  return (
    <Tr>
      <Td>
        <p className="font-medium text-fg">
          {user.name}
          {isSelf ? <span className="ml-2 text-xs text-fg-subtle">you</span> : null}
        </p>
        <p className="text-xs text-fg-subtle">{user.email}</p>
      </Td>
      <Td>
        <Tooltip content={lockReason} wrapDisabled={Boolean(lockReason)}>
          <Select
            value={user.role}
            aria-label={`Role for ${user.name}`}
            disabled={Boolean(lockReason) || patch.isPending}
            onChange={(event) => patch.mutate({ role: event.target.value as Role })}
            className="w-40"
          >
            {ROLES.map((role) => (
              <option key={role} value={role}>
                {ROLE_LABEL[role]}
              </option>
            ))}
          </Select>
        </Tooltip>
        <p className="mt-1 max-w-48 text-xs text-fg-subtle">{ROLE_DESCRIPTION[user.role]}</p>
      </Td>
      <Td className="whitespace-nowrap">
        {user.status === "active" ? (
          <span className="text-fg">Active</span>
        ) : user.status === "invited" ? (
          <span className="text-fg-muted">Invited</span>
        ) : (
          <span className="text-fg-subtle">Disabled</span>
        )}
      </Td>
      <Td className="whitespace-nowrap text-fg-muted">
        {user.last_login_at ? relativeTime(user.last_login_at) : "Never"}
      </Td>
      <Td className="whitespace-nowrap text-right">
        <div className="inline-flex flex-wrap items-center justify-end gap-1.5">
          {/* The link is the whole onboarding step here, and it was shown
              once. Offered on exactly the rows where it means something. */}
          {user.status === "invited" ? (
            <ReissueLinkButton name={user.name} reissue={() => reissueInvite(user.id)} />
          ) : null}
          {user.status === "disabled" ? (
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
          {/* Two different endings, offered separately because they mean
              different things: Disable is "they may come back", Remove is
              "they have moved to another team". */}
          <Tooltip content={lockReason} wrapDisabled={Boolean(lockReason)}>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Remove ${user.name} from this workspace`}
              disabled={Boolean(lockReason) || remove.isPending}
              onClick={() => setConfirmingRemove(true)}
            >
              <UserMinus aria-hidden />
            </Button>
          </Tooltip>
        </div>

        {/* Portalled by Radix, so it renders nothing inline — it lives in this
            cell only to stay inside valid table markup. */}
        <ConfirmDialog
          open={confirmingDisable}
          onOpenChange={setConfirmingDisable}
          title={`Disable ${user.name}?`}
          description="They are signed out everywhere and cannot sign back in."
          body={
            <p className="text-sm text-fg-muted">
              Every session they have open stops working on its next request. Their name stays on
              everything they did — the account is disabled, not deleted.
              {isSelf ? " This is your own account: you will be signed out." : ""}
            </p>
          }
          confirmLabel="Disable member"
          destructive
          onConfirm={() => patch.mutateAsync({ status: "disabled" }).then(() => undefined)}
        />

        <ConfirmDialog
          open={confirmingRemove}
          onOpenChange={setConfirmingRemove}
          title={`Remove ${user.name} from this workspace?`}
          description="They keep their account and every other workspace they are in."
          body={
            <p className="text-sm text-fg-muted">
              This workspace disappears from their switcher and any session they have open in it
              stops working. Their name stays on every project, run and approval they touched
              here, and they can be invited back at any time.
              {isSelf ? " This is your own membership: you will lose access to this workspace." : ""}
            </p>
          }
          confirmLabel="Remove from workspace"
          destructive
          onConfirm={() => remove.mutateAsync().then(() => undefined)}
        />
      </Td>
    </Tr>
  );
}

/**
 * Invite someone.
 *
 * The link is shown after the invite is created whether or not the email went
 * out, because that is the one moment it exists in plaintext — and when SMTP is
 * unconfigured, copying it is the whole delivery mechanism (PRD §16).
 */
function InviteDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const [error, setError] = useState<string | null>(null);

  const invite = useMutation({
    mutationFn: () =>
      inviteUser({
        email: email.trim(),
        // Omitted rather than sent empty: the API fills in a readable
        // placeholder from the address, and the person replaces it on accept.
        ...(name.trim() ? { name: name.trim() } : {}),
        role,
      }),
    onSuccess: async (result) => {
      setCreated(result);
      setError(null);
      await queryClient.invalidateQueries({ queryKey: keys.users });
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The invite could not be created."),
  });

  function close() {
    onOpenChange(false);
    setCreated(null);
    setEmail("");
    setName("");
    setRole("viewer");
    setError(null);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent
        title={created ? "Invite created" : "Invite someone"}
        description={
          created
            ? undefined
            : "An email address is all you need. They set their own name and password when they accept — there is no public signup."
        }
      >
        {created ? (
          <>
            <DialogBody>
              <InviteResult invite={created} />
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
              invite.mutate();
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
              />
              <Field
                label="Name (optional)"
                value={name}
                maxLength={120}
                onChange={(event) => setName(event.target.value)}
                error={error ?? undefined}
                hint="Leave it blank and they fill it in themselves. Guessing how a colleague spells their own name is how a placeholder survives for years."
              />
              <div className="flex flex-col gap-1.5">
                <label htmlFor="invite-role" className="text-sm font-medium text-fg">
                  Role
                </label>
                <Select
                  id="invite-role"
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
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" disabled={invite.isPending || !email.trim()}>
                {invite.isPending ? <Spinner label="Inviting" /> : null}
                Send invite
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
