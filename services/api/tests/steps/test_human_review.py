"""Integration tests for HumanReviewStep (the CVAT push gate).

The step downloads sample images, pushes a CVAT task with pre-labels, records a
labeling_jobs row, and raises GateException. The real cvops_cvat_client (and its
cvat_sdk dep) is not installed in the API test env, so a fake module is injected
into sys.modules — the step imports it lazily inside run(). Storage is faked.

Requires the testcontainers Postgres fixture (Docker), like the other step tests.
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from cvops_api.engine.step import GateException, StepContext
from cvops_steps.human_review import HumanReviewStep
from tests.db.conftest import make_ontology, make_project, make_run, make_sample

PRELABEL = [{"class_key": "person", "geometry": {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.2]}}]


class _FakeStorage:
    async def get_bytes(self, blob_hash: str) -> bytes:
        return b"\xff\xd8\xff\xe0fake-jpeg-bytes"


async def _emit(**kw):
    return None


def _ctx(session, project_id: str, run_id: str) -> StepContext:
    return StepContext(
        session=session,
        storage=_FakeStorage(),
        project_id=project_id,
        run_id=run_id,
        actor_id=str(uuid.uuid4()),
        emit_event=_emit,
    )


@pytest.fixture
def fake_cvat_client():
    """Inject a fake cvops_cvat_client; capture the calls the step makes."""
    mod = types.ModuleType("cvops_cvat_client")
    calls: dict = {}

    class ReviewImage:
        def __init__(self, path, width, height, annotations):
            self.path = path
            self.width = width
            self.height = height
            self.annotations = annotations

    def push_review_task(task_name, images, label_names=None):
        calls["push"] = {
            "task_name": task_name,
            "images": list(images),
            "label_names": label_names,
        }
        return {
            "task_id": 4242,
            "job_ids": [99],
            "cvat_url": "http://localhost:8080/tasks/4242/jobs/99",
            "label_map": {"person": 1},
        }

    def register_webhook(task_id, target, secret):
        calls["webhook"] = {"task_id": task_id, "target": target, "secret": secret}
        return 7

    mod.ReviewImage = ReviewImage
    mod.push_review_task = push_review_task
    mod.register_webhook = register_webhook
    with patch.dict(sys.modules, {"cvops_cvat_client": mod}):
        yield calls


async def _seed(session):
    """One project + ontology + sample with a model pre-label revision + a gate run."""
    proj = await make_project(session)
    ont = await make_ontology(session, org_id=proj.org_id)
    # A label class on the ontology — human_review seeds the CVAT task with it.
    await session.execute(
        text(
            "INSERT INTO label_classes (id, ontology_id, class_key, display_name, "
            "color, sort_order) VALUES (gen_random_uuid(), CAST(:oid AS uuid), "
            "'person', 'Person', '#FF0000', 0)"
        ),
        {"oid": str(ont.id)},
    )
    sample = await make_sample(session, project_id=proj.id)
    rev_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO annotation_revisions (id, project_id, sample_id, ontology_id, "
            "ontology_version, revision_no, payload, provenance) VALUES "
            "(CAST(:id AS uuid), CAST(:pid AS uuid), CAST(:sid AS uuid), CAST(:oid AS uuid), "
            "1, 1, CAST(:payload AS jsonb), CAST(:prov AS jsonb))"
        ),
        {
            "id": rev_id,
            "pid": str(proj.id),
            "sid": str(sample.id),
            "oid": str(ont.id),
            "payload": json.dumps(PRELABEL),
            "prov": json.dumps({"source": "model", "review_status": "pending"}),
        },
    )
    run = await make_run(
        session, project_id=proj.id, kind="step", status="running", step_id="review_node"
    )
    return str(proj.id), str(sample.id), rev_id, str(run.id)


async def test_push_gate_inserts_job_and_raises_gate(session, fake_cvat_client):
    proj_id, sample_id, rev_id, run_id = await _seed(session)
    inputs = {"sample_ids": [sample_id], "annotation_revision_ids": [rev_id]}

    with pytest.raises(GateException) as exc:
        await HumanReviewStep().run(_ctx(session, proj_id, run_id), {}, inputs)

    gate = exc.value.gate_data
    assert gate["cvat_task_id"] == 4242
    assert gate["cvat_url"].endswith("/tasks/4242/jobs/99")
    assert gate["sample_ids"] == [sample_id]  # ordered, for frame→sample mapping

    # labeling_jobs row recorded the push.
    row = (
        await session.execute(
            text(
                "SELECT cvat_task_id, status, sample_count, annotation_revision_ids_in, "
                "cvat_job_ids FROM labeling_jobs WHERE id = CAST(:id AS uuid)"
            ),
            {"id": gate["labeling_job_id"]},
        )
    ).first()
    assert row is not None
    assert row[0] == 4242
    assert row[1] == "pushed"
    assert row[2] == 1
    assert row[3] == [rev_id]
    assert row[4] == [99]

    # The pre-label payload reached CVAT as the image's annotations.
    pushed = fake_cvat_client["push"]
    assert len(pushed["images"]) == 1
    assert pushed["images"][0].annotations == PRELABEL
    # The CVAT task's label space was seeded from the project ontology.
    assert "person" in (pushed["label_names"] or [])
    # No webhook target configured → registration skipped.
    assert "webhook" not in fake_cvat_client


async def test_push_tolerates_none_revision_ids(session, fake_cvat_client):
    """Samples without a pre-label arrive as literal "None" in frozen input_refs.

    These must be filtered before the uuid[] cast (regression: asyncpg DataError
    'invalid UUID None'). The sample is still pushed, just with no pre-labels.
    """
    proj = await make_project(session)
    await make_ontology(session, org_id=proj.org_id)  # human_review requires one
    sample = await make_sample(session, project_id=proj.id)
    run = await make_run(
        session, project_id=proj.id, kind="step", status="running", step_id="review_node"
    )
    inputs = {
        "sample_ids": [str(sample.id)],
        "annotation_revision_ids": ["None"],  # frozen form for an unlabelled sample
    }

    with pytest.raises(GateException) as exc:
        await HumanReviewStep().run(_ctx(session, str(proj.id), str(run.id)), {}, inputs)

    assert exc.value.gate_data["cvat_task_id"] == 4242
    pushed = fake_cvat_client["push"]
    assert len(pushed["images"]) == 1
    assert pushed["images"][0].annotations == []  # no pre-label for this sample


async def test_push_gate_survives_webhook_failure(session, fake_cvat_client, monkeypatch):
    """A failing webhook registration must not abort the push: the gate still
    opens (regression: ImportError in register_webhook failed the whole run
    before the CVAT link could surface)."""
    monkeypatch.setenv("CVAT_WEBHOOK_TARGET", "http://api:8000/api/v1/internal/cvat/webhook")
    monkeypatch.setenv("CVAT_WEBHOOK_SECRET", "s3cret")

    def _boom(task_id, target, secret):
        raise ImportError("cannot import name 'WebhookContentTypeEnum'")

    sys.modules["cvops_cvat_client"].register_webhook = _boom

    proj_id, sample_id, rev_id, run_id = await _seed(session)
    inputs = {"sample_ids": [sample_id], "annotation_revision_ids": [rev_id]}

    with pytest.raises(GateException) as exc:
        await HumanReviewStep().run(_ctx(session, proj_id, run_id), {}, inputs)
    # Gate opened despite the webhook blowing up.
    assert exc.value.gate_data["cvat_url"].endswith("/tasks/4242/jobs/99")


async def test_push_registers_webhook_when_configured(session, fake_cvat_client, monkeypatch):
    monkeypatch.setenv("CVAT_WEBHOOK_TARGET", "http://api:8000/api/v1/internal/cvat/webhook")
    monkeypatch.setenv("CVAT_WEBHOOK_SECRET", "s3cret")
    proj_id, sample_id, rev_id, run_id = await _seed(session)
    inputs = {"sample_ids": [sample_id], "annotation_revision_ids": [rev_id]}

    with pytest.raises(GateException):
        await HumanReviewStep().run(_ctx(session, proj_id, run_id), {}, inputs)

    assert fake_cvat_client["webhook"]["task_id"] == 4242
    assert fake_cvat_client["webhook"]["secret"] == "s3cret"


async def test_push_requires_inputs(session, fake_cvat_client):
    proj = await make_project(session)
    run = await make_run(session, project_id=proj.id, kind="step", status="running")
    with pytest.raises(ValueError, match="requires sample_ids"):
        await HumanReviewStep().run(_ctx(session, str(proj.id), str(run.id)), {}, {})


async def test_push_requires_project_ontology(session, fake_cvat_client):
    """Without a project ontology there's no label space + nowhere to store the
    reviewed labels, so the step fails loudly instead of pushing a class-less task."""
    proj = await make_project(session)
    sample = await make_sample(session, project_id=proj.id)
    run = await make_run(
        session, project_id=proj.id, kind="step", status="running", step_id="review_node"
    )
    inputs = {"sample_ids": [str(sample.id)], "annotation_revision_ids": []}
    with pytest.raises(ValueError, match="requires the project to have an ontology"):
        await HumanReviewStep().run(_ctx(session, str(proj.id), str(run.id)), {}, inputs)
    # Failed before any CVAT push.
    assert "push" not in fake_cvat_client
