import { NextResponse, type NextRequest } from "next/server";

/**
 * Keeps signed-out browsers off the application shell.
 *
 * Presence of the session cookie is all this checks, and all it can check —
 * middleware runs on the edge with no database and no Redis. It exists to send
 * people to `/login` instead of rendering a shell that will fail its first
 * fetch; the real decision is the API's, on every request (PRD §18 law 6).
 */
const SESSION_COOKIE = "ara_session";

/** Reachable without a session. Everything else redirects. */
const PUBLIC_PATHS = ["/login", "/invite"];

export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  if (PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`))) {
    return NextResponse.next();
  }

  if (request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.next();
  }

  const login = new URL("/login", request.url);
  // So signing in lands where they were headed, not on the default page.
  if (pathname !== "/") login.searchParams.set("next", `${pathname}${search}`);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: [
    // Everything except Next's own assets, the API rewrite, the file-server
    // rewrite (each file there carries its own signed token, and a `<video>`'s
    // range request must never be answered with a login redirect), and static
    // files.
    "/((?!api/|files/|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
