import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.versioning import (
    Commit,
    CommitSample,
    Dataset,
    ProjectDatasetLink,
    Ref,
)
from tests.db.conftest import (
    make_commit,
    make_dataset,
    make_ontology,
    make_project,
    make_ref,
    make_sample,
)


# ===========================================================================
# Dataset
# ===========================================================================


async def test_dataset_create(session: AsyncSession):
    """Creating a Dataset with valid project_id flushes successfully and stores name."""
    proj = await make_project(session)
    ds = Dataset(project_id=proj.id, name="my-dataset")
    session.add(ds)
    await session.flush()

    assert ds.id is not None
    assert ds.name == "my-dataset"
    assert ds.project_id == proj.id


async def test_dataset_unique_name_per_project(session: AsyncSession):
    """Two Datasets with the same project_id and name violate the unique constraint."""
    proj = await make_project(session)
    ds1 = Dataset(project_id=proj.id, name="duplicate")
    session.add(ds1)
    await session.flush()

    ds2 = Dataset(project_id=proj.id, name="duplicate")
    session.add(ds2)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_dataset_same_name_different_projects(session: AsyncSession):
    """The same dataset name is allowed when the projects differ."""
    proj_a = await make_project(session)
    proj_b = await make_project(session)

    ds_a = Dataset(project_id=proj_a.id, name="shared-name")
    ds_b = Dataset(project_id=proj_b.id, name="shared-name")
    session.add(ds_a)
    session.add(ds_b)
    await session.flush()

    assert ds_a.id != ds_b.id
    assert ds_a.name == ds_b.name


# ===========================================================================
# Commit
# ===========================================================================


async def test_commit_create(session: AsyncSession):
    """A Commit with all required FKs flushes successfully and is assigned an id."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)

    commit = Commit(
        project_id=proj.id,
        dataset_id=ds.id,
        ontology_id=ont.id,
        ontology_version=1,
    )
    session.add(commit)
    await session.flush()

    assert commit.id is not None
    assert commit.dataset_id == ds.id
    assert commit.project_id == proj.id


async def test_commit_message_default(session: AsyncSession):
    """When message is omitted the server default returns an empty string."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)

    commit = Commit(
        project_id=proj.id,
        dataset_id=ds.id,
        ontology_id=ont.id,
        ontology_version=1,
    )
    session.add(commit)
    await session.flush()

    result = await session.execute(select(Commit).where(Commit.id == commit.id))
    fetched = result.scalar_one()

    assert fetched.message == ""


async def test_commit_parent_nullable(session: AsyncSession):
    """A root Commit with no parent_commit_id flushes successfully and the FK stays None."""
    commit = await make_commit(session)

    assert commit.parent_commit_id is None


async def test_commit_chain(session: AsyncSession):
    """A child Commit can reference a parent Commit via the self-referential FK."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)

    commit1 = Commit(
        project_id=proj.id,
        dataset_id=ds.id,
        ontology_id=ont.id,
        ontology_version=1,
    )
    session.add(commit1)
    await session.flush()

    commit2 = Commit(
        project_id=proj.id,
        dataset_id=ds.id,
        ontology_id=ont.id,
        ontology_version=1,
        parent_commit_id=commit1.id,
    )
    session.add(commit2)
    await session.flush()

    assert commit1.parent_commit_id is None
    assert commit2.parent_commit_id == commit1.id


async def test_commit_stats_jsonb(session: AsyncSession):
    """Stats JSONB is stored and retrieved with correct nested values."""
    stats_payload = {"total": 100, "by_split": {"train": 80, "val": 20}}
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)

    commit = Commit(
        project_id=proj.id,
        dataset_id=ds.id,
        ontology_id=ont.id,
        ontology_version=1,
        stats=stats_payload,
    )
    session.add(commit)
    await session.flush()

    result = await session.execute(select(Commit).where(Commit.id == commit.id))
    fetched = result.scalar_one()

    assert fetched.stats["total"] == 100
    assert fetched.stats["by_split"]["train"] == 80
    assert fetched.stats["by_split"]["val"] == 20


# ===========================================================================
# CommitSample
# ===========================================================================


async def _make_annotation_revision(
    session: AsyncSession,
    project_id: uuid.UUID,
    sample_id: uuid.UUID,
    ontology_id: uuid.UUID,
) -> AnnotationRevision:
    rev = AnnotationRevision(
        project_id=project_id,
        sample_id=sample_id,
        ontology_id=ontology_id,
        ontology_version=1,
        revision_no=1,
        payload=[],
        provenance={"source": "model", "review_status": "unreviewed"},
    )
    session.add(rev)
    await session.flush()
    return rev


async def test_commit_sample_create(session: AsyncSession):
    """A CommitSample with valid FKs flushes successfully and stores the split value."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)
    commit = await make_commit(session, project_id=proj.id, dataset_id=ds.id, ontology_id=ont.id)
    sample = await make_sample(session, project_id=proj.id)
    rev = await _make_annotation_revision(session, proj.id, sample.id, ont.id)

    cs = CommitSample(
        commit_id=commit.id,
        sample_id=sample.id,
        annotation_revision_id=rev.id,
        split="train",
    )
    session.add(cs)
    await session.flush()

    assert cs.commit_id == commit.id
    assert cs.sample_id == sample.id
    assert cs.split == "train"


