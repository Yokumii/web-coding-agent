# Edit Skill shared integration contract

Copy the selected reference files into the host project and call their public API from the real user-facing path. Keep the copied core unchanged unless the requested behavior is a generic capability that the core does not expose; put business data, labels, selectors and service wiring in a small host adapter.

Preserve the existing framework, routes, state identities, entry controls and unrelated behavior. Reuse existing state and events instead of creating a parallel engine. Do not mount a hidden reference while separate code owns the visible feature. Do not add dependencies, replace whole source files, scaffold another app or reformat unrelated code.

Choose the stack-specific reference already supplied by Harness. Keep its relative imports intact and ensure the final project has no dependency on `.harness` or the Skill library. Mount into an existing or narrowly added host region, connect requested callbacks to real behavior, and call `destroy()` when that region is permanently removed. Framework-owned DOM remains framework-owned; adapters update framework state instead of moving or mutating controlled nodes behind it.

When consecutive Edits target the same surface, explicitly compose, switch or replace views so mounted features do not overlap or compete for the same controls. Implement only the requested slice. A copied core plus a concise adapter is preferred over reproducing the component or placing generic component mechanics in business code.
