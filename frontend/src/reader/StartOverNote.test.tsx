import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { StartOverNote } from "./StartOverNote";

describe("StartOverNote", () => {
  test("explains what rewinds and what remains preserved", () => {
    render(<StartOverNote busy={false} error={null} onConfirm={() => {}} onCancel={() => {}} />);
    const dialog = screen.getByRole("alertdialog");
    expect(dialog.textContent).toMatch(/companion.*nothing read/i);
    expect(dialog.textContent).toMatch(/notes.*costs.*stay/i);
  });

  test("opens on the safe choice, Escape cancels, and confirmation is explicit", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <StartOverNote busy={false} error={null} onConfirm={onConfirm} onCancel={onCancel} />,
    );
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /keep my place/i }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /start again/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  test("busy state disables both choices and an error is announced", () => {
    render(
      <StartOverNote
        busy
        error="The new pass could not be started."
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    expect((screen.getByRole("button", { name: /starting/i }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: /keep my place/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toMatch(/could not be started/i);
    expect(document.activeElement).toBe(screen.getByRole("alertdialog"));
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByRole("alertdialog"));
  });

  test("has no axe violations", async () => {
    const { container } = render(
      <StartOverNote busy={false} error={null} onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
