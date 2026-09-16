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

export function AcceptInviteForm({ token, email }: { token: string; email: string }) {
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
    if (!name) found.name = "Tell us what to call you.";
    if (password.length < MIN_PASSWORD_LENGTH) {
      found.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
    }
    if (confirm !== password) found.confirm = "These two don't match.";
    if (found.name || found.password || found.confirm) {
      setErrors(found);
      return;
    }

    setPending(true);
    setErrors({});
    try {
      await acceptInvite(token, name, password);
      router.replace("/");
      router.refresh();
    } catch (error) {
      setPending(false);
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

      <Button type="submit" disabled={pending} className="mt-1 h-10 w-full">
        {pending ? <Spinner label="Creating your account" /> : null}
        {pending ? "Setting up…" : "Create account"}
      </Button>
    </form>
  );
}
