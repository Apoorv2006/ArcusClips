import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge, ProgressBar, StatCard, EmptyState } from "./ui";
import { fmtDuration as fd, fmtBytes as fb } from "../api";

describe("StatusBadge", () => {
  it("maps statuses to labels", () => {
    render(<StatusBadge status="completed" />);
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  it("falls back for unknown statuses", () => {
    render(<StatusBadge status="weird" />);
    expect(screen.getByText("weird")).toBeInTheDocument();
  });
});

describe("ProgressBar", () => {
  it("renders with a percentage width", () => {
    const { container } = render(<ProgressBar value={0.5} />);
    const fill = container.querySelector("span");
    expect(fill).toBeTruthy();
    expect(fill!.style.width).toBe("50%");
  });

  it("clamps out-of-range values", () => {
    const { container } = render(<ProgressBar value={3} />);
    expect(container.querySelector("span")!.style.width).toBe("100%");
  });
});

describe("StatCard", () => {
  it("renders label and value", () => {
    render(<StatCard label="Clips" value={42} />);
    expect(screen.getByText("Clips")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("renders title and hint", () => {
    render(<EmptyState title="Nothing here" hint="Create something" />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(screen.getByText("Create something")).toBeInTheDocument();
  });
});

describe("format helpers", () => {
  it("formats durations and bytes", () => {
    expect(fd(90)).toBe("1:30");
    expect(fb(2048)).toBe("2.0 KB");
  });
});
