import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LoginPage from "../src/pages/LoginPage";

// Mock auth context (LoginPage uses useAuth which delegates to context)
const mockLogin = vi.fn();
const mockHandleAuthCallback = vi.fn();
const mockAuthState = {
  user: null,
  isAuthenticated: false,
  isLoading: false,
  error: null as string | null,
  login: mockLogin,
  handleAuthCallback: mockHandleAuthCallback,
  logout: vi.fn(),
};

vi.mock("../src/contexts/AuthContext", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuthContext: () => mockAuthState,
}));

vi.mock("../src/hooks/useAuth", () => ({
  useAuth: () => mockAuthState,
}));

// Mock auth API — fetchLoginProviders
const mockFetchLoginProviders = vi.fn();

vi.mock("../src/api/auth", () => ({
  initiateLogin: vi.fn(),
  refreshToken: vi.fn(),
  fetchCurrentUser: vi.fn(),
  fetchLoginProviders: (...args: unknown[]) => mockFetchLoginProviders(...args),
}));

describe("LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAuthState.error = null;
    // Default: backend returns both providers
    mockFetchLoginProviders.mockResolvedValue({
      message: "SSO login required",
      providers: ["saml", "oidc"],
      redirect_url: "/",
      login_endpoint: "/api/v1/auth/login",
      usage: "POST to /api/v1/auth/login ...",
    });
  });

  it("renders SSO buttons for SAML and OIDC when both are available", async () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
    expect(screen.getByTestId("sso-oidc-button")).toBeInTheDocument();
    expect(screen.getByText("Sign in with SAML SSO")).toBeInTheDocument();
    expect(screen.getByText("Sign in with OpenID Connect")).toBeInTheDocument();
  });

  it("renders only SAML button when backend returns only saml provider", async () => {
    mockFetchLoginProviders.mockResolvedValue({
      message: "SSO login required",
      providers: ["saml"],
      redirect_url: "/",
      login_endpoint: "/api/v1/auth/login",
      usage: "",
    });

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("sso-oidc-button")).not.toBeInTheDocument();
  });

  it("renders only OIDC button when backend returns only oidc provider", async () => {
    mockFetchLoginProviders.mockResolvedValue({
      message: "SSO login required",
      providers: ["oidc"],
      redirect_url: "/",
      login_endpoint: "/api/v1/auth/login",
      usage: "",
    });

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-oidc-button")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("sso-saml-button")).not.toBeInTheDocument();
  });

  it("falls back to both providers with warning when backend is unreachable", async () => {
    mockFetchLoginProviders.mockRejectedValue(new Error("Network error"));

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
    expect(screen.getByTestId("sso-oidc-button")).toBeInTheDocument();
    expect(screen.getByTestId("providers-warning")).toBeInTheDocument();
    expect(screen.getByTestId("providers-warning")).toHaveTextContent(
      "Could not reach server",
    );
  });

  it("shows loading spinner while fetching providers", () => {
    // Never resolve to keep it loading
    mockFetchLoginProviders.mockReturnValue(new Promise(() => {}));

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("providers-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("sso-saml-button")).not.toBeInTheDocument();
  });

  it("calls login with 'saml' when SAML button is clicked", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("sso-saml-button"));
    expect(mockLogin).toHaveBeenCalledWith("saml");
  });

  it("calls login with 'oidc' when OIDC button is clicked", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-oidc-button")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("sso-oidc-button"));
    expect(mockLogin).toHaveBeenCalledWith("oidc");
  });

  it("renders Slate Health branding", async () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    // Wait for provider fetch to complete so no act warnings
    await waitFor(() => {
      expect(screen.getByTestId("sso-provider-list")).toBeInTheDocument();
    });

    expect(screen.getByText("Slate Health")).toBeInTheDocument();
    expect(
      screen.getByText("HIPAA-compliant healthcare AI platform"),
    ).toBeInTheDocument();
  });

  it("displays auth error message when login fails", async () => {
    mockAuthState.error = "Network error: unable to reach identity provider";

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    // Wait for provider fetch to settle
    await waitFor(() => {
      expect(screen.getByTestId("sso-provider-list")).toBeInTheDocument();
    });

    const alert = screen.getByTestId("login-error");
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent(
      "Network error: unable to reach identity provider",
    );
  });

  it("does not display error banner when there is no error", async () => {
    mockAuthState.error = null;

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    // Wait for provider fetch to settle
    await waitFor(() => {
      expect(screen.getByTestId("sso-provider-list")).toBeInTheDocument();
    });

    expect(screen.queryByTestId("login-error")).not.toBeInTheDocument();
  });

  it("scrubs tokens from URL via history.replaceState on callback", async () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");

    render(
      <MemoryRouter initialEntries={["/login?access_token=tok123&refresh_token=ref456"]}>
        <LoginPage />
      </MemoryRouter>,
    );

    // Wait for provider fetch to settle
    await waitFor(() => {
      expect(screen.getByTestId("sso-provider-list")).toBeInTheDocument();
    });

    // handleAuthCallback should have been called with the tokens
    expect(mockHandleAuthCallback).toHaveBeenCalledWith("tok123", "ref456");
    // URL should have been scrubbed via replaceState (pathname only, no query params)
    expect(replaceStateSpy).toHaveBeenCalledWith(
      {},
      "",
      expect.stringMatching(/^\//)  // any pathname, but no query params
    );
    // Crucially, the replacement URL must NOT contain tokens
    const replacedUrl = replaceStateSpy.mock.calls[0][2] as string;
    expect(replacedUrl).not.toContain("access_token");
    expect(replacedUrl).not.toContain("refresh_token");
    replaceStateSpy.mockRestore();
  });

  it("fetches providers from backend on mount", async () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockFetchLoginProviders).toHaveBeenCalledTimes(1);
    });
  });
});