async def test_commit_sample_duplicate_pk(session: AsyncSession):
    """Inserting two CommitSamples with the same (commit_id, sample_id) raises IntegrityError."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)
    commit = await make_commit(session, project_id=proj.id, dataset_id=ds.id, ontology_id=ont.id)
    sample = await make_sample(session, project_id=proj.id)
    rev = await _make_annotation_revision(session, proj.id, sample.id, ont.id)

    cs1 = CommitSample(
        commit_id=commit.id,
        sample_id=sample.id,
        annotation_revision_id=rev.id,
        split="train",
    )
    session.add(cs1)
    await session.flush()

    cs2 = CommitSample(
        commit_id=commit.id,
        sample_id=sample.id,
        annotation_revision_id=rev.id,
        split="val",
    )
    session.add(cs2)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_commit_sample_valid_splits(session: AsyncSession):
    """CommitSamples with split='train', 'val', 'test' all flush without error."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ont = await make_ontology(session, org_id=proj.org_id)
    commit = await make_commit(session, project_id=proj.id, dataset_id=ds.id, ontology_id=ont.id)

    splits_inserted = []
    for split_name in ("train", "val", "test"):
        sample = await make_sample(session, project_id=proj.id)
        rev = await _make_annotation_revision(session, proj.id, sample.id, ont.id)
        cs = CommitSample(
            commit_id=commit.id,
            sample_id=sample.id,
            annotation_revision_id=rev.id,
            split=split_name,
        )
        session.add(cs)
        await session.flush()
        splits_inserted.append(cs.split)

    assert splits_inserted == ["train", "val", "test"]


# ===========================================================================
# Ref
# ===========================================================================


async def test_ref_create(session: AsyncSession):
    """A Ref with valid dataset and commit FKs flushes successfully and stores ref_type and name."""
    ds = await make_dataset(session)
    commit = await make_commit(session, dataset_id=ds.id)

    ref = Ref(
        dataset_id=ds.id,
        ref_type="branch",
        name="main",
        target_commit_id=commit.id,
        is_mutable=True,
    )
    session.add(ref)
    await session.flush()

    assert ref.id is not None
    assert ref.ref_type == "branch"
    assert ref.name == "main"


async def test_ref_unique_constraint(session: AsyncSession):
    """Two Refs with the same (dataset_id, ref_type, name) triple raise IntegrityError."""
    ds = await make_dataset(session)
    commit = await make_commit(session, dataset_id=ds.id)

    ref1 = Ref(
        dataset_id=ds.id,
        ref_type="branch",
        name="collision",
        target_commit_id=commit.id,
        is_mutable=True,
    )
    session.add(ref1)
    await session.flush()

    ref2 = Ref(
        dataset_id=ds.id,
        ref_type="branch",
        name="collision",
        target_commit_id=commit.id,
        is_mutable=False,
    )
    session.add(ref2)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_ref_branch_mutable(session: AsyncSession):
    """A Ref created with is_mutable=True retains that value after flush."""
    ds = await make_dataset(session)
    commit = await make_commit(session, dataset_id=ds.id)

    ref = Ref(
        dataset_id=ds.id,
        ref_type="branch",
        name=f"feature-{uuid.uuid4().hex[:6]}",
        target_commit_id=commit.id,
        is_mutable=True,
    )
    session.add(ref)
    await session.flush()

    assert ref.is_mutable is True


