"""commit_dataset — freeze the current samples + reviewed annotations into an
immutable dataset commit and advance a branch ref.

A commit is a versioned snapshot: it pins each sample to one annotation revision
and one split, records ontology version and aggregate stats, and links to its
parent commit so history forms a chain. The branch ref (mutable pointer) is
advanced to the new commit.

Concurrency: committers on the same branch are serialized with
`SELECT ... FOR UPDATE` on the ref row. The worker runs the whole step inside the
transaction that the coordinator commits, so the lock is held until the new
commit + ref advance land atomically — no orphan commits, no lost updates. (The
datasets router uses an optimistic CAS for the same effect on a single request;
here a row lock is simpler and strictly correct under the worker's transaction.)

Layering: steps may only touch cvops_api.core/engine, not db.models or routers,
so all state is read/written with parameterized raw SQL via the engine session.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from pathlib import Path

from cvops_api.engine.step import Step, StepContext
from cvops_steps import split_strategies

with open(Path(__file__).parent / "schemas" / "commit_dataset.json") as f:
    _SCHEMA = json.load(f)


class CommitDatasetStep(Step):
    type_key = "step.commit_dataset"
    config_schema = _SCHEMA

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        from sqlalchemy import text  # noqa: PLC0415

        sample_ids: list[str] = [str(s) for s in inputs.get("sample_ids", [])]
        revision_ids: list[str] = [
            str(r) for r in inputs.get("annotation_revision_ids") or []
            if r is not None and str(r) not in ("None", "")
        ]
        if not sample_ids:
            raise ValueError("commit_dataset requires at least one sample_id")

        dataset_name = config["dataset_name"]
        branch_name = config.get("branch_name", "main")
        strategy_key = config.get("split_strategy", "by_source_group")
        train_ratio = float(config.get("train_ratio", 0.8))
        val_ratio = float(config.get("val_ratio", 0.2))
        seed = int(config.get("seed", 42))
        ontology_id = config["ontology_id"]
        message = config.get("message", "")

        session = ctx.session

        # ── Resolve the winning annotation revision per sample ──────────────
        # The input may carry several revisions for one sample (model + human);
        # the highest revision_no wins. Samples without any provided revision
        # can't be committed (commit_samples.annotation_revision_id is NOT NULL).
        rev_rows = []
        if revision_ids:
            rev_rows = (
                await session.execute(
                    text(
                        "SELECT id, sample_id, revision_no, payload, provenance "
                        "FROM annotation_revisions "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": revision_ids},
                )
            ).all()

        best_rev: dict[str, dict] = {}
        for rid, sid, rev_no, payload, provenance in rev_rows:
            sid = str(sid)
            cur = best_rev.get(sid)
            if cur is None or rev_no > cur["revision_no"]:
                best_rev[sid] = {
                    "revision_id": str(rid),
                    "revision_no": rev_no,
                    "payload": payload,
                    "provenance": provenance,
                }

        committed = [sid for sid in sample_ids if sid in best_rev]
        skipped = len(sample_ids) - len(committed)
        if not committed:
            raise ValueError(
                "no sample has an annotation revision among "
                f"{len(revision_ids)} provided; nothing to commit"
            )

        # ── Resolve ontology version ────────────────────────────────────────
        ont_version = (
            await session.execute(
                text("SELECT version FROM ontologies WHERE id = CAST(:o AS uuid)"),
                {"o": ontology_id},
            )
        ).scalar()
        if ont_version is None:
            raise ValueError(f"ontology {ontology_id!r} not found")

        # ── Source of each committed sample (for by_source_group) ───────────
        src_rows = (
            await session.execute(
                text(
                    "SELECT id, source_id FROM samples "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": committed},
            )
        ).all()
        source_of = {str(sid): str(src) for sid, src in src_rows}

        # ── Assign splits via the registered strategy ───────────────────────
        assign = split_strategies.get(strategy_key)
        splits = assign(committed, source_of, train_ratio, val_ratio, seed)

        # ── Resolve / create dataset ────────────────────────────────────────
        dataset_id = await self._get_or_create_dataset(
            session, ctx.project_id, dataset_name
        )

        # ── Lock the branch ref (serializes concurrent committers) ──────────
        parent_commit_id, ref_id = await self._lock_branch(
            session, dataset_id, branch_name
        )

        # ── Cumulative state (a git "tree") ─────────────────────────────────
        # The new commit's commit_samples = the parent commit's commit_samples
        # UNION this batch. The batch wins on overlap (fresh revision + freshly
        # assigned split); parent-only samples carry forward unchanged; batch-only
        # samples are added. `merged[sample_id] = (revision_id, split)`.
        merged: dict[str, tuple[str, str]] = {}
        if parent_commit_id is not None:
            parent_rows = (
                await session.execute(
                    text(
                        "SELECT sample_id, annotation_revision_id, split "
                        "FROM commit_samples WHERE commit_id = CAST(:cid AS uuid)"
                    ),
                    {"cid": parent_commit_id},
                )
            ).all()
            for p_sid, p_rev, p_split in parent_rows:
                if p_rev is not None:  # skip corrupt rows from pre-fix commits
                    merged[str(p_sid)] = (str(p_rev), p_split)

        # The batch wins on overlap (and adds batch-only samples).
        # TODO(#141): removal semantics — a future "this commit deletes sample X"
        # would drop X from `merged` here instead of always unioning.
        for sid in committed:
            merged[sid] = (best_rev[sid]["revision_id"], splits[sid])

        # ── Aggregate stats over the FULL cumulative set ────────────────────
        # Carried-forward parent samples need their revision payload/provenance
        # to contribute to by_class / by_review_status, so load any revision in
        # the merged set that the batch didn't already provide.
        rev_meta: dict[str, dict] = {
            best_rev[sid]["revision_id"]: best_rev[sid] for sid in committed
        }
        missing_rev_ids = [
            rid for (rid, _split) in merged.values() if rid not in rev_meta
        ]
        if missing_rev_ids:
            extra_rows = (
                await session.execute(
                    text(
                        "SELECT id, revision_no, payload, provenance "
                        "FROM annotation_revisions "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": missing_rev_ids},
                )
            ).all()
            for rid, rev_no, payload, provenance in extra_rows:
                rev_meta[str(rid)] = {
                    "revision_id": str(rid),
                    "revision_no": rev_no,
                    "payload": payload,
                    "provenance": provenance,
                }

        by_split: Counter = Counter()
        by_class: Counter = Counter()
        by_review: Counter = Counter()
        for rid, split in merged.values():
            by_split[split] += 1
            rev = rev_meta.get(rid)
            if rev is None:
                continue
            review = (rev["provenance"] or {}).get("review_status", "unknown")
            by_review[review] += 1
            for ann in rev["payload"] or []:
                key = ann.get("class_key")
                if key is not None:
                    by_class[key] += 1
        stats = {
            "sample_count": len(merged),
            "skipped_unannotated": skipped,
            "by_split": dict(by_split),
            "by_class": dict(by_class),
            "by_review_status": dict(by_review),
        }

        # ── Create the immutable commit ─────────────────────────────────────
        commit_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO commits (id, project_id, dataset_id, created_by, "
                "parent_commit_id, ontology_id, ontology_version, message, stats) "
                "VALUES (CAST(:cid AS uuid), CAST(:pid AS uuid), CAST(:did AS uuid), "
                "CAST(:by AS uuid), :parent, CAST(:oid AS uuid), :ov, :msg, "
                "CAST(:stats AS jsonb))"
            ),
            {
                "cid": commit_id,
                "pid": ctx.project_id,
                "did": dataset_id,
                "by": ctx.actor_id,
                "parent": parent_commit_id,
                "oid": ontology_id,
                "ov": ont_version,
                "msg": message,
                "stats": json.dumps(stats),
            },
        )

        # ── Pin each sample → (revision, split) over the cumulative set ─────
        await session.execute(
            text(
                "INSERT INTO commit_samples "
                "(commit_id, sample_id, annotation_revision_id, split) VALUES "
                "(CAST(:cid AS uuid), CAST(:sid AS uuid), CAST(:rid AS uuid), :split)"
            ),
            [
                {
                    "cid": commit_id,
                    "sid": sid,
                    "rid": rid,
                    "split": split,
                }
                for sid, (rid, split) in merged.items()
            ],
        )

        # ── Advance (or create) the branch ref ──────────────────────────────
        if ref_id is None:
            ref_id = str(uuid.uuid4())
            await session.execute(
                text(
                    "INSERT INTO refs (id, dataset_id, ref_type, name, "
                    "target_commit_id, is_mutable) VALUES "
                    "(CAST(:id AS uuid), CAST(:did AS uuid), 'branch', :name, "
                    "CAST(:cid AS uuid), TRUE)"
                ),
                {"id": ref_id, "did": dataset_id, "name": branch_name, "cid": commit_id},
            )
        else:
            await session.execute(
                text(
                    "UPDATE refs SET target_commit_id = CAST(:cid AS uuid) "
                    "WHERE id = CAST(:rid AS uuid)"
                ),
                {"cid": commit_id, "rid": ref_id},
            )

        await ctx.emit_event(
            actor_id=ctx.actor_id,
            actor_type="user",
            entity_type="commit",
            entity_id=commit_id,
            action="branch.advanced",
            payload={
                "dataset_id": dataset_id,
                "branch": branch_name,
                "commit_id": commit_id,
                "parent_commit_id": parent_commit_id,
            },
        )

        return {"commit_id": commit_id, "ref_id": ref_id, "dataset_id": dataset_id}

    @staticmethod
    async def _get_or_create_dataset(session, project_id: str, name: str) -> str:
        from sqlalchemy import text  # noqa: PLC0415

        new_id = str(uuid.uuid4())
        # ON CONFLICT makes this idempotent against the (project_id, name) unique
        # constraint when two runs target the same dataset name concurrently.
        await session.execute(
            text(
                "INSERT INTO datasets (id, project_id, name) VALUES "
                "(CAST(:id AS uuid), CAST(:pid AS uuid), :name) "
                "ON CONFLICT (project_id, name) DO NOTHING"
            ),
            {"id": new_id, "pid": project_id, "name": name},
        )
        # str() so the id stays JSON-serializable: it flows into the step's
        # output_refs and the branch.advanced event payload, both json.dumps'd by
        # the coordinator/emit_event (a raw asyncpg UUID raises there).
        return str(
            (
                await session.execute(
                    text(
                        "SELECT id FROM datasets WHERE project_id = CAST(:pid AS uuid) "
                        "AND name = :name"
                    ),
                    {"pid": project_id, "name": name},
                )
            ).scalar_one()
        )

    @staticmethod
    async def _lock_branch(session, dataset_id: str, branch_name: str):
        """Lock the branch ref FOR UPDATE; return (parent_commit_id, ref_id).

        ref_id is None when the branch doesn't exist yet (first commit).
        """
        from sqlalchemy import text  # noqa: PLC0415

        row = (
            await session.execute(
                text(
                    "SELECT id, target_commit_id FROM refs "
                    "WHERE dataset_id = CAST(:did AS uuid) AND ref_type = 'branch' "
                    "AND name = :name FOR UPDATE"
                ),
                {"did": dataset_id, "name": branch_name},
            )
        ).first()
        if row is None:
            return None, None
        return (str(row[1]) if row[1] is not None else None), str(row[0])
