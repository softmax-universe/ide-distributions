# AIDE 1.0.0 Contract Schema

`asyncapi.json` is an unmodified byte-for-byte copy of the AIDE 1.0.0 AsyncAPI 3.0.0
contract schema, vendored here so `core/tests/test_aide_backend.py` can validate every AIDE
request, result, error, and notification frame without depending on an external checkout.

## Provenance

- **Source repository:** `softmax-universe/softmax`
- **Source path:** `workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json`
- **Source commit:** `c6650c9042bef56daae7c2e4b20af16580ba9601`
- **SHA-256:** `5838c9a9ab420fd0ff65cf850cc295dee23d2e16f0e05cd9abfe09fce4a30c82`

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
