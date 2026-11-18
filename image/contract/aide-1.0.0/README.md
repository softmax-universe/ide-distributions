# AIDE 1.0.0 Contract Schema

`asyncapi.json` is an unmodified byte-for-byte copy of the published AIDE 1.0.0 AsyncAPI 3.0.0
contract schema, vendored here so `core/tests/test_aide_backend.py` can validate every AIDE
request, result, error, and notification frame without depending on an external checkout.

## Provenance

- **Source repository:** `softmax-universe/softmax`
- **Source path:** `workspaces/web/docs/public/static/aide/1.0.0/asyncapi.json`
- **Source commit:** `222fe9941c1cd984ffdc377b7f5f17205c2fe40d`
- **SHA-256:** `29c11fbe8b4c1b28b772377f69cc870ec9a4b4396764eafcb1ae0431715996b2`

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
