# AIDE 1.0.0 Contract Schema

`asyncapi.json` is an unmodified byte-for-byte copy of the canonical AIDE 1.0.0 AsyncAPI 3.0.0
contract schema, vendored here so `core/tests/test_aide_backend.py` can validate every AIDE
request, result, error, and notification frame without depending on an external checkout.

## Provenance

- **Source repository:** `softmax-universe/softmax`
- **Source path:** `workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json`
- **SHA-256:** `ddfb39f412c4959170959ceb9c850873ffe24cc870d5ea6cef63b54c9ca19a3a`

## Verify and Refresh

Check the vendored copy's checksum:

```sh
shasum -a 256 image/contract/aide-1.0.0/asyncapi.json
```

To refresh from a `softmax` / `metta` checkout, copy `asyncapi.json` verbatim as a byte copy and
update the source provenance and SHA-256 above:

```sh
cp /path/to/metta/workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json \
  image/contract/aide-1.0.0/asyncapi.json
shasum -a 256 image/contract/aide-1.0.0/asyncapi.json
```
