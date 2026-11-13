# AIDE 1.0.0 Contract Schema

`asyncapi.json` is an unmodified byte-for-byte copy of the published AIDE 1.0.0 AsyncAPI 3.0.0
contract schema, vendored here so `core/tests/test_aide_backend.py` can validate every AIDE
request, result, error, and notification frame without depending on an external checkout.

## Provenance

- **Source repository:** `softmax-universe/softmax`
- **Source path:** `workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json`
- **Source commit:** `802250f7c2`
- **SHA-256:** `b44a2ad6d2c3bf904d3b09f2d25a9c1da6426e8d5a2c12cc2229e0df0340c098`

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
