import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import AgentStatusCards from "../src/components/dashboard/AgentStatusCards";
import type { AgentStatsResponse } from "../src/types";

const mockAgents: AgentStatsResponse[] = [
  {
    agent_type: "eligibility",
    total_tasks: 120,
    pending: 5,
    running: 10,
    completed: 95,
    failed: 3,
    in_review: 7,
    cancelled: 0,
    avg_confidence: 0.89,
  },
  {
    agent_type: "scheduling",
    total_tasks: 80,
    pending: 2,
    running: 5,
    completed: 70,
    failed: 1,
    in_review: 2,
    cancelled: 0,
    avg_confidence: 0.92,
  },
  {
    agent_type: "claims",
    total_tasks: 200,
    pending: 10,
    running: 15,
    completed: 160,
    failed: 8,
    in_review: 7,
    cancelled: 0,
    avg_confidence: 0.85,
  },
  {
    agent_type: "prior_auth",
    total_tasks: 50,
    pending: 3,
    running: 8,
    completed: 30,
    failed: 2,
    in_review: 7,
    cancelled: 0,
    avg_confidence: 0.78,
  },
  {
    agent_type: "credentialing",
    total_tasks: 30,
    pending: 1,
    running: 4,
    completed: 22,
    failed: 1,
    in_review: 2,
    cancelled: 0,
    avg_confidence: 0.95,
  },
  {
    agent_type: "compliance",
    total_tasks: 15,
    pending: 0,
    running: 2,
    completed: 12,
    failed: 0,
    in_review: 1,
    cancelled: 0,
    avg_confidence: 0.91,
  },
];

describe("AgentStatusCards", () => {
  it("renders 6 agent cards", () => {
    render(<AgentStatusCards agents={mockAgents} />);

    const container = screen.getByTestId("agent-status-cards");
    expect(container).toBeInTheDocument();

    for (const agent of mockAgents) {
      expect(
        screen.getByTestId(`agent-card-${agent.agent_type}`),
      ).toBeInTheDocument();
    }
  });

  it("displays correct total tasks for each agent", () => {
    render(<AgentStatusCards agents={mockAgents} />);

    expect(screen.getByText("120 total tasks")).toBeInTheDocument();
    expect(screen.getByText("80 total tasks")).toBeInTheDocument();
    expect(screen.getByText("200 total tasks")).toBeInTheDocument();
  });

  it("displays agent labels", () => {
    render(<AgentStatusCards agents={mockAgents} />);

    expect(screen.getByText("Eligibility")).toBeInTheDocument();
    expect(screen.getByText("Scheduling")).toBeInTheDocument();
    expect(screen.getByText("Claims & Billing")).toBeInTheDocument();
    expect(screen.getByText("Prior Auth")).toBeInTheDocument();
    expect(screen.getByText("Credentialing")).toBeInTheDocument();
    expect(screen.getByText("Compliance")).toBeInTheDocument();
  });

  it("shows average confidence percentage", () => {
    render(<AgentStatusCards agents={mockAgents} />);

    // 0.89 => 89%
    expect(screen.getByText("89%")).toBeInTheDocument();
    expect(screen.getByText("92%")).toBeInTheDocument();
  });
});
