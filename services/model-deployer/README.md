# model-deployer (legacy)

**Status: legacy, no longer updated.** Superseded by [`services/worker-cvat`](../worker-cvat), which now
serves `GET /models` and `POST /deploy` on `:8001` and deploys models to Nuclio itself.
`MODEL_DEPLOYER_URL` still exists as a setting name, but it points at `worker-cvat`.

- Not built or run by any compose file or by Tilt.
- Do not add features or fixes here. Make the change in `worker-cvat` instead.
- Kept in the tree for reference until the team decides to remove it (#34).
- Its image still runs as a non-root user (uid 1001) so it stays safe if someone builds it.
