import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../src/App";

// Mock all page components to simple stubs so they don't pull in API deps
vi.mock("../src/pages/DashboardPage", () => ({
  default: () => <div data-testid="dashboard-page">Dashboard</div>,
}));
vi.mock("../src/pages/AgentPage", () => ({
  default: () => <div>Agent</div>,
}));
vi.mock("../src/pages/ReviewsPage", () => ({
  default: () => <div>Reviews</div>,
}));
vi.mock("../src/pages/WorkflowsPage", () => ({
  default: () => <div>Workflows</div>,
}));
vi.mock("../src/pages/PayerRulesPage", () => ({
  default: () => <div>PayerRules</div>,
}));
vi.mock("../src/pages/AuditPage", () => ({
  default: () => <div>Audit</div>,
}));

// Mock auth API — LoginPage fetches providers from backend
vi.mock("../src/api/auth", () => ({
  initiateLogin: vi.fn(),
  refreshToken: vi.fn(),
  fetchCurrentUser: vi.fn(),
  fetchLoginProviders: vi.fn().mockResolvedValue({
    message: "SSO login required",
    providers: ["saml", "oidc"],
    redirect_url: "/",
    login_endpoint: "/api/v1/auth/login",
    usage: "",
  }),
}));

// WebSocket context mock
vi.mock("../src/contexts/WebSocketContext", () => ({
  WebSocketProvider: ({ children }: { children: React.ReactNode }) => children,
  useWebSocketContext: () => ({
    isConnected: false,
    lastMessage: null,
    subscribe: () => () => {},
  }),
}));

// Variable to control auth state per test
let mockIsAuthenticated = false;
let mockIsLoading = false;

vi.mock("../src/contexts/AuthContext", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuthContext: () => ({
    user: mockIsAuthenticated
      ? { id: "u1", email: "a@b.com", full_name: "A", roles: ["admin"], organization_id: null, last_login: null, is_active: true }
      : null,
    isAuthenticated: mockIsAuthenticated,
    isLoading: mockIsLoading,
    error: null,
    login: vi.fn(),
    handleAuthCallback: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("../src/hooks/useAuth", () => ({
  useAuth: () => ({
    user: mockIsAuthenticated
      ? { id: "u1", email: "a@b.com", full_name: "A", roles: ["admin"], organization_id: null, last_login: null, is_active: true }
      : null,
    isAuthenticated: mockIsAuthenticated,
    isLoading: mockIsLoading,
    error: null,
    login: vi.fn(),
    handleAuthCallback: vi.fn(),
    logout: vi.fn(),
  }),
}));

describe("ProtectedRoute redirect behavior", () => {
  it("redirects unauthenticated user from / to /login", async () => {
    mockIsAuthenticated = false;
    mockIsLoading = false;

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    // Should see login page, NOT dashboard
    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("dashboard-page")).not.toBeInTheDocument();
  });

  it("redirects unauthenticated user from /reviews to /login", async () => {
    mockIsAuthenticated = false;
    mockIsLoading = false;

    render(
      <MemoryRouter initialEntries={["/reviews"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
    expect(screen.queryByText("Reviews")).not.toBeInTheDocument();
  });

  it("redirects unauthenticated user from /workflows to /login", async () => {
    mockIsAuthenticated = false;
    mockIsLoading = false;

    render(
      <MemoryRouter initialEntries={["/workflows"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sso-saml-button")).toBeInTheDocument();
    });
  });

  it("allows authenticated user to see dashboard at /", async () => {
    mockIsAuthenticated = true;
    mockIsLoading = false;

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("dashboard-page")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("sso-saml-button")).not.toBeInTheDocument();
  });
});
