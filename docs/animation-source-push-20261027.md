# Share snapshot scope

2026-10-27 · Codex

Intent: push the animation source routing change without workstation metadata. Decision: retain the selector, two CLI commands, neutral source catalog, tests and documentation; use a separate feature branch. Reason: the original upload was rejected for local absolute paths and internal inventory/environment details. Evidence: the inspected rejection, revised payload scan and clean-checkout regression results. Effective scope: this share snapshot only; the complete local implementation and other uncommitted work remain intact.

Local source paths and internal source records are omitted. The catalog therefore requires fresh local source binding before adaptation. No runtime is installed, no upstream media service is invoked, and no rendered-media quality or publishing approval is implied.
