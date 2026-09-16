# contracts

`schemas/` is generated from the API's Pydantic models:

```bash
make contracts          # from the repo root
```

`zod/` is generated from `schemas/` for the frontend:

```bash
pnpm --dir packages/contracts install
pnpm --dir packages/contracts generate
```

Neither directory is hand-edited. If a shape is wrong, fix the Pydantic model.
