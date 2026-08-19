import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.ontologies import LabelClass, Ontology
from tests.db.conftest import make_ontology, make_org


async def test_ontology_create(session: AsyncSession):
    org = await make_org(session)
    ont = Ontology(org_id=org.id, name="base-ontology")
    session.add(ont)
    await session.flush()

    assert ont.id is not None
    assert ont.name == "base-ontology"
    assert ont.org_id == org.id


async def test_ontology_version_default(session: AsyncSession):
    org = await make_org(session)
    ont = Ontology(org_id=org.id, name="versioned-ontology")
    session.add(ont)
    await session.flush()
    await session.refresh(ont)

    assert ont.version == 1


async def test_ontology_unique_name_per_org(session: AsyncSession):
    org = await make_org(session)
    shared_name = f"shared-ont-{uuid.uuid4().hex[:8]}"

    session.add(Ontology(org_id=org.id, name=shared_name))
    await session.flush()

    session.add(Ontology(org_id=org.id, name=shared_name))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_ontology_same_name_different_orgs(session: AsyncSession):
    org_a = await make_org(session)
    org_b = await make_org(session)
    shared_name = f"shared-ont-{uuid.uuid4().hex[:8]}"

    ont_a = Ontology(org_id=org_a.id, name=shared_name)
    ont_b = Ontology(org_id=org_b.id, name=shared_name)
    session.add(ont_a)
    session.add(ont_b)
    await session.flush()

    assert ont_a.id != ont_b.id
    assert ont_a.name == ont_b.name


async def test_ontology_org_fk(session: AsyncSession):
    fake_org_id = uuid.uuid4()
    ont = Ontology(org_id=fake_org_id, name="orphan-ontology")
    session.add(ont)

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_label_class_create(session: AsyncSession):
    ont = await make_ontology(session)
    lc = LabelClass(
        ontology_id=ont.id,
        class_key="vehicle.car",
        display_name="Car",
        sort_order=0,
    )
    session.add(lc)
    await session.flush()

    assert lc.id is not None
    assert lc.class_key == "vehicle.car"
    assert lc.ontology_id == ont.id


async def test_label_class_color_default(session: AsyncSession):
    ont = await make_ontology(session)
    lc = LabelClass(
        ontology_id=ont.id,
        class_key="vehicle.truck",
        display_name="Truck",
        sort_order=0,
    )
    session.add(lc)
    await session.flush()
    await session.refresh(lc)

    assert lc.color == "#FF0000"


async def test_label_class_unique_class_key(session: AsyncSession):
    ont = await make_ontology(session)
    shared_key = "vehicle.car"

    session.add(LabelClass(ontology_id=ont.id, class_key=shared_key, display_name="Car", sort_order=0))
    await session.flush()

    session.add(LabelClass(ontology_id=ont.id, class_key=shared_key, display_name="Car Dup", sort_order=1))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_label_class_unique_sort_order(session: AsyncSession):
    ont = await make_ontology(session)

    session.add(LabelClass(ontology_id=ont.id, class_key="vehicle.car", display_name="Car", sort_order=0))
    await session.flush()

    session.add(LabelClass(ontology_id=ont.id, class_key="vehicle.truck", display_name="Truck", sort_order=0))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_label_class_different_ontologies_same_key(session: AsyncSession):
    ont_a = await make_ontology(session)
    ont_b = await make_ontology(session)
    shared_key = "vehicle.car"

    lc_a = LabelClass(ontology_id=ont_a.id, class_key=shared_key, display_name="Car", sort_order=0)
    lc_b = LabelClass(ontology_id=ont_b.id, class_key=shared_key, display_name="Car", sort_order=0)
    session.add(lc_a)
    session.add(lc_b)
    await session.flush()

    assert lc_a.id != lc_b.id
    assert lc_a.class_key == lc_b.class_key


async def test_label_class_sort_order_invariant(session: AsyncSession):
    ont = await make_ontology(session)

    lc0 = LabelClass(ontology_id=ont.id, class_key="person", display_name="Person", sort_order=0)
    lc1 = LabelClass(ontology_id=ont.id, class_key="vehicle.car", display_name="Car", sort_order=1)
    lc2 = LabelClass(ontology_id=ont.id, class_key="vehicle.truck", display_name="Truck", sort_order=2)
    session.add_all([lc0, lc1, lc2])
    await session.flush()

    result = await session.execute(
        select(LabelClass).where(LabelClass.ontology_id == ont.id).order_by(LabelClass.sort_order)
    )
    ordered = result.scalars().all()

    assert len(ordered) == 3
    assert ordered[0].class_key == "person"
    assert ordered[1].class_key == "vehicle.car"
    assert ordered[2].class_key == "vehicle.truck"


async def test_label_class_ontology_fk(session: AsyncSession):
    fake_ontology_id = uuid.uuid4()
    lc = LabelClass(
        ontology_id=fake_ontology_id,
        class_key="vehicle.car",
        display_name="Car",
        sort_order=0,
    )
    session.add(lc)

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
