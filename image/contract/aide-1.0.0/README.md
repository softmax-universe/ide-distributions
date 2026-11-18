# AIDE 1.0.0 Contract Schema

`asyncapi.json` is an unmodified byte-for-byte copy of the published AIDE 1.0.0 AsyncAPI 3.0.0
contract schema, vendored here so `core/tests/test_aide_backend.py` can validate every AIDE
request, result, error, and notification frame without depending on an external checkout.

## Provenance

- **Source repository:** `softmax-universe/softmax`
- **Source path:** `workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json`
- **Source commit:** `f4900b76042f54a3f8f730749683302666f85eb4`
- **SHA-256:** `f8f408060baf9c01e75bb5b36fd5841b008c30b102a61e80e3bb95875b176f77`

## Verify and Refresh

Check the vendored copy's checksum:

```sh
shasum -a 256 image/contract/aide-1.0.0/asyncapi.json
```

To refresh from a `softmax` / `metta` checkout, copy `asyncapi.json` verbatim as a byte copy and
update the source commit and SHA-256 above:

```sh
cp /path/to/metta/workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json \
  image/contract/aide-1.0.0/asyncapi.json
shasum -a 256 image/contract/aide-1.0.0/asyncapi.json
```
