"""Integration test for the commit_dataset step.

Seeds an ontology, two data sources with samples, and one annotation revision
per sample in testcontainers Postgres, then drives CommitDatasetStep through the
real path: split assignment, immutable commit creation, and branch-ref advance.
No S3/ffmpeg needed — commit_dataset is pure relational state.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.engine.step import StepContext
from cvops_steps.commit_dataset import CommitDatasetStep


async def _emit(**kw):  # matches the coordinator's awaitable emit_event binding
    return None


async def _seed(session: AsyncSession):
    """Create org/project/ontology/classes + 2 sources × samples + revisions.

    Returns (project_id, ontology_id, sample_ids, revision_ids, source_of).
    """
    org_id, proj_id, ont_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
        {"i": org_id, "n": f"org-{uuid.uuid4().hex[:8]}"},
    )
    await session.execute(
        text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
        {"i": proj_id, "o": org_id, "n": f"proj-{uuid.uuid4().hex[:8]}"},
    )
    await session.execute(
        text("INSERT INTO ontologies (id, org_id, name, version) VALUES (:i, :o, 'o', 1)"),
        {"i": ont_id, "o": org_id},
    )
    for key, order in [("cat", 0), ("dog", 1)]:
        await session.execute(
            text(
                "INSERT INTO label_classes (id, ontology_id, class_key, display_name, "
                "sort_order) VALUES (:i, :o, :k, :k, :s)"
            ),
            {"i": uuid.uuid4(), "o": ont_id, "k": key, "s": order},
        )

    sample_ids: list[str] = []
    revision_ids: list[str] = []
    source_of: dict[str, str] = {}
    # Two sources: A has 3 frames, B has 2 frames.
    for source_idx, n_frames in enumerate([3, 2]):
        src_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO data_sources (id, project_id, type, status) "
                "VALUES (:i, :p, 'video', 'ingested')"
            ),
            {"i": src_id, "p": proj_id},
        )
        for fi in range(n_frames):
            sid = uuid.uuid4()
            # unique blob per sample (the project,blob uniqueness constraint)
            bh = f"sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}"
            await session.execute(
                text(
                    "INSERT INTO blobs (hash, storage_backend, storage_key, size_bytes, "
                    "media_type) VALUES (:h, 'garage', :k, 10, 'image/jpeg')"
                ),
                {"h": bh, "k": f"blobs/{bh[7:9]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO samples (id, project_id, blob_hash, source_id, width, "
                    "height, frame_index) VALUES (:i, :p, :b, :s, 64, 64, :f)"
                ),
                {"i": sid, "p": proj_id, "b": bh, "s": src_id, "f": fi},
            )
            rid = uuid.uuid4()
            payload = [
                {
                    "class_key": "cat" if fi % 2 == 0 else "dog",
                    "geometry": {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.2]},
                    "confidence": 0.9,
                }
            ]
            await session.execute(
                text(
                    "INSERT INTO annotation_revisions (id, project_id, sample_id, "
                    "ontology_id, ontology_version, revision_no, payload, provenance) "
                    "VALUES (:i, :p, :s, :o, 1, 1, CAST(:pl AS jsonb), CAST(:pv AS jsonb))"
                ),
                {
                    "i": rid,
                    "p": proj_id,
                    "s": sid,
                    "o": ont_id,
                    "pl": json.dumps(payload),
                    "pv": json.dumps({"source": "model", "review_status": "unreviewed"}),
                },
            )
            sample_ids.append(str(sid))
            revision_ids.append(str(rid))
            source_of[str(sid)] = str(src_id)
    await session.flush()
    return str(proj_id), str(ont_id), sample_ids, revision_ids, source_of


def _ctx(session, project_id) -> StepContext:
    return StepContext(
        session=session,
        storage=None,  # commit_dataset performs no blob I/O
        project_id=project_id,
        run_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        emit_event=_emit,
    )


async def test_commit_creates_commit_samples_and_branch(session: AsyncSession) -> None:
    proj_id, ont_id, sample_ids, revision_ids, source_of = await _seed(session)

    result = await CommitDatasetStep().run(
        _ctx(session, proj_id),
        {
            "dataset_name": "ds1",
            "ontology_id": ont_id,
            "split_strategy": "by_source_group",
            "train_ratio": 0.8,
            "val_ratio": 0.2,
        },
        {"sample_ids": sample_ids, "annotation_revision_ids": revision_ids},
    )

    assert result["commit_id"] and result["ref_id"] and result["dataset_id"]

    # All 5 samples pinned into the commit.
    cs = (
        await session.execute(
            text("SELECT sample_id, split FROM commit_samples WHERE commit_id = CAST(:c AS uuid)"),
            {"c": result["commit_id"]},
        )
    ).all()
    assert len(cs) == 5
    split_of = {str(sid): split for sid, split in cs}

    # by_source_group invariant: every sample from one source shares a split.
    by_source: dict[str, set[str]] = {}
    for sid, src in source_of.items():
        by_source.setdefault(src, set()).add(split_of[sid])
    assert all(len(splits) == 1 for splits in by_source.values())

    # Branch ref points at the new commit and is a mutable branch.
    ref = (
        await session.execute(
            text(
                "SELECT name, ref_type, is_mutable, target_commit_id FROM refs "
                "WHERE id = CAST(:r AS uuid)"
            ),
            {"r": result["ref_id"]},
        )
    ).first()
    assert ref.name == "main" and ref.ref_type == "branch" and ref.is_mutable is True
    assert str(ref.target_commit_id) == result["commit_id"]

    # Stats are frozen on the commit and match the frontend's expected keys.
    stats = (
        await session.execute(
            text("SELECT stats FROM commits WHERE id = CAST(:c AS uuid)"),
            {"c": result["commit_id"]},
        )
    ).scalar()
    assert stats["sample_count"] == 5
    assert sum(stats["by_split"].values()) == 5
    assert sum(stats["by_class"].values()) == 5  # one box per sample


async def test_second_commit_advances_branch_with_parent_link(session: AsyncSession) -> None:
    proj_id, ont_id, sample_ids, revision_ids, _ = await _seed(session)
    step = CommitDatasetStep()
    config = {"dataset_name": "ds2", "ontology_id": ont_id, "branch_name": "main"}

    first = await step.run(
        _ctx(session, proj_id),
        config,
        {"sample_ids": sample_ids[:3], "annotation_revision_ids": revision_ids[:3]},
    )
    second = await step.run(
        _ctx(session, proj_id),
        config,
        {"sample_ids": sample_ids, "annotation_revision_ids": revision_ids},
    )

    assert second["ref_id"] == first["ref_id"]  # same branch, advanced

    # Committing to an EXISTING dataset resolves its id via SELECT; it must stay a
    # str so output_refs/event payloads stay JSON-serializable under the
    # coordinator (regression: a raw UUID raised "not JSON serializable").
    assert isinstance(second["dataset_id"], str)
    json.dumps(second)

    parent = (
        await session.execute(
            text("SELECT parent_commit_id FROM commits WHERE id = CAST(:c AS uuid)"),
            {"c": second["commit_id"]},
        )
    ).scalar()
    assert str(parent) == first["commit_id"]

    head = (
        await session.execute(
            text("SELECT target_commit_id FROM refs WHERE id = CAST(:r AS uuid)"),
            {"r": first["ref_id"]},
        )
    ).scalar()
    assert str(head) == second["commit_id"]


async def test_random_seeded_is_deterministic(session: AsyncSession) -> None:
    proj_id, ont_id, sample_ids, revision_ids, _ = await _seed(session)
    step = CommitDatasetStep()

    def run(name: str):
        return step.run(
            _ctx(session, proj_id),
            {
                "dataset_name": name,
                "ontology_id": ont_id,
                "split_strategy": "random_seeded",
                "seed": 7,
            },
            {"sample_ids": sample_ids, "annotation_revision_ids": revision_ids},
        )

    async def splits_for(commit_id: str) -> dict[str, str]:
        rows = (
            await session.execute(
                text(
                    "SELECT sample_id, split FROM commit_samples WHERE commit_id = CAST(:c AS uuid)"
                ),
                {"c": commit_id},
            )
        ).all()
        return {str(sid): split for sid, split in rows}

    a = await run("rs_a")
    b = await run("rs_b")
    assert await splits_for(a["commit_id"]) == await splits_for(b["commit_id"])


async def test_import_batch_without_frame_index_spreads_across_splits(
    session: AsyncSession,
) -> None:
    proj_id, ont_id, sample_ids, revision_ids, _ = await _seed(session)
    # An import: images carry no frame_index, so each one is its own group.
    await session.execute(
        text("UPDATE samples SET frame_index = NULL WHERE project_id = CAST(:p AS uuid)"),
        {"p": proj_id},
    )
    result = await CommitDatasetStep().run(
        _ctx(session, proj_id),
        {
            "dataset_name": "ds-import",
            "ontology_id": ont_id,
            "split_strategy": "by_source_group",
            "train_ratio": 0.6,
            "val_ratio": 0.2,
        },
        {"sample_ids": sample_ids, "annotation_revision_ids": revision_ids},
    )
    stats = (
        await session.execute(
            text("SELECT stats FROM commits WHERE id = CAST(:c AS uuid)"),
            {"c": result["commit_id"]},
        )
    ).scalar()
    assert stats["by_split"] == {"train": 3, "val": 1, "test": 1}
    assert "split_warnings" not in stats


async def test_lone_video_commit_warns_about_empty_splits(session: AsyncSession) -> None:
    proj_id, ont_id, sample_ids, revision_ids, source_of = await _seed(session)
    first_source = source_of[sample_ids[0]]
    keep = [i for i, sid in enumerate(sample_ids) if source_of[sid] == first_source]
    result = await CommitDatasetStep().run(
        _ctx(session, proj_id),
        {
            "dataset_name": "ds-lone",
            "ontology_id": ont_id,
            "split_strategy": "by_source_group",
            "train_ratio": 0.7,
            "val_ratio": 0.15,
        },
        {
            "sample_ids": [sample_ids[i] for i in keep],
            "annotation_revision_ids": [revision_ids[i] for i in keep],
        },
    )
    stats = (
        await session.execute(
            text("SELECT stats FROM commits WHERE id = CAST(:c AS uuid)"),
            {"c": result["commit_id"]},
        )
    ).scalar()
    assert len(stats["by_split"]) == 1  # the video was not cut to fill other splits
    assert len(stats["split_warnings"]) == 2


async def test_later_commit_keeps_a_videos_split_even_with_new_ratios(
    session: AsyncSession,
) -> None:
    proj_id, ont_id, sample_ids, revision_ids, source_of = await _seed(session)
    step = CommitDatasetStep()
    first_source = source_of[sample_ids[0]]
    first = [i for i, sid in enumerate(sample_ids) if source_of[sid] == first_source]
    rest = [i for i in range(len(sample_ids)) if i not in first]

    async def _commit(idx: list[int], train: float, val: float):
        return await step.run(
            _ctx(session, proj_id),
            {
                "dataset_name": "ds-sticky",
                "ontology_id": ont_id,
                "branch_name": "main",
                "split_strategy": "by_source_group",
                "train_ratio": train,
                "val_ratio": val,
            },
            {
                "sample_ids": [sample_ids[i] for i in idx],
                "annotation_revision_ids": [revision_ids[i] for i in idx],
            },
        )

    c1 = await _commit(first, 0.9, 0.05)
    # Re-commit the same video's frames with very different ratios, plus the other video.
    c2 = await _commit(first + rest, 0.1, 0.1)
    rows = (
        await session.execute(
            text("SELECT sample_id, split FROM commit_samples WHERE commit_id = CAST(:c AS uuid)"),
            {"c": c2["commit_id"]},
        )
    ).all()
    before = (
        await session.execute(
            text("SELECT sample_id, split FROM commit_samples WHERE commit_id = CAST(:c AS uuid)"),
            {"c": c1["commit_id"]},
        )
    ).all()
    after = {str(sid): split for sid, split in rows}
    assert {after[str(sid)] for sid, _ in before} == {split for _, split in before}


async def test_second_commit_counts_parent_frames_when_filling_splits(
    session: AsyncSession,
) -> None:
    proj_id, ont_id, sample_ids, revision_ids, source_of = await _seed(session)
    step = CommitDatasetStep()
    first_source = source_of[sample_ids[0]]
    first = [i for i, sid in enumerate(sample_ids) if source_of[sid] == first_source]
    second = [i for i in range(len(sample_ids)) if i not in first]

    async def _commit(idx: list[int]):
        return await step.run(
            _ctx(session, proj_id),
            {
                "dataset_name": "ds-fill",
                "ontology_id": ont_id,
                "branch_name": "main",
                "split_strategy": "by_source_group",
                "train_ratio": 0.6,
                "val_ratio": 0.2,
            },
            {
                "sample_ids": [sample_ids[i] for i in idx],
                "annotation_revision_ids": [revision_ids[i] for i in idx],
            },
        )

    await _commit(first)  # 3 frames: the lone video lands in train
    result = await _commit(second)  # 2 frames: train (3 of 5 at 0.6) is already full
    stats = (
        await session.execute(
            text("SELECT stats FROM commits WHERE id = CAST(:c AS uuid)"),
            {"c": result["commit_id"]},
        )
    ).scalar()
    # Counting only this batch (2 frames) would put the video in train again.
    assert stats["by_split"] == {"train": 3, "val": 2}
