# Image Versions: policies published as container images

> **Partly implemented (2026-11-12).** #132 (merged November 2, 2026) shipped the core of this
> design: `ARTIFACT_KINDS = ("file", "image")` (`core/src/research_core/store/versions.py:40`,
> `core/src/research_core/mixin.py:29`), `upload` dispatching to `upload_image_policy`
> (`core/src/research_core/tools/upload.py:159`) on the real and fake platforms, the archive kept
> under `documents/artifacts/<digest>` (`core/src/research_core/tools/version.py:14`), and the
> BuildKit path (`buildctl` in `image/Dockerfile:8`, `BUILDKIT_HOST` passed through by
> `core/src/research_core/prepare.py:177`). Not built: the `player_runtime` check in
> `version.create` (the tool never reads it; only `platform` reports it,
> `core/src/research_core/tools/platform.py:566`), `artifacts.retain_unuploaded_days` and any
> pruning of archives, the `version.json` `artifact_type` mapping (the kind comes from the mixin
> front matter only), and an image lookup in the nightly platform check. Where the code differs:
> the upload capability check sends one real upload as a probe for both kinds rather than parsing
> the archive in a dry run (`core/src/research_core/watcher/checks.py:372-410`). For current
> behaviour see `core/README.md` and `image/README.md` ("buildctl", line 232).

**Original status: proposal** (October 29, 2026, Pacific). Follow-up to the profile rebuild
(`ide-profile-refactor-2026-10-28-final.html`), which deferred image policies: "`version.create`
refuses image policies with a clear message until image-policy Versions are designed". James
decided on October 29 that image support lands in the PR after #106 and that metta's pin to
the new profiles waits for it, so no platform-hosted Coworld regresses in production. Rohit
agreed on the PR on the same condition.

## Problem

Coworlds declare `player_runtime` in their platform manifest. `game-hosted` Coworlds (Paintbot,
the fake) take a policy *file*; `platform-hosted` ones take a *container image* built from a
Dockerfile. The old profiles handled both (`artifact_type: file | oci-image` in the build's
`version.json`). The new profiles' Version record allows `artifact.kind` of `file` only
(`store/versions.py` `ARTIFACT_KINDS`), `version.create` builds files only, and the platform
protocol exposes `upload_file_policy` only. Rohit's review asks for parity, including recovery
of an image upload whose confirmation was lost, verified against the real platform.

## Goals

- A Version whose artifact is a container image, built from the Coworld's code by the mixin's
  `build` command, uploaded through the official client, recorded and recoverable exactly like a
  file Version.
- The same identity rule: a Version is its commit plus its artifact digest; the retained
  artifact is the exact bytes that were uploaded.
- The same recovery rule (decision 3 of October 29): exact upload inputs saved with the intent,
  read-only lookup afterwards, one automatic retry only on a definitive "no such version".

## Non-goals

- A platform build service or a build inbox (Rohit: "instead of ... introducing a platform
  build service").
- Changing how a mixin's `build` command is written; the contract is the file it leaves behind.
- Running a Docker daemon in the sandbox.

## Design

**Artifact kinds.** `ARTIFACT_KINDS = ("file", "image")`. The mixin front matter already names
`artifact {kind, path}`; the build's `version.json` keeps the old profiles' `artifact_type`
(`file` or `oci-image`) for compatibility, mapped to the Version's `kind`.

**What the build produces.** For an image Version the mixin's `build` command leaves an OCI
image archive (a tar with `index.json`, as `docker save`/`buildah push oci-archive:` write) at
the artifact path. The identity of the image is the config digest in that archive's manifest,
which is what `coworld.upload.policy_upload_inputs(image_archive=True)` reads
(`image_client_hash`, attribute `kind: docker-img`). The Version's `artifact.digest` is the
sha256 of the archive bytes, as for files; the saved upload inputs carry the image hash.

**Runtime check.** `version.create` reads `player_runtime` from the platform manifest (never
from the local copy; design "Hosted runtime") and refuses a mismatch with a clear message: a
file artifact for a platform-hosted Coworld, or an image for a game-hosted one.

**Upload.** `upload` dispatches by `artifact.kind`: files through `upload_file_policy` as today;
images through a new protocol method `upload_image_policy(name, archive_path, tags)` that calls
the client's container-image upload (`CoworldUploadClient`, the path `coworld upload-policy`
takes for an image). Both paths save `policy_upload_inputs(...)` in the intent before the call
and recover through `lookup_policy_upload(inputs)` afterwards.

**Retention.** The archive is retained under `documents/artifacts/<digest>` like a file, so
recovery after the worktree is gone needs no rebuild. Image archives are large; a retention
rule keeps every uploaded Version's archive and prunes archives of Versions never uploaded
after 30 days (a setting, `artifacts.retain_unuploaded_days`).

**Checks.** The capability check's build step accepts either kind; the upload check's dry run
for images verifies the archive parses (`index.json`, one manifest) without uploading.

**Fake platform.** `FakePlatform` gains image upload and lookup (identity by the config digest)
so the scenario tests cover both kinds without a network.

## Builder

Settled with Rohit on the PR (October 29): reuse the hosted BuildKit path. The sandbox has no
Docker daemon; a private per-workspace BuildKit sidecar does the build, `buildctl` talks to it
and writes an OCI archive, which is the artifact the mixin's `build` command leaves behind. No
builder change is needed in #106. The profiles stay indifferent to the builder: they need only
the archive. The follow-up verifies build, upload and lost-response recovery together against
that hosted builder.

## Affected files

`store/versions.py` (kinds, registry), `store/migrations/coworld/0003_*.sql` only if a field is
added, `tools/version.py` (runtime check, `version.json` mapping), `tools/upload.py` (dispatch,
inputs), `platform/protocol.py`, `platform/real.py`, `platform/fake.py`, `watcher/checks.py`,
`core/tests/tools/test_version_tool.py`, `core/tests/tools/test_upload_tool.py`,
`core/tests/fixtures/fakegame_repo.py` (an image-building variant of the fixture),
`core/src/research_core/skills/common/method/candidate/SKILL.md` and
`platform/submission/SKILL.md` (one paragraph each), `docs/hosting.md` (the builder).

## Validation

- Unit and scenario tests on the fake for both kinds, including lost-response recovery.
- One real run against the platform as a test Player: a Dockerfile with `COPY` and `RUN`,
  the build, the upload, a lookup after the archive is deleted, exactly one policy version.
- The nightly read-only platform check adds the image lookup route.

## Rollout

Lands in its own PR after #106 merges. Metta's pin to the new profiles moves only after this
PR and the contract-layer follow-up (source editing) are in.

## Open questions

1. Whether `platform-hosted` Coworlds need extra manifest fields (`CoworldRunnableResources`,
   `CoworldPlayerStartup`, `secret_env` in `upload_policy_cmd`) at upload time, and where a
   mixin declares them.
2. The retention setting's default.
