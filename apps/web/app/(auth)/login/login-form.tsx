"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, login } from "@/lib/api";

type FormState = {
  email?: string;
  password?: string;
  form?: { title: string; detail: string; tone?: "error" | "warning" };
};

export function LoginForm({ next }: { next: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [errors, setErrors] = useState<FormState>({});

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const email = String(data.get("email") ?? "").trim();
    const password = String(data.get("password") ?? "");

    // Checked here only so the browser does not round-trip an obviously empty
    // form; the API validates both again.
    const missing: FormState = {};
    if (!email) missing.email = "Enter your email address.";
    if (!password) missing.password = "Enter your password.";
    if (missing.email || missing.password) {
      setErrors(missing);
      return;
    }

    setPending(true);
    setErrors({});
    try {
      await login(email, password);
      router.replace(next);
      // The authenticated layout reads the session on the server, so the tree
      // has to be re-rendered rather than just re-routed.
      router.refresh();
    } catch (error) {
      setPending(false);
      if (error instanceof ApiError && error.problem) {
        // A correct password with no workspace behind it is not a sign-in
        // failure, and colouring it like one sends someone round the
        // password-reset loop for an account that works. It is amber, and it
        // says who to ask.
        setErrors({
          form: {
            title: error.problem.title,
            detail: error.detail,
            tone: error.status === 403 ? "warning" : "error",
          },
        });
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
        <Alert tone={errors.form.tone ?? "error"} title={errors.form.title}>
          {errors.form.detail}
        </Alert>
      ) : null}

      <Field
        label="Email"
        name="email"
        type="email"
        autoComplete="username"
        autoFocus
        spellCheck={false}
        placeholder="you@company.com"
        disabled={pending}
        error={errors.email}
      />

      <Field
        label="Password"
        name="password"
        type="password"
        autoComplete="current-password"
        disabled={pending}
        error={errors.password}
      />

      <Button type="submit" disabled={pending} className="mt-1 h-10 w-full">
        {pending ? <Spinner label="Signing in" /> : null}
        {pending ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
