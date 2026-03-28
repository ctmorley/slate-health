import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Sidebar from "../src/components/layout/Sidebar";

describe("Sidebar", () => {
  it("renders all navigation items", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );

    const nav = screen.getByTestId("sidebar-nav");
    expect(nav).toBeInTheDocument();

    // 1 Dashboard + 6 Agents + Reviews + Workflows + Payer Rules + Audit = 11 items
    const links = nav.querySelectorAll("a");
    expect(links.length).toBe(11);

    // Verify key labels exist
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Eligibility")).toBeInTheDocument();
    expect(screen.getByText("Scheduling")).toBeInTheDocument();
    expect(screen.getByText("Claims & Billing")).toBeInTheDocument();
    expect(screen.getByText("Prior Auth")).toBeInTheDocument();
    expect(screen.getByText("Credentialing")).toBeInTheDocument();
    expect(screen.getByText("Compliance")).toBeInTheDocument();
    expect(screen.getByText("Reviews")).toBeInTheDocument();
    expect(screen.getByText("Workflows")).toBeInTheDocument();
    expect(screen.getByText("Payer Rules")).toBeInTheDocument();
    expect(screen.getByText("Audit Log")).toBeInTheDocument();
  });

  it("displays the Slate Health branding", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );

    expect(screen.getByText("Slate Health")).toBeInTheDocument();
  });
});
