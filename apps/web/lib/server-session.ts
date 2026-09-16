/**
 * Reading the session during a server render.
 *
 * This is the one place an absolute API address is used, and it is server-side
 * only: `API_INTERNAL_URL` names the private-network host, is never bundled for
 * the browser, and there is no public env var carrying an API address anywhere
 * in this repo (PRD §13.1). A server component cannot go through the Next
 * rewrite — there is no origin to be relative to — so it calls the API the same
 * way the rewrite does.
 */
import { cookies } from "next/headers";

import type { Me } from "@/lib/api";

const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://api:8000";

/** The signed-in user, or null if the cookie is missing, expired or revoked. */
export async function getServerSession(): Promise<Me | null> {
  const jar = await cookies();
  const cookieHeader = jar
    .getAll()
    .map((entry) => `${entry.name}=${entry.value}`)
    .join("; ");
  if (!cookieHeader) return null;

  try {
    const response = await fetch(`${API_INTERNAL_URL}/api/v1/auth/me`, {
      headers: { cookie: cookieHeader, accept: "application/json" },
      // Never cached: a revoked session must stop working on the next request,
      // and this call is what notices.
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as Me;
  } catch {
    // The API being unreachable is not the same as being signed out, but from
    // here it is indistinguishable — and treating it as signed out fails closed.
    return null;
  }
}
