# Working in IDE distributions

Read the [runtime contracts](.contracts/index.md), the [seam contract](core/src/research_core/contract/CONTRACT.md), and the nearest package documentation before changing execution behavior.

Use the root `uv.lock` with Python 3.12. Run `uv sync --frozen`, `uv run ruff check .`, and the affected pytest suites. Native execution changes also require the matching tests in `core/tests/hermes` against the pinned image from `image/Dockerfile`.

Keep repository contracts and their enforcing checks aligned. Change conventions only when explicitly requested. Preserve stored data when changing serialized records.