async def test_ref_cas_pattern(session: AsyncSession):
    """CAS UPDATE succeeds when expected matches, returns rowcount==0 when it doesn't."""
    ds = await make_dataset(session)
    commit1 = await make_commit(session, dataset_id=ds.id)
    commit2 = await make_commit(session, dataset_id=ds.id)
    commit3 = await make_commit(session, dataset_id=ds.id)

    ref = Ref(
        dataset_id=ds.id,
        ref_type="branch",
        name=f"cas-branch-{uuid.uuid4().hex[:6]}",
        target_commit_id=commit1.id,
        is_mutable=True,
    )
    session.add(ref)
    await session.flush()

    # CAS succeeds: expected value matches current target_commit_id
    result = await session.execute(
        text("UPDATE refs SET target_commit_id=:new WHERE id=:id AND target_commit_id=:expected"),
        {"new": str(commit2.id), "id": str(ref.id), "expected": str(commit1.id)},
    )
    assert result.rowcount == 1

    # CAS fails: expected value is stale (commit1 was already replaced by commit2)
    result = await session.execute(
        text("UPDATE refs SET target_commit_id=:new WHERE id=:id AND target_commit_id=:expected"),
        {"new": str(commit3.id), "id": str(ref.id), "expected": str(commit1.id)},
    )
    assert result.rowcount == 0


# ===========================================================================
# ProjectDatasetLink
# ===========================================================================


async def test_project_dataset_link_pinned(session: AsyncSession):
    """A ProjectDatasetLink with pinned_commit_id set and ref_id=None flushes successfully."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    commit = await make_commit(session, project_id=proj.id, dataset_id=ds.id)

    link = ProjectDatasetLink(
        project_id=proj.id,
        dataset_id=ds.id,
        pinned_commit_id=commit.id,
        ref_id=None,
    )
    session.add(link)
    await session.flush()

    assert link.id is not None
    assert link.pinned_commit_id == commit.id
    assert link.ref_id is None


async def test_project_dataset_link_floating(session: AsyncSession):
    """A ProjectDatasetLink with ref_id set and pinned_commit_id=None flushes successfully."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    ref = await make_ref(session, dataset_id=ds.id)

    link = ProjectDatasetLink(
        project_id=proj.id,
        dataset_id=ds.id,
        pinned_commit_id=None,
        ref_id=ref.id,
    )
    session.add(link)
    await session.flush()

    assert link.id is not None
    assert link.ref_id == ref.id
    assert link.pinned_commit_id is None


async def test_project_dataset_link_both_null_fails(session: AsyncSession):
    """Setting both pinned_commit_id and ref_id to None violates the CHECK constraint."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)

    link = ProjectDatasetLink(
        project_id=proj.id,
        dataset_id=ds.id,
        pinned_commit_id=None,
        ref_id=None,
    )
    session.add(link)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_project_dataset_link_both_set_fails(session: AsyncSession):
    """Setting both pinned_commit_id and ref_id to non-null values violates the CHECK constraint."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    commit = await make_commit(session, project_id=proj.id, dataset_id=ds.id)
    ref = await make_ref(session, dataset_id=ds.id)

    link = ProjectDatasetLink(
        project_id=proj.id,
        dataset_id=ds.id,
        pinned_commit_id=commit.id,
        ref_id=ref.id,
    )
    session.add(link)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_project_dataset_link_unique(session: AsyncSession):
    """A second ProjectDatasetLink for the same (project_id, dataset_id) raises IntegrityError."""
    proj = await make_project(session)
    ds = await make_dataset(session, project_id=proj.id)
    commit1 = await make_commit(session, project_id=proj.id, dataset_id=ds.id)
    commit2 = await make_commit(session, project_id=proj.id, dataset_id=ds.id)

    link1 = ProjectDatasetLink(
        project_id=proj.id,
        dataset_id=ds.id,
        pinned_commit_id=commit1.id,
        ref_id=None,
    )
    session.add(link1)
    await session.flush()

    link2 = ProjectDatasetLink(
        project_id=proj.id,
        dataset_id=ds.id,
        pinned_commit_id=commit2.id,
        ref_id=None,
    )
    session.add(link2)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
