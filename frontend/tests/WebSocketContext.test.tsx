import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";

// Track WebSocket instances created during tests
const wsInstances: MockWebSocket[] = [];

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  url: string;
  readyState = MockWebSocket.CONNECTING;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    wsInstances.push(this);
  }

  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
  });

  // Test helpers — all state-changing actions are wrapped in act() by callers
  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  simulateClose(code = 1006) {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code }));
  }

  simulateMessage(data: string) {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  simulateError() {
    this.onerror?.(new Event("error"));
  }
}

// Mock auth context
vi.mock("../src/contexts/AuthContext", () => ({
  useAuthContext: () => ({
    isAuthenticated: true,
    user: { id: "u1" },
  }),
}));

// Apply global WebSocket mock before importing the module
const originalWebSocket = globalThis.WebSocket;
Object.defineProperty(globalThis, "WebSocket", {
  writable: true,
  value: MockWebSocket,
});

// Ensure localStorage has a token
localStorage.setItem("access_token", "test-token");

// Import after mocks are set up
import { WebSocketProvider, useWebSocketContext } from "../src/contexts/WebSocketContext";

function wrapper({ children }: { children: ReactNode }) {
  return createElement(WebSocketProvider, null, children);
}

describe("WebSocketContext resilience", () => {
  beforeEach(() => {
    wsInstances.length = 0;
    vi.useFakeTimers();
    localStorage.setItem("access_token", "test-token");
  });

  afterEach(async () => {
    // Flush all pending timers inside act() so that any resulting React
    // state updates (setIsConnected, reconnect scheduling, etc.) are
    // properly batched. Running timers outside act() can trigger React
    // "not wrapped in act(...)" warnings in stricter environments.
    await act(async () => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
  });

  it("reconnects with exponential backoff on disconnect", async () => {
    renderHook(() => useWebSocketContext(), { wrapper });

    // Initial connection
    expect(wsInstances).toHaveLength(1);
    await act(async () => {
      wsInstances[0].simulateOpen();
    });

    // Disconnect
    await act(async () => {
      wsInstances[0].simulateClose(1006);
    });

    // First retry after 1s
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(wsInstances).toHaveLength(2);

    // Second disconnect
    await act(async () => {
      wsInstances[1].simulateClose(1006);
    });

    // Second retry after 2s
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(wsInstances).toHaveLength(3);
  });

  it("resets reconnect counter on successful connection", async () => {
    renderHook(() => useWebSocketContext(), { wrapper });

    // Simulate 3 failed attempts
    await act(async () => {
      wsInstances[0].simulateClose(1006);
    });
    await act(async () => {
      vi.advanceTimersByTime(1000);
    }); // attempt 1
    await act(async () => {
      wsInstances[1].simulateClose(1006);
    });
    await act(async () => {
      vi.advanceTimersByTime(2000);
    }); // attempt 2
    await act(async () => {
      wsInstances[2].simulateClose(1006);
    });
    await act(async () => {
      vi.advanceTimersByTime(4000);
    }); // attempt 3

    // This one connects successfully
    await act(async () => {
      wsInstances[3].simulateOpen();
    });

    // Then disconnects again
    await act(async () => {
      wsInstances[3].simulateClose(1006);
    });

    // Should retry after 1s (counter reset), not 16s
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(wsInstances.length).toBeGreaterThanOrEqual(5);
  });

  it("enters recovery cooldown after max attempts, then retries", async () => {
    renderHook(() => useWebSocketContext(), { wrapper });

    // Exhaust all 10 reconnect attempts.
    // After the loop the last call to connect() sees attempt >= MAX and
    // schedules a 60s recovery cooldown (instead of creating a new socket).
    for (let i = 0; i < 10; i++) {
      const lastIdx = wsInstances.length - 1;
      await act(async () => {
        wsInstances[lastIdx].simulateClose(1006);
      });
      // Advance past the backoff delay (capped at 30s)
      await act(async () => {
        vi.advanceTimersByTime(30_001);
      });
    }

    // At this point connect() returned early after scheduling the recovery
    // cooldown — no new socket was created.
    const countAfterExhausted = wsInstances.length;

    // Advance 30s — should NOT have reconnected yet (recovery cooldown is 60s)
    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    expect(wsInstances.length).toBe(countAfterExhausted);

    // Advance remaining 31s — cooldown fires, counter resets, connect()
    // creates a fresh socket.
    await act(async () => {
      vi.advanceTimersByTime(31_000);
    });
    expect(wsInstances.length).toBe(countAfterExhausted + 1);
  });

  it("does not reconnect on auth failure close codes and clears auth state", async () => {
    // Seed tokens before the test
    localStorage.setItem("access_token", "test-token");
    localStorage.setItem("refresh_token", "test-refresh");

    // Track window.location.href assignments
    const originalLocation = window.location;
    const hrefSetter = vi.fn();
    Object.defineProperty(window, "location", {
      writable: true,
      configurable: true,
      value: new URL(originalLocation.href),
    });
    Object.defineProperty(window.location, "href", {
      set: hrefSetter,
      get: () => originalLocation.href,
      configurable: true,
    });

    renderHook(() => useWebSocketContext(), { wrapper });

    await act(async () => {
      wsInstances[0].simulateOpen();
    });
    await act(async () => {
      wsInstances[0].simulateClose(4001);
    }); // auth failure

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });

    // Only the original connection, no retries
    expect(wsInstances).toHaveLength(1);

    // Auth tokens should have been cleared from localStorage
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();

    // Should redirect to login
    expect(hrefSetter).toHaveBeenCalledWith("/login");

    // Restore
    Object.defineProperty(window, "location", {
      writable: true,
      configurable: true,
      value: originalLocation,
    });
  });

  it("dispatches messages to subscribers", async () => {
    const listener = vi.fn();

    const { result } = renderHook(() => useWebSocketContext(), { wrapper });

    act(() => {
      result.current.subscribe(listener);
    });

    await act(async () => {
      wsInstances[0].simulateOpen();
    });
    await act(async () => {
      wsInstances[0].simulateMessage(
        JSON.stringify({ event: "task_status_changed", data: { task_id: "t1", agent_type: "eligibility", status: "completed" } }),
      );
    });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith(
      expect.objectContaining({ event: "task_status_changed" }),
    );
  });
});

// Restore original WebSocket
afterAll(() => {
  Object.defineProperty(globalThis, "WebSocket", {
    writable: true,
    value: originalWebSocket,
  });
});
