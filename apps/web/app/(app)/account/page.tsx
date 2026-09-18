"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Monitor } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ConnectionCard } from "@/components/settings/connection-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { RoleBadge } from "@/components/ui/role-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { changePassword, revokeSession, type SessionSummary } from "@/lib/api/account";
import { credentialFor } from "@/lib/api/credentials";
import { ROLE_DESCRIPTION } from "@/lib/permissions";
import { absoluteTime, relativeTime } from "@/lib/format";
import { errorMessage, keys, useCredentials, useSessions } from "@/lib/queries";
import { useSession } from "@/lib/session";

export default function AccountPage() {
  const { user } = useSession();

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Account</h1>
        <p className="mt-1 text-sm text-fg-muted">
          {user.name} · {user.email}
        </p>
        <div className="mt-2 flex items-center gap-2">
          <RoleBadge role={user.role} />
          <span className="text-xs text-fg-subtle">{ROLE_DESCRIPTION[user.role]}</span>
        </div>
      </header>

      <PasswordCard />
      <SessionsCard />
      <PersonalKeyCard />
    </div>
  );
}

function PasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  const mismatch = confirm.length > 0 && next !== confirm;

  const change = useMutation({
    mutationFn: () => changePassword(current, next),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirm("");
      setError(null);
      toast.success("Password changed", {
        description: "Your other sessions were signed out.",
      });
      // Changing a password drops every other session; refreshing makes sure
      // this one is still the one the server thinks it is.
      router.refresh();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "The password could not be changed."),
  });

  return (
    <Card>
      <CardHeader
        title="Password"
        description="Changing it signs out every other browser you are signed in on."
      />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          change.mutate();
        }}
      >
        <CardBody className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Current password"
            type="password"
            autoComplete="current-password"
            value={current}
            required
            onChange={(event) => setCurrent(event.target.value)}
            className="sm:col-span-2"
          />
          <Field
            label="New password"
            type="password"
            autoComplete="new-password"
            value={next}
            required
            onChange={(event) => setNext(event.target.value)}
            error={error ?? undefined}
            hint="Long beats complicated. A passphrase is fine."
          />
          <Field
            label="Repeat new password"
            type="password"
            autoComplete="new-password"
            value={confirm}
            required
            onChange={(event) => setConfirm(event.target.value)}
            error={mismatch ? "These two do not match." : undefined}
          />
        </CardBody>
        <CardFooter>
          <Button
            type="submit"
            disabled={change.isPending || !current || !next || mismatch || !confirm}
          >
            {change.isPending ? <Spinner label="Changing" /> : null}
            Change password
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}

function SessionsCard() {
  const sessions = useSessions();
  const queryClient = useQueryClient();
  const error = errorMessage(sessions);

  const revoke = useMutation({
    mutationFn: (id: string) => revokeSession(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.sessions });
      toast.success("Signed out on that device");
    },
    onError: (err) =>
      toast.error("Not signed out", {
        description: err instanceof ApiError ? err.detail : "Try again in a moment.",
      }),
  });

  const others = (sessions.data?.sessions ?? []).filter((item) => !item.current);

  return (
    <Card>
      <CardHeader
        title="Signed in on"
        description="Every browser holding a live session. Revoking one takes effect on its next request."
        actions={
          others.length ? (
            <Button
              size="sm"
              variant="secondary"
              disabled={revoke.isPending}
              onClick={async () => {
                for (const item of others) await revoke.mutateAsync(item.id);
              }}
            >
              Sign out everywhere else
            </Button>
          ) : null
        }
      />
      {error ? (
        <CardBody>
          <Alert tone="error" title="Sessions could not be loaded">
            {error}
          </Alert>
        </CardBody>
      ) : null}
      {sessions.isPending ? (
        <CardBody>
          <Skeleton className="h-24 w-full" />
        </CardBody>
      ) : null}
      {sessions.data ? (
        <Table label="Active sessions">
          <thead>
            <tr>
              <Th>Device</Th>
              <Th>Address</Th>
              <Th>Last seen</Th>
              <Th className="text-right">Actions</Th>
            </tr>
          </thead>
          <tbody>
            {sessions.data.sessions.map((item) => (
              <SessionRow key={item.id} session={item} onRevoke={() => revoke.mutate(item.id)} />
            ))}
          </tbody>
        </Table>
      ) : null}
    </Card>
  );
}

function SessionRow({
  session,
  onRevoke,
}: {
  session: SessionSummary;
  onRevoke: () => void;
}) {
  return (
    <Tr>
      <Td>
        <span className="flex items-center gap-2">
          <Monitor className="size-4 shrink-0 text-fg-subtle" aria-hidden />
          <span className="max-w-72 truncate text-fg-muted">
            {session.user_agent ?? "Unknown browser"}
          </span>
          {session.current ? (
            <span className="shrink-0 text-xs text-fg-subtle">this one</span>
          ) : null}
        </span>
      </Td>
      <Td className="whitespace-nowrap font-mono text-xs text-fg-subtle">{session.ip ?? "—"}</Td>
      <Td className="whitespace-nowrap text-fg-muted" title={absoluteTime(session.last_seen_at)}>
        {relativeTime(session.last_seen_at)}
      </Td>
      <Td className="whitespace-nowrap text-right">
        {session.current ? (
          <span className="text-xs text-fg-subtle">Use Sign out</span>
        ) : (
          <Button size="sm" variant="secondary" onClick={onRevoke}>
            Revoke
          </Button>
        )}
      </Td>
    </Tr>
  );
}

/**
 * A personal OpenRouter key.
 *
 * Optional, and it only applies to runs this person triggers (PRD §13.4 H).
 * Anyone may set one — it is their own secret and their own spend, which is why
 * this is the one credential that does not need the admin permission.
 */
function PersonalKeyCard() {
  const credentials = useCredentials();
  const spec = (credentials.data?.kinds ?? []).find((kind) => kind.kind === "openrouter");
  const mine = credentialFor(credentials.data?.credentials ?? [], "openrouter", "user");

  return (
    <Card>
      <CardHeader
        title="Your own OpenRouter key"
        description="Optional. When set, runs you launch bill to this key instead of the workspace's."
      />
      <CardBody>
        {spec ? (
          <div className="max-w-sm">
            <ConnectionCard
              spec={spec}
              credential={mine}
              canWrite
              scope="user"
              description="Only the runs you launch use this. Everyone else's keep billing to the workspace key."
            />
          </div>
        ) : (
          <div className="flex gap-3 text-sm text-fg-muted">
            <KeyRound className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
            Loading the key vault…
          </div>
        )}
      </CardBody>
    </Card>
  );
}
