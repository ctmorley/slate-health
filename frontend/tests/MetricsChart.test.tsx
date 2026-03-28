import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import MetricsChart, { formatDateLabel } from "../src/components/dashboard/MetricsChart";

// Mock recharts to avoid canvas/SVG issues in jsdom
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container" style={{ width: 400, height: 260 }}>
        {children}
      </div>
    ),
  };
});

describe("MetricsChart", () => {
  it("renders with the default title", () => {
    render(<MetricsChart data={[]} />);
    expect(
      screen.getByText("Task Volume (Last 7 Days)"),
    ).toBeInTheDocument();
  });

  it("renders with sample data without errors", () => {
    const data = [
      { date: "2026-03-21", count: 12 },
      { date: "2026-03-22", count: 18 },
      { date: "2026-03-23", count: 9 },
      { date: "2026-03-24", count: 25 },
      { date: "2026-03-25", count: 15 },
      { date: "2026-03-26", count: 22 },
      { date: "2026-03-27", count: 20 },
    ];

    render(<MetricsChart data={data} />);
    expect(screen.getByTestId("metrics-chart")).toBeInTheDocument();
    expect(screen.getByTestId("responsive-container")).toBeInTheDocument();
  });

  it("shows empty state when no data", () => {
    render(<MetricsChart data={[]} />);
    expect(screen.getByText("No data available")).toBeInTheDocument();
  });

  it("accepts a custom title", () => {
    render(<MetricsChart data={[]} title="Custom Chart Title" />);
    expect(screen.getByText("Custom Chart Title")).toBeInTheDocument();
  });

  it("shows 'Partial data' badge when incompleteAgents is non-empty", () => {
    const data = [{ date: "2026-03-21", count: 5 }];
    render(
      <MetricsChart data={data} incompleteAgents={["Eligibility", "Claims & Billing"]} />,
    );
    const badge = screen.getByTestId("chart-incomplete-badge");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveTextContent("Partial data");
    expect(badge).toHaveAttribute(
      "title",
      "Missing data from: Eligibility, Claims & Billing",
    );
  });

  it("does not show partial data badge when incompleteAgents is empty", () => {
    const data = [{ date: "2026-03-21", count: 5 }];
    render(<MetricsChart data={data} incompleteAgents={[]} />);
    expect(screen.queryByTestId("chart-incomplete-badge")).not.toBeInTheDocument();
  });

  it("applies amber border when data is partial", () => {
    render(
      <MetricsChart data={[]} incompleteAgents={["Scheduling"]} />,
    );
    const chart = screen.getByTestId("metrics-chart");
    expect(chart.className).toContain("border-amber-300");
  });
});

describe("formatDateLabel (timezone-safe)", () => {
  it("formats 2026-03-21 as 'Mar 21' regardless of timezone", () => {
    // This would previously produce "Mar 20" in America/New_York because
    // `new Date("2026-03-21")` is midnight UTC, which is 7pm on Mar 20 EST.
    const label = formatDateLabel("2026-03-21");
    expect(label).toContain("21");
    expect(label).toContain("Mar");
  });

  it("formats single-digit days correctly", () => {
    const label = formatDateLabel("2026-01-05");
    expect(label).toContain("5");
    expect(label).toContain("Jan");
  });

  it("handles year boundaries correctly", () => {
    const label = formatDateLabel("2025-12-31");
    expect(label).toContain("31");
    expect(label).toContain("Dec");
  });
});
