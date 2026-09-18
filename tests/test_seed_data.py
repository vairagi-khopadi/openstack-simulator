"""``--seed-data``: flavors and images read from a JSON file instead of the literals.

The validation tests are the point of the feature. Without them a typo like ``"vcpu"``
would reach ``Flavor(**spec)`` and raise a TypeError with half the seed already written,
so every check here is about failing before the first row is added -- and naming the
entry at fault when it does.

The seeding tests call ``seed_identity`` directly rather than take the ``cloud`` fixture,
which would have already written the built-in flavors and images.

Every test is async: the autouse ``fresh_db`` fixture in conftest is async, and pytest
will not run it for a synchronous test.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import seed as seed_module
from app.core.database import SessionLocal
from app.models.compute import Flavor
from app.models.storage import Image

pytestmark = pytest.mark.anyio

FLAVOR = {"name": "m1.huge", "vcpus": 16, "ram": 65536, "disk": 400}
IMAGE = {
    "name": "debian-12",
    "min_ram": 512,
    "min_disk": 10,
    "size": 2048,
    "disk_format": "qcow2",
}


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "seed-data.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


# --------------------------------------------------------------------------------------
# Reading a good file
# --------------------------------------------------------------------------------------


async def test_both_sections_are_returned(tmp_path: Path) -> None:
    flavors, images = seed_module.load_seed_data(
        _write(tmp_path, {"flavors": [FLAVOR], "images": [IMAGE]})
    )
    assert flavors == [FLAVOR]
    assert images == [IMAGE]


async def test_missing_section_keeps_the_builtin_list(tmp_path: Path) -> None:
    """None is 'use the shipped list' -- only a section that is present replaces one."""
    flavors, images = seed_module.load_seed_data(_write(tmp_path, {"images": [IMAGE]}))
    assert flavors is None
    assert images == [IMAGE]


async def test_empty_section_seeds_nothing(tmp_path: Path) -> None:
    """An empty list is a deliberate 'no flavors', not a missing section."""
    flavors, _ = seed_module.load_seed_data(_write(tmp_path, {"flavors": []}))
    assert flavors == []


async def test_optional_fields_are_accepted(tmp_path: Path) -> None:
    spec = {**FLAVOR, "id": "9", "swap": 512, "rxtx_factor": 1.5, "is_public": False,
            "description": "mine", "extra_specs": {"hw:numa_nodes": "2"}}
    flavors, _ = seed_module.load_seed_data(_write(tmp_path, {"flavors": [spec]}))
    assert flavors == [spec]


async def test_home_relative_path_is_expanded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write(tmp_path, {"images": [IMAGE]})
    _, images = seed_module.load_seed_data("~/seed-data.json")
    assert images == [IMAGE]


# --------------------------------------------------------------------------------------
# Rejecting a bad one
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"flavours": [FLAVOR]}, "unknown top-level key(s): flavours"),
        ({}, "nothing to seed"),
        ([FLAVOR], "top level must be an object"),
        ({"flavors": {"name": "x"}}, "'flavors' must be a list, not dict"),
        ({"flavors": ["m1.huge"]}, "flavors[0] must be an object, not str"),
        ({"flavors": [{"name": "x", "vcpus": 1, "ram": 1}]}, "flavors[0] is missing 'disk'"),
        ({"images": [{**IMAGE, "disk_format": None}]}, "must be str, not NoneType"),
        ({"flavors": [{**FLAVOR, "vcpu": 2}]}, "unknown key 'vcpu'"),
        ({"images": [{**IMAGE, "properties": ["a"]}]}, "must be dict, not list"),
        ({"flavors": [{**FLAVOR, "ram": 1024.5}]}, "must be int, not float"),
        # bool is a subclass of int, which makes these the easy two to let through.
        ({"flavors": [{**FLAVOR, "vcpus": True}]}, "must be int, not bool"),
        ({"flavors": [{**FLAVOR, "is_public": 1}]}, "must be bool, not int"),
        ({"flavors": [FLAVOR, FLAVOR]}, "flavors[1] repeats the name 'm1.huge'"),
    ],
)
async def test_bad_file_is_rejected(tmp_path: Path, data: Any, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        seed_module.load_seed_data(_write(tmp_path, data))


async def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        seed_module.load_seed_data(_write(tmp_path, "{not json"))


async def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No such file"):
        seed_module.load_seed_data(tmp_path / "absent.json")


# --------------------------------------------------------------------------------------
# What the seeders do with the specs
# --------------------------------------------------------------------------------------


async def test_seeding_custom_specs_writes_them() -> None:
    async with SessionLocal() as session:
        project, _ = await seed_module.seed_identity(session)
        await seed_module.seed_flavors(session, [FLAVOR])
        await seed_module.seed_images(session, project.id, [IMAGE])
        await session.commit()

        flavors = list((await session.execute(select(Flavor))).scalars().all())
        images = list((await session.execute(select(Image))).scalars().all())
        project_id = project.id

    assert [f.name for f in flavors] == ["m1.huge"]
    assert flavors[0].vcpus == 16 and flavors[0].ram == 65536
    assert flavors[0].description == "Simulated m1.huge"
    assert [i.name for i in images] == ["debian-12"]
    assert images[0].owner == project_id and images[0].status == "active"
    assert images[0].properties == {}, "properties is optional in a --seed-data file"
    assert len(images[0].checksum) == 32 and images[0].os_hash_algo == "sha512"


async def test_supplied_description_wins() -> None:
    """The built-in 'Simulated <name>' is a default, not an override."""
    async with SessionLocal() as session:
        await seed_module.seed_flavors(session, [{**FLAVOR, "description": "ours"}])
        await session.commit()
        flavor = (await session.execute(select(Flavor))).scalar_one()
        assert flavor.description == "ours"


async def test_custom_specs_are_still_idempotent() -> None:
    async with SessionLocal() as session:
        project, _ = await seed_module.seed_identity(session)
        for _ in range(2):
            await seed_module.seed_flavors(session, [FLAVOR])
            await seed_module.seed_images(session, project.id, [IMAGE])
            await session.commit()
        assert len((await session.execute(select(Flavor))).scalars().all()) == 1
        assert len((await session.execute(select(Image))).scalars().all()) == 1


# --------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------


async def test_main_rejects_a_bad_file_before_touching_the_database(
    tmp_path: Path, capsys: Any
) -> None:
    path = _write(tmp_path, {"flavors": [{"name": "x"}]})
    assert seed_module.main(["--seed-data", str(path)]) == 1
    assert "Cannot use seed data" in capsys.readouterr().err
