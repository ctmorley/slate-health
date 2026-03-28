import client from "./client";
import type {
  LoginRequest,
  LoginResponse,
  LoginPageResponse,
  TokenResponse,
  UserProfile,
} from "../types";

export async function initiateLogin(req: LoginRequest): Promise<LoginResponse> {
  const { data } = await client.post<LoginResponse>("/api/v1/auth/login", req);
  return data;
}

/**
 * Fetch available SSO providers from the backend.
 * The backend checks its IdP configuration and returns only the providers
 * that are actually configured, so the login page can render only valid options.
 */
export async function fetchLoginProviders(): Promise<LoginPageResponse> {
  const { data } = await client.get<LoginPageResponse>("/api/v1/auth/login");
  return data;
}

/**
 * SSO callback handling note:
 *
 * The backend SAML/OIDC callback endpoints (GET /api/v1/auth/callback/saml,
 * GET /api/v1/auth/callback/oidc) are browser-redirect flows, not JSON APIs.
 * After validating the IdP assertion/code, the backend redirects to the
 * frontend login page with access_token and refresh_token as URL query
 * parameters. The frontend's LoginPage extracts these and passes them to
 * AuthContext.handleAuthCallback() for storage and user profile fetching.
 *
 * There is no client-side API call needed for the callback step.
 */

export async function refreshToken(
  refresh_token: string,
): Promise<TokenResponse> {
  const { data } = await client.post<TokenResponse>("/api/v1/auth/refresh", {
    refresh_token,
  });
  return data;
}

export async function fetchCurrentUser(): Promise<UserProfile> {
  const { data } = await client.get<UserProfile>("/api/v1/auth/me");
  return data;
}
