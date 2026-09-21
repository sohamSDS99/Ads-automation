"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, acceptInvite } from "@/lib/api";

/** Mirrors `agent.auth.passwords.MIN_LENGTH`; the API is the one that enforces it. */
const MIN_PASSWORD_LENGTH = 12;

type Errors = {
  name?: string;
  password?: string;
  confirm?: string;
  form?: { title: string; detail: string };
};

/**
 * Accept an invite — two forms wearing one coat.
 *
 * A new person sets a name and a password. Someone who already has an account
 * is joining a second workspace, so the only question is whether the password
 * they type is the one on their account: the link grants this workspace, and
 * must never be able to change a credential.
 */
export function AcceptInviteForm({
  token,
  email,
  hasAccount = false,
  workspaceName = "this workspace",
}: {
  token: string;
  email: string;
  hasAccount?: boolean;
  workspaceName?: string;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [errors, setErrors] = useState<Errors>({});

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    const password = String(data.get("password") ?? "");
    const confirm = String(data.get("confirm") ?? "");

    const found: Errors = {};
    if (!hasAccount) {
      if (!name) found.name = "Tell us what to call you.";
      // Only a *new* password has to clear the policy. Holding an existing one
      // to it here would refuse the correct password of an account that
      // predates the rule, on a screen that cannot change it.
      if (password.length < MIN_PASSWORD_LENGTH) {
        found.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
      }
      if (confirm !== password) found.confirm = "These two don't match.";
    } else if (!password) {
      found.password = "Enter the password you sign in with.";
    }
    if (found.name || found.password || found.confirm) {
      setErrors(found);
      return;
    }

    setPending(true);
    setErrors({});
    try {
      await acceptInvite(token, password, hasAccount ? undefined : name);
      router.replace("/");
      router.refresh();
    } catch (error) {
      setPending(false);
      if (error instanceof ApiError && error.status === 403 && hasAccount) {
        setErrors({ password: error.detail });
        return;
      }
      if (error instanceof ApiError && error.status === 422) {
        // The API's password policy is stricter than the length check above:
        // it also rejects common passwords. Show its reason on the field.
        setErrors({ password: error.detail });
        return;
      }
      if (error instanceof ApiError && error.problem) {
        setErrors({ form: { title: error.problem.title, detail: error.detail } });
        return;
      }
      setErrors({
        form: {
          title: "Could not reach the server",
          detail: "Check your connection and try again.",
        },
      });
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-3">
      {errors.form ? (
        <Alert tone="error" title={errors.form.title}>
          {errors.form.detail}
        </Alert>
      ) : null}

      {/* Present, hidden, and filled in: password managers need the account
          this credential belongs to, and the address is not editable here. */}
      <input type="hidden" name="email" value={email} autoComplete="username" readOnly />

      {hasAccount ? (
        <Field
          label="Your password"
          name="password"
          type="password"
          autoComplete="current-password"
          autoFocus
          disabled={pending}
          error={errors.password}
          hint="The one you already sign in with. It is not changed."
        />
      ) : (
        <>
          <Field
            label="Your name"
            name="name"
            autoComplete="name"
            autoFocus
            placeholder="Alex Moreno"
            disabled={pending}
            error={errors.name}
            hint="Shown on approvals and run history."
          />

          <Field
            label="Password"
            name="password"
            type="password"
            autoComplete="new-password"
            disabled={pending}
            error={errors.password}
            hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
          />

          <Field
            label="Confirm password"
            name="confirm"
            type="password"
            autoComplete="new-password"
            disabled={pending}
            error={errors.confirm}
          />
        </>
      )}

      <Button type="submit" disabled={pending} className="mt-1 h-10 w-full">
        {pending ? <Spinner label={hasAccount ? "Joining" : "Creating your account"} /> : null}
        {pending
          ? hasAccount
            ? "Joining…"
            : "Setting up…"
          : hasAccount
            ? `Join ${workspaceName}`
            : "Create account"}
      </Button>
    </form>
  );
}
