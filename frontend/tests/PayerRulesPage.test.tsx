import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import PayerRulesPage from "../src/pages/PayerRulesPage";
import type { PayerResponse, PayerRuleResponse } from "../src/types";

// Mock API modules
vi.mock("../src/api/payers", () => ({
  listPayers: vi.fn(),
  listPayerRules: vi.fn(),
  updatePayerRule: vi.fn(),
  createPayerRule: vi.fn(),
}));

import { listPayers, listPayerRules, updatePayerRule } from "../src/api/payers";
const mockListPayers = vi.mocked(listPayers);
const mockListPayerRules = vi.mocked(listPayerRules);
const mockUpdatePayerRule = vi.mocked(updatePayerRule);

const samplePayers: PayerResponse[] = [
  {
    id: "payer-001",
    name: "Aetna",
    payer_id_code: "AETNA01",
    payer_type: "commercial",
    address: null,
    phone: null,
    electronic_payer_id: "60054",
    is_active: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
  },
  {
    id: "payer-002",
    name: "Blue Cross",
    payer_id_code: "BCBS01",
    payer_type: "commercial",
    address: null,
    phone: null,
    electronic_payer_id: "BCBS1",
    is_active: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
  },
];

const sampleRules: PayerRuleResponse[] = [
  {
    id: "rule-001",
    payer_id: "payer-001",
    agent_type: "eligibility",
    rule_type: "coverage_check",
    description: "Standard eligibility verification",
    conditions: { service_types: ["30", "47"] },
    actions: null,
    effective_date: "2026-01-01",
    termination_date: null,
    version: 1,
    is_active: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
  },
];

describe("PayerRulesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListPayers.mockResolvedValue(samplePayers);
    mockListPayerRules.mockResolvedValue(sampleRules);
  });

  it("renders payer list", async () => {
    render(
      <MemoryRouter>
        <PayerRulesPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("payer-list")).toBeInTheDocument();
    });

    expect(screen.getByText("Aetna")).toBeInTheDocument();
    expect(screen.getByText("Blue Cross")).toBeInTheDocument();
  });

  it("shows no payer selected message initially", async () => {
    render(
      <MemoryRouter>
        <PayerRulesPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("no-payer-selected")).toBeInTheDocument();
    });
  });

  it("shows rules when payer is selected", async () => {
    render(
      <MemoryRouter>
        <PayerRulesPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("payer-item-payer-001")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("payer-item-payer-001"));

    await waitFor(() => {
      expect(screen.getByTestId("rules-list")).toBeInTheDocument();
    });
    expect(screen.getByText("coverage_check")).toBeInTheDocument();
  });

  it("allows editing a rule and saving via API", async () => {
    mockUpdatePayerRule.mockResolvedValue(undefined as never);

    render(
      <MemoryRouter>
        <PayerRulesPage />
      </MemoryRouter>,
    );

    // Select payer
    await waitFor(() => {
      expect(screen.getByTestId("payer-item-payer-001")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("payer-item-payer-001"));

    // Wait for rules
    await waitFor(() => {
      expect(screen.getByTestId("rule-item-rule-001")).toBeInTheDocument();
    });

    // Click edit
    fireEvent.click(screen.getByTestId("edit-rule-rule-001"));

    // Verify edit mode
    await waitFor(() => {
      expect(screen.getByTestId("edit-conditions")).toBeInTheDocument();
    });

    // Click save
    fireEvent.click(screen.getByTestId("save-rule-button"));

    await waitFor(() => {
      expect(mockUpdatePayerRule).toHaveBeenCalledWith(
        "payer-001",
        "rule-001",
        expect.objectContaining({ conditions: expect.any(Object) }),
      );
    });
  });

  it("shows empty rules state for payer with no rules", async () => {
    mockListPayerRules.mockResolvedValue([]);

    render(
      <MemoryRouter>
        <PayerRulesPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("payer-item-payer-001")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("payer-item-payer-001"));

    await waitFor(() => {
      expect(screen.getByTestId("rules-empty")).toBeInTheDocument();
    });
  });

  it("shows empty payers state when no payers", async () => {
    mockListPayers.mockResolvedValue([]);

    render(
      <MemoryRouter>
        <PayerRulesPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("payer-list-empty")).toBeInTheDocument();
    });
  });
});
