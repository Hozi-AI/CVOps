"""
Factory helpers for DB tests.
Each helper inserts a minimal valid row, flushes (not commits), and returns the ORM object.
All generated names/emails include a UUID fragment to avoid UNIQUE constraint collisions.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.db.models.samples import DataSource, Sample
from cvops_api.db.models.versioning import Commit, Dataset, Ref
from cvops_api.db.models.workflows import Workflow


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def make_org(session: AsyncSession, **kwargs) -> Org:
    org = Org(name=f"org-{_uid()}", **kwargs)
    session.add(org)
    await session.flush()
    return org


async def make_user(session: AsyncSession, org_id: uuid.UUID | None = None, **kwargs) -> User:
    if org_id is None:
        org_id = (await make_org(session)).id
    user = User(org_id=org_id, email=f"user-{_uid()}@test.com", **kwargs)
    session.add(user)
    await session.flush()
    return user


async def make_project(session: AsyncSession, org_id: uuid.UUID | None = None, **kwargs) -> Project:
    if org_id is None:
        org_id = (await make_org(session)).id
    proj = Project(org_id=org_id, name=f"project-{_uid()}", **kwargs)
    session.add(proj)
    await session.flush()
    return proj


async def make_blob(session: AsyncSession, **kwargs) -> Blob:
    h = f"sha256:{'a' * 60}{_uid()}"
    blob = Blob(
        hash=h,
        storage_backend="garage",
        storage_key=f"blobs/aa/{_uid()}",
        size_bytes=1024,
        media_type="image/jpeg",
        **kwargs,
    )
    session.add(blob)
    await session.flush()
    return blob


async def make_data_source(
    session: AsyncSession, project_id: uuid.UUID | None = None, **kwargs
) -> DataSource:
    if project_id is None:
        project_id = (await make_project(session)).id
    ds = DataSource(project_id=project_id, type="video", **kwargs)
    session.add(ds)
    await session.flush()
    return ds


async def make_sample(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    blob_hash: str | None = None,
    **kwargs,
) -> Sample:
    if project_id is None:
        project_id = (await make_project(session)).id
    if source_id is None:
        source_id = (await make_data_source(session, project_id=project_id)).id
    if blob_hash is None:
        blob_hash = (await make_blob(session)).hash
    sample = Sample(
        project_id=project_id,
        source_id=source_id,
        blob_hash=blob_hash,
        width=1920,
        height=1080,
        **kwargs,
    )
    session.add(sample)
    await session.flush()
    return sample


async def make_ontology(
    session: AsyncSession, org_id: uuid.UUID | None = None, **kwargs
) -> Ontology:
    if org_id is None:
        org_id = (await make_org(session)).id
    ont = Ontology(org_id=org_id, name=f"ont-{_uid()}", **kwargs)
    session.add(ont)
    await session.flush()
    return ont


async def make_dataset(
    session: AsyncSession, project_id: uuid.UUID | None = None, **kwargs
) -> Dataset:
    if project_id is None:
        project_id = (await make_project(session)).id
    ds = Dataset(project_id=project_id, name=f"ds-{_uid()}", **kwargs)
    session.add(ds)
    await session.flush()
    return ds


async def make_commit(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    dataset_id: uuid.UUID | None = None,
    ontology_id: uuid.UUID | None = None,
    **kwargs,
) -> Commit:
    if project_id is None:
        project_id = (await make_project(session)).id
    if dataset_id is None:
        dataset_id = (await make_dataset(session, project_id=project_id)).id
    if ontology_id is None:
        ontology_id = (await make_ontology(session)).id
    commit = Commit(
        project_id=project_id,
        dataset_id=dataset_id,
        ontology_id=ontology_id,
        ontology_version=1,
        **kwargs,
    )
    session.add(commit)
    await session.flush()
    return commit


async def make_ref(
    session: AsyncSession,
    dataset_id: uuid.UUID | None = None,
    target_commit_id: uuid.UUID | None = None,
    **kwargs,
) -> Ref:
    if dataset_id is None:
        dataset_id = (await make_dataset(session)).id
    if target_commit_id is None:
        target_commit_id = (await make_commit(session, dataset_id=dataset_id)).id
    ref = Ref(
        dataset_id=dataset_id,
        ref_type="branch",
        name=f"branch-{_uid()}",
        target_commit_id=target_commit_id,
        is_mutable=True,
        **kwargs,
    )
    session.add(ref)
    await session.flush()
    return ref


async def make_workflow(
    session: AsyncSession, project_id: uuid.UUID | None = None, **kwargs
) -> Workflow:
    if project_id is None:
        project_id = (await make_project(session)).id
    wf = Workflow(
        project_id=project_id,
        name=f"wf-{_uid()}",
        definition={"steps": [], "edges": []},
        **kwargs,
    )
    session.add(wf)
    await session.flush()
    return wf


async def make_run(session: AsyncSession, project_id: uuid.UUID | None = None, **kwargs) -> Run:
    if project_id is None:
        project_id = (await make_project(session)).id
    run = Run(
        project_id=project_id,
        kind=kwargs.pop("kind", "workflow"),
        status=kwargs.pop("status", "pending"),
        input_refs=kwargs.pop("input_refs", {}),
        output_refs=kwargs.pop("output_refs", {}),
        config=kwargs.pop("config", {}),
        **kwargs,
    )
    session.add(run)
    await session.flush()
    return run
