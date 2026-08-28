import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EvalBar, formatScore } from "./EvalBar";

describe("formatScore", () => {
  it.each([
    [{ cp: 42 }, "+0.42"],
    [{ cp: -135 }, "-1.35"],
    [{ mate: 3 }, "#3"],
    [{ mate: -2 }, "-#2"],
    [{ mate: 0, mate_given: true }, "#"],
    [{ mate: 0, mate_given: false }, "-#"],
  ])("formats %o as %s", (score, expected) => {
    expect(formatScore(score)).toBe(expected);
  });
});

describe("EvalBar", () => {
  it("renders the empty state without an accessible evaluation", () => {
    render(<EvalBar winPercent={null} score={null} orientation="white" />);

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(document.querySelector(".evalbar.empty")).toBeInTheDocument();
  });

  it("describes the evaluation and fills from the correct side", () => {
    render(<EvalBar winPercent={65} score={{ cp: 42 }} orientation="white" />);

    expect(
      screen.getByRole("img", {
        name: "Evaluation +0.42, white win chance 65%",
      }),
    ).toBeInTheDocument();
    expect(document.querySelector(".white")).toHaveStyle({
      height: "65%",
      bottom: "0px",
    });
  });
});
