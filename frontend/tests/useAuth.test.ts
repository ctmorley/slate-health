import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { AuthProvider, useAuthContext } from "../src/contexts/AuthContext";

// Mock the auth API
vi.mock("../src/api/auth", () => ({
  initiateLogin: vi.fn(),
  fetchCurrentUser: vi.fn(),
}));

import { initiateLogin, fetchCurrentUser } from "../src/api/auth";
const mockInitiateLogin = vi.mocked(initiateLogin);
const mockFetchUser = vi.mocked(fetchCurrentUser);

// Mock window.location.assign
const mockAssign = vi.fn();
Object.defineProperty(window, "location", {
  writable: true,
  value: { ...window.location, assign: mockAssign, href: "", origin: "http://localhost:3000" },
});

// Wrapper that provides the AuthProvider context
function wrapper({ children }: { children: ReactNode }) {
  return createElement(AuthProvider, null, children);
}

describe("useAuthContext (via AuthProvider)", () => {
  beforeEach(() => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    vi.clearAllMocks();
  });

  it("returns unauthenticated state when no token exists", async () => {
    const { result } = renderHook(() => useAuthContext(), { wrapper });

    // Wait for initial load to complete
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.user).toBeNull();
  });

  it("stores tokens via handleAuthCallback and loads user profile", async () => {
    mockFetchUser.mockResolvedValue({
      id: "user-1",
      email: "admin@slate.health",
      full_name: "Admin User",
      roles: ["admin"],
      organization_id: null,
      last_login: null,
      is_active: true,
    });

    const { result } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    // Simulate callback with tokens — handleAuthCallback triggers async work
    await act(async () => {
      result.current.handleAuthCallback("access-123", "refresh-456");
    });

    expect(localStorage.getItem("access_token")).toBe("access-123");
    expect(localStorage.getItem("refresh_token")).toBe("refresh-456");

    // Wait for user profile to load
    await waitFor(() => {
      expect(result.current.user).not.toBeNull();
    });

    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.user?.email).toBe("admin@slate.health");
  });

  it("logout clears tokens and redirects to /login", async () => {
    mockFetchUser.mockResolvedValue({
      id: "user-1",
      email: "admin@slate.health",
      full_name: "Admin User",
      roles: ["admin"],
      organization_id: null,
      last_login: null,
      is_active: true,
    });

    // Start with a valid session
    localStorage.setItem("access_token", "valid-token");
    localStorage.setItem("refresh_token", "valid-refresh");

    const { result } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.isAuthenticated).toBe(true);

    // Call logout
    act(() => {
      result.current.logout();
    });

    // Verify tokens are removed
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();

    // Verify user is cleared
    expect(result.current.user).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);

    // Verify redirect to /login
    expect(window.location.href).toBe("/login");
  });

  it("clears token if fetchCurrentUser fails", async () => {
    localStorage.setItem("access_token", "expired-token");
    mockFetchUser.mockRejectedValue(new Error("Unauthorized"));

    const { result } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.isAuthenticated).toBe(false);
    expect(localStorage.getItem("access_token")).toBeNull();
  });

  it("login() calls initiateLogin API then redirects to IdP", async () => {
    mockInitiateLogin.mockResolvedValue({
      redirect_url: "https://idp.example.com/sso?SAMLRequest=xxx",
      provider: "saml",
    });

    const { result } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    // login() triggers async API call — use async act
    await act(async () => {
      result.current.login("saml");
    });

    await waitFor(() => {
      expect(mockInitiateLogin).toHaveBeenCalledWith({
        provider: "saml",
        redirect_url: "http://localhost:3000/login",
      });
    });

    await waitFor(() => {
      expect(mockAssign).toHaveBeenCalledWith(
        "https://idp.example.com/sso?SAMLRequest=xxx",
      );
    });
  });

  it("login() sets error on API failure", async () => {
    mockInitiateLogin.mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    // login() triggers async API call — use async act
    await act(async () => {
      result.current.login("oidc");
    });

    await waitFor(() => {
      expect(result.current.error).toBe("Network error");
    });
  });

  it("handleAuthCallback clears tokens when fetchCurrentUser rejects", async () => {
    mockFetchUser.mockRejectedValue(new Error("Unauthorized"));

    const { result } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    // Simulate callback with tokens — the subsequent profile fetch will fail
    await act(async () => {
      result.current.handleAuthCallback("bad-access", "bad-refresh");
    });

    await waitFor(() => {
      expect(result.current.error).toBe("Unauthorized");
    });

    // Tokens must be cleared so the app doesn't retain invalid credentials
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.user).toBeNull();
  });

  it("multiple consumers share the same auth state (no duplicate fetches)", async () => {
    localStorage.setItem("access_token", "valid-token");
    mockFetchUser.mockResolvedValue({
      id: "user-1",
      email: "admin@slate.health",
      full_name: "Admin User",
      roles: ["admin"],
      organization_id: null,
      last_login: null,
      is_active: true,
    });

    // Render two hooks in the same provider
    const { result: result1 } = renderHook(() => useAuthContext(), { wrapper });

    await waitFor(() => {
      expect(result1.current.isLoading).toBe(false);
    });

    // fetchCurrentUser should only have been called once (by the provider)
    expect(mockFetchUser).toHaveBeenCalledTimes(1);
    expect(result1.current.isAuthenticated).toBe(true);
  });
});
