import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ErrorCounts, SummaryStats, accuracyPercent } from "./Summary";
import type { AnalysisSummary } from "./api";

const summary: AnalysisSummary = {
  accuracy: 91.4,
  average_loss: 2.35,
  moves: 20,
  blunders: 1,
  mistakes: 2,
  inaccuracies: 3,
};

describe("Summary", () => {
  it("rounds accuracy to a whole percentage", () => {
    expect(accuracyPercent(summary)).toBe(91);
  });

  it("renders every severity count in the header", () => {
    render(<SummaryStats summary={summary} />);

    expect(screen.getByText("91%")).toBeInTheDocument();
    expect(screen.getByText("Accuracy")).toBeInTheDocument();
    expect(screen.getByText("Blunders")).toBeInTheDocument();
    expect(screen.getByText("Mistakes")).toBeInTheDocument();
    expect(screen.getByText("Inaccuracies")).toBeInTheDocument();
  });

  it("provides a spoken description for compact error marks", () => {
    render(<ErrorCounts summary={summary} />);

    expect(
      screen.getByLabelText("1 blunder, 2 mistakes, 3 inaccuracies"),
    ).toBeInTheDocument();
  });

  it("shows clean when there are no errors", () => {
    render(
      <ErrorCounts
        summary={{ ...summary, blunders: 0, mistakes: 0, inaccuracies: 0 }}
      />,
    );

    expect(screen.getByText("clean")).toBeInTheDocument();
  });
});
